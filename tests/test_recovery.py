import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location(
    "recovery", Path(__file__).resolve().parents[1] / "deploy/recovery.py"
)
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "home/deploy/server"
        self.repo.mkdir(parents=True)
        self.home = self.repo.parent
        self.code = self.repo / "version_api.py"
        self.code.write_text("original dirty code")
        self.env = self.root / "etc/lightinator"
        self.env.mkdir(parents=True)
        (self.env / "knock-secret.env").write_text("private-test-value")
        self.venv = self.root / "external/venv"
        self.venv.mkdir(parents=True)
        (self.venv / "installed.txt").write_text("original dependencies")
        (self.repo / ".venv").symlink_to(self.venv)
        self.db = self.repo / "firmware.db"
        with contextlib.closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("CREATE TABLE catalogue (version TEXT)")
            connection.execute("INSERT INTO catalogue VALUES ('original')")
        self.web = self.home / "nginx/html"
        self.web.mkdir(parents=True)
        (self.web / "index.html").write_text("original page")
        downloads = self.web / "download"
        downloads.mkdir()
        (downloads / "firmware.bin").write_text("published artifact")
        self.calls = []

        def run(args, **kwargs):
            self.calls.append(args)
            if args[0] == "firewall-cmd":
                return subprocess.CompletedProcess(args, 0, stdout="firewall state\n")
            if "is-active" in args:
                stdout = "active"
            elif "is-enabled" in args:
                stdout = "enabled"
            else:
                stdout = "loaded"
            return subprocess.CompletedProcess(args, 0, stdout=stdout)

        mocked = patch.object(recovery.subprocess, "run", side_effect=run)
        mocked.start()
        self.addCleanup(mocked.stop)
        self.output = io.StringIO()

    def snapshot(self):
        with contextlib.redirect_stdout(self.output):
            return recovery.snapshot(self.root, self.repo, self.home)

    def test_round_trip_dirty_code_external_venv_secrets_sqlite_and_page(self):
        backup = self.snapshot()
        self.assertEqual(backup.stat().st_mode & 0o777, 0o700)
        self.assertTrue((backup / "recovery.py").exists())
        self.assertNotIn("private-test-value", self.output.getvalue())
        self.code.write_text("new code")
        (self.repo / "new_module.py").write_text("not in original deployment")
        (self.venv / "installed.txt").write_text("new dependencies")
        (self.venv / "new_dependency.py").write_text("not in original environment")
        (self.env / "knock-secret.env").write_text("new secret")
        (self.web / "index.html").write_text("new page")
        (self.web / "download/firmware.bin").write_text("new artifact")
        with contextlib.closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute("UPDATE catalogue SET version='new'")
        with patch.object(recovery.os, "chown"), contextlib.redirect_stdout(self.output):
            recovery.restore(backup)
        self.assertEqual(self.code.read_text(), "original dirty code")
        self.assertFalse((self.repo / "new_module.py").exists())
        self.assertEqual((self.venv / "installed.txt").read_text(), "original dependencies")
        self.assertFalse((self.venv / "new_dependency.py").exists())
        self.assertTrue((self.repo / ".venv").is_symlink())
        self.assertEqual((self.env / "knock-secret.env").read_text(), "private-test-value")
        self.assertEqual((self.web / "index.html").read_text(), "original page")
        self.assertEqual((self.web / "download/firmware.bin").read_text(), "new artifact")
        with contextlib.closing(sqlite3.connect(self.db)) as connection, connection:
            self.assertEqual(connection.execute("SELECT version FROM catalogue").fetchone()[0], "original")
        self.assertNotIn(["firewall-cmd", "--reload"], self.calls)
        self.assertIn(["systemctl", "start", "version-api.service"], self.calls)

    def test_custom_database_path(self):
        custom = self.root / "private/catalogue.db"
        custom.parent.mkdir()
        self.db.rename(custom)
        (self.env / "version-api.env").write_text('VERSION_DB_PATH="/private/catalogue.db"\n')
        backup = self.snapshot()
        metadata = json.loads((backup / "manifest.json").read_text())
        self.assertEqual(metadata["database"], str(custom))
        self.assertTrue((backup / "database.sqlite").exists())

    def test_incomplete_or_public_backup_is_rejected_before_service_changes(self):
        backup = self.snapshot()
        self.calls.clear()
        (backup / "COMPLETE").unlink()
        with self.assertRaisesRegex(ValueError, "incomplete"):
            recovery.restore(backup)
        (backup / "COMPLETE").touch()
        backup.chmod(0o755)
        with self.assertRaisesRegex(ValueError, "private"):
            recovery.restore(backup)
        self.assertEqual(self.calls, [])

    def test_backup_failure_has_no_complete_marker(self):
        with patch.object(recovery.tarfile, "open", side_effect=OSError("disk full")):
            with contextlib.redirect_stderr(self.output), self.assertRaisesRegex(OSError, "disk full"):
                self.snapshot()
        storage = self.root / "var/lib/lightinator/backups"
        self.assertFalse(any(storage.glob("*/COMPLETE")))

    def test_tampered_archive_is_rejected_before_service_changes(self):
        backup = self.snapshot()
        with (backup / "files.tar").open("ab") as archive:
            archive.write(b"corruption")
        self.calls.clear()
        with self.assertRaisesRegex(ValueError, "integrity"):
            recovery.restore(backup)
        self.assertEqual(self.calls, [])

    def test_snapshot_includes_committed_wal_data(self):
        with contextlib.closing(sqlite3.connect(self.db)) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("INSERT INTO catalogue VALUES ('committed-in-wal')")
            connection.commit()
            backup = self.snapshot()
            with contextlib.closing(sqlite3.connect(backup / "database.sqlite")) as saved:
                self.assertEqual(saved.execute("SELECT COUNT(*) FROM catalogue").fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
