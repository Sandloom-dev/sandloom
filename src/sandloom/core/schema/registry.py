# src/sandloom/core/schema/registry.py
from __future__ import annotations

import json
import logging
import os
from typing import Any

from sandloom.core.contracts.schema_registry import (
    SchemaEntry,
    SchemaRegistryContract,
)
from sandloom.core.contracts.validators import (
    ContractValidationError,
    classify_schema_change,
    validate_channel_name,
)

logger = logging.getLogger("sandloom.core.schema_registry")


def _parse_version(channel: str) -> int:
    """Extract the ``vN`` version suffix from a channel name.

    Defensive: a channel without a numeric ``.vN`` tail defaults to version ``1``
    instead of crashing on ``int('...')``. Channel names are validated to be
    versioned upstream, so this only guards against malformed input.
    """
    last = channel.rsplit(".", 1)[-1]
    if len(last) > 1 and last[0] == "v" and last[1:].isdigit():
        return int(last[1:])
    return 1


class InMemorySchemaRegistry(SchemaRegistryContract):
    """
    In-memory, singleton implementation of the SchemaRegistryContract.
    Supports optional persistence to a JSON file.
    """

    def __init__(self) -> None:
        self._by_channel: dict[tuple[str, int], SchemaEntry] = {}
        self._persistence_path: str | None = None
        self._loaded_from_disk = False

    @property
    def persistence_path(self) -> str | None:
        """The configured persistence file (``None``: persistence disabled)."""
        return self._persistence_path

    @property
    def loaded_from_disk(self) -> bool:
        """True once the persisted schemas were loaded."""
        return self._loaded_from_disk

    def configure_persistence(self, path: str | None) -> None:
        self._persistence_path = path

    def load_from_disk(self, *, strict: bool = False) -> None:
        """Load persisted schemas; a missing file is a first boot (no-op).

        ``strict`` (production startup) raises on an unreadable / corrupt
        file instead of logging and serving without the persisted schemas.
        """
        if self._loaded_from_disk or not self._persistence_path:
            return
        if not os.path.exists(self._persistence_path):
            logger.info(
                "Schema registry persistence file not found at %s, skipping load.",
                self._persistence_path,
            )
            return

        data_raw = self._read_persisted(self._persistence_path, strict=strict)
        if data_raw is None:
            return

        # One corrupt entry must not prevent the rest from loading.
        skipped = 0
        for item in data_raw:
            if not isinstance(item, dict):
                skipped += 1
                continue
            channel = item.get("channel")
            name = item.get("name", channel)
            schema = item.get("schema")
            if not (channel and name):
                skipped += 1
                continue
            try:
                self.register(channel, name, schema)
            except Exception:
                skipped += 1
                logger.exception(
                    "Skipping invalid persisted schema entry channel=%r", channel
                )
        if skipped:
            logger.warning("Schema registry load: %d invalid entries skipped", skipped)
        self._loaded_from_disk = True

    @staticmethod
    def _read_persisted(path: str, *, strict: bool) -> list[Any] | None:
        """The persisted entries, or ``None`` (logged) — raises when strict."""
        logger.info("Loading schema registry from %s", path)
        try:
            with open(path, encoding="utf-8") as fh:
                data_raw = json.load(fh)
        except Exception as e:
            if strict:
                raise RuntimeError(
                    "SCHEMA_REGISTRY_PATH: cannot read the schema registry "
                    f"persistence file {path!r}: {e}"
                ) from e
            logger.exception("Failed to read schema registry persistence file: %s", e)
            return None
        if isinstance(data_raw, list):
            return data_raw
        if strict:
            raise RuntimeError(
                "SCHEMA_REGISTRY_PATH: the schema registry persistence file "
                f"{path!r} is not a JSON list"
            )
        logger.error(
            "Schema registry persistence file %s is not a JSON list; ignored", path
        )
        return None

    def _persist(self) -> None:
        if not self._persistence_path:
            return

        directory = os.path.dirname(self._persistence_path)
        if directory:
            os.makedirs(directory, exist_ok=True)

        snapshot = [
            {
                "channel": e.channel,
                "version": e.version,
                "name": e.name,
                "schema": e.schema,
            }
            for e in sorted(self.list(), key=lambda x: x.channel)
        ]

        tmp_path = f"{self._persistence_path}.tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as fh:
                json.dump(snapshot, fh, ensure_ascii=False, indent=2, sort_keys=True)
            os.replace(tmp_path, self._persistence_path)
        except Exception as e:
            logger.exception("Failed to persist schema registry: %s", e)
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    def register(
        self, channel: str, name: str, schema: dict[str, Any] | None
    ) -> SchemaEntry:
        validate_channel_name(channel)
        version = _parse_version(channel)
        key = (channel, version)
        existing = self._by_channel.get(key)

        if existing:
            if schema and existing.schema and existing.schema != schema:
                report = classify_schema_change(
                    existing.schema, schema, channel, version
                )
                if report.breaking:
                    raise ContractValidationError(
                        f"Breaking schema change for {channel}: {report.to_dict()}"
                    )
                existing.schema = schema  # Allow non-breaking update
                self._persist()
            elif schema and not existing.schema:
                existing.schema = schema  # Populate empty schema
                self._persist()
            return existing

        entry = SchemaEntry(channel=channel, version=version, name=name, schema=schema)
        self._by_channel[key] = entry
        self._persist()
        return entry

    def get(self, channel: str) -> SchemaEntry | None:
        try:
            validate_channel_name(channel)
            version = _parse_version(channel)
            return self._by_channel.get((channel, version))
        except ContractValidationError:
            return None

    def list(self) -> list[SchemaEntry]:
        return list(self._by_channel.values())

    # -- transactional rollback support --------------------------------------
    def snapshot(self) -> dict[tuple[str, int], SchemaEntry]:
        """Capture the channel table for a later :meth:`restore` (deep copy).

        Entries are copied so an in-place schema mutation after the snapshot
        does not leak into it — used to roll back a module registration that
        failed partway through ``ModuleRegistrationDraft.apply``.
        """
        return {
            key: SchemaEntry(
                channel=entry.channel,
                version=entry.version,
                name=entry.name,
                schema=dict(entry.schema) if entry.schema else entry.schema,
            )
            for key, entry in self._by_channel.items()
        }

    def restore(self, snapshot: dict[tuple[str, int], SchemaEntry]) -> None:
        """Reset the channel table to a prior :meth:`snapshot` and re-persist."""
        self._by_channel = dict(snapshot)
        self._persist()


__all__ = ["InMemorySchemaRegistry"]
