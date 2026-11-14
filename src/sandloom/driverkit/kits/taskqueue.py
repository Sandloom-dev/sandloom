"""``TaskQueueConformance``: what a ``taskqueue`` driver must do on the ``send``
side (spec 8.2). The host groups (execution, scheduler, ``check``) are
``idk-worker-port``'s and join this class there.

The kit sends real messages: point ``make_provider`` at a test backend (a
throw-away broker or database). It sends the task :attr:`known_task`, which
the kit never runs: the send groups ask what ``send`` does, not what a worker
does with the message.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import timedelta
from functools import partial
from typing import ClassVar
from uuid import uuid4

import pytest

from sandloom.core.infra.errors import (
    InfraError,
    InvalidArgument,
    Timeout,
    TooLarge,
    Unavailable,
)
from sandloom.core.infra.ports.taskqueue import (
    TASKQUEUE_CAPABILITY,
    TaskQueueProvider,
)
from sandloom.driverkit.kits._harness import Call, ProviderConformance, require

_PAYLOAD = b'{"conformance":true}'


@pytest.mark.asyncio
class TaskQueueConformance(ProviderConformance[TaskQueueProvider]):
    """What a ``taskqueue`` driver must do, after the harness's checks.

    ``send`` publishes one message under the task id it is given and returns
    that id. The error classes carry the kernel's dedupe decision (G-TASK-03):
    ``InvalidArgument`` and ``TooLarge`` mean nothing was published, so the
    kernel releases the key; ``Unavailable`` and ``Timeout`` mean the publish
    may have happened, so the key stays pending. A driver that cannot tell
    which, raises the latter.
    """

    contract = TaskQueueProvider
    protocol = None
    spec = TASKQUEUE_CAPABILITY

    #: A task name the driver accepts (it is never run by the send groups).
    known_task: ClassVar[str] = "sandloom.conformance.task"
    #: A task name the driver refuses with ``InvalidArgument``. ``None``: the
    #: driver cannot tell an unknown task at ``send`` (the kernel layer checks
    #: the name before reserving), and the group is skipped.
    unknown_task: ClassVar[str | None] = None

    def _send(
        self,
        provider: TaskQueueProvider,
        task: str | None = None,
        payload: bytes = _PAYLOAD,
        *,
        task_id: str | None = None,
        delay: timedelta | None = None,
        at_least_once: bool = False,
        queue: str = "default",
    ) -> Call:
        return partial(
            provider.send,
            task or self.known_task,
            payload,
            task_id=task_id or uuid4().hex,
            delay=delay,
            at_least_once=at_least_once,
            queue=queue,
        )

    def feature_calls(self, provider: TaskQueueProvider) -> Mapping[str, Call]:
        return {"delay": self._send(provider, delay=timedelta(seconds=60))}

    def operation_calls(self, provider: TaskQueueProvider) -> Mapping[str, Call]:
        return {"send": self._send(provider)}

    # ------------------------------------------------------------ send groups
    async def test_send_returns_the_task_id_it_was_given(
        self, provider: TaskQueueProvider
    ) -> None:
        task_id = uuid4().hex
        answered = await provider.send(
            self.known_task, _PAYLOAD, task_id=task_id, delay=None
        )
        require(
            answered == task_id,
            f"send answered {answered!r} for the task id {task_id!r}: the "
            "kernel chose the id (and reserved it), so the driver publishes "
            "under it and returns it",
        )

    async def test_send_of_an_unknown_task_raises_invalid_argument(
        self, provider: TaskQueueProvider
    ) -> None:
        if self.unknown_task is None:
            pytest.skip("this driver cannot tell an unknown task at send")
        with pytest.raises(InvalidArgument):
            await provider.send(self.unknown_task, _PAYLOAD, task_id=uuid4().hex)

    @pytest.mark.parametrize("task", ["", None, 7])
    async def test_send_of_a_bad_task_name_raises_invalid_argument(
        self, provider: TaskQueueProvider, task: object
    ) -> None:
        with pytest.raises(InvalidArgument):
            await provider.send(task, _PAYLOAD, task_id=uuid4().hex)  # type: ignore[arg-type]

    @pytest.mark.parametrize("payload", [b"not json", b"[1, 2]", b'"text"', b""])
    async def test_send_refuses_a_payload_that_is_not_a_json_object(
        self, provider: TaskQueueProvider, payload: bytes
    ) -> None:
        with pytest.raises(InvalidArgument):
            await provider.send(self.known_task, payload, task_id=uuid4().hex)

    async def test_send_with_a_delay_when_the_driver_declares_delay(
        self, provider: TaskQueueProvider
    ) -> None:
        self.requires_feature(provider, "delay")
        task_id = uuid4().hex
        answered = await provider.send(
            self.known_task, _PAYLOAD, task_id=task_id, delay=timedelta(seconds=60)
        )
        require(answered == task_id, f"send with a delay answered {answered!r}")

    async def test_send_marks_at_least_once_only_when_the_driver_redelivers(
        self, provider: TaskQueueProvider
    ) -> None:
        task_id = uuid4().hex
        # Without the feature the flag is the host's business, never a refusal:
        # the kernel derives ``redelivery`` for the module, not for the message.
        answered = await provider.send(
            self.known_task, _PAYLOAD, task_id=task_id, at_least_once=True
        )
        require(answered == task_id, f"send answered {answered!r}")

    async def test_send_carries_the_queue_of_the_task(
        self, provider: TaskQueueProvider
    ) -> None:
        # Where the message lands is the host's to read; ``send`` accepts the
        # queue and publishes under the given id (no refusal for a non-default
        # queue: the kernel routes by the task's declaration, not the table).
        task_id = uuid4().hex
        answered = await provider.send(
            self.known_task, _PAYLOAD, task_id=task_id, queue="sandloom-conformance"
        )
        require(
            answered == task_id,
            f"send with a queue answered {answered!r} for the task id {task_id!r}",
        )

    async def test_send_errors_carry_the_dedupe_decision(
        self, make_failing_provider: Callable[[], TaskQueueProvider] | None
    ) -> None:
        """A backend that fails mid-send may have published: ``Unavailable`` or
        ``Timeout``, never the ``InvalidArgument`` / ``TooLarge`` that make the
        kernel release the key and let a retry publish twice."""
        if make_failing_provider is None:
            pytest.skip("no make_failing_provider: the error classes are not checked")
        built = make_failing_provider()
        try:
            await built.aopen()
        except InfraError:
            pass  # a driver may refuse to open on a dead backend
        try:
            await built.send(self.known_task, _PAYLOAD, task_id=uuid4().hex)
        except (Unavailable, Timeout):
            return
        except (InvalidArgument, TooLarge) as exc:
            raise AssertionError(
                f"send raised {type(exc).__name__} for a failing backend: that "
                "tells the kernel nothing was published, and it releases the "
                "idempotency key. Raise Unavailable or Timeout"
            ) from exc
        except InfraError:
            return
        finally:
            await built.aclose()
        raise AssertionError("send succeeded on a provider built to fail")


__all__ = ["TaskQueueConformance"]
