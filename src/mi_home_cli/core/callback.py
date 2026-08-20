"""接收 OAuth 回调。

小米只接受 host 为 homeassistant.local:8123 的 redirect_uri（见 const.py），
所以想自动拿到 code，得同时满足两件事：

  1. 浏览器把 homeassistant.local 解析到跑 CLI 的这台机器；
  2. 这台机器上的 8123 端口由我们监听。

条件 1 有三条路：本机已有 hosts 记录、局域网里有 mDNS 响应者（我们可以自己
用 zeroconf 播一个）、或者用户手工加 hosts。任何一条都不成立时，回退到让用户
把浏览器地址栏里的 URL 粘回来——那条路永远可用。
"""
from __future__ import annotations

import queue
import socket
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from ..errors import UsageError
from . import const

_PAGE = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><title>{title}</title>
<style>body{{font-family:system-ui,-apple-system,"PingFang SC",sans-serif;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0;
background:#f6f7f9;color:#222}}div{{text-align:center}}
h1{{font-size:20px;margin:0 0 8px}}p{{color:#666;margin:0}}</style></head>
<body><div><h1>{title}</h1><p>{body}</p></div></body></html>
"""


@dataclass
class CallbackResult:
    code: str
    state: str | None
    source: str  # "server" 或 "paste"


class _Handler(BaseHTTPRequestHandler):
    server_version = "mi-home-cli"
    result_queue: "queue.Queue[CallbackResult]"

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler 的约定)
        params = parse_qs(urlparse(self.path).query)
        code = (params.get("code") or [None])[0]
        state = (params.get("state") or [None])[0]
        if code:
            self.result_queue.put(
                CallbackResult(code=code, state=state, source="server")
            )
            self._respond(200, "登录成功", "可以关闭这个页面，回到终端。")
        else:
            error = (params.get("error") or ["缺少 code 参数"])[0]
            self._respond(400, "登录失败", str(error))

    def _respond(self, status: int, title: str, body: str) -> None:
        payload = _PAGE.format(title=title, body=body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: Any) -> None:
        """默认会往 stderr 打访问日志，CLI 里不需要。"""


class CallbackServer:
    """监听 8123，等一次回调。"""

    def __init__(
        self, result_queue: "queue.Queue[object]",
        bind_host: str = "0.0.0.0",
        port: int = const.REDIRECT_PORT,
    ) -> None:
        handler = type("_BoundHandler", (_Handler,), {"result_queue": result_queue})
        self._httpd = HTTPServer((bind_host, port), handler)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="mi-oauth-callback", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    def __enter__(self) -> "CallbackServer":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def port_available(port: int = const.REDIRECT_PORT, host: str = "0.0.0.0") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def resolve_redirect_host(host: str = const.REDIRECT_HOST) -> list[str]:
    """homeassistant.local 当前解析到哪些地址（解析不了就是空列表）。"""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return []
    return sorted({info[4][0] for info in infos})


def local_ips() -> list[str]:
    """本机对外的 IP，用来判断 homeassistant.local 是不是指向自己。"""
    ips: set[str] = {"127.0.0.1"}
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            # 不会真的发包，只是让内核选出默认出口地址。
            sock.connect(("223.5.5.5", 53))
            ips.add(sock.getsockname()[0])
    except OSError:
        pass
    try:
        ips.update(
            info[4][0]
            for info in socket.getaddrinfo(socket.gethostname(), None)
        )
    except socket.gaierror:
        pass
    return sorted(ips)


class MdnsPublisher:
    """把 homeassistant.local 播成本机地址（需要可选依赖 zeroconf）。

    局域网里已经有真的 Home Assistant 时会撞名，这时放弃并让用户走粘贴。
    """

    def __init__(self, address: str) -> None:
        self._address = address
        self._zeroconf: Any = None
        self._info: Any = None

    def start(self) -> str | None:
        """成功返回 None，失败返回原因。"""
        try:
            from zeroconf import ServiceInfo, Zeroconf
        except ImportError:
            return "未安装 zeroconf（uv sync --extra mdns）"
        try:
            self._zeroconf = Zeroconf()
            self._info = ServiceInfo(
                "_http._tcp.local.",
                "mi-home-cli._http._tcp.local.",
                addresses=[socket.inet_aton(self._address)],
                port=const.REDIRECT_PORT,
                server=f"{const.REDIRECT_HOST}.",
            )
            self._zeroconf.register_service(self._info)
        except Exception as err:  # zeroconf 的异常类型比较杂
            self.close()
            return f"注册 mDNS 失败：{err}"
        return None

    def close(self) -> None:
        try:
            if self._zeroconf and self._info:
                self._zeroconf.unregister_service(self._info)
        except Exception:
            pass
        try:
            if self._zeroconf:
                self._zeroconf.close()
        except Exception:
            pass
        self._zeroconf = None
        self._info = None


def parse_pasted(text: str, *, expected_state: str | None = None) -> CallbackResult:
    """解析用户粘回来的内容。

    支持三种写法：
      * 完整 URL：http://homeassistant.local:8123/...?code=xxx&state=yyy
      * 光是 query：code=xxx&state=yyy 或 ?code=xxx&state=yyy
      * 光是 code：xxx
    """
    text = text.strip().strip('"').strip("'")
    if not text:
        raise UsageError("没有输入内容")

    query = ""
    if "://" in text:
        query = urlparse(text).query
    elif text.startswith("?"):
        query = text[1:]
    elif "code=" in text:
        query = text.lstrip("?")

    if query:
        params = parse_qs(query)
        error = (params.get("error") or params.get("error_description") or [None])[0]
        code = (params.get("code") or [None])[0]
        state = (params.get("state") or [None])[0]
        if not code:
            raise UsageError(
                f"这段内容里没有 code 参数：{error or text[:80]}",
            )
    else:
        if any(char in text for char in " \t\n"):
            raise UsageError("无法识别的内容，请粘贴完整的重定向 URL")
        code, state = text, None

    if expected_state and state and state != expected_state:
        raise UsageError(
            "state 不匹配，这不是本次登录的回调；请重新执行 `mi auth login`"
        )
    return CallbackResult(code=code, state=state, source="paste")
