import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from telethon.errors import FloodWaitError

from app.main import create_app
from app.models import Job, next_time
from app.runner import Runner
from app.store import Store, initialize
from app.telegram import TelegramService

PASSWORD = "test-only-password-123"


class FakeTelegram(TelegramService):
    calls = 0
    error = None

    async def send(self, account, job):
        self.calls += 1
        if self.error:
            raise self.error
        await asyncio.sleep(0.05)
        return "confirmed", "模拟回复匹配"

    async def dialogs(self, account):
        return [{"id": "-1001234", "name": "模拟群组"}]


@pytest.fixture
def client(tmp_path):
    initialize(tmp_path, "admin", PASSWORD)
    app = create_app(tmp_path, service_factory=FakeTelegram, scheduler=False)
    with TestClient(app) as c:
        yield c


def login(client):
    response = client.post("/api/login", json={"username":"admin","password":PASSWORD})
    assert response.status_code == 200
    client.headers["X-CSRF-Token"] = response.json()["csrf"]
    return response


def seed(client):
    db = client.app.state.store
    secret = {"phone":"+8613800000000","api_id":12345,"api_hash":"a"*32,"session":"fake-sensitive-session"}
    account_id = db.execute("INSERT INTO accounts(name,uid,secret,created) VALUES(?,?,?,?)",
                            ("测试帐号", 123, db.encrypt(secret), time.time())).lastrowid
    data = Job(account_id=account_id, name="每日签到", target="@example_bot", message="/checkin").model_dump()
    response = client.post("/api/jobs", json=data)
    assert response.status_code == 200
    return account_id, response.json()["id"], data


def test_auth_csrf_origin_and_cookie(client):
    assert client.get("/api/jobs").status_code == 401
    response = login(client)
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert client.get("/api/me").status_code == 200
    csrf = client.headers.pop("x-csrf-token")
    assert client.post("/api/pause", json={"paused":True}).status_code == 403
    client.headers["X-CSRF-Token"] = csrf
    assert client.post("/api/pause", json={"paused":True}, headers={"Origin":"https://attacker.invalid"}).status_code == 403
    assert client.post("/api/pause", json={"paused":True}).status_code == 200
    assert client.get("/api/overview").json()["paused"]
    assert client.post("/api/logout").status_code == 200
    assert client.get("/api/me").status_code == 401


def test_parallel_dashboard_refresh_keeps_authenticated_session(client):
    login(client)
    paths = ["/api/overview", "/api/accounts", "/api/jobs"]
    with ThreadPoolExecutor(max_workers=12) as pool:
        statuses = list(pool.map(lambda i: client.get(paths[i % 3]).status_code, range(180)))
    assert statuses == [200] * 180


def test_password_rotation_revokes_all_sessions(client):
    login(client)
    r = client.post("/api/password",json={"current_password":PASSWORD,"new_password":"new-secure-password-123"})
    assert r.status_code == 200
    assert client.get("/api/me").status_code == 401
    assert client.post("/api/login",json={"username":"admin","password":PASSWORD}).status_code == 401
    assert client.post("/api/login",json={"username":"admin","password":"new-secure-password-123"}).status_code == 200


def test_login_rate_limit_and_no_validation_secret_echo(client):
    for _ in range(8):
        assert client.post("/api/login",json={"username":"admin","password":"wrong"}).status_code == 401
    assert client.post("/api/login",json={"username":"admin","password":PASSWORD}).status_code == 429
    secret = "private"*100
    response = client.post("/api/login",json={"username":"admin","password":secret})
    assert response.status_code == 422
    assert secret not in response.text


def test_encryption_crud_validation_and_cascade(client):
    login(client)
    account_id, job_id, data = seed(client)
    raw = client.app.state.store.one("SELECT secret FROM accounts")["secret"]
    assert "fake-sensitive-session" not in raw and "13800000000" not in raw
    response = client.get("/api/accounts").json()
    assert "secret" not in response[0] and "13800000000" not in json.dumps(response)
    assert client.get(f"/api/accounts/{account_id}/dialogs").json()[0]["id"] == "-1001234"
    assert client.post("/api/jobs",json={**data,"cron":"invalid"}).status_code == 422
    assert client.post("/api/jobs",json={**data,"timezone":"not/a/zone"}).status_code == 422
    assert client.post("/api/jobs",json={**data,"account_id":9999}).status_code == 404
    assert client.put(f"/api/jobs/{job_id}",json={**data,"message":"签到"}).status_code == 200
    assert client.get("/api/jobs").json()[0]["message"] == "签到"
    client.patch(f"/api/jobs/{job_id}",json={"enabled":False})
    assert client.post(f"/api/jobs/{job_id}/run").status_code == 409
    client.delete(f"/api/accounts/{account_id}")
    assert client.get("/api/jobs").json() == []


def test_manual_execution_no_duplicate_and_result(client):
    login(client)
    _, job_id, _ = seed(client)
    assert client.post(f"/api/jobs/{job_id}/run").status_code == 200
    assert client.post(f"/api/jobs/{job_id}/run").status_code in (409,429)
    assert client.delete(f"/api/jobs/{job_id}").status_code == 409
    for _ in range(50):
        run = client.get("/api/runs").json()[0]
        if run["status"] != "running":
            break
        time.sleep(.1)
    assert run["status"] == "confirmed"
    assert client.app.state.telegram.calls == 1


def test_floodwait_persists_account_cooldown(client):
    login(client)
    account_id, job_id, _ = seed(client)
    client.app.state.telegram.error = FloodWaitError(request=None, capture=60)
    assert client.post(f"/api/jobs/{job_id}/run").status_code == 200
    for _ in range(50):
        run = client.get("/api/runs").json()[0]
        if run["status"] != "running":
            break
        time.sleep(.1)
    assert run["status"] == "failed"
    account = client.app.state.store.one("SELECT * FROM accounts WHERE id=?", (account_id,))
    assert account["cooldown"] > time.time()+50
    assert client.post(f"/api/jobs/{job_id}/run").status_code == 429


def test_scheduler_pause_misfire_and_claim(client):
    login(client)
    _, job_id, _ = seed(client)
    db = client.app.state.store
    now = time.time()
    db.execute("UPDATE jobs SET next_run=? WHERE id=?", (now-120, job_id))
    client.portal.call(client.app.state.runner.tick, now)
    assert db.one("SELECT next_run FROM jobs")["next_run"] > now
    assert db.all("SELECT * FROM runs") == []
    db.execute("UPDATE jobs SET next_run=? WHERE id=?", (now-1, job_id))
    db.execute("UPDATE settings SET value='1' WHERE key='paused'")
    client.portal.call(client.app.state.runner.tick, now)
    assert db.all("SELECT * FROM runs") == []
    db.execute("UPDATE settings SET value='0' WHERE key='paused'")
    db.execute("UPDATE jobs SET next_run=? WHERE id=?", (now-1, job_id))
    client.portal.call(client.app.state.runner.tick, now)
    client.portal.call(client.app.state.runner.tick, now)
    assert len(db.all("SELECT * FROM runs")) == 1


def test_cron_timezone():
    from datetime import datetime, timezone
    now = datetime(2026,10,20,0,0,tzinfo=timezone.utc).timestamp()
    value = next_time("0 9 * * *", "Asia/Shanghai", now)
    assert datetime.fromtimestamp(value,timezone.utc).isoformat() == "2026-10-20T01:00:00+00:00"
    value = next_time("0 9 * * mon", "Asia/Shanghai", now)
    assert datetime.fromtimestamp(value,timezone.utc).isoformat() == "2026-10-26T01:00:00+00:00"


def test_restart_marks_inflight_unknown(tmp_path):
    initialize(tmp_path,"admin",PASSWORD)
    db = Store(tmp_path)
    db.execute("INSERT INTO runs(job_name,account_name,started,status,source) VALUES(?,?,?,?,?)",
               ("interrupted","account",time.time(),"running","manual"))
    db.close()
    with TestClient(create_app(tmp_path,service_factory=FakeTelegram)) as c:
        login(c)
        result = c.get("/api/runs").json()[0]
        assert result["status"] == "unknown"
        assert "不会自动重发" in result["detail"]


def test_single_task_retry_config_round_trip_and_disable(client):
    login(client)
    _, job_id, data = seed(client)
    assert client.get("/api/jobs").json()[0]["retry_enabled"] == 0
    data.update(retry_enabled=True, send_timeout=45, max_retries=3)
    assert client.put(f"/api/jobs/{job_id}", json=data).status_code == 200
    saved = client.get("/api/jobs").json()[0]
    assert (saved["retry_enabled"], saved["send_timeout"], saved["max_retries"]) == (1, 45, 3)
    # A second connection proves persistence, rather than relying on in-memory state.
    db = Store(client.app.state.store.path)
    assert db.one("SELECT * FROM jobs WHERE id=?", (job_id,))["send_timeout"] == 45
    db.close()
    data["retry_enabled"] = False
    assert client.put(f"/api/jobs/{job_id}", json=data).status_code == 200
    assert client.get("/api/jobs").json()[0]["retry_enabled"] == 0


def test_backup_restores_encrypted_session(client,tmp_path,monkeypatch):
    from app.backup import backup
    import tarfile
    login(client)
    seed(client)
    monkeypatch.setenv("TGCHECKIN_DATA",str(client.app.state.store.path))
    archive = backup(tmp_path / "backups")
    restored = tmp_path / "restored"
    with tarfile.open(archive) as tar:
        tar.extractall(restored,filter="data")
    db = Store(restored)
    assert db.decrypt(db.one("SELECT secret FROM accounts")["secret"])["session"] == "fake-sensitive-session"
    assert db.one("SELECT name FROM jobs")["name"] == "每日签到"
    db.close()


def test_static_security_headers_and_no_data_routes(client):
    page = client.get("/")
    assert page.status_code == 200
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert "no-store" == page.headers["cache-control"]
    assert client.get("/data/master.key").status_code == 404
    assert client.get("/static/style.css").status_code == 200
