import asyncio
import time

import pytest

from test_groups import client, payload


def single(client, account_id=3):
    data = payload(account_id=account_id, enabled=False, retry_enabled=True,
                   send_timeout=45, max_retries=4)
    data.pop("account_ids")
    response = client.post("/api/jobs", json=data)
    assert response.status_code == 200
    job_id = response.json()["id"]
    db = client.app.state.store
    db.execute(
        "INSERT INTO runs(job_id,job_name,account_name,started,status,source) VALUES(?,?,?,?,?,?)",
        (job_id, data["name"], f"测试帐号 {account_id}", time.time() - 60, "confirmed", "manual"))
    return job_id, data


def converted_data(data, accounts):
    return {**{key: value for key, value in data.items() if key != "account_id"},
            "account_ids": accounts, "name": "转换后的集合", "message": "/updated"}


def test_convert_preserves_original_member_history_and_shared_configuration(client):
    job_id, data = single(client)
    db = client.app.state.store
    before = db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    history = db.all("SELECT * FROM runs")
    definition = converted_data(data, [1, 3, 4])
    response = client.post(f"/api/jobs/{job_id}/convert-to-group", json=definition)
    assert response.status_code == 200
    result = response.json()
    members = client.get("/api/jobs").json()
    assert len(members) == result["count"] == 3
    assert {j["account_id"] for j in members} == {1, 3, 4}
    assert {j["group_id"] for j in members} == {result["group_id"]}
    retained = next(j for j in members if j["account_id"] == 3)
    assert retained["id"] == job_id and retained["created"] == before["created"]
    for member in members:
        assert all(member[key] == value for key, value in definition.items() if key != "account_ids")
    assert len({j["next_run"] for j in members}) == 1
    assert db.all("SELECT * FROM runs") == history  # No execution on conversion.
    client.patch(f"/api/job-groups/{result['group_id']}", json={"enabled": True})
    assert client.get("/api/overview").json()["jobs"] == 1


def test_conversion_excluding_original_account_keeps_its_history(client):
    job_id, data = single(client)
    response = client.post(f"/api/jobs/{job_id}/convert-to-group",
                           json=converted_data(data, [1, 2]))
    assert response.status_code == 200
    members = client.get("/api/jobs").json()
    assert {j["account_id"] for j in members} == {1, 2}
    assert all(j["id"] != job_id for j in members)
    history = client.get("/api/runs").json()
    assert len(history) == 1 and history[0]["account_name"] == "测试帐号 3"
    assert history[0]["job_id"] is None and history[0]["status"] == "confirmed"


@pytest.mark.parametrize("accounts,status", [
    ([], 422), ([3], 422), ([3, 3], 422), ([3, 999], 404),
    (list(range(1, 102)), 422),
])
def test_invalid_conversion_leaves_source_untouched(client, accounts, status):
    job_id, data = single(client)
    db = client.app.state.store
    before, history = db.all("SELECT * FROM jobs"), db.all("SELECT * FROM runs")
    response = client.post(f"/api/jobs/{job_id}/convert-to-group",
                           json=converted_data(data, accounts))
    assert response.status_code == status
    assert db.all("SELECT * FROM jobs") == before
    assert db.all("SELECT * FROM runs") == history
    assert db.all("SELECT * FROM job_groups") == []


@pytest.mark.parametrize("accounts", [[3, 4], [1, 4]])
def test_insert_failure_rolls_back_source_attachment_configuration_and_history(client, accounts):
    job_id, data = single(client)
    db = client.app.state.store
    before, history = db.all("SELECT * FROM jobs"), db.all("SELECT * FROM runs")
    db.execute("""CREATE TEMP TRIGGER fail_conversion BEFORE INSERT ON jobs
                  WHEN NEW.account_id=4 BEGIN SELECT RAISE(ABORT,'test-only failure'); END""")
    response = client.post(f"/api/jobs/{job_id}/convert-to-group",
                           json=converted_data(data, accounts))
    assert response.status_code == 503
    assert db.all("SELECT * FROM jobs") == before
    assert db.all("SELECT * FROM runs") == history
    assert db.all("SELECT * FROM job_groups") == []


def test_conversion_requires_csrf_and_cannot_duplicate_an_existing_collection(client):
    job_id, data = single(client)
    path, definition = f"/api/jobs/{job_id}/convert-to-group", converted_data(data, [3, 4])
    csrf = client.headers.pop("x-csrf-token")
    assert client.post(path, json=definition).status_code == 403
    assert client.app.state.store.all("SELECT * FROM job_groups") == []
    client.headers["X-CSRF-Token"] = csrf
    assert client.post(path, json=definition).status_code == 200
    before = client.get("/api/jobs").json()
    assert client.post(path, json=definition).status_code == 409
    assert client.get("/api/jobs").json() == before
    assert len(client.app.state.store.all("SELECT * FROM job_groups")) == 1


@pytest.mark.parametrize("phase", ["sending", "awaiting_reply", "queued"])
def test_conversion_is_blocked_until_active_execution_finishes(client, phase):
    job_id, data = single(client)

    async def hold(account, job, *, on_sent=None):
        if phase == "awaiting_reply":
            on_sent("mock；")
        await asyncio.Event().wait()

    client.app.state.telegram.send = hold
    client.patch(f"/api/jobs/{job_id}", json={"enabled": True})
    if phase == "queued":
        blocker = client.post("/api/jobs", json={**data, "account_id": 1, "enabled": True}).json()["id"]
        assert client.post(f"/api/jobs/{blocker}/run").status_code == 200
    assert client.post(f"/api/jobs/{job_id}/run").status_code == 200
    expected = {"sending": "running", "awaiting_reply": "awaiting_reply", "queued": "queued"}[phase]
    for _ in range(100):
        latest = next(r for r in client.get("/api/runs").json()
                      if r["job_id"] == job_id and r["source"] == "manual")
        if latest["status"] == expected:
            break
        time.sleep(.01)
    assert latest["status"] == expected
    response = client.post(f"/api/jobs/{job_id}/convert-to-group",
                           json=converted_data(data, [3, 4]))
    assert response.status_code == 409
    assert client.app.state.store.one("SELECT group_id FROM jobs WHERE id=?", (job_id,))["group_id"] is None
    assert client.app.state.store.all("SELECT * FROM job_groups") == []


def test_editing_only_one_account_still_saves_an_independent_task(client):
    job_id, data = single(client)
    response = client.put(f"/api/jobs/{job_id}", json={**data, "account_id": 2, "message": "/single"})
    assert response.status_code == 200
    saved = client.app.state.store.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    assert saved["group_id"] is None and saved["account_id"] == 2 and saved["message"] == "/single"
    assert len(client.get("/api/runs").json()) == 1
