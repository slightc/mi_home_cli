"""语义命令：`mi light`、`mi climate`、`mi cover`、`mi fan`。

都是 `mi set` 的糖：把人话选项翻成 spec 属性再批量下发。不带任何选项时
显示这台设备的当前状态。
"""
from __future__ import annotations

from typing import Annotated, Any, Optional

import typer

from .. import render
from ..core.registry import Device
from ..core.semantic import STATUS_PROPERTIES, Planner, format_color, parse_color
from ..core.spec import DeviceSpec, format_value
from ..errors import CloudError
from .context import AppContext, OutputOption, pick_output
from .prop import (
    ACCEPTED_CODES,
    _channel,
    _explain_code,
    _target,
    read_current,
    verify_after_write,
)

DeviceArg = Annotated[str, typer.Argument(help="设备名称、别名或 did")]


def _ctx(ctx: typer.Context) -> AppContext:
    return ctx.obj


def _show_status(
    app_ctx: AppContext,
    device: Device,
    spec: DeviceSpec,
    domain: str,
    output: Any,
) -> None:
    props = [
        prop
        for name in STATUS_PROPERTIES[domain]
        if (prop := spec.by_name(name, readable=True)) is not None
    ]
    if not props:
        render.warn(f"{device.label} 没有可读的状态属性")
        return
    with app_ctx.session() as session:
        results = _channel(app_ctx, session, device).get_props(
            [{"did": device.did, "siid": p.siid, "piid": p.piid} for p in props]
        )
    by_key = {(item.get("siid"), item.get("piid")): item for item in results}
    data: dict[str, Any] = {}
    for prop in props:
        item = by_key.get((prop.siid, prop.piid), {})
        if item.get("code") != 0:
            data[prop.description or prop.name] = _explain_code(
                int(item.get("code", -1))
            )
            continue
        value = item.get("value")
        data[prop.description or prop.name] = (
            format_color(value) if prop.name == "color" else format_value(prop, value)
        )
    render.output(data, pick_output(app_ctx, output), title=f"{device.label}（{device.location}）")


def _apply(
    app_ctx: AppContext, device: Device, planner: Planner, output: Any
) -> None:
    params = planner.params(device.did)
    if app_ctx.dry_run:
        render.info("[dim]--dry-run，只解析不下发：[/dim]")
        render.output(
            [
                {
                    "属性": change.prop.full_name,
                    "id": change.prop.ref,
                    "写入值": change.value,
                }
                for change in planner.changes
            ],
            pick_output(app_ctx, output),
        )
        return

    with app_ctx.session() as session:
        api = _channel(app_ctx, session, device)
        before = read_current(
            api, device.did, [change.prop for change in planner.changes]
        )
        results = api.set_props(params)
        actual = (
            verify_after_write(
                api,
                device.did,
                [(change.prop, change.value) for change in planner.changes],
            )
            if app_ctx.verify
            else {}
        )
    by_key = {(item.get("siid"), item.get("piid")): item for item in results}
    rows = []
    failed = False

    def shown(prop: Any, value: Any) -> str:
        if value is None:
            return "-"
        return (
            format_color(value)
            if prop.name == "color"
            else format_value(prop, value)
        )

    for change in planner.changes:
        key = (change.prop.siid, change.prop.piid)
        code = int(by_key.get(key, {}).get("code", -1))
        ok = code in ACCEPTED_CODES
        failed = failed or not ok
        result = _explain_code(code)
        if ok and key in actual:
            result = (
                "成功"
                if actual[key] == change.value
                else f"已下发，回读仍是 {shown(change.prop, actual[key])}"
            )
        rows.append(
            {
                "属性": change.prop.full_name,
                "旧值": shown(change.prop, before.get(key)),
                "新值": shown(change.prop, change.value),
                "结果": result,
            }
        )
    render.output(rows, pick_output(app_ctx, output), title=device.label)
    if failed:
        raise typer.Exit(code=CloudError.exit_code)


def _switch(planner: Planner, on: bool | None) -> None:
    if on is not None:
        planner.add_raw("--on/--off", ("on",), on)


def light(
    ctx: typer.Context,
    device: DeviceArg,
    on: Annotated[
        Optional[bool], typer.Option("--on/--off", help="开灯 / 关灯")
    ] = None,
    brightness: Annotated[
        Optional[str], typer.Option("--brightness", "-b", help="亮度，一般是 1~100")
    ] = None,
    ct: Annotated[
        Optional[str], typer.Option("--ct", help="色温，单位开尔文，如 4000")
    ] = None,
    color: Annotated[
        Optional[str], typer.Option("--color", help="颜色，如 #ff8800 或 红")
    ] = None,
    mode: Annotated[
        Optional[str], typer.Option("--mode", help="模式，如 日光 / 月光")
    ] = None,
    output: OutputOption = None,
) -> None:
    """控制灯。不带选项时显示当前状态。"""
    app_ctx = _ctx(ctx)
    target, spec = _target(app_ctx, device)
    if on is None and not any((brightness, ct, color, mode)):
        _show_status(app_ctx, target, spec, "light", output)
        return

    planner = Planner(spec, target.label)
    _switch(planner, on)
    if brightness is not None:
        planner.add_parsed("--brightness", ("brightness",), brightness)
    if ct is not None:
        planner.add_parsed("--ct", ("color-temperature",), ct)
    if color is not None:
        planner.add_raw("--color", ("color",), parse_color(color))
    if mode is not None:
        planner.add_parsed("--mode", ("mode",), mode)
    _apply(app_ctx, target, planner, output)


def climate(
    ctx: typer.Context,
    device: DeviceArg,
    on: Annotated[
        Optional[bool], typer.Option("--on/--off", help="开机 / 关机")
    ] = None,
    mode: Annotated[
        Optional[str], typer.Option("--mode", help="模式，如 制冷 / 制热 / 自动")
    ] = None,
    temp: Annotated[
        Optional[str], typer.Option("--temp", "-t", help="目标温度")
    ] = None,
    fan: Annotated[
        Optional[str], typer.Option("--fan", help="风速挡位")
    ] = None,
    output: OutputOption = None,
) -> None:
    """控制空调、暖风机等温控设备。不带选项时显示当前状态。"""
    app_ctx = _ctx(ctx)
    target, spec = _target(app_ctx, device)
    if on is None and not any((mode, temp, fan)):
        _show_status(app_ctx, target, spec, "climate", output)
        return

    planner = Planner(spec, target.label)
    _switch(planner, on)
    if mode is not None:
        planner.add_parsed("--mode", ("mode",), mode)
    if temp is not None:
        planner.add_parsed("--temp", ("target-temperature",), temp)
    if fan is not None:
        planner.add_parsed("--fan", ("fan-level", "speed-level"), fan)
    _apply(app_ctx, target, planner, output)


def cover(
    ctx: typer.Context,
    device: DeviceArg,
    open_: Annotated[bool, typer.Option("--open", help="打开")] = False,
    close: Annotated[bool, typer.Option("--close", help="关闭")] = False,
    stop: Annotated[bool, typer.Option("--stop", help="停住")] = False,
    position: Annotated[
        Optional[str], typer.Option("--position", "-p", help="开合百分比 0~100")
    ] = None,
    output: OutputOption = None,
) -> None:
    """控制窗帘等开合类设备。不带选项时显示当前状态。"""
    app_ctx = _ctx(ctx)
    target, spec = _target(app_ctx, device)
    if not any((open_, close, stop, position)):
        _show_status(app_ctx, target, spec, "cover", output)
        return
    if sum((open_, close, stop)) > 1:
        raise typer.BadParameter("--open / --close / --stop 只能给一个")

    planner = Planner(spec, target.label)
    if open_:
        planner.add_enum("--open", ("motor-control",), "open", "打开", "上升")
    if close:
        planner.add_enum("--close", ("motor-control",), "close", "关闭", "下降")
    if stop:
        planner.add_enum("--stop", ("motor-control",), "pause", "stop", "暂停", "停止")
    if position is not None:
        planner.add_parsed("--position", ("target-position",), position)
    _apply(app_ctx, target, planner, output)


def fan(
    ctx: typer.Context,
    device: DeviceArg,
    on: Annotated[
        Optional[bool], typer.Option("--on/--off", help="开 / 关")
    ] = None,
    speed: Annotated[
        Optional[str], typer.Option("--speed", "-s", help="风速挡位")
    ] = None,
    mode: Annotated[Optional[str], typer.Option("--mode", help="模式")] = None,
    swing: Annotated[
        Optional[bool], typer.Option("--swing/--no-swing", help="摇头")
    ] = None,
    output: OutputOption = None,
) -> None:
    """控制风扇、净化器等带挡位的设备。不带选项时显示当前状态。"""
    app_ctx = _ctx(ctx)
    target, spec = _target(app_ctx, device)
    if on is None and swing is None and not any((speed, mode)):
        _show_status(app_ctx, target, spec, "fan", output)
        return

    planner = Planner(spec, target.label)
    _switch(planner, on)
    if speed is not None:
        planner.add_parsed("--speed", ("fan-level", "speed-level"), speed)
    if mode is not None:
        planner.add_parsed("--mode", ("mode",), mode)
    if swing is not None:
        planner.add_raw("--swing", ("horizontal-swing", "swing-mode"), swing)
    _apply(app_ctx, target, planner, output)
