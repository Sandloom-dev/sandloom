"""No services needed: regression checks for the measurement harness itself.

Run with python -m unittest discover -s design/kernel-boundary/poc/authz.
"""

import importlib.util
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
SPEC = importlib.util.spec_from_file_location("authz_stand", HERE / "stand.py")
stand = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(stand)


class StandTests(unittest.TestCase):
    def test_selected_cases_mint_only_their_subject(self):
        with patch.object(stand, "mint", return_value="sealed") as mint:
            ops = stand._ops(object(), ["bound.orgrole_and_subactive"])
        self.assertEqual(list(ops), ["bound.orgrole_and_subactive"])
        mint.assert_called_once_with("admin")

    def test_docker_failure_cannot_be_reported_as_a_measurement(self):
        self._check_docker_result("echo '==1== I refs: 1,000' >&2\nexit 1\n")

    def test_success_without_a_cachegrind_count_is_an_error(self):
        self._check_docker_result("exit 0\n")

    def _check_docker_result(self, body):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            (directory / "checkout").symlink_to(ROOT, target_is_directory=True)
            docker = directory / "docker"
            docker.write_text("#!/bin/sh\n" + body)
            docker.chmod(0o755)
            result = subprocess.run(  # noqa: S603 - fixed local harness and fake Docker
                [str(HERE / "cg_poc.sh"), "control"],
                env={
                    **os.environ,
                    "PATH": os.pathsep.join(
                        (str(directory), str(ROOT / ".venv/bin"), os.environ["PATH"])
                    ),
                    "S": str(directory),
                    "SIDE": "checkout",
                    "CG_ROOT": str(ROOT),
                    "VARIANT": "authzstand",
                    "CG_K": "1",
                    "CG_LOGS": str(directory / "logs"),
                    "DATABASE_URL": "postgresql://unused/stand",
                    "CACHE_URL": "redis://unused/0",
                },
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertNotIn("instr/op", result.stdout)


if __name__ == "__main__":
    unittest.main()
