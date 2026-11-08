#!/usr/bin/env python3
"""Safely restore a Donation Alerts archive into a new, isolated directory."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import tarfile
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: restore_drill.py ARCHIVE.tar.gz NEW_EMPTY_DESTINATION", file=sys.stderr)
        return 2
    archive_path = Path(sys.argv[1]).resolve()
    destination = Path(sys.argv[2]).resolve()
    if not archive_path.is_file():
        raise SystemExit("archive_missing")
    if destination.exists():
        raise SystemExit("destination_must_not_exist")
    destination.mkdir(parents=True, mode=0o700)
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            members = archive.getmembers()
            names = set()
            for member in members:
                name = Path(member.name)
                if name.is_absolute() or ".." in name.parts or not (member.isfile() or member.isdir()):
                    raise RuntimeError("unsafe_archive_member")
                if member.name in names:
                    raise RuntimeError("duplicate_archive_member")
                names.add(member.name)
            if not {"manifest.json", "donations.db"} <= names:
                raise RuntimeError("archive_incomplete")
            archive.extractall(destination, members=members, filter="data")

        manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
        for relative, expected in manifest["sha256"].items():
            rel = Path(relative)
            if rel.is_absolute() or ".." in rel.parts:
                raise RuntimeError("unsafe_manifest_path")
            item = destination / rel
            if not item.is_file() or sha256(item) != expected:
                raise RuntimeError(f"checksum_mismatch:{relative}")
        with sqlite3.connect(destination / "donations.db") as db:
            result = db.execute("PRAGMA integrity_check").fetchone()[0]
            if result != "ok":
                raise RuntimeError("sqlite_integrity_failed")
            count = db.execute("SELECT COUNT(*) FROM donations").fetchone()[0]
        config = destination / "config.json"
        if config.exists():
            json.loads(config.read_text(encoding="utf-8"))
        media_count = sum(1 for p in (destination / "media").rglob("*") if p.is_file()) if (destination / "media").exists() else 0
        print(json.dumps({
            "result": "restore_drill_ok",
            "destination": str(destination),
            "sqlite_integrity": result,
            "donation_rows": count,
            "media_files": media_count,
            "config_valid": config.exists(),
            "manifest_files_verified": len(manifest["sha256"]),
        }, ensure_ascii=False))
        return 0
    except Exception:
        # Keep failed drill output for diagnosis; never touch production paths.
        raise


if __name__ == "__main__":
    raise SystemExit(main())
