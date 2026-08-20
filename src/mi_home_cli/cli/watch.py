"""`mi watch`：盯着设备的实时变化。

走云端 MQTT 长连接，不是轮询——属性一变、事件一发、设备上下线，立刻出一行。
"""
from __future__ import annotations

import json
import queue
import time
from typing import Annotated, Any, Optional

import typer

from .. import render
from ..core.mqtt import CloudMqtt, Message, topic_events, topic_props, topic_state
from ..core.registry import Device
from ..core.spec import DeviceSpec, SpecStore, format_value
from ..errors import UsageError
from ..render import OutputFormat
from .context import AppContext, OutputOption, pick_output
from .device import load_registry


def _ctx(ctx: typer.Context) -> AppContext:
    return ctx.obj


def _load_specs(
    app_ctx: AppContext, devices: list[Device]
) -> dict[str, DeviceSpec]:
    """把涉及到的 spec 都准备好，用来把 siid/piid 翻成属性名。

    第一次会逐个去 miot-spec.org 拉，之后走缓存。拉不到的设备退化成显示
    裸的 siid.piid，不影响 watch 本身。
    """
    specs: dict[str, DeviceSpec] = {}
    urns = {device.urn for device in devices}
    store = SpecStore(app_ctx.profile, timeout=app_ctx.timeout)
    missing = [
        urn for urn in urns if not store._cache_path(urn).exists()  # noqa: SLF001
    ]
    if missing:
        render.info(f"[dim]首次运行，正在拉取 {len(missing)} 份 spec…[/dim]")
    for urn in urns:
        try:
            specs[urn] = store.get(urn)
        except Exception as err:  # 单个 spec 挂了不该拖垮整个 watch
            render.warn(f"spec 获取失败（{urn}）：{err}")
    return specs


def _describe(
    message: Message,
    device: Device,
    spec: DeviceSpec | None,
    previous: dict[tuple[str, int, int], Any],
) -> dict[str, Any] | None:
    """把一条推送变成可展示的一行；被过滤掉时返回 None。"""
    stamp = time.strftime("%H:%M:%S")
    base = {
        "时间": stamp,
        "设备": device.label,
        "房间": device.room_name or "-",
    }
    if message.kind == "state":
        return {
            **base,
            "类型": "上下线",
            "名称": "-",
            "变化": "上线" if message.state == "online" else "离线",
            "_json": {"kind": "state", "state": message.state},
        }
    if message.kind == "event":
        event = None
        if spec:
            event = next(
                (
                    item
                    for item in spec.events
                    if item.siid == message.siid and item.eiid == message.iid
                ),
                None,
            )
        name = event.full_name if event else f"{message.siid}.{message.iid}"
        return {
            **base,
            "类型": "事件",
            "名称": event.description if event else name,
            "变化": json.dumps(message.arguments, ensure_ascii=False)
            if message.arguments
            else "-",
            "_json": {
                "kind": "event",
                "event": name,
                "arguments": message.arguments,
            },
        }

    prop = (
        spec.property_at(message.siid, message.iid)
        if spec and message.siid is not None and message.iid is not None
        else None
    )
    name = prop.full_name if prop else f"{message.siid}.{message.iid}"
    key = (device.did, message.siid or -1, message.iid or -1)
    old = previous.get(key)
    previous[key] = message.value
    shown_new = format_value(prop, message.value) if prop else str(message.value)
    shown_old = (
        (format_value(prop, old) if prop else str(old)) if old is not None else None
    )
    return {
        **base,
        "类型": "属性",
        "名称": prop.description if prop else name,
        "变化": f"{shown_old} → {shown_new}" if shown_old is not None else shown_new,
        "_json": {
            "kind": "prop",
            "property": name,
            "value": message.value,
            "old": old,
        },
    }


def watch(
    ctx: typer.Context,
    devices: Annotated[
        Optional[list[str]], typer.Argument(help="要盯的设备，不给就盯全部")
    ] = None,
    props: Annotated[
        Optional[list[str]],
        typer.Option("--prop", help="只看这些属性，可重复，如 --prop on"),
    ] = None,
    events: Annotated[
        bool, typer.Option("--events/--no-events", help="是否包含事件")
    ] = True,
    state: Annotated[
        bool, typer.Option("--state/--no-state", help="是否包含上下线")
    ] = True,
    exit_after: Annotated[
        Optional[int], typer.Option("--exit-after", help="收到 N 条后退出")
    ] = None,
    duration: Annotated[
        Optional[float], typer.Option("--duration", help="盯多少秒后退出")
    ] = None,
    output: OutputOption = None,
) -> None:
    """实时盯设备的属性变化、事件和上下线。Ctrl-C 退出。"""
    app_ctx = _ctx(ctx)
    registry = load_registry(app_ctx)
    scope = app_ctx.home_filter()

    if devices:
        targets = [registry.resolve(ref, **scope) for ref in devices]
    else:
        targets = registry.filter(**scope)
    if not targets:
        raise UsageError("没有可盯的设备")

    specs = _load_specs(app_ctx, targets)
    by_did = {device.did: device for device in targets}
    fmt = pick_output(app_ctx, output)
    wanted_props = {name.lower() for name in (props or [])}

    topics: list[str] = []
    for device in targets:
        topics.append(topic_props(device.did))
        if events:
            topics.append(topic_events(device.did))
        if state and not device.did.startswith(("blt.", "proxy.")):
            # 蓝牙和网关子设备云端不发上下线，订了也没用
            topics.append(topic_state(device.did))

    with app_ctx.session() as session:
        identity = app_ctx.profile.identity(session.region)
        # 先确保 token 是新的，长连接期间也用它作为密码
        session.access_token()
        client = CloudMqtt(
            region=session.region,
            client_id=identity.device_id,
            token_provider=session.access_token,
            debug=app_ctx.verbose,
            on_note=lambda text: render.info(f"[dim]{text}[/dim]"),
            on_state_change=lambda ok: render.info(
                "[dim]已连接[/dim]" if ok else "[yellow]连接断开，正在重连…[/yellow]"
            ),
        )
        client.subscribe(topics)
        render.info(
            f"[dim]盯着 {len(targets)} 台设备"
            f"（{len(topics)} 个订阅），Ctrl-C 退出[/dim]"
        )
        previous: dict[tuple[str, int, int], Any] = {}
        seen = 0
        deadline = time.monotonic() + duration if duration else None
        try:
            client.start()
            while True:
                if deadline and time.monotonic() >= deadline:
                    break
                try:
                    message = client.messages.get(timeout=0.5)
                except queue.Empty:
                    continue
                device = by_did.get(message.did)
                if device is None:
                    continue
                row = _describe(message, device, specs.get(device.urn), previous)
                if row is None:
                    continue
                if wanted_props and row["_json"].get("kind") == "prop":
                    name = str(row["_json"]["property"]).lower()
                    if not any(
                        item == name or item == name.split(".")[-1]
                        for item in wanted_props
                    ):
                        continue
                _emit(row, fmt, device)
                seen += 1
                if exit_after and seen >= exit_after:
                    break
        except KeyboardInterrupt:
            render.info("")
        finally:
            client.stop()
        render.info(f"[dim]共 {seen} 条[/dim]")


def _emit(row: dict[str, Any], fmt: OutputFormat, device: Device) -> None:
    """流式输出：一条一行，表格在这里没意义。"""
    payload = row.pop("_json")
    if fmt in (OutputFormat.json, OutputFormat.yaml):
        print(
            json.dumps(
                {
                    "ts": int(time.time()),
                    "time": row["时间"],
                    "did": device.did,
                    "device": device.label,
                    "room": device.room_name,
                    **payload,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return
    if fmt is OutputFormat.plain:
        print(
            f"{row['时间']}\t{device.label}\t{row['名称']}\t{row['变化']}",
            flush=True,
        )
        return
    icon = {"属性": "·", "事件": "!", "上下线": "~"}.get(str(row["类型"]), "·")
    render.stream(
        f"[dim]{row['时间']}[/dim] {icon} [cyan]{row['设备']}[/cyan]"
        f" [dim]{row['房间']}[/dim] {row['名称']}  {row['变化']}"
    )
