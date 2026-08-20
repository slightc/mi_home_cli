"""`mi spec` 命令：查设备有哪些能力。"""
from __future__ import annotations

import shutil
import time
from typing import Annotated, Optional

import typer

from .. import render
from ..core.spec import DeviceSpec, SpecStore, unit_symbol
from ..render import OutputFormat
from .context import AppContext, OutputOption, pick_output
from .device import resolve

app = typer.Typer(help="设备能力（MIoT spec）", no_args_is_help=True)
cache_app = typer.Typer(help="spec 缓存", no_args_is_help=True)
app.add_typer(cache_app, name="cache")


def _ctx(ctx: typer.Context) -> AppContext:
    return ctx.obj


def load_spec(app_ctx: AppContext, target: str, *, refresh: bool = False):
    """target 可以是设备（名称/别名/did），也可以直接是 urn。"""
    store = SpecStore(app_ctx.profile, timeout=app_ctx.timeout)
    if target.startswith("urn:"):
        return None, store.get(target, refresh=refresh)
    device = resolve(app_ctx, target)
    return device, store.get(device.urn, refresh=refresh)


def describe_action_inputs(spec: DeviceSpec, action) -> str:
    """把动作的入参 piid 翻成属性名，方便用户知道该传什么。"""
    names = []
    for piid in action.in_piids:
        prop = spec.property_at(action.siid, piid)
        names.append(prop.name if prop else str(piid))
    return ", ".join(names)


def _property_rows(spec: DeviceSpec) -> list[dict[str, object]]:
    return [
        {
            "服务": prop.service,
            "属性": prop.name,
            "id": prop.ref,
            "权限": prop.access_text(),
            "类型": prop.format,
            "取值": prop.range_text(),
            "单位": unit_symbol(prop.unit) or "-",
            "说明": prop.description,
        }
        for prop in spec.properties
    ]


@app.command("show")
def spec_show(
    ctx: typer.Context,
    target: Annotated[str, typer.Argument(help="设备名称、别名、did 或 urn")],
    siid: Annotated[Optional[int], typer.Option("--siid", help="只看某个服务")] = None,
    readable: Annotated[bool, typer.Option("--readable", help="只看可读属性")] = False,
    writable: Annotated[bool, typer.Option("--writable", help="只看可写属性")] = False,
    actions: Annotated[bool, typer.Option("--actions", help="改为列出动作")] = False,
    refresh: Annotated[bool, typer.Option("--refresh", help="忽略缓存重新拉取")] = False,
    output: OutputOption = None,
) -> None:
    """列出设备的属性（或动作）。"""
    app_ctx = _ctx(ctx)
    device, spec = load_spec(app_ctx, target, refresh=refresh)
    fmt = pick_output(app_ctx, output)

    if actions:
        rows = [
            {
                "服务": action.service,
                "动作": action.name,
                "id": action.ref,
                "入参": describe_action_inputs(spec, action) or "-",
                "说明": action.description,
            }
            for action in spec.actions
            if siid is None or action.siid == siid
        ]
        render.output(rows, fmt)
        return

    rows = _property_rows(spec)
    if siid is not None:
        rows = [row for row in rows if row["id"].split(".")[0] == str(siid)]
    if readable:
        rows = [row for row in rows if "r" in row["权限"]]
    if writable:
        rows = [row for row in rows if "w" in row["权限"]]
    if fmt is OutputFormat.table:
        # 取值范围那一列在窄终端里会把表挤爆，宽度不够就截断
        width = shutil.get_terminal_size((120, 24)).columns
        if width < 140:
            for row in rows:
                text = str(row["取值"])
                if len(text) > 34:
                    row["取值"] = text[:33] + "…"
        title = f"{device.label} · {spec.description}" if device else spec.description
        render.output(rows, fmt, title=title)
        return
    render.output(rows, fmt)


@app.command("search")
def spec_search(
    ctx: typer.Context,
    target: Annotated[str, typer.Argument(help="设备名称、别名、did 或 urn")],
    keyword: Annotated[str, typer.Argument(help="关键词，匹配名称和说明")],
    output: OutputOption = None,
) -> None:
    """在一台设备的属性和动作里搜关键词。"""
    app_ctx = _ctx(ctx)
    _, spec = load_spec(app_ctx, target)
    needle = keyword.lower()
    rows: list[dict[str, object]] = []
    for prop in spec.properties:
        if needle in f"{prop.full_name} {prop.description}".lower():
            rows.append(
                {
                    "类型": "属性",
                    "名称": prop.full_name,
                    "id": prop.ref,
                    "权限": prop.access_text(),
                    "说明": prop.description,
                    "取值": prop.range_text(),
                }
            )
    for action in spec.actions:
        if needle in f"{action.full_name} {action.description}".lower():
            rows.append(
                {
                    "类型": "动作",
                    "名称": action.full_name,
                    "id": action.ref,
                    "权限": "-",
                    "说明": action.description,
                    "取值": "-",
                }
            )
    render.output(rows, pick_output(app_ctx, output))


@app.command("dump")
def spec_dump(
    ctx: typer.Context,
    target: Annotated[str, typer.Argument(help="设备名称、别名、did 或 urn")],
    output: OutputOption = None,
) -> None:
    """输出完整 spec，方便喂给脚本。"""
    app_ctx = _ctx(ctx)
    _, spec = load_spec(app_ctx, target)
    data = {
        "urn": spec.urn,
        "description": spec.description,
        "properties": [
            {
                "siid": p.siid,
                "piid": p.piid,
                "name": p.full_name,
                "description": p.description,
                "format": p.format,
                "access": p.access,
                "unit": p.unit,
                "value_range": p.value_range,
                "value_list": [
                    {"value": v.value, "description": v.description}
                    for v in p.value_list
                ],
            }
            for p in spec.properties
        ],
        "actions": [
            {
                "siid": a.siid,
                "aiid": a.aiid,
                "name": a.full_name,
                "description": a.description,
                "in": a.in_piids,
            }
            for a in spec.actions
        ],
        "events": [
            {
                "siid": e.siid,
                "eiid": e.eiid,
                "name": e.full_name,
                "description": e.description,
            }
            for e in spec.events
        ],
    }
    render.output(data, pick_output(app_ctx, output, default=OutputFormat.json))


@cache_app.command("info")
def cache_info(ctx: typer.Context, output: OutputOption = None) -> None:
    """看看缓存了哪些 spec。"""
    app_ctx = _ctx(ctx)
    spec_dir = app_ctx.profile.spec_dir
    rows = []
    for path in sorted(spec_dir.glob("*.json")) if spec_dir.is_dir() else []:
        stat = path.stat()
        rows.append(
            {
                "urn": path.stem,
                "大小": f"{stat.st_size / 1024:.0f} KB",
                "缓存于": time.strftime(
                    "%Y-%m-%d %H:%M", time.localtime(stat.st_mtime)
                ),
            }
        )
    render.output(rows, pick_output(app_ctx, output))


@cache_app.command("clear")
def cache_clear(ctx: typer.Context) -> None:
    """清空 spec 缓存。"""
    app_ctx = _ctx(ctx)
    spec_dir = app_ctx.profile.spec_dir
    count = 0
    if spec_dir.is_dir():
        for path in spec_dir.glob("*.json"):
            path.unlink()
            count += 1
    render.success(f"已清除 {count} 份 spec 缓存")
