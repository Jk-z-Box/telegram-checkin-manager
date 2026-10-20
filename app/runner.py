import asyncio
import time

from telethon.errors import FloodWaitError

from .models import next_time
from .telegram import execution_timeout, friendly_error


ACCOUNT_GAP = 2


class Runner:
    def __init__(self, store, telegram):
        self.store, self.telegram = store, telegram
        self.tasks = {}
        self.send_lock = asyncio.Lock()
        self.loop_task = None
        self.stopping = False

    def start(self):
        self.store.execute(
            "UPDATE runs SET status='unconfirmed',finished=?,detail=? WHERE status='awaiting_reply'",
            (time.time(), "消息已发送，服务曾中断，回复未确认；不会自动重发"),
        )
        self.store.execute(
            "UPDATE runs SET status='skipped',finished=?,detail=? WHERE status='queued'",
            (time.time(), "服务曾中断，排队任务尚未执行；不会自动补发"),
        )
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
            """INSERT INTO runs(job_id,job_name,account_name,started,status,source,detail)
            VALUES(?,?,?,?,?,?,?)""",
            (job["id"], job["name"], account["name"], time.time(), "queued", source,
             "等待统一发送队列"),
        ).lastrowid
        task = asyncio.create_task(self.execute(job, run_id, source))
        self.tasks[job["id"]] = task
        task.add_done_callback(lambda _: self.tasks.pop(job["id"], None))
        return run_id

    async def execute(self, job, run_id, source):
        status, detail = "failed", "执行未完成"
        started = acknowledged = False
        operation = ack_waiter = None
        sent = asyncio.Event()

        def on_sent(send_detail):
            nonlocal acknowledged
            acknowledged = True
            waiting = bool(job["button"] or job["success_text"])
            self.store.execute("UPDATE runs SET status=?,detail=? WHERE id=?",
                               ("awaiting_reply" if waiting else "sent",
                                send_detail + ("已发送，等待回复确认" if waiting else "消息已发送"), run_id))
            sent.set()

        try:
            # One FIFO for all manual/scheduled jobs, including collection members.
            # Only preparation, retries and submission occupy it; replies do not.
            async with self.send_lock:
                async with self.telegram.lock(job["account_id"]):
                    current = self.store.one("SELECT * FROM jobs WHERE id=?", (job["id"],))
                    account = self.store.one("SELECT * FROM accounts WHERE id=?", (job["account_id"],))
                    if (not current or not account or not account["enabled"] or self.store.paused()
                            or not current["enabled"]):
                        status, detail = "skipped", "任务、帐号或全局调度已暂停"
                    elif account["cooldown"] > time.time():
                        status, detail = "skipped", "帐号处于 Telegram 限流冷却期"
                    else:
                        started = True
                        self.store.execute("UPDATE runs SET status='running',detail=? WHERE id=?",
                                           ("正在连接并发送签到消息", run_id))
                        operation = asyncio.create_task(self.perform(account, current, on_sent))
                        ack_waiter = asyncio.create_task(sent.wait())
                        try:
                            await asyncio.wait((operation, ack_waiter), return_when=asyncio.FIRST_COMPLETED)
                        finally:
                            ack_waiter.cancel()
                            await asyncio.gather(ack_waiter, return_exceptions=True)
                        # Starts after send acknowledgement (or final send failure),
                        # not after the potentially much longer reply timeout.
                        await asyncio.sleep(ACCOUNT_GAP)
            if operation is not None:
                status, detail = await operation
        except asyncio.CancelledError:
            if operation is not None and operation.done() and not operation.cancelled():
                status, detail = operation.result()
            elif acknowledged:
                status, detail = "unconfirmed", "消息已发送，服务停止导致回复确认中断；不会重发"
            elif not started:
                status, detail = "skipped", "服务停止，排队任务尚未执行；不会自动补发"
            else:
                status, detail = "unknown", "服务停止，结果可能已发送；请核实 Telegram"
            raise
        except Exception as exc:
            status = "unconfirmed" if acknowledged else "unknown" if started else "failed"
            detail = friendly_error(exc) + ("；消息已发送，回复未确认" if acknowledged else
                                          "；请核实是否已发送" if started else "；尚未开始发送")
        finally:
            if operation is not None and not operation.done():
                operation.cancel()
                await asyncio.gather(operation, return_exceptions=True)
            self.store.execute("UPDATE runs SET status=?,detail=?,finished=? WHERE id=?",
                               (status, detail, time.time(), run_id))

    async def perform(self, account, job, on_sent):
        acknowledged = False

        def report_sent(detail):
            nonlocal acknowledged
            acknowledged = True
            on_sent(detail)

        try:
            return await asyncio.wait_for(self.telegram.send(account, job, on_sent=report_sent),
                                          timeout=execution_timeout(job))
        except FloodWaitError as exc:
            self.store.execute("UPDATE accounts SET cooldown=? WHERE id=?",
                               (time.time() + exc.seconds + 1, job["account_id"]))
            return getattr(exc, "checkin_status", "unconfirmed" if acknowledged else "failed"), (
                getattr(exc, "checkin_detail", "") + friendly_error(exc))
        except Exception as exc:
            if acknowledged:
                return "unconfirmed", "消息已发送，回复确认未完成：" + friendly_error(exc) + "；不会重发"
            return "unknown", friendly_error(exc) + "；请核实是否已发送，不会自动重试"

    async def tick(self, now=None):
        now = now if now is not None else time.time()
        for job in self.store.all("SELECT * FROM jobs WHERE enabled=1 AND next_run<=? ORDER BY next_run,id", (now,)):
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
