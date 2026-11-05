import tempfile
import asyncio
import io
import tarfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from starlette.datastructures import UploadFile

import version_api as api


class VersionDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        database = patch.object(api, "DB_PATH", str(Path(self.directory.name) / "test.db"))
        database.start()
        self.addCleanup(database.stop)
        api.init_db()
        with api.get_db_connection() as connection:
            for soc, branch, version in [
                ("esp32", "develop", "V5.0-40-develop"),
                ("esp32", "5.1-develop", "V5.1.0-2-5.1-develop"),
                ("esp32", "5.1/develop", "V5.1.0"),
                ("esp32", "experimental", "V5.0-41-experimental"),
                ("esp8266", "5.1/develop", "V5.1.0"),
                ("esp32", "5.9/develop", "V5.9.0"),
                ("esp32", "5.10/develop", "V5.10.0"),
            ]:
                connection.execute("INSERT INTO firmware_builds (soc, type, branch, version, file_path, url) VALUES (?, 'release', ?, ?, '/test.bin', 'https://example.test/test.bin')", (soc, branch, version))

    def test_identity_does_not_promote_historical_experimental(self):
        self.assertEqual(api.release_identity("V5.0-41-experimental", "experimental")["release_line"], "5.0")
        self.assertIsNone(api.release_identity("V5.0.0", "5.1/develop"))

    def test_explicit_line_and_stage_filter(self):
        result = api.firmware_versions_v2("esp32", "5.1", "develop", "release")
        self.assertEqual([entry["version"] for entry in result], ["V5.1.0", "V5.1.0-2-5.1-develop"])
        self.assertEqual(result[0]["source_ref"], "5.1/develop")

    def test_esp8266_cannot_discover_51(self):
        self.assertEqual(api.firmware_options("esp8266")["combinations"], [])
        with self.assertRaises(HTTPException) as error:
            api.firmware_latest_v2("esp8266", "5.1", "develop", "release")
        self.assertEqual(error.exception.status_code, 404)

    def test_numeric_lines_and_prerelease_order(self):
        lines = [entry["release_line"] for entry in api.firmware_options("esp32")["combinations"]]
        self.assertLess(lines.index("5.9"), lines.index("5.10"))
        self.assertGreater(api.version_sort_key("5.1.0"), api.version_sort_key("5.1.0-rc.10"))
        self.assertGreater(api.version_sort_key("5.1.0-rc.10"), api.version_sort_key("5.1.0-rc.2"))

    def test_legacy_endpoint_keeps_its_shape(self):
        result = api.get_firmware_versions("esp32", "develop", "release")
        self.assertEqual(result[0].branch, "develop")
        self.assertNotIn("release_line", result[0].model_dump())

    def test_schema_migration_is_repeatable_and_v2_has_stable_ids(self):
        api.init_db()
        first = api.firmware_latest_v2("esp32", "5.1", "develop", "release")
        second = api.firmware_latest_v2("esp32", "5.1", "develop", "release")
        self.assertEqual(first["id"], second["id"])
        self.assertIsNone(first["sha256"])

    def test_mismatched_stored_metadata_is_not_advertised(self):
        with api.get_db_connection() as connection:
            connection.execute("UPDATE firmware_builds SET release_line = '5.0' WHERE branch LIKE '5.1%'")
        self.assertEqual(api.firmware_versions_v2("esp32", "5.1", "develop", "release"), [])

    def test_artifact_paths_allow_versioned_branches_not_traversal(self):
        self.assertEqual(api.validate_branch_path("5.1/develop"), "5.1/develop")
        for value in ["../develop", "/develop", "5.1//develop"]:
            with self.assertRaises(HTTPException):
                api.validate_branch_path(value)

    def add_webapp(self, version, minimum="V5.0", maximum=None, capabilities=None):
        directory = Path(self.directory.name) / version
        directory.mkdir()
        (directory / "index.html.gz").write_bytes(b"bridge webapp")
        with api.get_db_connection() as connection:
            connection.execute("INSERT INTO webapp_builds (branch, version, min_firmware, max_firmware, file_path, basepath, required_capabilities) VALUES ('develop', ?, ?, ?, ?, 'https://example.test/webapp/', ?)",
                               (version, minimum, maximum, str(directory), capabilities))

    def test_old_firmware_receives_latest_webapp_in_its_release_line(self):
        self.add_webapp("V5.0.4")
        self.add_webapp("V5.1.2", "V5.1")
        result = api.get_latest_webapp("develop", "V5.0-40-develop")
        self.assertEqual(result["version"], "V5.0.4")
        self.assertEqual(result["files"][0]["filename"], "index.html.gz")
        self.assertEqual(result["total_size"], len(b"bridge webapp"))
        self.assertEqual(len(result["files"][0]["md5"]), 32)

    def test_compatibility_bounds_do_not_change_webapp_stream_membership(self):
        self.add_webapp("V5.0.4", "V5.0", "V5.2")
        self.add_webapp("V5.1.2", "V5.0", "V5.2")
        self.assertEqual(api.get_latest_webapp("5.1", "V5.1.0-2-5.1-develop")["version"], "V5.1.2")
        self.assertEqual(api.get_latest_webapp("experimental", "V5.0-40-experimental")["version"], "V5.0.4")
        self.assertEqual(api.get_latest_webapp("5.0/develop", "V5.0-40-develop")["version"], "V5.0.4")

    def test_capabilities_and_flash_budget_filter_within_the_webapp_stream(self):
        self.add_webapp("V5.0.4")
        self.add_webapp("V5.0.5", capabilities='["lights.multiple"]')
        self.assertEqual(api.webapp_latest_v2("V5.0-40-develop", "develop")["version"], "V5.0.4")
        self.assertEqual(api.webapp_versions_v2("V5.0-40-develop", "develop", asset_budget=1), [])

    def test_cross_line_upgrades_do_not_require_qualification_or_a_bridge(self):
        target = api.firmware_latest_v2("esp32", "5.1", "develop", "release")
        for webapp_version in ("", "V5.0-412"):
            with self.subTest(webapp_version=webapp_version):
                transition = api.firmware_transition("esp32", "V5.0.0-1022-develop", target["id"], webapp_version)
                self.assertEqual(transition, {"status": "ready", "steps": ["install_firmware"]})

    def test_cross_line_downgrades_require_configdb_review(self):
        target = api.firmware_latest_v2("esp32", "5.0", "develop", "release")
        transition = api.firmware_transition("esp32", "V5.1.0-2-develop", target["id"])
        self.assertEqual(transition["status"], "blocked")
        self.assertIn("ConfigDB schema", transition["reason"])
        self.assertEqual(transition["steps"], [])

    def test_cross_line_upgrades_compare_lines_numerically(self):
        target = api.firmware_latest_v2("esp32", "5.10", "develop", "release")
        self.assertEqual(api.firmware_transition("esp32", "V5.9.0", target["id"])["status"], "ready")

    def test_cross_line_upgrades_still_require_a_matching_soc(self):
        target = api.firmware_latest_v2("esp32", "5.1", "develop", "release")
        with self.assertRaises(HTTPException) as error:
            api.firmware_transition("esp8266", "V5.0.0-1022-develop", target["id"])
        self.assertEqual(error.exception.status_code, 422)

    def test_same_line_updates_do_not_depend_on_a_new_webapp(self):
        target = api.firmware_latest_v2("esp32", "5.0", "develop", "release")
        self.assertEqual(api.firmware_transition("esp32", "V5.0-40-develop", target["id"])["status"], "ready")

    def test_webapp_publication_preserves_bundle_on_invalid_or_changed_upload(self):
        def publish(payload):
            return asyncio.run(api.upload_webapp(branch="develop", version="V5.0.6", type="release",
                min_firmware="V5.0", max_firmware="V5.2", api_min="", api_max="", required_capabilities="[]",
                signature="test", comment="", archive=UploadFile(filename="bundle.tar", file=io.BytesIO(payload)), _=None))

        def archive(content):
            stream = io.BytesIO()
            with tarfile.open(fileobj=stream, mode="w") as bundle:
                entry = tarfile.TarInfo("index.html")
                entry.size = len(content)
                bundle.addfile(entry, io.BytesIO(content))
            return stream.getvalue()

        with patch.object(api, "STATIC_ROOT", Path(self.directory.name) / "uploads"), patch.object(api, "verify_sig"):
            original = archive(b"bridge")
            publish(original)
            publish(original)
            with self.assertRaises(HTTPException):
                publish(b"broken archive")
            with self.assertRaises(HTTPException) as conflict:
                publish(archive(b"incompatible replacement"))
            self.assertEqual(conflict.exception.status_code, 409)
            result = api.get_latest_webapp("develop", "V5.0-40-develop")
            self.assertEqual(result["total_size"], len(b"bridge"))


if __name__ == "__main__":
    unittest.main()