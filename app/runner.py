import asyncio
import time

from telethon.errors import FloodWaitError

from .models import next_time
from .telegram import friendly_error


class Runner:
    def __init__(self, store, telegram):
        self.store, self.telegram = store, telegram
        self.tasks = {}
        self.loop_task = None
        self.stopping = False

    def start(self):
        self.store.execute(
            "UPDATE runs SET status='unknown',finished=?,detail=? WHERE status='running'",
            (time.time(), "服务曾中断，执行结果未知；请核实 Telegram，不会自动重发"),
        )
        self.loop_task = asyncio.create_task(self.loop())

    def launch(self, job, source):
        if job["id"] in self.tasks:
            return None
        account = self.store.one("SELECT * FROM accounts WHERE id=?", (job["account_id"],))
        if not account or not account["enabled"]:
            return None
        run_id = self.store.execute(
            """INSERT INTO runs(job_id,job_name,account_name,started,status,source)
            VALUES(?,?,?,?,?,?)""",
            (job["id"], job["name"], account["name"], time.time(), "running", source),
        ).lastrowid
        task = asyncio.create_task(self.execute(job, run_id, source))
        self.tasks[job["id"]] = task
        task.add_done_callback(lambda _: self.tasks.pop(job["id"], None))
        return run_id

    async def execute(self, job, run_id, source):
        status, detail = "failed", "执行未完成"
        try:
            async with self.telegram.lock(job["account_id"]):
                # Recheck after waiting: the account/job may have been paused or deleted.
                current = self.store.one("SELECT * FROM jobs WHERE id=?", (job["id"],))
                account = self.store.one("SELECT * FROM accounts WHERE id=?", (job["account_id"],))
                if (not current or not account or not account["enabled"] or self.store.paused()
                        or not current["enabled"]):
                    status, detail = "skipped", "任务、帐号或全局调度已暂停"
                elif account["cooldown"] > time.time():
                    status, detail = "skipped", "帐号处于 Telegram 限流冷却期"
                else:
                    try:
                        status, detail = await asyncio.wait_for(
                            self.telegram.send(account, current), timeout=current["reply_timeout"] + 90)
                    finally:
                        # Serialize each account and leave a small gap between messages.
                        await asyncio.sleep(2)
        except FloodWaitError as exc:
            self.store.execute("UPDATE accounts SET cooldown=? WHERE id=?",
                               (time.time() + exc.seconds + 1, job["account_id"]))
            detail = friendly_error(exc)
        except asyncio.CancelledError:
            status, detail = "unknown", "服务停止，结果可能已发送；请核实 Telegram"
            raise
        except Exception as exc:
            # A network error can occur after Telegram accepted the message.
            status, detail = "unknown", friendly_error(exc) + "；请核实是否已发送，不会自动重试"
        finally:
            self.store.execute("UPDATE runs SET status=?,detail=?,finished=? WHERE id=?",
                               (status, detail, time.time(), run_id))

    async def tick(self, now=None):
        now = now if now is not None else time.time()
        for job in self.store.all("SELECT * FROM jobs WHERE enabled=1 AND next_run<=?", (now,)):
            # Advance before launching. Downtime does not produce a burst of catch-up messages.
            self.store.execute("UPDATE jobs SET next_run=? WHERE id=?",
                               (next_time(job["cron"], job["timezone"], now), job["id"]))
            if not self.store.paused() and now - job["next_run"] <= 60:
                self.launch(job, "schedule")

    async def loop(self):
        count = 0
        while not self.stopping:
            try:
                await self.tick()
                if count % 30 == 0:
                    await self.telegram.prune()
                    self.store.cleanup()
            except Exception:
                # No credentials or message bodies in system logs.
                import logging
                logging.getLogger(__name__).error("Scheduler iteration failed; check database/configuration")
            count += 1
            await asyncio.sleep(2)

    async def close(self):
        self.stopping = True
        if self.loop_task:
            self.loop_task.cancel()
            await asyncio.gather(self.loop_task, return_exceptions=True)
        pending = list(self.tasks.values())
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await self.telegram.close()
