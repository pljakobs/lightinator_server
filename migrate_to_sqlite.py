import argparse
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit

import version_api as api


def import_catalogue(source: Path, asset_root: Path) -> dict[str, int]:
    data = json.loads(source.read_text())
    root = asset_root.resolve()

    def local_path(url: str) -> str:
        candidate = (root / unquote(urlsplit(url).path).lstrip("/")).resolve()
        if not candidate.is_relative_to(root):
            raise ValueError("Catalogue URL escapes the asset root")
        return str(candidate)

    counts = {"firmware": 0, "webapp": 0}
    with api.get_db_connection() as connection:
        for entry in data.get("firmware", []) + data.get("history", []):
            version = entry.get("fw_version") or entry.get("version")
            url = entry.get("files", {}).get("rom", {}).get("url")
            if not all(entry.get(key) for key in ("soc", "type", "branch")) or not version or not url:
                continue
            cursor = connection.execute("""INSERT INTO firmware_builds
                (soc, type, branch, version, file_path, url, comment) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(soc, branch, type, version) DO NOTHING""",
                (entry["soc"].lower(), entry["type"].lower(), entry["branch"].lower(), version,
                 local_path(url), url, entry.get("comment") or ""))
            counts["firmware"] += cursor.rowcount
        for entry in data.get("webapp", []) + data.get("webapp_history", []):
            if not entry.get("branch") or not entry.get("version"):
                continue
            branch, version = entry["branch"].lower(), entry["version"]
            basepath = entry.get("basepath") or f"{api.DOWNLOAD_BASE_URL}/download/webui/{branch}/{version}/"
            cursor = connection.execute("""INSERT INTO webapp_builds
                (branch, version, type, min_firmware, file_path, basepath, comment, files_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(branch, version) DO UPDATE SET
                    files_json = COALESCE(webapp_builds.files_json, excluded.files_json)""",
                (branch, version, entry.get("type") or "debug", entry.get("min_firmware") or "",
                 local_path(basepath), basepath, entry.get("comment") or "", json.dumps(entry.get("files") or [])))
            counts["webapp"] += cursor.rowcount
    return counts


def main():
    parser = argparse.ArgumentParser(description="Import legacy catalogue records without replacing existing releases")
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--db", required=True)
    parser.add_argument("--asset-root", type=Path)
    parser.add_argument("--backup", action="store_true", help="Back up an existing database before schema migration")
    args = parser.parse_args()
    api.DB_PATH = args.db
    database = Path(args.db)
    if args.backup and database.exists():
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = database.with_name(f"{database.name}.pre-m1-{timestamp}.bak")
        with closing(sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True)) as source:
            with closing(sqlite3.connect(backup)) as destination:
                source.backup(destination)
        print(f"Database backup: {backup}")
    api.init_db()
    print(json.dumps(import_catalogue(args.source, args.asset_root or args.source.parent), sort_keys=True))


if __name__ == "__main__":
    main()