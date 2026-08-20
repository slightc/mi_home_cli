"""`mi doctor`：登录相关的环境自检。"""
from __future__ import annotations

import socket
import time
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
import typer

from .. import render
from ..render import OutputFormat
from ..core import const
from ..core.callback import local_ips, port_available, resolve_redirect_host
from ..store import file_is_private
from .context import AppContext, pick_output

OK = "OK"
WARN = "WARN"
FAIL = "FAIL"

# 颜色只在表格里加，json/yaml/plain 输出不该混进 rich 标记。
_COLORS = {OK: "green", WARN: "yellow", FAIL: "red"}


def _check_reachable(url: str, timeout: float) -> tuple[str, str]:
    try:
        response = httpx.get(url, timeout=timeout, follow_redirects=False)
    except httpx.HTTPError as err:
        return FAIL, f"{type(err).__name__}: {err}"
    return OK, f"HTTP {response.status_code}"


def _check_clock(timeout: float) -> tuple[str, str]:
    """时钟偏差过大会让 OAuth 和 token 有效期判断出问题。"""
    try:
        response = httpx.get(
            "https://account.xiaomi.com/", timeout=timeout, follow_redirects=False
        )
        server_date = response.headers.get("date")
        if not server_date:
            return WARN, "响应里没有 Date 头"
        skew = time.time() - parsedate_to_datetime(server_date).timestamp()
    except (httpx.HTTPError, ValueError, TypeError) as err:
        return WARN, f"无法判断：{err}"
    if abs(skew) > 300:
        return FAIL, f"本机时钟与服务端相差 {skew:.0f} 秒"
    return OK, f"偏差 {skew:.0f} 秒"


def run(ctx: typer.Context, output=None) -> None:
    app_ctx: AppContext = ctx.obj
    timeout = app_ctx.timeout
    rows: list[dict[str, Any]] = []

    def add(item: str, status: str, detail: str) -> None:
        rows.append({"检查项": item, "结果": status, "说明": detail})

    # 1. 配置目录与权限
    profile = app_ctx.profile
    if profile.auth_path.exists():
        private = file_is_private(profile.auth_path)
        add(
            "凭据文件权限",
            OK if private else FAIL,
            str(profile.auth_path) + ("" if private else "，其他用户可读，建议 chmod 600"),
        )
    else:
        add("凭据文件", WARN, f"未登录（{profile.auth_path} 不存在）")

    # 2. 登录态
    auth = profile.read_auth()
    if auth:
        left = auth.expires_at - time.time()
        add(
            "登录态",
            OK if left > 0 else FAIL,
            f"{auth.nickname or '?'} / {auth.region} / 剩余 {max(int(left), 0)} 秒",
        )

    # 3. 回调链路：端口 + 域名解析
    free = port_available(const.REDIRECT_PORT)
    add(
        f"{const.REDIRECT_PORT} 端口",
        OK if free else WARN,
        "可用" if free else "被占用，登录只能用粘贴方式",
    )
    resolved = resolve_redirect_host()
    mine = set(local_ips())
    if not resolved:
        add(
            f"{const.REDIRECT_HOST} 解析",
            WARN,
            "解析不到；登录时会尝试 mDNS，不行就用粘贴方式",
        )
    elif set(resolved) & mine:
        add(f"{const.REDIRECT_HOST} 解析", OK, f"指向本机（{', '.join(resolved)}）")
    else:
        add(
            f"{const.REDIRECT_HOST} 解析",
            WARN,
            f"指向 {', '.join(resolved)}，不是本机，回调会落到那台机器",
        )

    try:
        import zeroconf  # noqa: F401

        add("zeroconf", OK, "已安装，可自动广播 mDNS")
    except ImportError:
        add("zeroconf", WARN, "未安装，装了可提高自动回调成功率：pip install 'mi-home-cli[mdns]'")

    # 4. 网络连通性
    status, detail = _check_reachable("https://account.xiaomi.com/", timeout)
    add("account.xiaomi.com", status, detail)
    region = app_ctx.resolved_region()
    status, detail = _check_reachable(f"https://{const.api_host(region)}/", timeout)
    add(f"{const.api_host(region)}", status, detail)

    # 5. 时钟
    status, detail = _check_clock(timeout)
    add("系统时钟", status, detail)

    fmt = pick_output(app_ctx, output)
    display = rows
    if fmt is OutputFormat.table:
        display = [
            {**row, "结果": f"[{_COLORS[row['结果']]}]{row['结果']}[/]"}
            for row in rows
        ]
    render.output(display, fmt, columns=["检查项", "结果", "说明"])
    if any(row["结果"] == FAIL for row in rows):
        raise typer.Exit(code=1)


def dns_hint() -> str:
    """给用户看的 hosts 兜底方案。"""
    ip = next((item for item in local_ips() if item != "127.0.0.1"), "127.0.0.1")
    return f"{ip}\t{const.REDIRECT_HOST}"


def resolve_debug() -> dict[str, Any]:
    return {
        "resolved": resolve_redirect_host(),
        "local_ips": local_ips(),
        "hostname": socket.gethostname(),
    }
