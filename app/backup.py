"""Consistent SQLite + encryption key/config backup. Archive is confidential."""
import os
import shutil
import sqlite3
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .store import data_dir


def backup(destination):
    os.umask(0o077)
    path = data_dir()
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(dir=destination) as temp:
        temp = Path(temp)
        with sqlite3.connect(path / "checkin.db") as source, sqlite3.connect(temp / "checkin.db") as target:
            source.backup(target)
        for filename in ("master.key", "config.json"):
            shutil.copyfile(path / filename, temp / filename)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        archive = destination / f"tgcheckin-{stamp}.tar.gz"
        with tarfile.open(archive, "x:gz") as tar:
            for filename in ("checkin.db", "master.key", "config.json"):
                tar.add(temp / filename, arcname=filename)
    return archive


if __name__ == "__main__":
    print("备份完成（包含登录凭据，请私密保存）:", backup(sys.argv[1]))
