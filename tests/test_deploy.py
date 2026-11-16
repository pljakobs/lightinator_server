import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

# Exercise the real shell scripts and filesystem, replacing host/service commands.
COMMAND = r'''
import os, pathlib, shutil, subprocess, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
root = pathlib.Path(os.environ["FIXTURE"])
with (root / "commands").open("a") as log:
    log.write(name + " " + " ".join(args) + "\n")
if name == "git":
    sys.exit(1 if os.environ.get("DIRTY") else 0)
elif name in ("python3.11", "python3"):
    if os.environ.get("BAD_BACKUP"):
        sys.exit(1)
elif name == "install":
    filtered = []
    while args:
        arg = args.pop(0)
        if arg in ("-o", "-g"):
            args.pop(0)
        else:
            filtered.append(arg)
    sys.exit(subprocess.call(["/usr/bin/install", *filtered]))
elif name == "runuser":
    sys.exit(subprocess.call(args[args.index("--") + 1:]))
elif name == "python":
    if args == ["--version"]:
        print("Python 3.11.0")
elif name == "systemctl":
    if args[:2] == ["restart", "version-api.service"]:
        (root / "active").touch()
    if args[:2] == ["is-active", "--quiet"]:
        sys.exit(0 if (root / "active").exists() else 3)
elif name == "podman":
    if args == ["exec", "nginx", "nginx", "-t"] and os.environ.get("BAD_NGINX"):
        sys.exit(1)
elif name == "curl":
    if os.environ.get("BAD_HEALTH"):
        sys.exit(22)
    url = next(a for a in args if a.startswith("http"))
    if os.environ.get("BAD_PROXY") and url == "http://127.0.0.1/api/demo/":
        sys.exit(22)
    dest = pathlib.Path(args[args.index("-o") + 1])
    if url.endswith("/api/demo/"):
        dest.write_text("<meta name=lightinator-demo content=1>")
    elif os.environ.get("BAD_PAGE"):
        dest.write_text("stale landing page")
    else:
        dest.write_bytes((pathlib.Path(os.environ["DEPLOY_HOME"]) / "nginx/html/index.html").read_bytes())
'''


class DeployTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        for directory in ("deploy", "nginx", "systemd", "html", "fail2ban", "containers"):
            shutil.copytree(ROOT / directory, self.repo / directory)
        for name in ("version_api.py", "requirements-api.txt"):
            shutil.copyfile(ROOT / name, self.repo / name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for command in ("git", "install", "runuser", "python", "pip", "systemctl",
                        "podman", "curl", "sleep", "chown", "chcon", "dnf",
                        "firewall-cmd", "certbot", "getent", "id", "usermod",
                        "fail2ban-client", "python3.11", "python3"):
            path = self.bin / command
            path.write_text(f"#!{sys.executable}\n" + COMMAND)
            path.chmod(0o755)
        venv = self.root / "external-venv"
        venv.mkdir()
        (venv / "bin").symlink_to(self.bin)
        (self.repo / ".venv").symlink_to(venv)
        self.home = self.root / "home"
        self.web = self.home / "nginx/html"
        self.web.mkdir(parents=True)
        self.conf = self.home / "nginx/conf.d/default.conf"
        self.conf.parent.mkdir()
        self.conf.write_text("original nginx\n")
        self.unit = self.root / "system/etc/systemd/system/version-api.service"
        self.unit.parent.mkdir(parents=True)
        self.unit.write_text("original unit\n")
        self.env = self.root / "system/etc/lightinator/version-api.env"
        self.env.parent.mkdir()
        self.env.write_text("VERSION_DB_PATH=existing.db\n")
        self.preserved = [
            self.repo / "firmware.db", self.web / "version.json",
            self.web / "manifest_release.json", self.web / "manifest_debug.json",
            self.web / "download/build.bin", self.env,
        ]
        for path in self.preserved[:-1]:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"preserve exactly\n")
        (self.web / "index.html").write_text("original page\n")
        self.environment = {
            **os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}",
            "FIXTURE": str(self.root), "SYSTEM_ROOT": str(self.root / "system"),
            "DEPLOY_HOME": str(self.home),
        }

    def deploy(self, **overrides):
        return subprocess.run(
            ["bash", str(self.repo / "deploy/update.sh")],
            env={**self.environment, **overrides}, capture_output=True, text=True,
        )

    def test_second_run_preserves_data_and_does_not_restart_or_reinstall(self):
        before = {p: p.read_bytes() for p in self.preserved}
        result = self.deploy()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.web / "index.html").read_bytes(), (ROOT / "html/index.html").read_bytes())
        self.assertEqual(self.conf.read_bytes(), (ROOT / "nginx/default.conf").read_bytes())
        (self.root / "commands").write_text("")
        result = self.deploy()
        self.assertEqual(result.returncode, 0, result.stderr)
        commands = (self.root / "commands").read_text()
        self.assertNotIn("systemctl restart", commands)
        self.assertNotIn("systemctl daemon-reload", commands)
        self.assertNotIn("nginx -s reload", commands)
        self.assertNotIn("pip install", commands)
        self.assertNotIn("migrate_to_sqlite", commands)
        self.assertTrue((self.repo / ".venv").is_symlink())
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)

    def test_failed_nginx_validation_restores_managed_files(self):
        result = self.deploy(BAD_NGINX="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.conf.read_text(), "original nginx\n")
        self.assertEqual(self.unit.read_text(), "original unit\n")
        self.assertEqual((self.web / "index.html").read_text(), "original page\n")
        self.assertIn("restoring managed files", result.stderr)

    def test_failed_health_check_does_not_publish_link_or_success_stamp(self):
        result = self.deploy(BAD_HEALTH="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.web / "index.html").read_text(), "original page\n")
        self.assertFalse((self.root / "system/var/lib/lightinator/deploy/application").exists())

    def test_dirty_checkout_is_rejected_before_changes(self):
        result = self.deploy(DIRTY="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("reconcile", result.stderr)
        self.assertEqual((self.root / "commands").read_text().count("\n"), 1)

    def test_backup_failure_prevents_all_changes(self):
        result = self.deploy(BAD_BACKUP="1")
        self.assertNotEqual(result.returncode, 0)
        commands = (self.root / "commands").read_text().splitlines()
        self.assertEqual(len(commands), 2)
        self.assertIn("recovery.py backup", commands[1])
        self.assertEqual((self.web / "index.html").read_text(), "original page\n")

    def test_failure_after_page_install_restores_previous_page(self):
        for failure in ("BAD_PROXY", "BAD_PAGE"):
            with self.subTest(failure=failure):
                result = self.deploy(**{failure: "1"})
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual((self.web / "index.html").read_text(), "original page\n")
                self.assertEqual(self.conf.read_text(), "original nginx\n")
                self.assertEqual(self.unit.read_text(), "original unit\n")

    def test_changed_api_restarts_but_static_change_does_not(self):
        self.assertEqual(self.deploy().returncode, 0)
        (self.repo / "version_api.py").write_text("# new reviewed revision\n")
        (self.root / "commands").write_text("")
        self.assertEqual(self.deploy().returncode, 0)
        self.assertIn("systemctl restart version-api.service", (self.root / "commands").read_text())
        with (self.repo / "html/index.html").open("a") as page:
            page.write("\n<!-- updated -->\n")
        (self.root / "commands").write_text("")
        self.assertEqual(self.deploy().returncode, 0)
        self.assertNotIn("systemctl restart", (self.root / "commands").read_text())

    def test_setup_rerun_does_not_reload_and_does_not_log_secret(self):
        secret = "0123456789abcdef" * 4
        secret_file = self.env.parent / "knock-secret.env"
        secret_file.write_text(f"DEPLOY_KNOCK_HASH={secret}\n")
        for directory in ("filter.d", "action.d", "jail.d"):
            (self.root / "system/etc/fail2ban" / directory).mkdir(parents=True)
        command = ["bash", "-x", str(self.repo / "deploy/setup.sh")]
        first = subprocess.run(command, env=self.environment, capture_output=True, text=True)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertNotIn(secret, first.stdout + first.stderr)
        commands = self.root / "commands"
        self.assertNotIn(secret, commands.read_text())
        rendered = self.root / "system/etc/fail2ban/filter.d/gh-knocker.conf"
        self.assertIn(secret, rendered.read_text())
        self.assertEqual(rendered.stat().st_mode & 0o777, 0o600)
        commands.write_text("")
        second = subprocess.run(command, env=self.environment, capture_output=True, text=True)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertNotIn(secret, second.stdout + second.stderr + commands.read_text())
        self.assertNotIn("systemctl reload", commands.read_text())
        self.assertNotIn("systemctl restart", commands.read_text())

    def test_update_does_not_read_knock_secret_or_modify_fail2ban(self):
        (self.env.parent / "knock-secret.env").write_text("exit 99\n")
        result = self.deploy()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("fail2ban", (self.root / "commands").read_text())


if __name__ == "__main__":
    unittest.main()
