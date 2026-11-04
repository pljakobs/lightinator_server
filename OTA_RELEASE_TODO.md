# OTA Release TODO

## Active M1 Boundary

Retain product-aware read-only discovery, SQLite/legacy JSON views and the secured
SSH/rsync plus database-writing `manage_version.py` publishing process. HTTP
publishing, release lifecycle APIs and explicit firmware API-contract management
are later work. Retained branch implementations are not M1 deployment requirements.
Production now registers GET routes only, without upload/registration schemas.

## CI Publishing

- [x] Retain secured SSH/rsync publishing and adapt `manage_version.py` to write SQLite directly with a compatible CI command interface.
- [x] Test CI-compatible script commands in a separate process without reading or writing `version.json`.
- [x] Retain per-run uploads sourced from an immutable, tested server-repository revision and require the SQLite publisher contract.
- [ ] Publish the reviewed publisher commit and set `DISTRIBUTION_PUBLISHER_SHA` in both CI repositories before enabling the changed workflows.
- [x] Disable HTTP publishing by default; local discovery remains available.
- HTTP publishing migration and its security hardening are deferred, not prerequisites for this rollout.
- [ ] Generate the canonical signed manifest and commit-derived changelog in CI.
- [ ] Upload and finalize all declared artifacts, then promote releases through the authorized channel workflow.
- [ ] Exercise retry behavior for interrupted uploads and failed finalization without publishing partial releases.

## Device Verification

- [ ] Pin the trusted Ed25519 release public key in firmware; do not trust a key supplied only by the HTTP API.
- [ ] Verify the returned manifest digest and signature before using release metadata.
- [ ] Verify each downloaded artifact against the SHA-256 and size covered by the signed manifest before installation.
- [ ] Test HTTP OTA downloads and verification on ESP8266, plus ESP32 and ESP32-C3 targets.

## Legacy Compatibility

- [x] Add a deterministic SQLite-to-`version.json` projection for legacy consumers that still require the file.
- [x] Keep SQLite authoritative and test that projection generation does not modify the OTA release catalogue.
- [x] Import old publisher records without replacing existing IDs, qualification metadata, or the input catalogue.
- [x] Verify frontend and HA discovery clients against an isolated loopback server with HTTP publishing disabled.
- [ ] Verify existing firmware and webapp discovery clients during a deployment with both old and new publishers.

## Production Rollout

- [x] Run 32 server tests locally and under production Python.
- [x] Deploy and externally verify read-only product discovery with retained ownership and all catalogue records.
- [ ] Qualify the adapted script publisher and deployed nginx/download routes before a production upgrade.
- HTTP publishing credentials and signing/promotion API rollout remain deferred.
- [ ] Document signing-key backup and rotation, including firmware trust-anchor updates.
- [x] Validate the deployed nginx configuration and API routes in the server environment.
- [ ] Run an end-to-end staged release, publication, promotion, discovery, and download test before enabling CI migration.
- [ ] Keep `controller` as the only enabled product until a separate product is explicitly registered with its own policy and signing key.
