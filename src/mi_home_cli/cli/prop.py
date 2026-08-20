"""`mi get` / `mi set` / `mi action` 以及 on/off/toggle。"""
from __future__ import annotations

from typing import Annotated, Any, Optional

import typer

from .. import render
from ..core.cloud import CloudApi
from ..core.registry import Device
from ..core.spec import DeviceSpec, Property, format_value, parse_value
from ..errors import (
    CloudError,
    InvalidValue,
    MiCliError,
    SpecNotFound,
    UsageError,
)
from ..render import OutputFormat
from .context import AppContext, OutputOption, pick_output
from .device import resolve
from .spec_cmd import describe_action_inputs, load_spec


def _ctx(ctx: typer.Context) -> AppContext:
    return ctx.obj


def _target(app_ctx: AppContext, ref: str) -> tuple[Device, DeviceSpec]:
    device, spec = load_spec(app_ctx, ref)
    if device is None:  # load_spec 只有传 urn 时才会是 None
        raise UsageError("这里需要一台具体设备，不能只给 urn")
    return device, spec


def read_current(
    api: CloudApi, did: str, props: list[Property]
) -> dict[tuple[int, int], Any]:
    """写之前先读一遍旧值。

    多一次请求换「旧值 → 新值」的可读性，也让误操作能照着恢复。读不到就
    算了，不因此挡住写入。
    """
    readable = [p for p in props if p.readable]
    if not readable:
        return {}
    try:
        results = api.get_props(
            [{"did": did, "siid": p.siid, "piid": p.piid} for p in readable]
        )
    except MiCliError:
        return {}
    return {
        (item["siid"], item["piid"]): item.get("value")
        for item in results
        if item.get("code") == 0 and "siid" in item and "piid" in item
    }


def _explain_code(code: int) -> str:
    """把常见的返回码翻成人话。"""
    return {
        0: "成功",
        -704010000: "设备不在线",
        -704042011: "设备不支持这个属性",
        -704220025: "属性不可写",
        -704053026: "设备拒绝（可能被童锁/物理开关限制）",
    }.get(code, f"失败（code={code}）")


def get(
    ctx: typer.Context,
    device: Annotated[str, typer.Argument(help="设备名称、别名或 did")],
    props: Annotated[
        Optional[list[str]], typer.Argument(help="属性，如 on / light.brightness / 2.1")
    ] = None,
    output: OutputOption = None,
) -> None:
    """读属性。不指定属性就读所有可读属性。"""
    app_ctx = _ctx(ctx)
    target, spec = _target(app_ctx, device)

    if props:
        wanted: list[Property] = [spec.find_property(ref) for ref in props]
        unreadable = [p.full_name for p in wanted if not p.readable]
        if unreadable:
            raise UsageError(f"这些属性不可读：{'、'.join(unreadable)}")
    else:
        wanted = [p for p in spec.properties if p.readable]
        if not wanted:
            raise SpecNotFound(f"{target.label} 没有可读属性")

    with app_ctx.session() as session:
        results = CloudApi(session).get_props(
            [{"did": target.did, "siid": p.siid, "piid": p.piid} for p in wanted]
        )

    by_key = {(item.get("siid"), item.get("piid")): item for item in results}
    fmt = pick_output(app_ctx, output)
    if fmt in (OutputFormat.json, OutputFormat.yaml):
        render.output(
            {
                p.full_name: by_key.get((p.siid, p.piid), {}).get("value")
                for p in wanted
            },
            fmt,
        )
        return
    if fmt is OutputFormat.plain and len(wanted) == 1:
        item = by_key.get((wanted[0].siid, wanted[0].piid), {})
        print(item.get("value") if item.get("value") is not None else "")
        return

    rows = []
    for prop in wanted:
        item = by_key.get((prop.siid, prop.piid), {})
        code = item.get("code", -1)
        rows.append(
            {
                "属性": prop.full_name,
                "id": prop.ref,
                "说明": prop.description,
                "值": format_value(prop, item.get("value"))
                if code == 0
                else _explain_code(int(code)),
            }
        )
    render.output(rows, fmt, title=f"{target.label}（{target.location}）")


def set_(
    ctx: typer.Context,
    device: Annotated[str, typer.Argument(help="设备名称、别名或 did")],
    assignments: Annotated[
        list[str], typer.Argument(help="属性=值，可以写多个，如 on=true brightness=60")
    ],
    output: OutputOption = None,
) -> None:
    """写属性。"""
    app_ctx = _ctx(ctx)
    target, spec = _target(app_ctx, device)

    params: list[dict[str, Any]] = []
    props: list[Property] = []
    for item in assignments:
        if "=" not in item:
            raise UsageError(f"`{item}` 不是 属性=值 的形式")
        ref, raw = item.split("=", 1)
        prop = spec.find_property(ref)
        if not prop.writable:
            raise InvalidValue(
                f"{prop.full_name} 不可写",
                hint=f"权限是 {prop.access_text()}",
            )
        value = parse_value(prop, raw)
        props.append(prop)
        params.append(
            {"did": target.did, "siid": prop.siid, "piid": prop.piid, "value": value}
        )

    if app_ctx.dry_run:
        render.info("[dim]--dry-run，只解析不下发：[/dim]")
        render.output(
            [
                {
                    "属性": prop.full_name,
                    "id": prop.ref,
                    "写入值": param["value"],
                }
                for prop, param in zip(props, params)
            ],
            pick_output(app_ctx, output),
        )
        return

    with app_ctx.session() as session:
        api = CloudApi(session)
        before = read_current(api, target.did, props)
        results = api.set_props(params)

    by_key = {(item.get("siid"), item.get("piid")): item for item in results}
    rows = []
    failed = False
    for prop, param in zip(props, params):
        code = int(by_key.get((prop.siid, prop.piid), {}).get("code", -1))
        failed = failed or code != 0
        old = before.get((prop.siid, prop.piid))
        rows.append(
            {
                "属性": prop.full_name,
                "旧值": format_value(prop, old) if old is not None else "-",
                "新值": format_value(prop, param["value"]),
                "结果": _explain_code(code),
            }
        )
    render.output(rows, pick_output(app_ctx, output), title=target.label)
    if failed:
        raise typer.Exit(code=CloudError.exit_code)


def action(
    ctx: typer.Context,
    device: Annotated[str, typer.Argument(help="设备名称、别名或 did")],
    name: Annotated[
        Optional[str], typer.Argument(help="动作名，不写则列出所有动作")
    ] = None,
    values: Annotated[
        Optional[list[str]], typer.Option("--in", help="按顺序传入参，可重复")
    ] = None,
    output: OutputOption = None,
) -> None:
    """调用动作。不指定动作名就列出这台设备支持的动作。"""
    app_ctx = _ctx(ctx)
    target, spec = _target(app_ctx, device)
    fmt = pick_output(app_ctx, output)

    if not name:
        render.output(
            [
                {
                    "动作": item.full_name,
                    "id": item.ref,
                    "入参": describe_action_inputs(spec, item) or "-",
                    "说明": item.description,
                }
                for item in spec.actions
            ],
            fmt,
            title=f"{target.label} 支持的动作",
        )
        return

    target_action = spec.find_action(name)
    raw_values = list(values or [])
    if len(raw_values) != len(target_action.in_piids):
        raise UsageError(
            f"{target_action.full_name} 需要 {len(target_action.in_piids)} 个入参"
            f"（{describe_action_inputs(spec, target_action) or '无'}），"
            f"给了 {len(raw_values)} 个",
            hint="用 --in 依次传入，顺序按上面列的入参",
        )
    parsed: list[Any] = []
    for piid, raw in zip(target_action.in_piids, raw_values):
        prop = spec.property_at(target_action.siid, piid)
        parsed.append(parse_value(prop, raw) if prop else raw)

    if app_ctx.dry_run:
        render.info(
            f"[dim]--dry-run：{target.label} {target_action.full_name}"
            f"（{target_action.ref}）入参 {parsed}[/dim]"
        )
        return

    with app_ctx.session() as session:
        result = CloudApi(session).call_action(
            target.did, target_action.siid, target_action.aiid, parsed
        )
    code = int(result.get("code", -1))
    render.output(
        {
            "设备": target.label,
            "动作": target_action.full_name,
            "结果": _explain_code(code),
            "返回": result.get("out") or "-",
        },
        fmt,
    )
    if code != 0:
        raise typer.Exit(code=CloudError.exit_code)


def _switch(app_ctx: AppContext, device: str, value: bool | None) -> None:
    """on / off / toggle 共用：找到这台设备的开关属性再写。"""
    target, spec = _target(app_ctx, device)
    candidates = [
        prop
        for prop in spec.properties
        if prop.name == "on" and prop.writable and prop.format == "bool"
    ]
    if not candidates:
        raise SpecNotFound(
            f"{target.label} 没有可写的开关属性",
            hint="用 `mi spec show` 看看它支持什么，再用 `mi set`",
        )
    # 多个服务都有 on 时（比如带夜灯的灯），取 siid 最小的主服务
    prop = min(candidates, key=lambda p: (p.siid, p.piid))
    with app_ctx.session() as session:
        api = CloudApi(session)
        if value is None:
            current = api.get_props(
                [{"did": target.did, "siid": prop.siid, "piid": prop.piid}]
            )
            if not current or current[0].get("code") != 0:
                raise CloudError(f"读不到 {target.label} 的当前开关状态")
            value = not bool(current[0].get("value"))
        results = api.set_props(
            [
                {
                    "did": target.did,
                    "siid": prop.siid,
                    "piid": prop.piid,
                    "value": value,
                }
            ]
        )
    code = int(results[0].get("code", -1)) if results else -1
    if code != 0:
        render.error(f"{target.label} {'开' if value else '关'}失败：{_explain_code(code)}")
        raise typer.Exit(code=CloudError.exit_code)
    render.success(f"{target.label} 已{'打开' if value else '关闭'}")


def on(
    ctx: typer.Context,
    device: Annotated[str, typer.Argument(help="设备名称、别名或 did")],
) -> None:
    """打开设备。"""
    _switch(_ctx(ctx), device, True)


def off(
    ctx: typer.Context,
    device: Annotated[str, typer.Argument(help="设备名称、别名或 did")],
) -> None:
    """关闭设备。"""
    _switch(_ctx(ctx), device, False)


def toggle(
    ctx: typer.Context,
    device: Annotated[str, typer.Argument(help="设备名称、别名或 did")],
) -> None:
    """切换开关。"""
    _switch(_ctx(ctx), device, None)
