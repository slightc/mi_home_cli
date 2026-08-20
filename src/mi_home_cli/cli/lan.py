"""`mi lan`：局域网直连的发现与探测。"""
from __future__ import annotations

import time
from typing import Annotated, Optional

import typer

from .. import render
from ..core import lan as lan_core
from ..core.channel import LAN_CAPABLE_TYPES, lan_capable, locate
from ..errors import MiCliError
from .context import AppContext, OutputOption, pick_output
from .device import load_registry, resolve

app = typer.Typer(help="局域网直连", no_args_is_help=True)


def _ctx(ctx: typer.Context) -> AppContext:
    return ctx.obj


@app.command("discover")
def lan_discover(
    ctx: typer.Context,
    timeout: Annotated[
        float, typer.Option("--timeout", help="等待应答的秒数")
    ] = 3.0,
    address: Annotated[
        Optional[str],
        typer.Option("--address", help="广播地址，默认 255.255.255.255"),
    ] = None,
    output: OutputOption = None,
) -> None:
    """广播扫描局域网上的米家设备，并和设备清单对上号。"""
    app_ctx = _ctx(ctx)
    found = lan_core.discover(timeout, address or lan_core.BROADCAST)
    if not found:
        render.warn("没有设备应答")
        render.info(
            "[dim]检查：和设备同一网段？路由器开了 AP 隔离？"
            "macOS 要在 系统设置 → 隐私与安全性 → 本地网络 放行终端[/dim]"
        )
        return

    registry = load_registry(app_ctx)
    by_did = {device.did: device for device in registry.devices}
    cache = app_ctx.profile.read_lan()
    cache.update(
        {
            did: {"ip": item.ip, "seen_at": int(time.time())}
            for did, item in found.items()
        }
    )
    app_ctx.profile.write_lan(cache)

    rows = []
    for did, endpoint in sorted(found.items(), key=lambda kv: kv[1].ip):
        device = by_did.get(did)
        rows.append(
            {
                "设备": device.label if device else "（不在设备清单里）",
                "房间": (device.room_name or "-") if device else "-",
                "IP": endpoint.ip,
                "did": did,
                "延迟": f"{endpoint.latency_ms:.0f}ms",
                "型号": device.model if device else "-",
            }
        )
    render.output(rows, pick_output(app_ctx, output))
    render.info(f"[dim]{len(rows)} 台应答，地址已缓存到 {app_ctx.profile.lan_path}[/dim]")


@app.command("status")
def lan_status(
    ctx: typer.Context,
    device: Annotated[str, typer.Argument(help="设备名称、别名或 did")],
    output: OutputOption = None,
) -> None:
    """看一台设备能不能直连、在哪个 IP、延迟多少。"""
    app_ctx = _ctx(ctx)
    target = resolve(app_ctx, device)
    data: dict[str, object] = {
        "设备": target.label,
        "did": target.did,
        "connect_type": target.connect_type,
        "支持直连": "是" if lan_capable(target) else "否",
    }
    if not lan_capable(target):
        data["原因"] = (
            "没有局域网 token"
            if not target.token
            else f"connect_type 不在 {sorted(LAN_CAPABLE_TYPES)} 内（走网关的设备）"
        )
        render.output(data, pick_output(app_ctx, output))
        return

    endpoint = locate(app_ctx.profile, target)
    if endpoint is None:
        data["可达"] = "否"
        data["提示"] = "同一网段？设备在线？先跑 `mi lan discover`"
    else:
        data["可达"] = "是"
        data["IP"] = endpoint.ip
        data["延迟"] = f"{endpoint.latency_ms:.0f}ms"
        data["设备时间戳"] = endpoint.stamp
    render.output(data, pick_output(app_ctx, output))


@app.command("list")
def lan_list(ctx: typer.Context, output: OutputOption = None) -> None:
    """列出设备清单里哪些设备理论上支持局域网直连。"""
    app_ctx = _ctx(ctx)
    registry = load_registry(app_ctx)
    scope = app_ctx.home_filter()
    rows = [
        {
            "设备": device.label,
            "房间": device.room_name or "-",
            "型号": device.model,
            "connect_type": device.connect_type,
            "支持直连": "是" if lan_capable(device) else "否",
            "在线": "是" if device.online else "否",
        }
        for device in registry.filter(**scope)
    ]
    rows.sort(key=lambda row: (row["支持直连"] != "是", str(row["设备"])))
    render.output(rows, pick_output(app_ctx, output))
    supported = sum(1 for row in rows if row["支持直连"] == "是")
    render.info(f"[dim]{supported} / {len(rows)} 台支持局域网直连[/dim]")
