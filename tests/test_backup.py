import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tarfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_backup_contains_database_config_and_media_and_restores_isolated(tmp_path):
    data = tmp_path / "data"
    media = data / "media"
    media.mkdir(parents=True)
    database = data / "donations.db"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE donations(id TEXT PRIMARY KEY, amount REAL)")
        db.execute("INSERT INTO donations VALUES('donation-1', 125.0)")
    config = data / "config.json"
    config.write_text('{"theme":"blue"}', encoding="utf-8")
    (media / "alert.png").write_bytes(b"test-media")

    backup_dir = data / "backups"
    status = data / "backup-status.json"
    env = os.environ.copy()
    env.update({
        "DB_PATH": str(database),
        "CONFIG_PATH": str(config),
        "MEDIA_DIR": str(media),
        "BACKUP_STATUS_PATH": str(status),
        "BACKUP_KEEP": "14",
    })
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "backup.py"), str(backup_dir)],
        check=True, env=env, capture_output=True, text=True,
    )

    archives = list(backup_dir.glob("donation-alerts-*.tar.gz"))
    assert len(archives) == 1
    assert status.exists()
    state = json.loads(status.read_text(encoding="utf-8"))
    assert state["status"] == "success"
    assert state["db_integrity"] == "ok"
    assert state["media_included"] is True
    assert status.stat().st_mode & 0o777 == 0o600
    with tarfile.open(archives[0], "r:gz") as archive:
        assert {"donations.db", "config.json", "media/alert.png", "manifest.json"} <= set(archive.getnames())

    restore_target = data / "restore-test"
    restore = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "restore_backup.py"), str(archives[0]),
         "--target", str(restore_target), "--apply"],
        check=True, capture_output=True, text=True,
    )
    assert "isolated directory" in restore.stdout
    assert restore_target.is_dir()
    assert (restore_target / "media" / "alert.png").read_bytes() == b"test-media"
    with sqlite3.connect(restore_target / "donations.db") as db:
        assert db.execute("SELECT amount FROM donations WHERE id='donation-1'").fetchone()[0] == 125.0


def test_restore_dry_run_does_not_create_target(tmp_path):
    archive = tmp_path / "invalid.tar.gz"
    with tarfile.open(archive, "w:gz"):
        pass
    target = tmp_path / "restore-dry-run"
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "restore_backup.py"), str(archive),
         "--target", str(target)],
        capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert not target.exists()



def test_restore_backup_rejects_duplicate_archive_members(tmp_path):
    database = tmp_path / "donations.db"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE donations(id TEXT PRIMARY KEY)")
    database_bytes = database.read_bytes()
    manifest = json.dumps({
        "sha256": {"donations.db": hashlib.sha256(database_bytes).hexdigest()}
    }).encode("utf-8")
    archive_path = tmp_path / "duplicate.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        manifest_info = tarfile.TarInfo("manifest.json")
        manifest_info.size = len(manifest)
        archive.addfile(manifest_info, io.BytesIO(manifest))
        for _ in range(2):
            db_info = tarfile.TarInfo("donations.db")
            db_info.size = len(database_bytes)
            archive.addfile(db_info, io.BytesIO(database_bytes))

    target = tmp_path / "restore-target"
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "restore_backup.py"), str(archive_path),
         "--target", str(target)],
        capture_output=True, text=True,
    )

    assert result.returncode != 0
    assert "Duplicate archive member" in result.stderr
    assert not target.exists()
