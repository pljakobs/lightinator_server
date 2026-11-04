import argparse
import os
import tempfile
from contextlib import nullcontext
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="Run an isolated loopback-only distribution server")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--data-dir", type=Path, help="Retain a marked local-test directory between runs")
    parser.add_argument("--catalogue", type=Path, help="Read-only legacy catalogue import; original URLs are preserved")
    parser.add_argument("--asset-root", type=Path, help="Local webroot for imported asset paths")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("Port must be between 1 and 65535")
    if args.catalogue and not args.catalogue.is_file():
        parser.error("Catalogue does not exist")
    storage = nullcontext(str(args.data_dir)) if args.data_dir else tempfile.TemporaryDirectory(prefix="lightinator-local-")
    with storage as directory:
        root = Path(directory).resolve()
        root.mkdir(parents=True, exist_ok=True)
        marker = root / ".lightinator-local-test"
        if any(root.iterdir()) and not marker.exists():
            parser.error("Refusing an existing directory not marked for local testing")
        marker.touch()
        uploads = root / "downloads"
        uploads.mkdir(exist_ok=True)
        base_url = f"http://127.0.0.1:{args.port}"
        os.environ.update({
            "VERSION_DB_PATH": str(root / "firmware.db"),
            "VERSION_HTML_ROOT": str(uploads),
            "OTA_STORAGE_ROOT": str(root / "ota-artifacts"),
            "VERSION_DOWNLOAD_BASE_URL": base_url,
            "ENABLE_PUBLISHING_API": "0",
            "VERSION_LEGACY_CATALOG": str(args.catalogue.resolve()) if args.catalogue else "",
            "VERSION_LEGACY_ASSET_ROOT": str(args.asset_root.resolve()) if args.asset_root else "",
        })
        for name in ("CI_UPLOAD_TOKEN", "OTA_ADMIN_TOKEN", "OTA_PROMOTION_TOKEN", "RELEASE_PUBLIC_KEY_HEX"):
            os.environ.pop(name, None)

        import uvicorn
        from fastapi.staticfiles import StaticFiles
        import version_api as api

        api.init_db()
        api.sync_legacy_catalogue()
        api.app.mount("/downloads", StaticFiles(directory=uploads), name="local-uploaded-assets")
        print(f"Local distribution API: {base_url}/api/docs", flush=True)
        print(f"Isolated test storage: {root}", flush=True)
        print("Publishing credentials are unset. Imported download URLs are not rewritten; no firmware is flashed.", flush=True)
        uvicorn.run(api.app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()