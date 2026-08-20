"""`mi lan`：局域网直连的发现与探测。"""
from __future__ import annotations

import json
import time
from typing import Annotated, Optional

import typer

from .. import render
from ..core import lan as lan_core
from ..core.channel import (
    LAN_CAPABLE_TYPES,
    clear_lan_failures,
    lan_capable,
    lan_failure,
    locate,
)
from ..errors import MiCliError
from ..render import OutputFormat
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
    # 重新扫到了就当作新证据，清掉「这台设备局域网干不了活」的标记
    clear_lan_failures(app_ctx.profile)

    rows = []
    for did, endpoint in sorted(found.items(), key=lambda kv: kv[1].ip):
        device = by_did.get(did)
        rows.append(
            {
                "设备": device.label if device else "（不在设备清单里）",
                "房间": (device.room_name or "-") if device else "-",
                "IP": endpoint.ip,
                "did": did,
                # 广播扫描里这个数包含排队等待，不是往返延迟，别叫它「延迟」
                "应答用时": f"{endpoint.elapsed_ms:.0f}ms",
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
        data["延迟"] = (
            f"{endpoint.elapsed_ms:.0f}ms"
            if endpoint.is_rtt
            else f"{endpoint.elapsed_ms:.0f}ms（广播应答用时，非往返延迟）"
        )
        data["设备时间戳"] = endpoint.stamp
    failure = lan_failure(app_ctx.profile, target.did)
    if failure:
        failed_at, reason = failure
        data["上次局域网调用"] = (
            f"失败：{reason}（{time.strftime('%m-%d %H:%M', time.localtime(failed_at))}）"
        )
        data["说明"] = "auto 模式会直接走云端；`mi lan discover` 可清除这个标记"
    render.output(data, pick_output(app_ctx, output))


@app.command("raw")
def lan_raw(
    ctx: typer.Context,
    device: Annotated[
        str, typer.Argument(help="设备名称、别名或 did（名字带空格要加引号）")
    ],
    method: Annotated[
        str, typer.Argument(help="miIO 方法名，如 miIO.info / get_properties")
    ],
    params: Annotated[
        Optional[str], typer.Option("--params", help="参数，JSON 格式")
    ] = None,
    timeout: Annotated[float, typer.Option("--timeout", help="等待秒数")] = 5.0,
    output: OutputOption = None,
) -> None:
    """直接对设备发一条 miIO 请求。

    排查用：`miIO.info` 每台 miIO 设备都实现，能用来判断「协议通不通」和
    「这个方法设备支不支持」是两回事。
    """
    app_ctx = _ctx(ctx)
    target = resolve(app_ctx, device)
    if not target.token:
        raise MiCliError(f"{target.label} 没有局域网 token")
    endpoint = locate(app_ctx.profile, target)
    if endpoint is None:
        raise MiCliError(f"局域网上找不到 {target.label}，先跑 `mi lan discover`")

    try:
        parsed = json.loads(params) if params else []
    except json.JSONDecodeError as err:
        raise MiCliError(f"--params 不是合法 JSON：{err}") from err

    lan = lan_core.LanDevice(target.did, target.token, endpoint)
    result = lan.call(method, parsed, timeout=timeout)
    render.output(
        result if isinstance(result, (dict, list)) else {"result": result},
        pick_output(app_ctx, output, default=OutputFormat.json),
    )


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
