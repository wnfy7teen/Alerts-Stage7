"""
Резервная копия базы донатов и настроек.

    python scripts/backup.py              # копия в ./backups, хранится 14 последних
    python scripts/backup.py D:\\backups   # своя папка

Копия делается встроенным механизмом SQLite, поэтому она целая, даже если сервер в этот момент пишет в базу.
Cron (Linux), каждый день в 04:00:   0 4 * * *  cd /opt/donation-alerts && .venv/bin/python scripts/backup.py
Планировщик заданий (Windows): действие «python scripts\\backup.py», запуск раз в день.
В копии есть имена и сообщения зрителей: храните её в закрытом месте.
"""
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
KEEP = int(os.getenv("BACKUP_KEEP", "14"))


def main() -> None:
    db = Path(os.getenv("DB_PATH") or BASE / "donations.db")
    cfg = Path(os.getenv("CONFIG_PATH") or BASE / "config.json")
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else BASE / "backups"
    out.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")

    if not db.exists():
        raise SystemExit(f"База не найдена: {db}")
    target = out / f"donations-{stamp}.db"
    src, dst = sqlite3.connect(db), sqlite3.connect(target)
    with dst:
        src.backup(dst)
    src.close(); dst.close()
    if sqlite3.connect(target).execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        target.unlink(missing_ok=True)
        raise SystemExit("Копия не прошла проверку целостности")
    if cfg.exists():
        shutil.copy2(cfg, out / f"config-{stamp}.json")

    for pattern in ("donations-*.db", "config-*.json"):  # удаляем старые копии
        for old in sorted(out.glob(pattern))[:-KEEP]:
            old.unlink()
    print(f"Готово: {target}")


if __name__ == "__main__":
    main()
