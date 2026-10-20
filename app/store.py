import json
import os
import secrets
import sqlite3
import time
from pathlib import Path

from argon2 import PasswordHasher
from cryptography.fernet import Fernet

from .configuration import validate_host, validate_port, validate_public_url, validate_username

PASSWORDS = PasswordHasher()


def data_dir():
    return Path(os.environ.get("TGCHECKIN_DATA", "data")).resolve()


def save_config(path, config):
    tmp = path / ("config." + secrets.token_hex(8) + ".tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(config, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path / "config.json")


def initialize(path, username, password, *, host="127.0.0.1", port=8080,
               secure_cookie=False, autostart=True, public_url=""):
    if not 12 <= len(password) <= 256:
        raise ValueError("管理员密码长度必须为 12–256 字符")
    username = validate_username(username)
    host, port = validate_host(host), validate_port(port)
    public_url = validate_public_url(public_url)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if (path / "config.json").exists():
        raise ValueError("已经初始化，请使用 reset-password 修改密码")
    key_path = path / "master.key"
    if not key_path.exists():
        fd = os.open(key_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(Fernet.generate_key())
    save_config(path, {
        "username": username,
        "password_hash": PASSWORDS.hash(password),
        "host": host,
        "port": port,
        "secure_cookie": bool(secure_cookie),
        "autostart": bool(autostart),
        "public_url": public_url,
    })
    Store(path).close()


class Store:
    def __init__(self, path):
        self.path = path
        self.crypto = Fernet((path / "master.key").read_bytes().strip())
        self.db = sqlite3.connect(path / "checkin.db", timeout=10, check_same_thread=False)
        os.chmod(path / "checkin.db", 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS accounts(
          id INTEGER PRIMARY KEY, name TEXT NOT NULL, uid INTEGER UNIQUE NOT NULL,
          secret TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
          cooldown REAL NOT NULL DEFAULT 0, created REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS job_groups(
          id INTEGER PRIMARY KEY, created REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS jobs(
          id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
          group_id INTEGER REFERENCES job_groups(id) ON DELETE CASCADE,
          name TEXT NOT NULL, target TEXT NOT NULL, message TEXT NOT NULL,
          cron TEXT NOT NULL, timezone TEXT NOT NULL, button TEXT NOT NULL DEFAULT '',
          success_text TEXT NOT NULL DEFAULT '', reply_timeout INTEGER NOT NULL DEFAULT 30,
          enabled INTEGER NOT NULL DEFAULT 1, next_run REAL NOT NULL, created REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS runs(
          id INTEGER PRIMARY KEY, job_id INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
          job_name TEXT NOT NULL, account_name TEXT NOT NULL, started REAL NOT NULL,
          finished REAL, status TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
          source TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS runs_started ON runs(started);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions(
          token TEXT PRIMARY KEY, csrf TEXT NOT NULL, expires REAL NOT NULL
        );
        INSERT OR IGNORE INTO settings VALUES('paused','0');
        """)
        # Existing installations keep their single-account tasks unchanged.
        if "group_id" not in {row["name"] for row in self.db.execute("PRAGMA table_info(jobs)")}:
            self.db.execute("ALTER TABLE jobs ADD COLUMN group_id INTEGER REFERENCES job_groups(id) ON DELETE CASCADE")
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(jobs)")}
        for name, default in (("retry_enabled", 0), ("send_timeout", 30), ("max_retries", 2)):
            if name not in columns:
                self.db.execute(f"ALTER TABLE jobs ADD COLUMN {name} INTEGER NOT NULL DEFAULT {default}")
        self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS jobs_group_account ON jobs(group_id,account_id)")
        self.db.commit()

    def config(self):
        return json.loads((self.path / "config.json").read_text())

    def execute(self, sql, params=()):
        cur = self.db.execute(sql, params)
        self.db.commit()
        return cur

    def all(self, sql, params=()):
        return [dict(row) for row in self.db.execute(sql, params).fetchall()]

    def one(self, sql, params=()):
        row = self.db.execute(sql, params).fetchone()
        return dict(row) if row else None

    def encrypt(self, value):
        return self.crypto.encrypt(json.dumps(value).encode()).decode()

    def decrypt(self, value):
        return json.loads(self.crypto.decrypt(value.encode()))

    def paused(self):
        return self.one("SELECT value FROM settings WHERE key='paused'")["value"] == "1"

    def cleanup(self):
        self.prune_groups()
        self.execute("DELETE FROM sessions WHERE expires < ?", (time.time(),))
        self.execute("DELETE FROM runs WHERE started < ? AND status NOT IN ('running','queued','awaiting_reply')",
                     (time.time() - 90 * 86400,))

    def prune_groups(self):
        self.execute("DELETE FROM job_groups WHERE NOT EXISTS (SELECT 1 FROM jobs WHERE jobs.group_id=job_groups.id)")

    def close(self):
        self.db.close()
