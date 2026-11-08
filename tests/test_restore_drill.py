from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DRILL = ROOT / "scripts" / "restore_drill.py"


def _archive(tmp_path: Path, *, extra_members=(), corrupt_hash=False) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    database = source / "donations.db"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE donations (id TEXT PRIMARY KEY, amount REAL)")
        db.execute("INSERT INTO donations VALUES ('d1', 25.0)")
    config = source / "config.json"
    config.write_text('{"theme":"blue"}', encoding="utf-8")
    media = source / "media"
    media.mkdir()
    (media / "alert.png").write_bytes(b"media-data")

    checksums = {}
    for relative in ("donations.db", "config.json", "media/alert.png"):
        checksums[relative] = hashlib.sha256((source / relative).read_bytes()).hexdigest()
    if corrupt_hash:
        checksums["donations.db"] = "0" * 64
    (source / "manifest.json").write_text(
        json.dumps({"format": 1, "sha256": checksums}), encoding="utf-8"
    )

    archive_path = tmp_path / "backup.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        for path in sorted(source.rglob("*")):
            # Avoid tarfile.add() recursively adding a directory's children;
            # each member is deliberately written exactly once.
            archive.add(
                path,
                arcname=path.relative_to(source).as_posix(),
                recursive=False,
            )
        for name, payload in extra_members:
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return archive_path


def test_restore_drill_verifies_archive_in_new_isolated_directory(tmp_path):
    archive = _archive(tmp_path)
    destination = tmp_path / "isolated-restore"

    result = subprocess.run(
        [sys.executable, str(DRILL), str(archive), str(destination)],
        check=True, capture_output=True, text=True,
    )

    report = json.loads(result.stdout)
    assert report["result"] == "restore_drill_ok"
    assert report["sqlite_integrity"] == "ok"
    assert report["donation_rows"] == 1
    assert report["media_files"] == 1
    assert report["config_valid"] is True
    assert (destination / "media" / "alert.png").read_bytes() == b"media-data"


def test_restore_drill_rejects_existing_destination_without_modifying_it(tmp_path):
    archive = _archive(tmp_path)
    destination = tmp_path / "existing"
    destination.mkdir()
    marker = destination / "keep.txt"
    marker.write_text("untouched", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(DRILL), str(archive), str(destination)],
        capture_output=True, text=True,
    )

    assert result.returncode != 0
    assert marker.read_text(encoding="utf-8") == "untouched"


def test_restore_drill_rejects_path_traversal_before_extraction(tmp_path):
    archive = _archive(tmp_path, extra_members=[("../outside.txt", b"nope")])
    destination = tmp_path / "isolated-restore"

    result = subprocess.run(
        [sys.executable, str(DRILL), str(archive), str(destination)],
        capture_output=True, text=True,
    )

    assert result.returncode != 0
    assert not (tmp_path / "outside.txt").exists()


def test_restore_drill_rejects_duplicate_archive_members(tmp_path):
    archive = _archive(tmp_path, extra_members=[("donations.db", b"duplicate")])
    destination = tmp_path / "isolated-restore"

    result = subprocess.run(
        [sys.executable, str(DRILL), str(archive), str(destination)],
        capture_output=True, text=True,
    )

    assert result.returncode != 0


def test_restore_drill_rejects_checksum_mismatch(tmp_path):
    archive = _archive(tmp_path, corrupt_hash=True)
    destination = tmp_path / "isolated-restore"

    result = subprocess.run(
        [sys.executable, str(DRILL), str(archive), str(destination)],
        capture_output=True, text=True,
    )

    assert result.returncode != 0
    assert "checksum_mismatch" in result.stderr
