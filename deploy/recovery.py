#!/usr/bin/env python3
"""Private, host-local snapshots and recovery for Lightinator deployments."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile


SERVICES = ("nginx.service", "version-api.service", "fail2ban.service")


def digest(path):
    checksum = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


def rooted(root, path):
    return root / str(path).lstrip("/")


def database_path(root, repo):
    env = rooted(root, "/etc/lightinator/version-api.env")
    if env.exists():
        for line in env.read_text().splitlines():
            if line.strip().startswith("VERSION_DB_PATH="):
                values = shlex.split(line.split("=", 1)[1])
                if len(values) != 1:
                    raise ValueError("VERSION_DB_PATH must be a single path")
                path = Path(values[0])
                return (rooted(root, path) if path.is_absolute() else repo / path).resolve()
    return repo / "firmware.db"


def snapshot(root, repo, home):
    storage = rooted(root, "/var/lib/lightinator/backups")
    storage.mkdir(parents=True, exist_ok=True, mode=0o700)
    if storage.is_symlink() or storage.stat().st_mode & 0o077 or storage.stat().st_uid != os.geteuid():
        raise ValueError("Backup directory must be private (mode 700) and owned by the current user, not a symlink")
    if storage.is_relative_to(repo) or storage.is_relative_to(home / "nginx/html"):
        raise ValueError("Backups must be outside the checkout and public webroot")
    backup = Path(tempfile.mkdtemp(prefix="before-deploy-", dir=storage))
    try:
        db = database_path(root, repo)
        paths = [
            repo, home / "nginx/conf.d", home / "nginx/html/index.html",
            home / "nginx/html/manifest_debug.json", home / "nginx/html/manifest_release.json",
            home / "nginx/html/images", home / "nginx/html/favicon.png",
            rooted(root, "/etc/lightinator"), rooted(root, "/etc/fail2ban"),
            rooted(root, "/etc/systemd/system/version-api.service"),
            rooted(root, "/etc/containers/systemd/nginx.container"),
            rooted(root, "/etc/firewalld"),
            rooted(root, "/var/lib/lightinator/deploy"),
        ]
        venv = repo / ".venv"
        if venv.is_symlink():
            paths.append(venv.resolve(strict=True))
        states = {}
        for service in SERVICES:
            states[service] = {}
            for action in ("is-active", "is-enabled"):
                result = subprocess.run(["systemctl", action, service], capture_output=True, text=True)
                states[service][action] = result.stdout.strip()
        metadata = {
            "format": 1, "repo": str(repo), "database": str(db),
            "paths": [{"path": str(p), "present": p.exists() or p.is_symlink()} for p in paths],
            "database_present": db.exists(), "services": states,
            "system_root": str(root), "host": os.uname().nodename,
            "venv": str(venv.resolve()) if venv.exists() else None,
        }
        # Never archive live SQLite pages/WAL; use a coherent SQLite snapshot.
        excluded = {str(db), str(db) + "-wal", str(db) + "-shm"}

        def include(info):
            absolute = "/" + info.name.lstrip("/")
            if absolute in excluded or Path(absolute).name.startswith(".venv.pre-"):
                return None
            return info

        with tarfile.open(backup / "files.tar", "w") as archive:
            for entry in metadata["paths"]:
                if entry["present"]:
                    archive.add(entry["path"], arcname=entry["path"].lstrip("/"), filter=include)
        if db.exists():
            source = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)
            destination = sqlite3.connect(backup / "database.sqlite")
            try:
                source.backup(destination)
            finally:
                destination.close()
                source.close()
            shutil.copystat(db, backup / "database.sqlite")
            stat = db.stat()
            metadata["database_owner"] = [stat.st_uid, stat.st_gid]
        for option, name in (("--list-all-zones", "firewall-runtime.txt"),
                             ("--permanent", "firewall-permanent.txt")):
            args = ["firewall-cmd", option]
            if option == "--permanent":
                args.append("--list-all-zones")
            result = subprocess.run(args, capture_output=True, text=True)
            if result.returncode:
                raise RuntimeError("Cannot capture firewall state; backup aborted")
            (backup / name).write_text(result.stdout)
        shutil.copyfile(__file__, backup / "recovery.py")
        metadata["checksums"] = {p.name: digest(p) for p in backup.iterdir() if p.is_file()}
        (backup / "manifest.json").write_text(json.dumps(metadata, indent=2))
        (backup / "COMPLETE").touch()
        print("Backup completed: " + str(backup))
        print("Restore after pausing CI publishers: sudo python3.11 " + str(backup / "recovery.py")
              + " restore " + str(backup) + " --confirm-publishers-paused")
        print("Recovery excludes OS packages, published downloads and monitoring data; firewall recovery is manual.")
        return backup
    except BaseException:
        print("ERROR: Backup incomplete; no deployment should proceed. Inspect " + str(backup), file=sys.stderr)
        raise


def restore(backup):
    if not (backup / "COMPLETE").exists():
        raise ValueError("Refusing to restore an incomplete backup")
    if backup.is_symlink() or backup.stat().st_mode & 0o077 or backup.stat().st_uid != os.geteuid():
        raise ValueError("Backup directory must be private and owned by the restoring user")
    metadata = json.loads((backup / "manifest.json").read_text())
    if metadata["format"] != 1:
        raise ValueError("Unsupported backup format")
    if metadata["host"] != os.uname().nodename:
        raise ValueError("Restore must run on the original host")
    for name, checksum in metadata["checksums"].items():
        if Path(name).name != name or digest(backup / name) != checksum:
            raise ValueError("Backup integrity check failed")
    # Validate before stopping anything. Only restore trusted, root-private snapshots.
    with tarfile.open(backup / "files.tar") as archive:
        all_members = archive.getmembers()
        for member in all_members:
            if member.name.startswith("/") or ".." in Path(member.name).parts:
                raise ValueError("Invalid archive member")
        firewall = str(rooted(Path(metadata["system_root"]), "/etc/firewalld")).lstrip("/")
        members = [m for m in all_members if m.name != firewall and not m.name.startswith(firewall + "/")]
        for service in SERVICES:
            state = subprocess.run(["systemctl", "show", service, "--property=LoadState", "--value"],
                                   capture_output=True, text=True, check=True)
            if state.stdout.strip() != "not-found":
                subprocess.run(["systemctl", "stop", service], check=True)
        # Keep the failed deployment alongside the original paths, rather than
        # merging Python modules/dependencies or deleting a checkout.
        repo = Path(metadata["repo"])
        trees = [repo]
        if metadata["venv"] and not Path(metadata["venv"]).is_relative_to(repo):
            trees.append(Path(metadata["venv"]))
        for path in trees:
            if path.exists():
                retained = Path(tempfile.mkdtemp(prefix=path.name + ".before-restore-", dir=path.parent))
                retained.rmdir()
                path.rename(retained)
                print("Retained pre-restore tree: " + str(retained))
        # Retain today's files that were not in the snapshot; do not delete whole
        # trees, data, or Git worktrees. Known managed files absent before are removed.
        for entry in metadata["paths"]:
            path = Path(entry["path"])
            if not entry["present"] and (path.is_file() or path.is_symlink()):
                path.unlink()
        for member in members:
            target = Path("/") / member.name
            if target.is_symlink() and not member.issym():
                target.unlink()
        # Trusted local archive: preserve owners/modes and original venv symlinks.
        archive.extractall("/", members=members, numeric_owner=True, filter="fully_trusted")
    db = Path(metadata["database"])
    for suffix in ("-wal", "-shm"):
        Path(str(db) + suffix).unlink(missing_ok=True)
    if metadata["database_present"]:
        db.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(backup / "database.sqlite", db)
        os.chown(db, *metadata["database_owner"])
    elif db.exists():
        db.unlink()
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    for service, state in metadata["services"].items():
        enabled = state["is-enabled"]
        if enabled in ("enabled", "disabled"):
            subprocess.run(["systemctl", "enable" if enabled == "enabled" else "disable", service], check=True)
        if state["is-active"] == "active":
            subprocess.run(["systemctl", "start", service], check=True)
    print("Files, database and service activation restored. Verify HTTP/API/SSH and fail2ban.")
    print("Firewall was NOT restored or reloaded; its saved state/configuration require manual reconciliation.")
    print("OS package changes and additional files outside the replaced code/environment trees are not undone.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    create = sub.add_parser("backup")
    create.add_argument("--repo", type=Path, required=True)
    create.add_argument("--home", type=Path, required=True)
    create.add_argument("--system-root", type=Path, default=Path("/"))
    recover = sub.add_parser("restore")
    recover.add_argument("backup", type=Path)
    recover.add_argument("--confirm-publishers-paused", action="store_true", required=True,
                         help="Confirm CI/SSH publishing is paused before restoring SQLite and code")
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("Must run as root")
    os.umask(0o077)
    if args.action == "backup":
        snapshot(args.system_root.resolve(), args.repo.resolve(), args.home.resolve())
    else:
        restore(args.backup.resolve())


if __name__ == "__main__":
    main()
