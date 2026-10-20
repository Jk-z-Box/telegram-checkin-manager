import argparse
import getpass
import ipaddress
import json
import os
import subprocess
import sys
import time

from .models import Job, next_time
from .store import PASSWORDS, Store, data_dir, initialize, save_config


def main():
    parser = argparse.ArgumentParser(description="Telegram 签到管理")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    sub.add_parser("serve")
    sub.add_parser("reset-password")
    sub.add_parser("config")
    sub.add_parser("status")
    sub.add_parser("pause")
    sub.add_parser("resume")
    sub.add_parser("list-jobs")
    load = sub.add_parser("import-jobs")
    load.add_argument("file", help="JSON 任务数组；从 example.jobs.json 开始")
    toggle = sub.add_parser("toggle-job")
    toggle.add_argument("id", type=int)
    toggle.add_argument("state", choices=["on", "off"])
    sub.add_parser("menu")
    args = parser.parse_args()
    path = data_dir()
    if args.cmd == "init":
        username = input("管理员用户名 [admin]: ").strip() or "admin"
        if len(username) > 64:
            raise ValueError("用户名不能超过 64 字符")
        password = getpass.getpass("管理员密码（至少 12 字符）: ")
        if password != getpass.getpass("再次输入密码: "):
            raise ValueError("两次密码不一致")
        initialize(path, username, password)
        print("初始化完成；请妥善备份 data 目录及 master.key")
        return
    if not (path / "config.json").exists():
        raise ValueError("请先运行 python -m app.cli init")
    if args.cmd == "serve":
        import uvicorn
        config = json.loads((path / "config.json").read_text())
        uvicorn.run("app.main:app", host=config["host"], port=config["port"],
                    workers=1, access_log=False, forwarded_allow_ips="127.0.0.1,::1")
        return
    if args.cmd == "menu":
        return menu()
    db = Store(path)
    try:
        if args.cmd == "reset-password":
            password = getpass.getpass("新管理员密码（至少 12 字符）: ")
            if len(password) < 12 or len(password) > 256:
                raise ValueError("密码长度必须为 12–256 字符")
            if password != getpass.getpass("再次输入密码: "):
                raise ValueError("两次密码不一致")
            config = db.config()
            config["password_hash"] = PASSWORDS.hash(password)
            save_config(path, config)
            db.execute("DELETE FROM sessions")
            print("密码已更新，所有 Web 登录已失效")
        elif args.cmd in ("pause", "resume"):
            db.execute("UPDATE settings SET value=? WHERE key='paused'",
                       ("1" if args.cmd == "pause" else "0",))
            print("已暂停后续任务（已发出的消息无法撤回）" if args.cmd == "pause" else "已恢复调度")
        elif args.cmd == "config":
            config = db.config()
            host = input(f"监听 IP [{config['host']}]: ").strip() or config["host"]
            ipaddress.ip_address(host)
            port = int(input(f"端口 [{config['port']}]: ").strip() or config["port"])
            if not 1 <= port <= 65535:
                raise ValueError("端口必须为 1–65535")
            secure = input(f"仅 HTTPS Cookie? y/n [{'y' if config['secure_cookie'] else 'n'}]: ").strip().lower()
            if secure not in ("", "y", "n"):
                raise ValueError("请输入 y 或 n")
            config.update(host=host, port=port)
            if secure:
                config["secure_cookie"] = secure == "y"
            save_config(path, config)
            print("已保存，请重启服务使监听配置生效")
        elif args.cmd == "status":
            print(json.dumps({
                "data": str(path), "paused": db.paused(),
                "accounts": db.one("SELECT COUNT(*) n FROM accounts")["n"],
                "jobs": db.one("SELECT COUNT(*) n FROM jobs")["n"],
                "host": db.config()["host"], "port": db.config()["port"],
            }, ensure_ascii=False, indent=2))
        elif args.cmd == "list-jobs":
            for job in db.all("SELECT id,name,account_id,cron,timezone,enabled FROM jobs"):
                print(json.dumps(job, ensure_ascii=False))
        elif args.cmd == "toggle-job":
            job = db.one("SELECT * FROM jobs WHERE id=?", (args.id,))
            if not job:
                raise ValueError("任务不存在")
            db.execute("UPDATE jobs SET enabled=?,next_run=? WHERE id=?",
                       (args.state == "on", next_time(job["cron"], job["timezone"]), args.id))
            print("任务状态已更新")
        elif args.cmd == "import-jobs":
            with open(args.file) as f:
                data = json.load(f)
            if not isinstance(data, list) or len(data) > 500:
                raise ValueError("需要包含最多 500 个任务的 JSON 数组")
            jobs = [Job.model_validate(item).model_dump() for item in data]
            for job in jobs:
                if not db.one("SELECT id FROM accounts WHERE id=?", (job["account_id"],)):
                    raise ValueError(f"帐号 {job['account_id']} 不存在")
            with db.db:
                for job in jobs:
                    job.update(next_run=next_time(job["cron"], job["timezone"]), created=time.time())
                    db.db.execute(f"INSERT INTO jobs({','.join(job)}) VALUES({','.join('?' for _ in job)})",
                                  tuple(job.values()))
            print(f"已新增 {len(jobs)} 个任务")
    finally:
        db.close()


def menu():
    labels = [
        ("查看服务状态", ["systemctl", "status", "tgcheckin", "--no-pager"]),
        ("启动服务", ["systemctl", "start", "tgcheckin"]),
        ("停止服务", ["systemctl", "stop", "tgcheckin"]),
        ("重启服务", ["systemctl", "restart", "tgcheckin"]),
        ("暂停全部签到", ["cli", "pause"]),
        ("恢复全部签到", ["cli", "resume"]),
        ("修改监听配置", ["cli", "config"]),
        ("重置 Web 密码", ["cli", "reset-password"]),
        ("查看任务", ["cli", "list-jobs"]),
        ("开关指定任务", ["toggle"]),
        ("查看服务日志", ["journalctl", "-u", "tgcheckin", "-n", "80", "--no-pager"]),
        ("备份数据", ["backup"]),
        ("从 GitHub 更新", ["update"]),
        ("导入 JSON 任务", ["import"]),
        ("查看应用状态", ["cli", "status"]),
    ]
    while True:
        print("\nTelegram 签到管理\n" + "\n".join(f"{i}. {x[0]}" for i, x in enumerate(labels, 1)) + "\n0. 退出")
        choice = input("请选择: ").strip()
        if choice == "0":
            return
        if not choice.isdigit() or not 1 <= int(choice) <= len(labels):
            continue
        cmd = labels[int(choice)-1][1]
        installed = os.path.isfile("/usr/local/bin/tgcheckin")
        cli = ["/usr/local/bin/tgcheckin"] if installed else [sys.executable, "-m", "app.cli"]
        if cmd[0] == "cli":
            cmd = [*cli, *cmd[1:]]
        elif cmd[0] == "toggle":
            cmd = [*cli, "toggle-job",
                   input("任务 ID: ").strip(), input("on / off: ").strip()]
        elif cmd[0] == "import":
            cmd = [*cli, "import-jobs", input("JSON 文件路径: ").strip()]
        elif cmd[0] in ("backup", "update"):
            cmd = ["/usr/local/bin/tgcheckin", cmd[0]]
        try:
            subprocess.run(cmd, check=False)
        except FileNotFoundError:
            print("此功能需要 Linux systemd 安装环境")


if __name__ == "__main__":
    os.umask(0o077)
    try:
        main()
    except (ValueError, KeyboardInterrupt) as exc:
        print(str(exc) or "已取消", file=sys.stderr)
        sys.exit(1)
