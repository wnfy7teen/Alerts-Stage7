"""Verified private backup of SQLite, config and user media; secrets are excluded."""
from __future__ import annotations
import hashlib, json, os, shutil, sqlite3, sys, tarfile, tempfile, time
from contextlib import closing
from pathlib import Path
from dotenv import load_dotenv

BASE = Path(__file__).resolve().parent.parent
load_dotenv(BASE / ".env")
KEEP = max(1, int(os.getenv("BACKUP_KEEP", "14")))
STATUS_PATH = Path(os.getenv("BACKUP_STATUS_PATH") or BASE / "backup-status.json")

def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

def write_status(data: dict) -> None:
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATUS_PATH.with_name(STATUS_PATH.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, STATUS_PATH)

def verify_archive(path: Path) -> dict:
    with tempfile.TemporaryDirectory(prefix="donation-backup-verify-") as tmp:
        root = Path(tmp)
        with tarfile.open(path, "r:gz") as archive:
            members = archive.getmembers()
            if not {"manifest.json", "donations.db"} <= {m.name for m in members}:
                raise RuntimeError("backup_archive_incomplete")
            for member in members:
                name = Path(member.name)
                if name.is_absolute() or ".." in name.parts or not (member.isfile() or member.isdir()):
                    raise RuntimeError("backup_archive_unsafe_member")
            for member in members:
                destination = root / member.name
                if member.isdir():
                    destination.mkdir(parents=True, exist_ok=True)
                else:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    source = archive.extractfile(member)
                    if source is None:
                        raise RuntimeError("backup_archive_member_unreadable")
                    with source, destination.open("wb") as output:
                        shutil.copyfileobj(source, output)
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        for relative, expected in manifest["sha256"].items():
            file = root / relative
            if not file.is_file() or sha256(file) != expected:
                raise RuntimeError("backup_checksum_mismatch")
        with closing(sqlite3.connect(root / "donations.db")) as db:
            integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError("backup_integrity_failed")
        return {"integrity": integrity, "file_count": len(manifest["sha256"])}

def main() -> None:
    started = time.time()
    previous_success = None
    try:
        previous_success = json.loads(STATUS_PATH.read_text()).get("last_success_at")
    except (OSError, ValueError, TypeError):
        pass
    try:
        db_path = Path(os.getenv("DB_PATH") or BASE / "donations.db")
        config_path = Path(os.getenv("CONFIG_PATH") or BASE / "config.json")
        media_path = Path(os.getenv("MEDIA_DIR") or BASE / "media")
        output = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(os.getenv("BACKUP_DIR") or BASE / "backups")
        output.mkdir(parents=True, exist_ok=True)
        os.chmod(output, 0o700)
        if not db_path.is_file():
            raise FileNotFoundError("database_missing")
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = output / f"donation-alerts-{stamp}.tar.gz"
        with tempfile.TemporaryDirectory(prefix="donation-backup-", dir=output) as tmp:
            stage = Path(tmp)
            db_copy = stage / "donations.db"
            with closing(sqlite3.connect(db_path)) as source, closing(sqlite3.connect(db_copy)) as dest:
                source.backup(dest)
                dest.commit()
            with closing(sqlite3.connect(db_copy)) as db:
                integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise RuntimeError("database_integrity_failed")
            checksums = {"donations.db": sha256(db_copy)}
            if config_path.is_file():
                shutil.copy2(config_path, stage / "config.json")
                checksums["config.json"] = sha256(stage / "config.json")
            if media_path.is_dir():
                shutil.copytree(media_path, stage / "media", symlinks=False)
                for file in sorted((stage / "media").rglob("*")):
                    if file.is_file():
                        checksums[file.relative_to(stage).as_posix()] = sha256(file)
            manifest = {"format": 1, "created_at": time.time(), "database_integrity": integrity,
                        "sha256": checksums, "secrets_included": False}
            (stage / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            tmp_archive = output / f".{target.name}.tmp"
            with tarfile.open(tmp_archive, "w:gz") as archive:
                for file in sorted(stage.rglob("*")):
                    archive.add(file, arcname=file.relative_to(stage).as_posix(), recursive=False)
            verify_archive(tmp_archive)
            os.chmod(tmp_archive, 0o600)
            os.replace(tmp_archive, target)
        # Retention only after a new archive has passed verification.
        archives = sorted(output.glob("donation-alerts-*.tar.gz"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in archives[KEEP:]:
            old.unlink()
        finished = time.time()
        write_status({"status":"success","last_attempt_at":started,"last_success_at":finished,
                      "backup_file":target.name,"size_bytes":target.stat().st_size,"sha256":sha256(target),
                      "db_integrity":integrity,"config_included":config_path.is_file(),
                      "media_included":media_path.is_dir(),"file_count":len(checksums),"retention_count":KEEP})
        print(f"Backup verified: {target}")
    except Exception as exc:
        write_status({"status":"failed","last_attempt_at":started,"last_success_at":previous_success,
                      "error":type(exc).__name__})
        raise

if __name__ == "__main__":
    main()
