"""Verify pinned OME information exits without ignoring real CLI failures."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


@unittest.skipIf(os.name == "nt", "The packaged CLI checker runs on Linux")
class TestOvenMediaEngineCLI(unittest.TestCase):
    def run_checker(self, status: int, help_text: str, version: str):
        with tempfile.TemporaryDirectory(prefix="ome cli ") as directory:
            root = Path(directory)
            checker = root / "check.sh"
            checker.write_text(
                Path(__file__).with_name("check_ovenmediaengine_cli.sh").read_text()
            )
            binary = root / "OvenMediaEngine"
            binary.write_text(
                "#!/bin/sh\n"
                f"case \"$1\" in -h) printf '%s\\n' '{help_text}';; "
                f"-v) printf '%s\\n' '{version}';; esac\n"
                f"exit {status}\n"
            )
            binary.chmod(0o700)
            return subprocess.run(
                ["/bin/sh", str(checker), str(binary)],
                capture_output=True,
                text=True,
                timeout=5,
            )

    def test_pinned_information_flags_return_one_with_expected_output(self):
        result = self.run_checker(
            1, "Usage: OvenMediaEngine [OPTION]...", "OvenMediaEngine v0.21.0"
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_unexpected_exit_status_is_not_ignored(self):
        for status in (0, 2, 127):
            with self.subTest(status=status):
                result = self.run_checker(
                    status, "Usage: OvenMediaEngine", "OvenMediaEngine v0.21.0"
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Unexpected OME", result.stderr)

    def test_wrong_version_and_loader_errors_fail(self):
        for version in ("OvenMediaEngine v0.20.5", "error loading shared libraries"):
            with self.subTest(version=version):
                result = self.run_checker(1, "Usage: OvenMediaEngine", version)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Unexpected OME -v output", result.stderr)

    def test_missing_help_banner_fails(self):
        result = self.run_checker(1, "unrelated error", "OvenMediaEngine v0.21.0")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unexpected OME -h output", result.stderr)
