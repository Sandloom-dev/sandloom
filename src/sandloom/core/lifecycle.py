from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable, Sequence
from contextlib import AbstractContextManager
from time import perf_counter

import sandloom.core.observability.metrics as _metrics
from sandloom.core.config.settings import settings
from sandloom.core.descriptors import HealthResult, ModuleDescriptor
from sandloom.core.facade import CoreFacade
from sandloom.core.observability import metrics as _m

_start_span: Callable[[str], AbstractContextManager[object]] | None
try:  # optional tracing
    from sandloom.core.observability.tracing import start_span as _start_span_impl

    _start_span = _start_span_impl
except Exception:  # pragma: no cover  # noqa: BLE001 - tracing is optional
    _start_span = None

# Public task callable type (module scope for type checkers)
TaskFn = Callable[[], object | Awaitable[object]]

logger = logging.getLogger("sandloom.core.lifecycle")


class CriticalStartupError(RuntimeError):
    """A critical startup task failed: the application must not start."""


async def run_startup(core: CoreFacade) -> None:  # noqa: C901
    """Execute startup tasks in defined staging order.

    Three phases, each in registration order: ``infra`` (composition-root
    infrastructure), ``modules`` (everything a module registers) and ``post``.
    The phase is an explicit registration attribute (``add_startup_task(...,
    phase=...)``), never inferred from a module task's function name.
    """
    # (task, critical, phase) triples so each registration's flags travel with
    # it through partitioning — no identity-based lookup that a duplicate
    # callable could make ambiguous. The phase is EXPLICIT (decided at
    # registration, see CoreFacade.add_startup_task): a module task named
    # ``infra_*`` no longer runs before the infrastructure is ready.
    specs: Sequence[tuple[TaskFn, bool, str]] = core.startup_task_phased_specs
    if not specs:
        return
    infra: list[tuple[TaskFn, bool]] = []
    mods: list[tuple[TaskFn, bool]] = []
    post: list[tuple[TaskFn, bool]] = []
    buckets = {"infra": infra, "modules": mods, "post": post}
    for t, crit, phase in specs:
        buckets.get(phase, mods).append((t, crit))

    async def _run(group_name: str, items: Iterable[tuple[TaskFn, bool]]) -> None:
        timeout = settings.startup_task_timeout_seconds
        for task, critical in items:
            t_name = getattr(task, "__name__", "?")
            try:
                if _start_span is not None:
                    with _start_span(f"startup:{group_name}:{t_name}"):
                        res = task()
                        if asyncio.iscoroutine(res):
                            await asyncio.wait_for(res, timeout=timeout)
                else:
                    res = task()
                    if asyncio.iscoroutine(res):
                        await asyncio.wait_for(res, timeout=timeout)
            except TimeoutError as exc:  # pragma: no cover
                logger.error(
                    "Startup task timeout (%s:%s) > %ss",
                    group_name,
                    t_name,
                    timeout,
                )
                _inc_metric(core, "startup_task_timeouts")
                if critical:
                    raise CriticalStartupError(
                        f"Critical startup task timed out: {group_name}:{t_name}"
                    ) from exc
            except asyncio.CancelledError:  # pragma: no cover
                logger.warning("Startup task cancelled (%s:%s)", group_name, t_name)
                _inc_metric(core, "startup_task_cancelled")
                raise  # cancellation must always propagate
            except Exception as exc:
                logger.exception("Startup task failed (%s:%s)", group_name, t_name)
                _inc_metric(core, "startup_task_failures")
                # A failed critical task means the process must not serve
                # traffic — fail the lifespan instead of degrading silently.
                if critical:
                    raise CriticalStartupError(
                        f"Critical startup task failed: {group_name}:{t_name}"
                    ) from exc

    start = perf_counter()
    # Schema registry persistence load is wired in main.py as an infra startup task.
    await _run("infra", infra)
    await _run("modules", mods)
    await _run("post", post)
    # Record duration + export facade metrics (best-effort)
    try:
        _m.record_startup_duration(perf_counter() - start)
        _m.update_facade_metrics(core.list_metrics())
    except Exception:  # pragma: no cover  # noqa: BLE001 - metrics must never break startup
        logger.debug("Failed to record startup metrics")


async def run_shutdown(core: CoreFacade, *, deadline: float | None = None) -> None:  # noqa: C901
    """Run teardown in reverse order within one shared monotonic deadline."""
    tasks: Sequence[TaskFn] = list(core.shutdown_tasks)
    if not tasks:
        return
    timeout = settings.shutdown_task_timeout_seconds
    loop = asyncio.get_running_loop()
    if deadline is None:
        deadline = loop.time() + settings.shutdown_budget_seconds
    for task in reversed(tasks):
        t_name = getattr(task, "__name__", "?")
        remaining = deadline - loop.time()
        if remaining <= 0:
            logger.error("Shutdown budget exhausted before %s", t_name)
            _inc_metric(core, "shutdown_task_timeouts")
            break
        task_timeout = min(timeout, remaining)
        try:
            if _start_span is not None:
                with _start_span(f"shutdown:{t_name}"):
                    res = task()
                    if asyncio.iscoroutine(res):
                        await asyncio.wait_for(res, timeout=task_timeout)
            else:
                res = task()
                if asyncio.iscoroutine(res):
                    await asyncio.wait_for(res, timeout=task_timeout)
        except TimeoutError:  # pragma: no cover
            logger.error("Shutdown task timeout (%s) > %ss", t_name, timeout)
            _inc_metric(core, "shutdown_task_timeouts")
        except asyncio.CancelledError:  # pragma: no cover
            logger.warning("Shutdown task cancelled (%s)", t_name)
            _inc_metric(core, "shutdown_task_cancelled")
            raise
        except Exception:  # pragma: no cover
            logger.exception("Shutdown task failed (%s)", t_name)
            _inc_metric(core, "shutdown_task_failures")


def _inc_metric(core: CoreFacade, name: str) -> None:
    try:
        current = core.get_metric(name) or 0
        core.register_metric(name, int(current) + 1)
        try:  # attempt Prometheus counter increment if mapped
            counter = _metrics.PROM_COUNTER_MAP.get(name)
            if counter is not None:
                counter.inc()
        except Exception:  # pragma: no cover  # noqa: BLE001, S110 - metrics must never break startup
            pass
    except Exception:  # pragma: no cover  # noqa: BLE001 - metrics must never break startup
        logger.debug("Failed to increment metric %s", name)


async def aggregate_health(core: CoreFacade) -> dict[str, HealthResult]:
    """Collect health results from core + module descriptors.

    Module descriptor health checks are namespaced as `mod:<module>` to avoid
    collisions with core-registered names.
    """
    checks: list[tuple[str, Callable[[], Awaitable[HealthResult]]]] = list(
        core.health_checks.items()
    )
    seen = {name for name, _ in checks}
    for d in getattr(core, "module_descriptors", []):
        if isinstance(d, ModuleDescriptor) and d.health_check:
            key = f"mod:{d.name}"
            if key in seen:
                continue  # unlikely collision
            seen.add(key)
            checks.append((key, d.health_check))
    if not checks:
        return {}

    timeout = getattr(settings, "health_check_timeout_seconds", 5.0)

    async def _run_check(
        name: str, fn: Callable[[], Awaitable[HealthResult]]
    ) -> tuple[str, HealthResult]:
        try:
            return name, await asyncio.wait_for(fn(), timeout=timeout)
        except TimeoutError:
            return name, HealthResult(
                status="fail", details={"error": f"timeout > {timeout}s"}
            )
        except Exception as exc:  # pragma: no cover  # noqa: BLE001 - any failure is a failed probe
            return name, HealthResult(status="fail", details={"error": str(exc)})

    # Run all checks concurrently: one slow dependency must not serialize
    # the whole health endpoint.
    pairs = await asyncio.gather(*(_run_check(n, f) for n, f in checks))
    return dict(pairs)


def overall_status(results: dict[str, HealthResult]) -> str:
    if not results:
        return "ok"
    if all(r.status == "ok" for r in results.values()):
        return "ok"
    # degraded if no explicit 'fail' but at least one not-ok (future: 'degraded')
    if any(r.status == "fail" for r in results.values()):
        return "fail"
    return "degraded"
