from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REMOTE_BACKUP = ROOT / "scripts" / "remote_backup.sh"
RESTORE_DRILL = ROOT / "scripts" / "restore_drill.py"


def _valid_archive(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    database = source / "donations.db"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE donations (id TEXT PRIMARY KEY)")
        db.execute("INSERT INTO donations VALUES ('d1')")
    digest = hashlib.sha256(database.read_bytes()).hexdigest()
    (source / "manifest.json").write_text(
        json.dumps({"format": 1, "sha256": {"donations.db": digest}}),
        encoding="utf-8",
    )
    archive_path = tmp_path / "donation-alerts-test.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        archive.add(database, arcname="donations.db")
        archive.add(source / "manifest.json", arcname="manifest.json")
    return archive_path


def _run_remote_backup(tmp_path: Path, archive: Path, *, extra_env=None):
    project = tmp_path / "project"
    scripts = project / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(RESTORE_DRILL, scripts / "restore_drill.py")

    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(exist_ok=True)
    local_archive = backup_dir / archive.name
    if archive != local_archive:
        shutil.copy2(archive, local_archive)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    rclone = fake_bin / "rclone"
    rclone.write_text(
        "#!/usr/bin/env bash\n"
        "set -Eeuo pipefail\n"
        'printf "%s\\n" "$*" >> "$RCLONE_CALL_LOG"\n'
        'if [[ "$1" == "copyto" && "${FAIL_COPYTO:-0}" == "1" ]]; then exit 17; fi\n'
        'if [[ "$1" == "cryptcheck" && "${FAIL_CRYPTCHECK:-0}" == "1" ]]; then exit 19; fi\n',
        encoding="utf-8",
    )
    rclone.chmod(0o755)
    config = tmp_path / "rclone.conf"
    config.write_text("[fake]\ntype = local\n", encoding="utf-8")
    call_log = tmp_path / "rclone-calls.log"
    env = os.environ.copy()
    env.update({
        "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
        "BACKUP_DIR": str(backup_dir),
        "PROJECT_PATH": str(project),
        "BACKUP_MAX_AGE_SECONDS": "172800",
        "REMOTE_PATH": "encrypted-test:donation-alerts",
        "RCLONE_CONFIG": str(config),
        "LOCK_FILE": str(tmp_path / "remote-backup.lock"),
        "RCLONE_CALL_LOG": str(call_log),
    })
    if extra_env:
        env.update(extra_env)
    result = subprocess.run(
        ["bash", str(REMOTE_BACKUP)],
        env=env, capture_output=True, text=True, timeout=30,
    )
    calls = call_log.read_text(encoding="utf-8").splitlines() if call_log.exists() else []
    return result, calls


def test_remote_backup_verifies_restore_before_upload_and_cryptcheck(tmp_path):
    archive = _valid_archive(tmp_path)
    result, calls = _run_remote_backup(tmp_path, archive)

    assert result.returncode == 0, result.stderr
    assert len(calls) == 2
    assert calls[0].startswith("copyto ")
    assert calls[1].startswith("cryptcheck ")
    assert "remote_backup=verified" in result.stdout


def test_remote_backup_rejects_stale_archive_without_upload(tmp_path):
    archive = _valid_archive(tmp_path)
    old_time = archive.stat().st_mtime - 3600
    os.utime(archive, (old_time, old_time))
    result, calls = _run_remote_backup(
        tmp_path, archive, extra_env={"BACKUP_MAX_AGE_SECONDS": "1"},
    )

    assert result.returncode != 0
    assert "stale" in result.stderr
    assert calls == []


def test_remote_backup_stops_when_upload_fails(tmp_path):
    archive = _valid_archive(tmp_path)
    result, calls = _run_remote_backup(
        tmp_path, archive, extra_env={"FAIL_COPYTO": "1"},
    )

    assert result.returncode != 0
    assert len(calls) == 1
    assert calls[0].startswith("copyto ")
    assert "remote_backup=verified" not in result.stdout


def test_remote_backup_fails_when_remote_cryptcheck_fails(tmp_path):
    archive = _valid_archive(tmp_path)
    result, calls = _run_remote_backup(
        tmp_path, archive, extra_env={"FAIL_CRYPTCHECK": "1"},
    )

    assert result.returncode != 0
    assert len(calls) == 2
    assert calls[0].startswith("copyto ")
    assert calls[1].startswith("cryptcheck ")
    assert "remote_backup=verified" not in result.stdout


def test_remote_backup_rejects_invalid_archive_before_upload(tmp_path):
    archive = tmp_path / "invalid.tar.gz"
    with tarfile.open(archive, "w:gz"):
        pass
    result, calls = _run_remote_backup(tmp_path, archive)

    assert result.returncode != 0
    assert calls == []
