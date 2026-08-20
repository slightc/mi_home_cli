"""`mi home` / `mi room` / `mi device` 命令。"""
from __future__ import annotations

import time
from typing import Annotated, Optional

import typer

from .. import render
from ..core.cloud import CloudApi
from ..core.registry import Device, Registry
from ..errors import UsageError
from ..render import mask
from .context import AppContext, OutputOption, pick_output

app = typer.Typer(help="设备清单", no_args_is_help=True)
home_app = typer.Typer(help="家庭", no_args_is_help=True)
room_app = typer.Typer(help="房间", no_args_is_help=True)
alias_app = typer.Typer(help="设备别名", no_args_is_help=True)
app.add_typer(alias_app, name="alias")

# 缓存超过这个时间就在用之前提醒一句（不自动刷新，免得每次命令都慢）
STALE_AFTER = 7 * 24 * 3600


def _ctx(ctx: typer.Context) -> AppContext:
    return ctx.obj


def sync(app_ctx: AppContext) -> Registry:
    """从云端重新拉一遍清单并落盘。"""
    with app_ctx.session() as session:
        data = CloudApi(session).fetch_all()
        # uid 只有 gethome 才给，登录时拿不到，顺手补进凭据里
        if data.get("uid") and session.auth.uid != data["uid"]:
            auth = session.auth
            auth.uid = data["uid"]
            app_ctx.profile.write_auth(auth)
    registry = Registry.write(app_ctx.profile, data)
    render.info(
        f"[dim]已同步 {len(registry.devices)} 台设备、"
        f"{len(registry.homes)} 个家庭[/dim]"
    )
    return registry


def load_registry(app_ctx: AppContext, *, refresh: bool = False) -> Registry:
    """拿到设备清单：优先用缓存，没有就自动同步一次。"""
    if not refresh:
        registry = Registry.read(app_ctx.profile)
        if registry is not None:
            if registry.age > STALE_AFTER:
                render.warn(
                    f"设备缓存是 {registry.age // 86400} 天前的，"
                    "需要的话跑 `mi device sync` 刷新"
                )
            return registry
        render.info("[dim]本地还没有设备缓存，正在同步…[/dim]")
    return sync(app_ctx)


def resolve(app_ctx: AppContext, ref: str, **filters: object) -> Device:
    return load_registry(app_ctx).resolve(ref, **filters)


@home_app.command("list")
def home_list(ctx: typer.Context, output: OutputOption = None) -> None:
    """列出家庭。"""
    app_ctx = _ctx(ctx)
    registry = load_registry(app_ctx)
    counts: dict[str, int] = {}
    for device in registry.devices:
        counts[device.home_id] = counts.get(device.home_id, 0) + 1
    rows = [
        {
            "家庭": home["home_name"],
            "房间数": len(home.get("rooms") or []),
            "设备数": counts.get(home["home_id"], 0),
            "共享": "是" if home.get("shared") else "否",
            "home_id": home["home_id"],
        }
        for home in registry.homes
    ]
    render.output(rows, pick_output(app_ctx, output))


@room_app.command("list")
def room_list(
    ctx: typer.Context,
    home: Annotated[Optional[str], typer.Option("--home", help="限定家庭")] = None,
    output: OutputOption = None,
) -> None:
    """列出房间。"""
    app_ctx = _ctx(ctx)
    registry = load_registry(app_ctx)
    counts: dict[tuple[str, str], int] = {}
    for device in registry.devices:
        key = (device.home_id, device.room_id)
        counts[key] = counts.get(key, 0) + 1
    rows = []
    for item in registry.homes:
        if home and home.lower() not in item["home_name"].lower():
            continue
        for room in item.get("rooms") or []:
            rows.append(
                {
                    "家庭": item["home_name"],
                    "房间": room["room_name"],
                    "设备数": counts.get((item["home_id"], room["room_id"]), 0),
                    "room_id": room["room_id"],
                }
            )
    render.output(rows, pick_output(app_ctx, output))


@app.command("list")
def device_list(
    ctx: typer.Context,
    home: Annotated[Optional[str], typer.Option("--home", help="限定家庭")] = None,
    room: Annotated[Optional[str], typer.Option("--room", help="限定房间")] = None,
    model: Annotated[Optional[str], typer.Option("--model", help="限定型号")] = None,
    search: Annotated[
        Optional[str], typer.Option("--search", "-s", help="按名称/型号搜索")
    ] = None,
    online: Annotated[
        Optional[bool],
        typer.Option("--online/--offline", help="只看在线/离线设备"),
    ] = None,
    wide: Annotated[
        bool, typer.Option("--wide", "-w", help="多显示 did、IP、固件")
    ] = False,
    refresh: Annotated[
        bool, typer.Option("--refresh", help="先从云端刷新缓存")
    ] = False,
    output: OutputOption = None,
) -> None:
    """列出设备。"""
    app_ctx = _ctx(ctx)
    registry = load_registry(app_ctx, refresh=refresh)
    devices = registry.filter(
        home=home, room=room, model=model, online=online, search=search
    )
    fmt = pick_output(app_ctx, output)
    if fmt.value in ("json", "yaml"):
        render.output(
            [
                {
                    **device.detail(),
                    "did": device.did,
                    "token": mask(device.token),
                }
                for device in devices
            ],
            fmt,
        )
        return
    render.output([device.summary(wide=wide) for device in devices], fmt)
    if fmt.value == "table":
        render.info(f"[dim]共 {len(devices)} 台[/dim]")


@app.command("show")
def device_show(
    ctx: typer.Context,
    device: Annotated[str, typer.Argument(help="设备名称、别名或 did")],
    output: OutputOption = None,
) -> None:
    """看一台设备的详情。"""
    app_ctx = _ctx(ctx)
    target = resolve(app_ctx, device)
    render.output(target.detail(), pick_output(app_ctx, output))


@app.command("sync")
def device_sync(ctx: typer.Context) -> None:
    """从云端重新拉取家庭、房间、设备清单。"""
    app_ctx = _ctx(ctx)
    started = time.time()
    registry = sync(app_ctx)
    render.success(
        f"同步完成，{len(registry.devices)} 台设备，耗时 {time.time() - started:.1f}s"
    )


@app.command("token")
def device_token(
    ctx: typer.Context,
    device: Annotated[str, typer.Argument(help="设备名称、别名或 did")],
    show: Annotated[
        bool, typer.Option("--show-secrets", help="明文显示（默认打码）")
    ] = False,
) -> None:
    """打印设备的局域网 token。"""
    app_ctx = _ctx(ctx)
    target = resolve(app_ctx, device)
    if not target.token:
        raise UsageError(f"{target.label} 没有局域网 token")
    print(target.token if show else mask(target.token))


@alias_app.command("set")
def alias_set(
    ctx: typer.Context,
    device: Annotated[str, typer.Argument(help="设备名称或 did")],
    alias: Annotated[str, typer.Argument(help="要设置的别名")],
) -> None:
    """给设备起个短名字。"""
    app_ctx = _ctx(ctx)
    target = resolve(app_ctx, device)
    aliases = app_ctx.profile.read_aliases()
    aliases[alias] = target.did
    app_ctx.profile.write_aliases(aliases)
    render.success(f"{target.label} → 别名 `{alias}`")


@alias_app.command("list")
def alias_list(ctx: typer.Context, output: OutputOption = None) -> None:
    """列出所有别名。"""
    app_ctx = _ctx(ctx)
    registry = load_registry(app_ctx)
    by_did = {device.did: device for device in registry.devices}
    rows = [
        {
            "别名": alias,
            "设备": by_did[did].name if did in by_did else "（已不在清单里）",
            "did": did,
        }
        for alias, did in sorted(app_ctx.profile.read_aliases().items())
    ]
    render.output(rows, pick_output(app_ctx, output))


@alias_app.command("rm")
def alias_rm(ctx: typer.Context, alias: str) -> None:
    """删除别名。"""
    app_ctx = _ctx(ctx)
    aliases = app_ctx.profile.read_aliases()
    if alias not in aliases:
        raise UsageError(f"别名 `{alias}` 不存在")
    aliases.pop(alias)
    app_ctx.profile.write_aliases(aliases)
    render.success(f"已删除别名 `{alias}`")
