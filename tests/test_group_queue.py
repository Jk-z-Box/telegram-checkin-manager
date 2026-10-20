import asyncio
import time

import pytest
from telethon.errors import FloodWaitError

from app.models import Job
from app.runner import Runner
from app.store import Store, initialize
from app.telegram import TelegramService


class GatedTelegram(TelegramService):
    def __init__(self, store):
        super().__init__(store)
        self.entered = {i: asyncio.Event() for i in range(1, 5)}
        self.release = {i: asyncio.Event() for i in range(1, 5)}
        self.outcomes = {}
        self.calls = []
        self.starts = {}
        self.finishes = {}
        self.concurrent = 0
        self.peak = 0

    async def send(self, account, job):
        ident = job["id"]
        self.concurrent += 1
        self.peak = max(self.peak, self.concurrent)
        self.calls.append(ident)
        self.starts[ident] = time.monotonic()
        self.entered[ident].set()
        try:
            await self.release[ident].wait()
            outcome = self.outcomes.get(ident, ("confirmed", "mock reply"))
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        finally:
            self.finishes[ident] = time.monotonic()
            self.concurrent -= 1


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr("app.runner.ACCOUNT_GAP", .02)
    initialize(tmp_path, "admin", "queue-test-password")
    db = Store(tmp_path)
    db.execute("INSERT INTO job_groups(id,created) VALUES(1,0),(2,0)")
    for ident in range(1, 5):
        db.execute("INSERT INTO accounts(id,name,uid,secret,created) VALUES(?,?,?,?,?)",
                   (ident, f"Mock {ident}", ident, "unused", 0))
        data = Job(account_id=ident, name="Mock", target="@example_bot", message="/checkin").model_dump()
        data.update(id=ident, group_id=1 if ident <= 3 else 2, next_run=100, created=0)
        db.execute(f"INSERT INTO jobs({','.join(data)}) VALUES({','.join('?' for _ in data)})", tuple(data.values()))
    yield db
    db.close()


def launch_group(runner, db):
    for job in db.all("SELECT * FROM jobs WHERE group_id=1 ORDER BY id"):
        runner.launch(job, "manual")


async def entered(service, ident):
    await asyncio.wait_for(service.entered[ident].wait(), 1)


@pytest.mark.parametrize("source", ["manual", "schedule"])
def test_collection_serializes_members_with_gap_for_both_launch_paths(setup, source):
    async def run():
        db = setup
        db.execute("UPDATE jobs SET enabled=0 WHERE id=4")
        service = GatedTelegram(db)
        runner = Runner(db, service)
        if source == "manual":
            launch_group(runner, db)
        else:
            await runner.tick(now=100)
        await entered(service, 1)
        assert service.calls == [1]
        assert [row["status"] for row in db.all("SELECT status FROM runs ORDER BY id")] == ["running", "queued", "queued"]
        # Queued work is already claimed, preventing duplicate manual/scheduled launches.
        assert runner.launch(db.one("SELECT * FROM jobs WHERE id=2"), "manual") is None
        for ident in (1, 2, 3):
            await entered(service, ident)
            service.release[ident].set()
        await asyncio.wait_for(asyncio.gather(*list(runner.tasks.values())), 2)
        assert service.calls == [1, 2, 3] and service.peak == 1
        assert service.starts[2] - service.finishes[1] >= .019
        assert service.starts[3] - service.finishes[2] >= .019
        assert all(row["status"] == "confirmed" for row in db.all("SELECT status FROM runs"))
        await runner.close()
    asyncio.run(run())


@pytest.mark.parametrize("change", ["global", "account", "job", "delete"])
def test_waiting_members_recheck_pause_and_deletion_before_sending(setup, change):
    async def run():
        db = setup
        service = GatedTelegram(db)
        runner = Runner(db, service)
        for ident in (1, 2):
            runner.launch(db.one("SELECT * FROM jobs WHERE id=?", (ident,)), "manual")
        await entered(service, 1)
        if change == "global":
            db.execute("UPDATE settings SET value='1' WHERE key='paused'")
        elif change == "account":
            db.execute("UPDATE accounts SET enabled=0 WHERE id=2")
        elif change == "job":
            db.execute("UPDATE jobs SET enabled=0 WHERE id=2")
        else:
            db.execute("DELETE FROM accounts WHERE id=2")
        service.release[1].set()
        await asyncio.wait_for(asyncio.gather(*list(runner.tasks.values())), 2)
        assert service.calls == [1]
        assert db.all("SELECT status FROM runs ORDER BY id")[1]["status"] == "skipped"
        await runner.close()
    asyncio.run(run())


@pytest.mark.parametrize("outcome", [
    ConnectionError("mock transport failure"),
    FloodWaitError(request=None, capture=60),
    ("unconfirmed", "mock reply timeout"),
])
def test_one_member_failure_does_not_stop_remaining_members(setup, outcome):
    async def run():
        db = setup
        service = GatedTelegram(db)
        service.outcomes[1] = outcome
        for event in service.release.values():
            event.set()
        runner = Runner(db, service)
        launch_group(runner, db)
        await asyncio.wait_for(asyncio.gather(*list(runner.tasks.values())), 2)
        assert service.calls == [1, 2, 3] and service.peak == 1
        assert [r["status"] for r in db.all("SELECT status FROM runs ORDER BY id")][1:] == ["confirmed", "confirmed"]
        if isinstance(outcome, FloodWaitError):
            assert db.one("SELECT cooldown FROM accounts WHERE id=1")["cooldown"] > time.time() + 50
        await runner.close()
    asyncio.run(run())


def test_unrelated_collections_can_progress_independently(setup):
    async def run():
        service = GatedTelegram(setup)
        runner = Runner(setup, service)
        for ident in (1, 4):
            runner.launch(setup.one("SELECT * FROM jobs WHERE id=?", (ident,)), "manual")
        await entered(service, 1)
        await entered(service, 4)
        assert service.peak == 2
        for event in service.release.values():
            event.set()
        await asyncio.gather(*list(runner.tasks.values()))
        await runner.close()
    asyncio.run(run())


def test_queue_time_does_not_consume_member_execution_timeout(setup, monkeypatch):
    monkeypatch.setattr("app.runner.execution_timeout", lambda job: 1 if job["id"] == 1 else .025)
    async def run():
        service = GatedTelegram(setup)
        runner = Runner(setup, service)
        for ident in (1, 2):
            runner.launch(setup.one("SELECT * FROM jobs WHERE id=?", (ident,)), "manual")
        await entered(service, 1)
        await asyncio.sleep(.05)  # Longer than member 2's entire execution budget.
        assert not service.entered[2].is_set()
        service.release[2].set()
        service.release[1].set()
        await asyncio.wait_for(asyncio.gather(*list(runner.tasks.values())), 2)
        assert all(r["status"] == "confirmed" for r in setup.all("SELECT status FROM runs"))
        await runner.close()
    asyncio.run(run())


def test_shutdown_distinguishes_queued_from_inflight_and_releases_locks(setup):
    async def run():
        service = GatedTelegram(setup)
        runner = Runner(setup, service)
        launch_group(runner, setup)
        await entered(service, 1)
        await asyncio.wait_for(runner.close(), 2)
        assert service.calls == [1]
        assert [r["status"] for r in setup.all("SELECT status FROM runs ORDER BY id")] == ["unknown", "skipped", "skipped"]
        assert not runner.group_locks[1].locked() and not service.lock(1).locked()
    asyncio.run(run())


def test_restart_does_not_mark_unsent_queue_as_delivery_unknown(setup):
    async def run():
        for status in ("queued", "running"):
            setup.execute("INSERT INTO runs(job_name,account_name,started,status,source) VALUES(?,?,?,?,?)",
                          ("mock", "mock", time.time(), status, "manual"))
        setup.execute("UPDATE jobs SET enabled=0")
        runner = Runner(setup, GatedTelegram(setup))
        runner.start()
        assert [r["status"] for r in setup.all("SELECT status FROM runs ORDER BY id")] == ["skipped", "unknown"]
        await runner.close()
    asyncio.run(run())
