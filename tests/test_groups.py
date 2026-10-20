import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.store import Store, initialize
from app.telegram import TelegramService


PASSWORD = "test-only-group-password"


class FakeTelegram(TelegramService):
    async def send(self, account, job):
        return "confirmed", "模拟签到成功"


@pytest.fixture
def client(tmp_path):
    initialize(tmp_path, "admin", PASSWORD)
    app = create_app(tmp_path, service_factory=FakeTelegram, scheduler=False)
    with TestClient(app) as browser:
        login = browser.post("/api/login", json={"username": "admin", "password": PASSWORD})
        browser.headers["X-CSRF-Token"] = login.json()["csrf"]
        db = app.state.store
        for number in range(1, 5):
            db.execute("INSERT INTO accounts(id,name,uid,secret,created) VALUES(?,?,?,?,?)",
                       (number, f"测试帐号 {number}", number,
                        db.encrypt({"phone": "+12025550123"}), time.time()))
        yield browser


def payload(**updates):
    return {"account_ids": [1, 2, 3], "name": "每日签到集合", "target": "@example_bot",
            "message": "/checkin", "cron": "0 9 * * *", "timezone": "Asia/Shanghai",
            "button": "签到", "success_text": "签到成功", "reply_timeout": 20, **updates}


def create(client, **updates):
    result = client.post("/api/jobs/batch", json=payload(**updates))
    assert result.status_code == 200, result.text
    return result.json()


def test_collection_create_and_counts_and_single_tasks(client):
    result = create(client)
    jobs = client.get("/api/jobs").json()
    assert result["count"] == 3 and len(set(result["ids"])) == 3
    assert {j["group_id"] for j in jobs} == {result["group_id"]}
    assert {j["account_id"] for j in jobs} == {1, 2, 3}
    assert all(j["target"] == "@example_bot" and j["button"] == "签到" for j in jobs)
    assert len({j["next_run"] for j in jobs}) == 1
    assert client.get("/api/overview").json()["jobs"] == 1
    single = payload(account_id=1)
    single.pop("account_ids")
    assert client.post("/api/jobs", json=single).status_code == 200
    assert client.get("/api/overview").json()["jobs"] == 2
    assert client.get("/api/runs").json() == []  # Creating a group does not send messages.


@pytest.mark.parametrize("updates,status", [
    ({"account_ids": []}, 422), ({"account_ids": [1, 1]}, 422),
    ({"account_ids": [1, 999]}, 404), ({"account_ids": [True]}, 422),
    ({"account_ids": list(range(1, 102))}, 422), ({"cron": "invalid"}, 422),
])
def test_invalid_batch_never_creates_partial_collection(client, updates, status):
    assert client.post("/api/jobs/batch", json=payload(**updates)).status_code == status
    assert client.app.state.store.all("SELECT * FROM jobs") == []
    assert client.app.state.store.all("SELECT * FROM job_groups") == []


def test_group_insert_failure_rolls_back_every_member_and_metadata(client):
    db = client.app.state.store
    db.execute("""CREATE TEMP TRIGGER fail_second BEFORE INSERT ON jobs
                  WHEN NEW.account_id=2 BEGIN SELECT RAISE(ABORT,'test failure'); END""")
    response = client.post("/api/jobs/batch", json=payload())
    assert response.status_code == 503
    assert db.all("SELECT * FROM jobs") == [] and db.all("SELECT * FROM job_groups") == []


def test_group_edit_members_preserves_retained_ids_and_history(client):
    result = create(client)
    db = client.app.state.store
    before = {j["account_id"]: j for j in db.all("SELECT * FROM jobs")}
    for j in before.values():
        db.execute("INSERT INTO runs(job_id,job_name,account_name,started,status,source) VALUES(?,?,?,?,?,?)",
                   (j["id"], j["name"], "旧帐号名", time.time(), "confirmed", "manual"))
    response = client.put(f"/api/job-groups/{result['group_id']}",
                          json=payload(account_ids=[2, 3, 4], message="/sign", name="修改后的集合", enabled=False))
    assert response.status_code == 200
    after = {j["account_id"]: j for j in db.all("SELECT * FROM jobs")}
    assert set(after) == {2, 3, 4}
    assert after[2]["id"] == before[2]["id"] and after[3]["id"] == before[3]["id"]
    assert all(j["message"] == "/sign" and not j["enabled"] for j in after.values())
    assert len(db.all("SELECT * FROM runs")) == 3
    assert db.one("SELECT COUNT(*) n FROM runs WHERE job_id IS NULL")["n"] == 1
    assert client.get("/api/overview").json()["jobs"] == 0
    # A retained member cannot diverge from the collection's shared definition.
    single = payload(account_id=2)
    single.pop("account_ids")
    assert client.put(f"/api/jobs/{after[2]['id']}", json=single).status_code == 409


def test_failed_edit_restores_original_members_and_configuration(client):
    result = create(client, account_ids=[1, 2])
    db = client.app.state.store
    before = db.all("SELECT * FROM jobs ORDER BY id")
    db.execute("""CREATE TEMP TRIGGER fail_addition BEFORE INSERT ON jobs
                  WHEN NEW.account_id=4 BEGIN SELECT RAISE(ABORT,'test failure'); END""")
    response = client.put(f"/api/job-groups/{result['group_id']}",
                          json=payload(account_ids=[2, 4], name="不应保存"))
    assert response.status_code == 503
    assert db.all("SELECT * FROM jobs ORDER BY id") == before


def test_group_toggle_delete_and_csrf(client):
    result = create(client)
    path = f"/api/job-groups/{result['group_id']}"
    csrf = client.headers.pop("x-csrf-token")
    assert client.post("/api/jobs/batch", json=payload()).status_code == 403
    assert client.patch(path, json={"enabled": False}).status_code == 403
    assert client.delete(path).status_code == 403
    client.headers["X-CSRF-Token"] = csrf
    assert client.patch(path, json={"enabled": False}).status_code == 200
    assert all(not j["enabled"] for j in client.get("/api/jobs").json())
    assert client.patch(path, json={"enabled": True}).status_code == 200
    assert all(j["enabled"] for j in client.get("/api/jobs").json())
    assert client.delete(path).status_code == 200
    assert client.get("/api/jobs").json() == []
    assert client.app.state.store.all("SELECT * FROM job_groups") == []


def test_group_run_honors_disabled_accounts_cooldowns_and_no_duplicates(client):
    result = create(client)
    db = client.app.state.store
    db.execute("UPDATE accounts SET enabled=0 WHERE id=2")
    db.execute("UPDATE accounts SET cooldown=? WHERE id=3", (time.time()+120,))
    path = f"/api/job-groups/{result['group_id']}"
    run = client.post(path+"/run", json={}).json()
    assert run["started"] == 1
    assert [r["status"] for r in run["results"]] == ["started", "skipped", "skipped"]
    assert "冷却" in run["results"][2]["detail"]
    assert client.post(path+"/run", json={}).json()["started"] == 0
    assert len(client.get("/api/runs").json()) == 1
    assert client.put(path, json=payload()).status_code == 409
    assert client.delete(path).status_code == 409


def test_deleting_account_removes_member_and_last_member_removes_group(client):
    group = create(client, account_ids=[1, 2])
    assert client.delete("/api/accounts/1").status_code == 200
    jobs = client.get("/api/jobs").json()
    assert len(jobs) == 1 and jobs[0]["group_id"] == group["group_id"]
    assert client.delete("/api/accounts/2").status_code == 200
    assert client.app.state.store.all("SELECT * FROM job_groups") == []


def test_pre_group_database_migrates_without_changing_existing_tasks(tmp_path):
    initialize(tmp_path, "admin", PASSWORD)
    raw = sqlite3.connect(tmp_path / "checkin.db")
    raw.executescript("DROP INDEX jobs_group_account; ALTER TABLE jobs DROP COLUMN group_id; DROP TABLE job_groups;")
    for column in ("retry_enabled", "send_timeout", "max_retries"):
        raw.execute(f"ALTER TABLE jobs DROP COLUMN {column}")
    raw.execute("INSERT INTO accounts(id,name,uid,secret,created) VALUES(1,'legacy',1,'encrypted',0)")
    raw.execute("""INSERT INTO jobs(account_id,name,target,message,cron,timezone,next_run,created)
                   VALUES(1,'legacy job','@example_bot','/checkin','0 9 * * *','Asia/Shanghai',123,0)""")
    raw.commit()
    original = raw.execute("SELECT id,name,next_run FROM jobs").fetchone()
    raw.close()
    db = Store(tmp_path)
    try:
        migrated = db.one("SELECT * FROM jobs")
        assert (migrated["id"], migrated["name"], migrated["next_run"]) == original
        assert migrated["group_id"] is None
        assert (migrated["retry_enabled"], migrated["send_timeout"], migrated["max_retries"]) == (0, 30, 2)
        assert db.db.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        db.close()


def test_collection_retry_config_applies_to_retained_and_new_members(client):
    group = create(client, retry_enabled=True, send_timeout=25, max_retries=4)
    assert all((j["retry_enabled"], j["send_timeout"], j["max_retries"]) == (1, 25, 4)
               for j in client.get("/api/jobs").json())
    response = client.put(f"/api/job-groups/{group['group_id']}", json=payload(
        account_ids=[2, 3, 4], retry_enabled=True, send_timeout=40, max_retries=2))
    assert response.status_code == 200
    assert all((j["retry_enabled"], j["send_timeout"], j["max_retries"]) == (1, 40, 2)
               for j in client.get("/api/jobs").json())


@pytest.mark.parametrize("settings", [
    {"send_timeout": 4}, {"send_timeout": 301}, {"send_timeout": 5.5},
    {"max_retries": 0}, {"max_retries": 11}, {"max_retries": True},
])
def test_invalid_retry_settings_do_not_create_collection(client, settings):
    response = client.post("/api/jobs/batch", json=payload(retry_enabled=True, **settings))
    assert response.status_code == 422
    assert client.get("/api/jobs").json() == []
