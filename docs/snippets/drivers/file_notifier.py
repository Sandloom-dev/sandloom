"""A notification driver that appends every message to a file."""

import asyncio
from pathlib import Path

from sandloom.sdk.drivers import (
    InfraRegistry,
    InstanceConfig,
    Notification,
    NotifierProvider,
)


class FileNotifier(NotifierProvider):
    def __init__(self, config: InstanceConfig) -> None:
        super().__init__(name=config.name, driver=config.driver, options=config.options)
        path = config.options.get("path")
        if not path:
            raise ValueError("the file notification driver needs a 'path' option")
        self._path = Path(path)

    def features(self) -> frozenset[str]:
        return frozenset({"email"})

    async def send(self, notification: Notification) -> None:
        line = (
            f"{notification.channel}|{notification.recipient}|{notification.subject}\n"
        )
        await asyncio.to_thread(self._append, line)

    def _append(self, line: str) -> None:
        with self._path.open("a", encoding="utf-8") as target:
            target.write(line)


def register(registry: InfraRegistry) -> None:
    registry.register_driver("notification", "file", FileNotifier, features={"email"})
