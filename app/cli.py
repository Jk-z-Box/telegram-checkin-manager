import argparse
import json
import os
import shlex
import subprocess
import sys
import time

from .models import Job, next_time
from .store import PASSWORDS, Store, data_dir, initialize, save_config
from .configuration import (access_summary, ask, ask_password, validate_host,
                            validate_port, validate_username, wizard)


def main():
    parser = argparse.ArgumentParser(description="Telegram 签到管理")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    sub.add_parser("serve")
    sub.add_parser("reset-password", aliases=["passwd"])
    sub.add_parser("config", aliases=["setup"])
    sub.add_parser("access")
    port = sub.add_parser("port", help="修改 Web 端口")
    port.add_argument("port", type=validate_port)
    bind = sub.add_parser("bind", help="指定监听 IP，可附带端口")
    bind.add_argument("host", type=validate_host)
    bind.add_argument("port", nargs="?", type=validate_port)
    for name in ("public", "local"):
        command = sub.add_parser(name, help="监听所有 IPv4 网卡" if name == "public" else "仅监听本机")
        command.add_argument("port", nargs="?", type=validate_port)
    user = sub.add_parser("username", help="修改管理员用户名")
    user.add_argument("username", nargs="?", type=validate_username)
    sub.add_parser("enable", help="启用开机自启")
    sub.add_parser("disable", help="关闭开机自启")
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
        if (path / "config.json").exists():
            raise ValueError("已经初始化，请使用 config 修改配置")
        config, password = wizard()
        initialize(path, password=password, **config)
        print(f"初始化完成；请妥善备份 {path} 目录及 master.key")
        access_summary(config)
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
        if args.cmd in ("reset-password", "passwd"):
            password = ask_password()
            config = db.config()
            config["password_hash"] = PASSWORDS.hash(password)
            save_config(path, config)
            db.execute("DELETE FROM sessions")
            print("密码已更新，所有 Web 登录已失效")
        elif args.cmd in ("pause", "resume"):
            db.execute("UPDATE settings SET value=? WHERE key='paused'",
                       ("1" if args.cmd == "pause" else "0",))
            print("已暂停后续任务（已发出的消息无法撤回）" if args.cmd == "pause" else "已恢复调度")
        elif args.cmd in ("config", "setup", "port", "public", "local", "bind",
                          "username", "enable", "disable"):
            original = db.config()
            config = original.copy()
            password = None
            if args.cmd in ("config", "setup"):
                updates, password = wizard(config)
                config.update(updates)
            elif args.cmd in ("public", "local", "bind", "port"):
                if args.cmd == "public":
                    config["host"] = "0.0.0.0"
                elif args.cmd == "local":
                    config["host"] = "127.0.0.1"
                elif args.cmd == "bind":
                    config["host"] = args.host
                if args.port is not None:
                    config["port"] = args.port
            elif args.cmd == "username":
                config["username"] = args.username or ask("管理员用户名", config["username"], validate_username)
            else:
                config["autostart"] = args.cmd == "enable"
            if password:
                config["password_hash"] = PASSWORDS.hash(password)
            save_config(path, config)
            if password or config["username"] != original["username"]:
                db.execute("DELETE FROM sessions")
                print("管理员凭据已更新，所有 Web 登录已失效")
            print("配置已保存。安装版快捷指令会应用服务配置；手动运行时需重启服务。")
            access_summary(config)
        elif args.cmd == "access":
            access_summary(db.config())
        elif args.cmd == "status":
            print(json.dumps({
                "data": str(path), "paused": db.paused(),
                "accounts": db.one("SELECT COUNT(*) n FROM accounts")["n"],
                "jobs": db.one("SELECT COUNT(*) n FROM jobs")["n"],
                "host": db.config()["host"], "port": db.config()["port"],
                "username": db.config()["username"],
                "secure_cookie": db.config()["secure_cookie"],
                "autostart": db.config().get("autostart", True),
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
        ("查看服务状态", "service-status"),
        ("启动服务", "start"),
        ("停止服务", "stop"),
        ("重启服务", "restart"),
        ("暂停全部签到", "pause"),
        ("恢复全部签到", "resume"),
        ("完整配置向导", "config"),
        ("重置 Web 密码", "passwd"),
        ("查看任务", "list-jobs"),
        ("开关指定任务", "toggle-job"),
        ("查看服务日志", "logs"),
        ("备份数据", "backup"),
        ("从 GitHub 更新", "update"),
        ("导入 JSON 任务", "import-jobs"),
        ("查看应用状态", "status"),
        ("修改管理员用户名", "username"),
        ("修改端口", "port"),
        ("切换为公网/局域网监听", "public"),
        ("切换为仅本机监听", "local"),
        ("开启开机自启", "enable"),
        ("关闭开机自启", "disable"),
        ("查看访问地址", "access"),
    ]
    allowed = {command for _, command in labels} | {"bind", "setup", "reset-password", "help"}
    while True:
        print("\nTelegram 签到管理 · 快捷入口：tgc\n" +
              "\n".join(f"{i:2}. {label}  [tgc {command}]" for i, (label, command) in enumerate(labels, 1)) +
              "\n 0. 退出 [q]\n可输入数字或快捷指令，例如：port 9090、public 9090、restart")
        choice = input("请选择: ").strip()
        if choice.lower() in ("0", "q", "quit", "exit"):
            return
        try:
            if choice.isdigit() and 1 <= int(choice) <= len(labels):
                args = [labels[int(choice)-1][1]]
            else:
                args = shlex.split(choice)
                if args and args[0] in ("tgc", "tgcheckin"):
                    args = args[1:]
        except ValueError:
            print("指令格式错误，请检查引号")
            continue
        if not args or args[0] not in allowed:
            print("未知选项，请输入菜单数字或列出的快捷指令")
            continue
        installed = os.path.isfile("/usr/local/bin/tgcheckin")
        cli = ["/usr/local/bin/tgcheckin"] if installed else [sys.executable, "-m", "app.cli"]
        if len(args) == 1:
            if args[0] == "toggle-job":
                args.extend([input("任务 ID: ").strip(), input("on / off: ").strip()])
            elif args[0] == "import-jobs":
                args.append(input("JSON 文件路径: ").strip())
            elif args[0] == "port":
                args.append(input("新端口（1–65535）: ").strip())
        if not installed and args[0] in ("start", "stop", "restart", "service-status", "logs", "backup", "update", "help"):
            print("此快捷指令需要 Linux 安装环境")
            continue
        cmd = [*cli, *args]
        try:
            subprocess.run(cmd, check=False)
        except FileNotFoundError:
            print("此功能需要 Linux systemd 安装环境")


if __name__ == "__main__":
    os.umask(0o077)
    try:
        main()
    except (ValueError, KeyboardInterrupt, EOFError) as exc:
        print(str(exc) or "已取消", file=sys.stderr)
        sys.exit(1)
