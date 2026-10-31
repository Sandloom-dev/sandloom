"""A development notification driver: appends every message to a JSON-lines file."""

import asyncio
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from sandloom.driverkit import Notification, NotifierProvider


class FileNotifier(NotifierProvider):
    contract_version = "v1"
    supported_features = frozenset({"email"})

    def __init__(self, *, name: str, driver: str, options: Mapping[str, Any]) -> None:
        super().__init__(name=name, driver=driver, options=options)
        path = options.get("path")
        if not path:
            raise ValueError("the file notification driver needs a 'path' option")
        self._path = Path(path)

    async def send(self, notification: Notification) -> None:
        line = json.dumps(
            {
                "channel": notification.channel,
                "recipient": notification.recipient,
                "subject": notification.subject,
                "body": notification.body,
            }
        )
        await asyncio.to_thread(self._append, line)

    def _append(self, line: str) -> None:
        # Bodies can hold secret links: only the owner may read the file.
        descriptor = os.open(self._path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as target:
            target.write(line + "\n")
