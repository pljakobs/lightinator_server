import gzip
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

import version_api as api


class DemoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        for name, value in (("DB_PATH", str(root / "test.db")), ("DEMO_ROOT", root / "builds"),
                            ("LEGACY_CATALOG_PATH", "")):
            mocked = patch.object(api, name, value)
            mocked.start()
            self.addCleanup(mocked.stop)
        api.init_db()

    def publish(self, version, demo=True):
        directory = api.DEMO_ROOT / version
        (directory / "assets").mkdir(parents=True)
        (directory / "icons").mkdir()
        marker = "<meta name=lightinator-demo content=1>" if demo else ""
        html = f'{marker}<script src="/assets/app.js"></script><link href="/assets/app.css"><link href=/icons/favicon.ico>'
        for name, data in (("index.html", html), ("assets/app.js", "app"), ("assets/app.css", "css"), ("icons/iconsSprite.svg", "<svg/>")):
            (directory / (name + ".gz")).write_bytes(gzip.compress(data.encode()))
        with api.get_db_connection() as conn:
            conn.execute("INSERT INTO webapp_builds (branch, version, file_path, basepath) VALUES (?, ?, ?, ?)",
                         ("5.1/develop", version, str(directory), "http://example.test/"))
        return directory

    def test_selects_latest_and_keeps_versioned_assets_immutable(self):
        self.publish("V5.1-9-develop")
        self.publish("V5.1-10-develop")
        with TestClient(api.app) as client:
            result = client.get("/api/demo/", follow_redirects=False)
            self.assertEqual(result.status_code, 302)
            self.assertEqual(result.headers["location"], "/api/demo/V5.1-10-develop/index.html?demo=1")
            document = client.get("/api/demo/V5.1-9-develop/index.html?demo=1")
            self.assertIn('src="assets/app.js"', document.text)
            self.assertIn("href=data:,", document.text)
            self.assertIn("connect-src 'self'", document.headers["content-security-policy"])
            self.assertEqual(client.get("/api/demo/V5.1-9-develop/assets/app.js").content, b"app")
            self.assertEqual(client.get("/api/demo/V5.1-9-develop/index.html", follow_redirects=False).status_code, 302)

    def test_missing_or_unsupported_latest_does_not_fall_back(self):
        self.publish("V5.1-9-develop")
        self.publish("V5.1-10-develop", demo=False)
        with self.assertRaises(HTTPException) as error:
            api.demo_latest()
        self.assertEqual(error.exception.status_code, 503)

    def test_rejects_traversal_and_symlinks(self):
        directory = self.publish("V5.1-9-develop")
        (directory / "assets" / "link.js.gz").symlink_to(directory / "assets" / "app.js.gz")
        for name in ("../index.html", "assets/link.js", "system"):
            with self.subTest(name=name), self.assertRaises(HTTPException):
                api.demo_file("V5.1-9-develop", name)
