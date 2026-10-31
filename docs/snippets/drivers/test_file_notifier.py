"""The conformance kit for the file notification driver."""

from collections.abc import Callable
from pathlib import Path

import pytest
from sandloom_notify_file.driver import FileNotifier

from sandloom.driverkit import NotifierProvider
from sandloom.driverkit.testing import NotifierConformance


class TestFileNotifier(NotifierConformance):
    @pytest.fixture
    def make_provider(self, tmp_path: Path) -> Callable[[], NotifierProvider]:
        options = {"path": str(tmp_path / "mail.log")}
        return lambda: FileNotifier(name="default", driver="file", options=options)
