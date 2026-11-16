import asyncio
import base64
import hashlib
import io
import json
import os
import shutil
import tarfile
import tempfile
import subprocess
import sys
import unittest
import uuid
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import HTTPException
from starlette.datastructures import UploadFile
import httpx

import version_api as api
import manage_version
from migrate_to_sqlite import import_catalogue


class StreamRequest:
    def __init__(self, payload: bytes):
        self.payload = payload

    async def stream(self):
        yield self.payload


class OTAReleaseAPITest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.saved_settings = {
            name: getattr(api, name)
            for name in (
                "DB_PATH",
                "STATIC_ROOT",
                "OTA_STORAGE_ROOT",
                "DOWNLOAD_BASE_URL",
                "CI_TOKEN",
                "OTA_PROMOTION_TOKEN",
                "OTA_ADMIN_TOKEN",
                "PUBLIC_KEY_HEX",
                "LEGACY_CATALOG_PATH",
                "LEGACY_ASSET_ROOT",
                "PUBLISHING_API_ENABLED",
            )
        }
        self.private_key = Ed25519PrivateKey.generate()
        self.public_key_hex = self.private_key.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        ).hex()
        api.DB_PATH = str(root / "test.db")
        api.STATIC_ROOT = root / "downloads"
        api.OTA_STORAGE_ROOT = root / "private-ota"
        api.DOWNLOAD_BASE_URL = "http://downloads.example.test"
        api.CI_TOKEN = "ci-test-token"
        api.OTA_PROMOTION_TOKEN = "promotion-test-token"
        api.OTA_ADMIN_TOKEN = "admin-test-token"
        api.PUBLIC_KEY_HEX = self.public_key_hex
        api.LEGACY_CATALOG_PATH = ""
        api.LEGACY_ASSET_ROOT = ""
        api.PUBLISHING_API_ENABLED = True
        api.init_db()

    def tearDown(self):
        for name, value in self.saved_settings.items():
            setattr(api, name, value)
        self.temp_dir.cleanup()

    def manifest(self, payload=b"controller!", *, version="V5.0.1-1", channel="5.0/develop", soc="esp8266"):
        return api.OTAReleaseManifest.model_validate(
            {
                "release_id": str(uuid.uuid4()),
                "product": "controller",
                "version": version,
                "channel": channel,
                "source_repository": "pljakobs/esp_rgbww_firmware",
                "source_commit": "a" * 40,
                "changelog": [{"commit": "b" * 7, "subject": "Fix controller update"}],
                "targets": [
                    {
                        "hardware": "controller-board",
                        "hardware_revision": "rev1",
                        "soc": soc,
                        "build_type": "release",
                        "artifacts": [
                            {
                                "artifact_id": "controller-release",
                                "role": "firmware",
                                "filename": "rom0.bin" if soc == "esp8266" else "app.bin",
                                "sha256": hashlib.sha256(payload).hexdigest(),
                                "size": len(payload),
                            }
                        ],
                    }
                ],
            }
        )

    def create_request(self, manifest, *, signature=None):
        canonical = api._canonical_json(manifest.model_dump(mode="json", exclude_none=True))
        if signature is None:
            signature = self.private_key.sign(canonical).hex()
        return api.OTAReleaseCreateRequest(manifest=manifest, signature=signature)

    def test_signed_staging_finalize_promotion_and_discovery(self):
        payload = b"controller!"
        manifest = self.manifest(payload)
        staged = api.create_ota_release(self.create_request(manifest), None)
        release_id = staged["release_id"]
        self.assertEqual(staged["status"], "staged")

        with self.assertRaises(HTTPException) as not_public:
            api.download_ota_artifact(release_id, "controller-release")
        self.assertEqual(not_public.exception.status_code, 404)

        with self.assertRaises(HTTPException) as bad_hash:
            asyncio.run(
                api.upload_ota_artifact(
                    release_id,
                    "controller-release",
                    StreamRequest(b"x" * len(payload)),
                    None,
                )
            )
        self.assertEqual(bad_hash.exception.status_code, 422)

        uploaded = asyncio.run(
            api.upload_ota_artifact(
                release_id,
                "controller-release",
                StreamRequest(payload),
                None,
            )
        )
        self.assertEqual(uploaded["sha256"], hashlib.sha256(payload).hexdigest())

        finalized = api.finalize_ota_release(release_id, None)
        self.assertEqual(finalized["status"], "published")
        self.assertEqual(finalized["changelog"][0]["subject"], "Fix controller update")
        self.assertEqual(finalized["targets"][0]["soc"], "esp8266")
        self.assertFalse((Path(api.OTA_STORAGE_ROOT) / "staging" / "controller" / release_id).exists())
        self.assertTrue(finalized["targets"][0]["artifacts"][0]["url"].startswith("http://"))
        returned_manifest = api._canonical_json(finalized["manifest"])
        self.private_key.public_key().verify(bytes.fromhex(finalized["signature"]), returned_manifest)
        self.assertEqual(finalized["signing_public_key"], self.public_key_hex)

        latest = api.get_latest_ota_release(
            channel="5.0/develop",
            product="controller",
            hardware="controller-board",
            hardware_revision="rev1",
            soc="esp8266",
            build_type="release",
        )
        self.assertEqual(latest["release_id"], release_id)

        download = api.download_ota_artifact(release_id, "controller-release")
        self.assertEqual(Path(download.path).read_bytes(), payload)

        with self.assertRaises(HTTPException) as wrong_line:
            api.promote_ota_release(
                release_id,
                api.OTAPromoteRequest(channel="5.1/develop"),
                None,
            )
        self.assertEqual(wrong_line.exception.status_code, 422)

        promoted = api.promote_ota_release(
            release_id,
            api.OTAPromoteRequest(channel="5.0/testing"),
            None,
        )
        self.assertEqual(promoted["status"], "promoted")
        latest_testing = api.get_latest_ota_release(
            channel="5.0/testing",
            product="controller",
            hardware="controller-board",
            hardware_revision="rev1",
            soc="esp8266",
            build_type="release",
        )
        self.assertEqual(latest_testing["release_id"], release_id)

    def test_controller_policy_rejects_esp8266_on_51(self):
        manifest = self.manifest(version="V5.1.0-1", channel="5.1/develop", soc="esp8266")
        request = self.create_request(manifest)
        with self.assertRaises(HTTPException) as unsupported:
            api.create_ota_release(request, None)
        self.assertEqual(unsupported.exception.status_code, 422)

    def test_legacy_projection_is_deterministic_read_only_and_published_only(self):
        payload = b"controller!"
        manifest = self.manifest(payload)
        api.create_ota_release(self.create_request(manifest), None)
        self.assertEqual(api.get_legacy_catalogue()["firmware"], [])
        asyncio.run(api.upload_ota_artifact(manifest.release_id, "controller-release", StreamRequest(payload), None))
        api.finalize_ota_release(manifest.release_id, None)
        with api.get_db_connection() as connection:
            before = list(connection.iterdump())
        first = api.get_legacy_catalogue()
        self.assertEqual(first, api.get_legacy_catalogue())
        self.assertEqual(first["firmware"][0]["fw_version"], manifest.version)
        self.assertEqual(first["firmware"][0]["branch"], "develop")
        self.assertIn("/api/ota/artifacts/", first["firmware"][0]["files"]["rom"]["url"])
        with api.get_db_connection() as connection:
            self.assertEqual(before, list(connection.iterdump()))
            legacy = api.get_latest_firmware("esp8266", "develop", "release")
            self.assertEqual(legacy.version, manifest.version)
            self.assertEqual(api.get_firmware_branches("esp8266", "release"), ["develop"])

    def test_legacy_projection_does_not_expose_new_lines_to_old_fleet_fallbacks(self):
        payload = b"controller!"
        manifest = self.manifest(payload, version="V5.1.0-1", channel="5.1/develop", soc="esp32")
        api.create_ota_release(self.create_request(manifest), None)
        asyncio.run(api.upload_ota_artifact(manifest.release_id, "controller-release", StreamRequest(payload), None))
        api.finalize_ota_release(manifest.release_id, None)
        self.assertEqual(api.get_legacy_catalogue()["firmware"], [])
        self.assertEqual(api.catalogue_firmware_entries(include_new_lines=True)[0]["version"], manifest.version)

    def test_v2_adapter_discovers_signed_release_and_keeps_stable_identity(self):
        payload = b"controller!"
        manifest = self.manifest(payload, version="V5.1.0-1", channel="5.1/develop", soc="esp32")
        api.create_ota_release(self.create_request(manifest), None)
        self.assertEqual(api.firmware_options("esp32")["combinations"], [])
        asyncio.run(api.upload_ota_artifact(manifest.release_id, "controller-release", StreamRequest(payload), None))
        api.finalize_ota_release(manifest.release_id, None)
        candidate = api.firmware_latest_v2("esp32", "5.1", "develop", "release")
        self.assertEqual(candidate["release_id"], manifest.release_id)
        self.assertEqual(candidate["sha256"], hashlib.sha256(payload).hexdigest())
        self.assertEqual(candidate["source_ref"], "5.1/develop")
        self.assertEqual(api.firmware_options("esp8266")["combinations"], [])
        self.assertEqual(api.firmware_transition("esp32", "V5.0-40-develop", candidate["id"])["status"], "blocked")

    def test_versioned_and_legacy_routes_share_the_published_catalogue(self):
        with api.get_db_connection() as connection:
            connection.execute("INSERT INTO firmware_builds (soc, type, branch, version, file_path, url) VALUES ('esp32', 'release', 'experimental', 'V5.0-40-experimental', '/old.bin', 'https://example.test/old.bin')")
        candidate = api.firmware_latest_v2("esp32", "5.0", "develop", "release")
        self.assertTrue(candidate["id"].startswith("legacy:"))
        self.assertEqual(api.get_legacy_catalogue()["firmware"][0]["version"], candidate["version"])
        self.assertGreater(api.version_sort_key("V5.1.0"), api.version_sort_key("V5.1.0-rc.10"))
        self.assertGreater(api.version_sort_key("V5.10.0"), api.version_sort_key("V5.9.9"))

    def test_import_preserves_ids_policy_urls_and_the_original_catalogue(self):
        source = Path(self.temp_dir.name) / "version.json"
        url = "http://legacy.test/download/develop/old/esp32/release/app.bin"
        source.write_text(json.dumps({"firmware": [{"version": "V5.0-40-develop", "branch": "develop",
            "soc": "esp32", "type": "release", "files": {"rom": {"url": url}}}]}))
        original = source.read_bytes()
        self.assertEqual(import_catalogue(source, source.parent)["firmware"], 1)
        candidate = api.firmware_latest_v2("esp32", "5.0", "develop", "release")
        with api.get_db_connection() as connection:
            connection.execute("UPDATE firmware_builds SET sha256 = 'verified', upgrade_from_lines = '[\"5.0\"]'")
        self.assertEqual(import_catalogue(source, source.parent)["firmware"], 0)
        updated = api.firmware_latest_v2("esp32", "5.0", "develop", "release")
        self.assertEqual(candidate["id"], updated["id"])
        self.assertEqual(updated["sha256"], "verified")
        self.assertEqual(updated["url"], url)
        self.assertEqual(source.read_bytes(), original)

    def test_bridge_upload_and_signed_migration_are_integrated(self):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as archive:
            entry = tarfile.TarInfo("index.html")
            entry.size = 6
            archive.addfile(entry, io.BytesIO(b"bridge"))
        payload = stream.getvalue()

        def publish(bundle):
            return asyncio.run(api.upload_webapp(branch="develop", version="V6.0.0", type="release",
                min_firmware="V5.0", max_firmware="V5.2", api_min="", api_max="", required_capabilities="[]",
                signature=self.private_key.sign(bundle).hex(), comment="Bridge",
                archive=UploadFile(filename="bridge.tar", file=io.BytesIO(bundle)), _=None))

        publish(payload)
        publish(payload)
        with self.assertRaises(HTTPException):
            publish(b"broken")
        manifest = self.manifest(version="V5.1.0-1", channel="5.1/develop", soc="esp32")
        manifest.upgrade_from_lines = ["5.0"]
        api.create_ota_release(self.create_request(manifest), None)
        asyncio.run(api.upload_ota_artifact(manifest.release_id, "controller-release", StreamRequest(b"controller!"), None))
        api.finalize_ota_release(manifest.release_id, None)
        candidate = api.firmware_latest_v2("esp32", "5.1", "develop", "release")
        self.assertEqual(api.firmware_transition("esp32", "V5.0-40-develop", candidate["id"])["status"], "bridge_required")
        self.assertEqual(api.firmware_transition("esp32", "V5.0-40-develop", candidate["id"], "V6.0.0")["status"], "ready")
        legacy = api.get_latest_webapp("develop", "V5.0-40-develop")
        self.assertEqual(legacy["files"][0]["filename"], "index.html")
        self.assertEqual(legacy["total_size"], 6)

    def test_old_publisher_changes_are_imported_without_replacing_database_records(self):
        source = Path(self.temp_dir.name) / "version.json"
        entry = {"soc": "esp32", "type": "release", "branch": "develop",
                 "version": "V5.0-40-develop", "files": {"rom": {"url": "http://legacy.test/old.bin"}}}
        source.write_text(json.dumps({"firmware": [entry]}))
        api.LEGACY_CATALOG_PATH = str(source)
        original = api.firmware_latest_v2("esp32", "5.0", "develop", "release")
        source.write_text("unfinished JSON write")
        with self.assertLogs("version_api", level="WARNING"):
            self.assertEqual(api.firmware_latest_v2("esp32", "5.0", "develop", "release")["id"], original["id"])
        entry["version"] = "V5.0-41-develop"
        source.write_text(json.dumps({"firmware": [entry]}))
        self.assertEqual(api.firmware_latest_v2("esp32", "5.0", "develop", "release")["version"], entry["version"])
        self.assertEqual(len(api.firmware_versions_v2("esp32", "5.0", "develop", "release")), 2)

    def test_http_old_and_new_clients_and_artifact_download(self):
        payload = b"controller!"
        manifest = self.manifest(payload)
        api.create_ota_release(self.create_request(manifest), None)
        asyncio.run(api.upload_ota_artifact(manifest.release_id, "controller-release", StreamRequest(payload), None))
        api.finalize_ota_release(manifest.release_id, None)

        async def exercise_routes():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://test") as client:
                old = await client.get("/version.json")
                self.assertEqual(old.status_code, 200)
                self.assertEqual(old.json(), (await client.get("/api/version.json")).json())
                self.assertEqual(old.json()["firmware"][0]["branch"], "develop")
                options = await client.get("/api/v2/firmware/options", params={"soc": "esp8266"})
                self.assertEqual(options.json()["combinations"], [{"release_line": "5.0", "stage": "develop", "build_type": "release"}])
                latest = await client.get("/api/v2/firmware/latest", params={"soc": "esp8266", "release_line": "5.0", "stage": "develop", "build_type": "release"})
                self.assertEqual(latest.status_code, 200)
                legacy = await client.get("/api/firmware/versions", params={"soc": "esp8266", "branch": "develop", "type": "release"})
                self.assertEqual(legacy.json()[0]["version"], latest.json()["version"])
                download = await client.get(f"/api/ota/artifacts/{manifest.release_id}/controller-release")
                self.assertEqual(download.status_code, 404)
                missing = await client.get("/api/v2/firmware/latest", params={"soc": "esp8266", "release_line": "5.1", "stage": "develop", "build_type": "release"})
                self.assertEqual(missing.status_code, 404)

        asyncio.run(exercise_routes())

    def test_local_launcher_refuses_existing_non_test_directory(self):
        root = Path(self.temp_dir.name)
        existing = root / "keep.txt"
        existing.write_text("do not modify")
        result = subprocess.run([sys.executable, "local_server.py", "--data-dir", str(root)],
                                cwd=Path(api.__file__).parent, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Refusing an existing directory", result.stderr)
        self.assertEqual(existing.read_text(), "do not modify")

    def test_publishing_can_be_disabled_without_blocking_discovery(self):
        api.PUBLISHING_API_ENABLED = False

        async def exercise_routes():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://test") as client:
                response = await client.post("/api/ota/releases", json={}, headers={"Authorization": "Bearer ci-test-token"})
                self.assertEqual(response.status_code, 404)
                self.assertIn("manage_version.py", response.json()["detail"])
                self.assertEqual((await client.get("/version.json")).status_code, 200)
                self.assertEqual((await client.get("/api/v2/firmware/options", params={"soc": "esp32"})).status_code, 200)

        asyncio.run(exercise_routes())

    def publish_cli(self, *arguments):
        root = Path(self.temp_dir.name)
        manage_version.database_main([str(root / "version.json"), "--db", api.DB_PATH,
                                      "--asset-root", str(root), *arguments])

    def test_script_publishes_to_database_without_reading_or_writing_json(self):
        root = Path(self.temp_dir.name)
        source = root / "version.json"
        source.write_text("this is not a JSON input")
        asset = root / "download/firmware/5.1/develop/V5.1.0/esp32/release/app.bin"
        asset.parent.mkdir(parents=True)
        asset.write_bytes(b"new firmware")
        command = ("add", "esp32", "release", "5.1/develop", "V5.1.0",
                   "http://legacy.test/download/firmware/5.1/develop/V5.1.0/esp32/release/app.bin")
        self.publish_cli(*command)
        first = api.firmware_latest_v2("esp32", "5.1", "develop", "release")
        self.publish_cli(*command)
        self.assertEqual(api.firmware_latest_v2("esp32", "5.1", "develop", "release")["id"], first["id"])
        self.assertEqual(first["sha256"], hashlib.sha256(b"new firmware").hexdigest())
        self.assertEqual(source.read_text(), "this is not a JSON input")
        asset.write_bytes(b"changed firmware")
        with self.assertRaisesRegex(ValueError, "immutable"):
            self.publish_cli(*command)

    def test_script_rejects_missing_assets_and_wrong_soc_without_publishing(self):
        with self.assertRaises(ValueError):
            self.publish_cli("add", "esp32", "release", "5.1/develop", "V5.1.0", "http://legacy.test/missing.bin")
        with self.assertRaisesRegex(ValueError, "SoC"):
            self.publish_cli("add", "esp8266", "release", "5.1/develop", "V5.1.0", "http://legacy.test/missing.bin")
        self.assertEqual(api.firmware_options("esp32")["combinations"], [])

    def test_script_publishes_bridge_and_retention_preserves_it(self):
        root = Path(self.temp_dir.name)
        assets = root / "download/webui/develop/V6.0.0"
        assets.mkdir(parents=True)
        (assets / "index.html").write_bytes(b"bridge")
        command = ("add-webapp", "develop", "V6.0.0", "--type", "release",
                   "--min-firmware", "V5.0", "--max-firmware", "V5.2")
        self.publish_cli(*command)
        self.publish_cli(*command)
        self.publish_cli("cull")
        candidate = api.get_latest_webapp("develop", "V5.0-40-develop")
        self.assertEqual(candidate["version"], "V6.0.0")
        self.assertEqual(candidate["total_size"], 6)
        self.assertFalse((root / "version.json").exists())

    def test_existing_ci_command_shape_writes_database_in_a_separate_process(self):
        root = Path(self.temp_dir.name)
        catalogue = root / "version.json"
        catalogue.write_text("read-only projection placeholder")
        asset = root / "download/firmware/develop/V5.0-42-develop/esp32/release/app.bin"
        asset.parent.mkdir(parents=True)
        asset.write_bytes(b"rsynced firmware")
        environment = {**os.environ, "VERSION_DB_PATH": api.DB_PATH, "VERSION_ASSET_ROOT": str(root)}
        result = subprocess.run([sys.executable, str(Path(manage_version.__file__)), "version.json", "add",
            "esp32", "release", "develop", "V5.0-42-develop",
            "http://legacy.test/download/firmware/develop/V5.0-42-develop/esp32/release/app.bin",
            "--comment-b64", base64.b64encode(b"Published through existing secured transport").decode()],
            cwd=root, env=environment, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        candidate = api.firmware_latest_v2("esp32", "5.0", "develop", "release")
        self.assertEqual(candidate["comment"], "Published through existing secured transport")
        self.assertEqual(catalogue.read_text(), "read-only projection placeholder")

    def test_script_validates_ci_webapp_checksums_and_preserves_bridge_requirements(self):
        root = Path(self.temp_dir.name)
        assets = root / "download/webui/develop/V6.0.0"
        assets.mkdir(parents=True)
        (assets / "index.html").write_bytes(b"bridge")
        files = [{"path": "index.html", "md5": hashlib.md5(b"bridge", usedforsecurity=False).hexdigest()}]
        encoded = base64.b64encode(json.dumps(files).encode()).decode()
        command = ("add-webapp", "develop", "V6.0.0", "--type", "release", "--min-firmware", "V5.0", "--files-json-b64", encoded)
        self.publish_cli(*command, "--max-firmware", "V5.2")
        self.publish_cli(*command)
        with api.get_db_connection() as connection:
            self.assertEqual(connection.execute("SELECT max_firmware FROM webapp_builds").fetchone()[0], "V5.2")
        files[0]["md5"] = "0" * 32
        with self.assertRaisesRegex(ValueError, "CI file manifest"):
            self.publish_cli("add-webapp", "develop", "V6.0.0", "--files-json-b64", base64.b64encode(json.dumps(files).encode()).decode())

    def test_script_does_not_create_an_unintended_database(self):
        root = Path(self.temp_dir.name)
        database = root / "wrong.db"
        with self.assertRaisesRegex(ValueError, "Database does not exist"):
            manage_version.database_main([str(root / "version.json"), "--db", str(database), "list"])
        self.assertFalse(database.exists())

    def test_publisher_contract_probe_requires_no_catalogue_or_database(self):
        root = Path(self.temp_dir.name)
        unused = root / "must-not-be-created.db"
        result = subprocess.run([sys.executable, str(Path(manage_version.__file__)), "--publisher-contract"],
            cwd=root, env={**os.environ, "VERSION_DB_PATH": str(unused)}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "lightinator-sqlite-v1")
        self.assertFalse(unused.exists())

    def test_live_devel_alias_keeps_its_legacy_webapp_upgrade_feed(self):
        root = Path(self.temp_dir.name) / "devel-assets"
        root.mkdir()
        (root / "index.html.gz").write_bytes(b"legacy")
        with api.get_db_connection() as connection:
            connection.execute("INSERT INTO webapp_builds (branch, version, min_firmware, file_path, basepath) VALUES ('devel', 'V5.0-341-devel', '5.0.0', ?, 'http://example.test/devel/')", (str(root),))
        self.assertEqual(api.get_latest_webapp("devel", "V5.0-340-devel")["version"], "V5.0-341-devel")
        self.assertEqual(api.release_identity("V5.0-340-devel", "devel")["stage"], "develop")

    def test_imported_unavailable_webapp_metadata_survives_projection_not_new_selection(self):
        source = Path(self.temp_dir.name) / "catalogue.json"
        entry = {"branch": "devel", "version": "V5.0-341-devel", "min_firmware": "5.0.0",
                 "files": [{"path": "index.html.gz", "md5": "a" * 32}]}
        source.write_text(json.dumps({"webapp": [entry]}))
        import_catalogue(source, source.parent)
        view = api.get_legacy_catalogue()["webapp"][0]
        self.assertEqual(view["version"], entry["version"])
        self.assertEqual(view["files"][0]["path"], "index.html.gz")
        self.assertEqual(view["files"][0]["md5"], "a" * 32)
        self.assertEqual(api.webapp_versions_v2("V5.0-340-devel", "develop"), [])

    def test_run_specific_publisher_bundle_uses_its_pinned_modules(self):
        root = Path(self.temp_dir.name)
        bundle = root / "publisher-123-1"
        bundle.mkdir()
        source = Path(api.__file__).parent
        script = bundle / "manage_version-123-1.py"
        shutil.copyfile(source / "manage_version.py", script)
        for name in ("version_api.py", "migrate_to_sqlite.py"):
            shutil.copyfile(source / name, bundle / name)
        shutil.copytree(source / "images", bundle / "images")
        asset = root / "download/firmware/develop/V5.0-42-develop/esp32/release/app.bin"
        asset.parent.mkdir(parents=True)
        asset.write_bytes(b"rsynced")
        result = subprocess.run([sys.executable, str(script), "version.json", "add", "esp32", "release",
            "develop", "V5.0-42-develop", "https://example.test/download/firmware/develop/V5.0-42-develop/esp32/release/app.bin"],
            cwd=root, env={**os.environ, "VERSION_DB_PATH": api.DB_PATH, "VERSION_ASSET_ROOT": str(root)},
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(api.firmware_latest_v2("esp32", "5.0", "develop", "release")["size"], 7)

    def test_release_manifest_cannot_publish_directly_to_prod(self):
        manifest = self.manifest(channel="5.0/prod")
        request = self.create_request(manifest)
        with self.assertRaises(HTTPException) as rejected:
            api.create_ota_release(request, None)
        self.assertEqual(rejected.exception.status_code, 422)

    def test_bad_manifest_signature_is_rejected(self):
        manifest = self.manifest()
        request = self.create_request(manifest, signature="00" * 64)
        with self.assertRaises(HTTPException) as rejected:
            api.create_ota_release(request, None)
        self.assertEqual(rejected.exception.status_code, 400)

    def test_legacy_upload_token_formats_remain_supported(self):
        self.assertIsNone(api.verify_token("ci-test-token"))
        self.assertIsNone(api.verify_token("Bearer ci-test-token"))
        with self.assertRaises(HTTPException) as rejected:
            api.verify_token("Bearer wrong-token")
        self.assertEqual(rejected.exception.status_code, 401)

    def test_http_routes_enforce_ci_admin_and_promotion_credentials(self):
        manifest_request = self.create_request(self.manifest())

        async def exercise_routes():
            transport = httpx.ASGITransport(app=api.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                products = await client.get("/api/ota/products")
                self.assertEqual(products.status_code, 200)
                self.assertEqual([item["product"] for item in products.json()], ["controller"])

                release = await client.post(
                    "/api/ota/releases",
                    json=manifest_request.model_dump(mode="json"),
                    headers={"Authorization": "Bearer wrong-ci-token"},
                )
                self.assertEqual(release.status_code, 404)

                registration = await client.post(
                    "/api/ota/products",
                    json={
                        "product_id": "wallpanel",
                        "display_name": "Wallpanel",
                        "publisher_repository": "pljakobs/lightinator-wallpanel",
                        "signing_public_key": self.public_key_hex,
                        "target_policy": {"1.0": ["esp32"]},
                    },
                    headers={"Authorization": "Bearer wrong-admin-token"},
                )
                self.assertEqual(registration.status_code, 405)

                promotion = await client.post(
                    f"/api/ota/releases/{manifest_request.manifest.release_id}/promote",
                    json={"channel": "5.0/testing"},
                    headers={"Authorization": "Bearer wrong-promotion-token"},
                )
                self.assertEqual(promotion.status_code, 404)

                accepted = await client.post(
                    "/api/ota/releases",
                    json=manifest_request.model_dump(mode="json"),
                    headers={"Authorization": "Bearer ci-test-token"},
                )
                self.assertEqual(accepted.status_code, 404, accepted.text)

        asyncio.run(exercise_routes())

    def test_product_registry_starts_with_controller_and_supports_admin_addition(self):
        self.assertEqual([item["product"] for item in api.list_ota_products()], ["controller"])
        registration = api.OTAProductRegistration(
            product_id="wallpanel",
            display_name="Lightinator Wallpanel",
            publisher_repository="pljakobs/lightinator-wallpanel",
            signing_public_key=self.public_key_hex,
            target_policy={"1.0": ["esp32"]},
        )
        api.verify_ota_admin_token("Bearer admin-test-token")
        result = api.register_ota_product(registration, None)
        self.assertEqual(result["product"], "wallpanel")
        self.assertEqual(
            [item["product"] for item in api.list_ota_products()],
            ["controller", "wallpanel"],
        )

    def test_public_schema_is_read_only_but_keeps_product_discovery(self):
        schema = api.app.openapi()
        self.assertIn("/api/ota/products", schema["paths"])
        self.assertIn("/api/products", schema["paths"])
        self.assertNotIn("/api/ota/latest", schema["paths"])
        self.assertNotIn("/api/ota/artifacts/{release_id}/{artifact_id}", schema["paths"])
        self.assertTrue(all(set(operations) <= {"get"} for operations in schema["paths"].values()))
        self.assertFalse(any(path.endswith("/upload") or path == "/api/ota/releases" for path in schema["paths"]))
        models = schema.get("components", {}).get("schemas", {})
        self.assertNotIn("OTAReleaseManifest", models)
        self.assertNotIn("OTAProductRegistration", models)
        parameters = schema["paths"]["/api/v2/webapp/versions"]["get"]["parameters"]
        self.assertFalse({"api_version", "capabilities"} & {parameter["name"] for parameter in parameters})
        with self.assertRaises(HTTPException) as unknown:
            api.firmware_options("esp32", product="unknown")
        self.assertEqual(unknown.exception.status_code, 404)

    def test_deferred_contract_requirements_are_not_silently_ignored(self):
        root = Path(self.temp_dir.name) / "contract-assets"
        root.mkdir()
        (root / "index.html").write_bytes(b"not qualified")
        with api.get_db_connection() as connection:
            connection.execute("INSERT INTO webapp_builds (branch, version, min_firmware, file_path, basepath, api_min) VALUES ('develop', 'V5.0.1', 'V5.0', ?, 'http://example.test/', '2.0.0')", (str(root),))
        self.assertEqual(api.webapp_versions_v2("V5.0-40-develop", "develop"), [])

    def test_explicit_product_discovery_does_not_mix_controller_records(self):
        api.register_ota_product(api.OTAProductRegistration(
            product_id="wallpanel", display_name="Wallpanel", publisher_repository="pljakobs/lightinator-wallpanel",
            signing_public_key=self.public_key_hex, target_policy={"1.0": ["esp32"]}), None)
        manifest = self.manifest(version="V1.0.0-1", channel="1.0/develop", soc="esp32")
        manifest.product = "wallpanel"
        manifest.source_repository = "pljakobs/lightinator-wallpanel"
        api.create_ota_release(self.create_request(manifest), None)
        asyncio.run(api.upload_ota_artifact(manifest.release_id, "controller-release", StreamRequest(b"controller!"), None))
        api.finalize_ota_release(manifest.release_id, None)
        self.assertEqual(api.firmware_options("esp32")["combinations"], [])
        result = api.firmware_latest_v2("esp32", "1.0", "develop", "release", product="wallpanel")
        self.assertEqual(result["product"], "wallpanel")
        self.assertEqual(api.get_legacy_catalogue()["firmware"], [])

    def test_legacy_catalog_remains_available(self):
        with api.get_db_connection() as conn:
            conn.execute(
                """
                INSERT INTO firmware_builds (soc, type, branch, version, file_path, url, comment)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                ("esp8266", "debug", "develop", "V5.0-1-develop", "/tmp/rom.bin", "https://example.test/rom.bin", ""),
            )
            conn.commit()
        entries = api.get_firmware_versions(soc="esp8266", branch="develop", type="debug")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].version, "V5.0-1-develop")

    def test_latest_uses_version_order_not_upload_time(self):
        with api.get_db_connection() as conn:
            for release_id, version in (("11111111-1111-4111-8111-111111111111", "V5.0.10-1"),
                                        ("22222222-2222-4222-8222-222222222222", "V5.0.2-99")):
                conn.execute(
                    """
                    INSERT INTO ota_releases
                        (release_id, product_id, version, origin_channel, source_repository,
                         source_commit, signing_public_key, signature_hex, changelog_json, manifest_json,
                         manifest_sha256, status)
                    VALUES (?, 'controller', ?, '5.0/develop', 'pljakobs/esp_rgbww_firmware',
                            ?, ?, '0' * 128, '[]', '{}', '0' * 64, 'published')
                    """,
                        (release_id, version, "a" * 40, self.public_key_hex),
                )
                conn.execute(
                    "INSERT INTO ota_release_channels (release_id, channel) VALUES (?, '5.0/develop')",
                    (release_id,),
                )
                conn.execute(
                    """
                    INSERT INTO ota_artifacts
                        (release_id, artifact_id, hardware, hardware_revision, soc, build_type,
                         role, filename, sha256, size, storage_path, uploaded)
                    VALUES (?, 'firmware', 'controller-board', 'rev1', 'esp32', 'release',
                            'firmware', 'app.bin', ?, 12, '/private/app.bin', 1)
                    """,
                    (release_id, "c" * 64),
                )
            conn.commit()

        latest = api.get_latest_ota_release(
            channel="5.0/develop",
            product="controller",
            hardware="controller-board",
            hardware_revision="rev1",
            soc="esp32",
            build_type="release",
        )
        self.assertEqual(latest["version"], "V5.0.10-1")