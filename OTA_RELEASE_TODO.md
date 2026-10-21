# OTA Release TODO

## CI Publishing

- [ ] Migrate firmware CI from SSH/rsync to the product-aware OTA API.
- [ ] Generate the canonical signed manifest and commit-derived changelog in CI.
- [ ] Upload and finalize all declared artifacts, then promote releases through the authorized channel workflow.
- [ ] Exercise retry behavior for interrupted uploads and failed finalization without publishing partial releases.

## Device Verification

- [ ] Pin the trusted Ed25519 release public key in firmware; do not trust a key supplied only by the HTTP API.
- [ ] Verify the returned manifest digest and signature before using release metadata.
- [ ] Verify each downloaded artifact against the SHA-256 and size covered by the signed manifest before installation.
- [ ] Test HTTP OTA downloads and verification on ESP8266, plus ESP32 and ESP32-C3 targets.

## Legacy Compatibility

- [ ] Add a deterministic SQLite-to-`version.json` projection for legacy consumers that still require the file.
- [ ] Keep SQLite authoritative and test that projection generation does not modify the OTA release catalogue.
- [ ] Verify existing firmware and webapp discovery clients during a deployment with both old and new publishers.

## Production Rollout

- [ ] Provision CI, admin, promotion, and release-signing credentials on the server using the protected environment file.
- [ ] Document signing-key backup and rotation, including firmware trust-anchor updates.
- [ ] Validate the deployed nginx configuration and API routes in the server environment.
- [ ] Run an end-to-end staged release, publication, promotion, discovery, and download test before enabling CI migration.
- [ ] Keep `controller` as the only enabled product until a separate product is explicitly registered with its own policy and signing key.
