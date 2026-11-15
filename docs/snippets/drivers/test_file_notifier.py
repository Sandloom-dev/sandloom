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

    @pytest.fixture
    def make_failing_provider(self, tmp_path: Path) -> Callable[[], NotifierProvider]:
        # A directory that does not exist: send fails, and its error must show
        # neither the path nor the kit's secret, which the path carries.
        options = {"path": str(tmp_path / self.leak_marker / "mail.log")}
        return lambda: FileNotifier(name="default", driver="file", options=options)
