import asyncio
import hashlib
import tempfile
import unittest
import uuid
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import HTTPException
import httpx

import version_api as api


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
                self.assertEqual(release.status_code, 401)

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
                self.assertEqual(registration.status_code, 401)

                promotion = await client.post(
                    f"/api/ota/releases/{manifest_request.manifest.release_id}/promote",
                    json={"channel": "5.0/testing"},
                    headers={"Authorization": "Bearer wrong-promotion-token"},
                )
                self.assertEqual(promotion.status_code, 401)

                accepted = await client.post(
                    "/api/ota/releases",
                    json=manifest_request.model_dump(mode="json"),
                    headers={"Authorization": "Bearer ci-test-token"},
                )
                self.assertEqual(accepted.status_code, 201, accepted.text)

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