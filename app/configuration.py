"""Shared validation and interactive deployment configuration."""
import getpass
import ipaddress
import re
from urllib.parse import urlsplit


def validate_host(value):
    try:
        address = ipaddress.ip_address(value.strip())
    except ValueError as exc:
        raise ValueError("请输入有效的 IPv4 或 IPv6 地址") from exc
    if address.is_multicast or str(address) == "255.255.255.255":
        raise ValueError("不能监听组播或广播地址")
    return str(address)


def validate_port(value):
    try:
        port = int(value)
    except (ValueError, TypeError) as exc:
        raise ValueError("端口必须为 1–65535 的整数") from exc
    if not 1 <= port <= 65535:
        raise ValueError("端口必须为 1–65535 的整数")
    return port


def validate_username(value):
    value = value.strip()
    if not 1 <= len(value) <= 64 or any(c.isspace() or not c.isprintable() for c in value):
        raise ValueError("用户名须为 1–64 个可见字符，不能包含空白")
    return value


def validate_public_url(value):
    """One explicit browser origin; never derive it from untrusted proxy headers."""
    value = value.strip()
    if not value or value.lower() == "off":
        return ""
    message = "请输入完整的 http:// 或 https:// 访问地址，不带路径、参数或帐号；off 表示不设置"
    try:
        if len(value) > 2048 or any(c.isspace() or not c.isprintable() for c in value):
            raise ValueError(message)
        url = urlsplit(value)
        if (url.scheme not in ("http", "https") or not url.hostname or
                url.username is not None or url.password is not None or
                url.path not in ("", "/") or "?" in value or "#" in value or
                "\\" in value or "%" in url.netloc):
            raise ValueError(message)
        host = url.hostname.encode("idna").decode("ascii").lower()
        try:
            address = ipaddress.ip_address(host)
            host = f"[{address}]" if address.version == 6 else str(address)
        except ValueError:
            labels = host.rstrip(".").split(".")
            if len(host) > 253 or any(
                    not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                    for label in labels):
                raise ValueError(message)
        port = url.port
        if port is not None:
            validate_port(port)
        suffix = f":{port}" if port is not None and port != {"http": 80, "https": 443}[url.scheme] else ""
        return f"{url.scheme}://{host}{suffix}"
    except (ValueError, UnicodeError) as exc:
        raise ValueError(message) from exc


def ask(prompt, default, validator=lambda value: value):
    while True:
        value = input(f"{prompt} [{default}]: ").strip() or str(default)
        try:
            return validator(value)
        except ValueError as exc:
            print(exc)


def yes_no(prompt, default):
    def parse(value):
        if value.lower() not in ("y", "n", "yes", "no"):
            raise ValueError("请输入 y 或 n")
        return value.lower() in ("y", "yes")
    return ask(prompt, "y" if default else "n", parse)


def ask_password(optional=False):
    while True:
        hint = "新密码（至少 12 字符，直接回车保留）: " if optional else "管理员密码（至少 12 字符）: "
        password = getpass.getpass(hint)
        if optional and not password:
            return None
        if not 12 <= len(password) <= 256:
            print("密码长度必须为 12–256 字符")
            continue
        if password != getpass.getpass("再次输入密码: "):
            print("两次密码不一致，请重新输入")
            continue
        return password


def wizard(current=None):
    current = current or {}
    host = current.get("host", "127.0.0.1")
    default_mode = "1" if host == "127.0.0.1" else "2" if host == "0.0.0.0" else "3"
    print("\n部署配置（直接回车使用方括号中的值）")
    print("1. 仅本机：127.0.0.1，适合 SSH 隧道或本机反向代理")
    print("2. 公网/局域网：0.0.0.0，监听所有 IPv4 网卡")
    print("3. 自定义 IP：指定网卡地址，支持 IPv6")

    def mode(value):
        if value not in ("1", "2", "3"):
            raise ValueError("请选择 1、2 或 3")
        return value

    selected = ask("监听方式", default_mode, mode)
    host = {"1": "127.0.0.1", "2": "0.0.0.0"}.get(selected, host)
    if selected == "3":
        host = ask("监听 IP", host, validate_host)
    port = ask("Web 端口", current.get("port", 8080), validate_port)
    public_url = ask("浏览器访问地址（Cloudflare/反向代理域名，off 不设置）",
                     current.get("public_url") or "off", validate_public_url)
    print("HTTPS Cookie 需要浏览器经 HTTPS 域名访问；应用本身提供 HTTP。")
    secure = yes_no("仅允许通过 HTTPS 登录", current.get("secure_cookie", False))
    autostart = yes_no("服务开机自启", current.get("autostart", True))
    username = ask("管理员用户名", current.get("username", "admin"), validate_username)
    password = ask_password(optional=bool(current))
    return {"host": host, "port": port, "secure_cookie": secure,
            "autostart": autostart, "username": username, "public_url": public_url}, password


def access_summary(config):
    host, port = config["host"], config["port"]
    address = ipaddress.ip_address(host)
    print(f"监听地址：{host}    端口：{port}")
    print(f"管理员：{config['username']}    开机自启：{'是' if config.get('autostart', True) else '否'}")
    if config.get("public_url"):
        print(f"Web 地址：{config['public_url']}")
        print("来源校验以此地址为准；回源监听地址和端口保持独立。")
    elif config["secure_cookie"]:
        print("访问方式：打开已配置的 HTTPS 反向代理域名；普通 HTTP 无法登录。")
    else:
        if address.is_unspecified:
            print(f"Web 地址：http://服务器IP:{port}")
        else:
            url_host = f"[{host}]" if address.version == 6 else host
            print(f"Web 地址：http://{url_host}:{port}")
        if address.is_loopback:
            remote = f"[{host}]" if address.version == 6 else host
            print(f"远程访问：ssh -N -L 18080:{remote}:{port} 用户名@服务器")
            print("然后在你的电脑打开 http://127.0.0.1:18080")
    if not address.is_loopback:
        print(f"远程访问还取决于路由、防火墙和云安全组；需要放行 TCP {port}。")
    print("管理菜单：tgc（也可使用 tgcheckin）    快捷指令：tgc help")
