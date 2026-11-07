"""
Резервная копия базы донатов и настроек.

    python scripts/backup.py              # копия в ./backups, хранится 14 последних
    python scripts/backup.py D:\\backups   # своя папка

Копия делается встроенным механизмом SQLite, поэтому она целая, даже если сервер в этот момент пишет в базу.
Cron (Linux), каждый день в 04:00:   0 4 * * *  cd /opt/donation-alerts && .venv/bin/python scripts/backup.py
Планировщик заданий (Windows): действие «python scripts\\backup.py», запуск раз в день.
В копии есть имена и сообщения зрителей: храните её в закрытом месте.
"""
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path

from dotenv import load_dotenv

BASE = Path(__file__).resolve().parent.parent
load_dotenv(BASE / ".env")
KEEP = max(1, int(os.getenv("BACKUP_KEEP", "14")))
STATUS_PATH = Path(os.getenv("BACKUP_STATUS_PATH") or BASE / "backup-status.json")


def _previous_success() -> float | None:
    try:
        data = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
        value = data.get("last_success_at")
        return float(value) if value is not None else None
    except (OSError, ValueError, TypeError):
        return None


def _write_status(data: dict) -> None:
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATUS_PATH.with_name(STATUS_PATH.name + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(temporary, 0o644)
    os.replace(temporary, STATUS_PATH)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    started = time.time()
    last_success = _previous_success()
    try:
        db_path = Path(os.getenv("DB_PATH") or BASE / "donations.db")
        cfg_path = Path(os.getenv("CONFIG_PATH") or BASE / "config.json")
        out = Path(sys.argv[1]) if len(sys.argv) > 1 else BASE / "backups"
        out.mkdir(parents=True, exist_ok=True)
        os.chmod(out, 0o700)
        stamp = time.strftime("%Y%m%d-%H%M%S")

        if not db_path.exists():
            raise FileNotFoundError("database_missing")
        target = out / f"donations-{stamp}.db"
        with closing(sqlite3.connect(db_path)) as source, closing(sqlite3.connect(target)) as destination:
            source.backup(destination)
            destination.commit()
        with closing(sqlite3.connect(target)) as check:
            integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            target.unlink(missing_ok=True)
            raise RuntimeError("backup_integrity_failed")
        os.chmod(target, 0o600)

        config_target = None
        if cfg_path.exists():
            config_target = out / f"config-{stamp}.json"
            shutil.copy2(cfg_path, config_target)
            os.chmod(config_target, 0o600)

        for pattern in ("donations-*.db", "config-*.json"):
            for old in sorted(out.glob(pattern))[:-KEEP]:
                old.unlink()

        finished = time.time()
        _write_status({
            "status": "success",
            "last_attempt_at": started,
            "last_success_at": finished,
            "backup_file": target.name,
            "size_bytes": target.stat().st_size,
            "sha256": _sha256(target),
            "db_integrity": integrity,
            "config_included": config_target is not None,
            "retention_count": KEEP,
        })
        print(f"Готово: {target}")
    except BaseException as exc:
        _write_status({
            "status": "failed",
            "last_attempt_at": started,
            "last_success_at": last_success,
            "error": type(exc).__name__,
        })
        raise


if __name__ == "__main__":
    main()
