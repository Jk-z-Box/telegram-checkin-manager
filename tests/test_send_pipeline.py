import asyncio

import pytest

from app.runner import Runner
from test_group_queue import GatedTelegram, entered, setup


class PhasedTelegram(GatedTelegram):
    def __init__(self, store):
        super().__init__(store)
        self.replies = {i: asyncio.Event() for i in range(1, 5)}
        self.acked = {i: asyncio.Event() for i in range(1, 5)}

    async def send(self, account, job, *, on_sent=None):
        ident = job["id"]
        self.calls.append(ident)
        self.entered[ident].set()
        await self.release[ident].wait()
        if on_sent:
            on_sent("发送尝试 1 次；")
        self.acked[ident].set()
        await self.replies[ident].wait()
        outcome = self.outcomes.get(ident, ("confirmed", "mock reply"))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.mark.parametrize("source", ["manual", "schedule"])
def test_next_job_sends_while_same_account_previous_job_waits_for_reply(setup, source):
    async def run():
        # A collection member and an independent task on the same account.
        setup.execute("UPDATE jobs SET enabled=0 WHERE id IN (2,3)")
        setup.execute("UPDATE jobs SET group_id=NULL,account_id=1 WHERE id=4")
        setup.execute("UPDATE jobs SET success_text='ok'")
        service = PhasedTelegram(setup)
        runner = Runner(setup, service)
        if source == "manual":
            for ident in (1, 4):
                runner.launch(setup.one("SELECT * FROM jobs WHERE id=?", (ident,)), source)
        else:
            await runner.tick(now=100)
        await entered(service, 1)
        assert not service.entered[4].is_set()
        service.release[1].set()
        await entered(service, 4)  # No reply was released for job 1.
        assert not service.replies[1].is_set() and not runner.tasks[1].done()
        assert setup.one("SELECT status FROM runs WHERE job_id=1")["status"] == "awaiting_reply"
        service.release[4].set()
        await asyncio.wait_for(service.acked[4].wait(), 1)
        assert setup.one("SELECT status FROM runs WHERE job_id=4")["status"] == "awaiting_reply"
        assert runner.launch(setup.one("SELECT * FROM jobs WHERE id=1"), "manual") is None
        # Replies may complete in a different order from sends.
        service.replies[4].set()
        await asyncio.wait_for(asyncio.shield(runner.tasks[4]), 1)
        assert not runner.tasks[1].done()
        service.replies[1].set()
        await asyncio.gather(*list(runner.tasks.values()))
        assert service.calls == [1, 4]
        assert all(row["status"] == "confirmed" for row in setup.all("SELECT status FROM runs"))
        await runner.close()
    asyncio.run(run())


def test_reply_timeout_does_not_hold_queue_or_resend(setup):
    async def run():
        setup.execute("UPDATE jobs SET success_text='ok'")
        service = PhasedTelegram(setup)
        service.outcomes[1] = ("reply_timeout", "消息已发送，但等待回复超时")
        runner = Runner(setup, service)
        for ident in (1, 2, 3):
            runner.launch(setup.one("SELECT * FROM jobs WHERE id=?", (ident,)), "manual")
            service.release[ident].set()
        await entered(service, 3)
        assert service.calls == [1, 2, 3]
        assert all(not event.is_set() for event in service.replies.values())
        for event in service.replies.values():
            event.set()
        await asyncio.gather(*list(runner.tasks.values()))
        assert [r["status"] for r in setup.all("SELECT status FROM runs ORDER BY id")] == [
            "reply_timeout", "confirmed", "confirmed"]
        await runner.close()
    asyncio.run(run())


def test_stop_after_send_acknowledgement_preserves_delivery_knowledge(setup):
    async def run():
        setup.execute("UPDATE jobs SET success_text='ok'")
        service = PhasedTelegram(setup)
        runner = Runner(setup, service)
        runner.launch(setup.one("SELECT * FROM jobs WHERE id=1"), "manual")
        await entered(service, 1)
        service.release[1].set()
        await asyncio.wait_for(service.acked[1].wait(), 1)
        await runner.close()
        result = setup.one("SELECT * FROM runs")
        assert result["status"] == "unconfirmed" and "消息已发送" in result["detail"]
        assert not runner.send_lock.locked() and not service.lock(1).locked()
    asyncio.run(run())


def test_unexpected_reply_exception_is_not_reported_as_unknown_send(setup):
    async def run():
        setup.execute("UPDATE jobs SET success_text='ok'")
        service = PhasedTelegram(setup)
        service.outcomes[1] = ConnectionError("mock reply disconnect")
        service.release[1].set()
        service.replies[1].set()
        runner = Runner(setup, service)
        runner.launch(setup.one("SELECT * FROM jobs WHERE id=1"), "manual")
        await asyncio.gather(*list(runner.tasks.values()))
        result = setup.one("SELECT * FROM runs")
        assert result["status"] == "unconfirmed" and "消息已发送" in result["detail"]
        await runner.close()
    asyncio.run(run())
