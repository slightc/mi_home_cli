"""`mi config`：默认值配置。"""
from __future__ import annotations

from typing import Annotated, Any

import typer

from .. import render
from ..core import const
from ..errors import UsageError
from ..render import OutputFormat
from ..store import read_config, write_config
from .context import AppContext, OutputOption, pick_output

app = typer.Typer(help="默认配置", no_args_is_help=True)

# 可配置的键，以及各自的校验/转换
KEYS = {
    "profile": "默认 profile",
    "region": "默认区域：" + "/".join(const.CLOUD_SERVERS),
    "output": "默认输出格式：table/json/yaml/plain",
    "home": "默认家庭（等价于 mi home use）",
    "channel": "默认控制通道：cloud / auto / lan",
}

CHANNELS = ("cloud", "auto", "lan")


def _ctx(ctx: typer.Context) -> AppContext:
    return ctx.obj


def _check_key(key: str) -> None:
    if key not in KEYS:
        raise UsageError(
            f"未知配置项 `{key}`",
            hint="可配置：" + "、".join(KEYS),
        )


def _coerce(app_ctx: AppContext, key: str, value: str) -> Any:
    if key == "region":
        if value not in const.CLOUD_SERVERS:
            raise UsageError(f"未知区域 `{value}`，可选：{'、'.join(const.CLOUD_SERVERS)}")
        return value
    if key == "output":
        try:
            return OutputFormat(value).value
        except ValueError as err:
            raise UsageError(f"未知输出格式 `{value}`") from err
    if key == "channel":
        if value not in CHANNELS:
            raise UsageError(f"未知通道 `{value}`，可选：{'、'.join(CHANNELS)}")
        return value
    if key == "home":
        # 存 id + 名字：按 id 过滤不怕改名，名字用来显示
        from .device import load_registry

        home = load_registry(app_ctx).find_home(value)
        return {"id": home["home_id"], "name": home["home_name"]}
    return value


def _display(value: Any) -> str:
    if isinstance(value, dict) and "name" in value:
        return f"{value['name']}（{value.get('id', '')}）"
    return str(value)


@app.command("list")
def config_list(ctx: typer.Context, output: OutputOption = None) -> None:
    """列出所有配置项。"""
    app_ctx = _ctx(ctx)
    config = read_config(app_ctx.root)
    rows = [
        {
            "配置项": key,
            "当前值": _display(config[key]) if key in config else "-",
            "说明": description,
        }
        for key, description in KEYS.items()
    ]
    render.output(rows, pick_output(app_ctx, output))
    render.info(f"[dim]配置文件：{app_ctx.root / 'config.json'}[/dim]")


@app.command("get")
def config_get(ctx: typer.Context, key: Annotated[str, typer.Argument()]) -> None:
    """读一个配置项。"""
    app_ctx = _ctx(ctx)
    _check_key(key)
    value = read_config(app_ctx.root).get(key)
    print(_display(value) if value is not None else "")


@app.command("set")
def config_set(
    ctx: typer.Context,
    key: Annotated[str, typer.Argument()],
    value: Annotated[str, typer.Argument()],
) -> None:
    """设置一个配置项。"""
    app_ctx = _ctx(ctx)
    _check_key(key)
    config = read_config(app_ctx.root)
    config[key] = _coerce(app_ctx, key, value)
    write_config(config, app_ctx.root)
    render.success(f"{key} = {_display(config[key])}")


@app.command("unset")
def config_unset(ctx: typer.Context, key: Annotated[str, typer.Argument()]) -> None:
    """清掉一个配置项。"""
    app_ctx = _ctx(ctx)
    _check_key(key)
    config = read_config(app_ctx.root)
    if config.pop(key, None) is None:
        render.info(f"{key} 本来就没设")
        return
    write_config(config, app_ctx.root)
    render.success(f"已清除 {key}")


@app.command("path")
def config_path(ctx: typer.Context) -> None:
    """打印配置目录。"""
    print(_ctx(ctx).root)
