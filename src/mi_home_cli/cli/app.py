"""CLI 入口。"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated, Optional

import typer

from .. import __version__, render
from ..core import const
from ..errors import MiCliError
from ..render import OutputFormat
from ..store import config_dir, read_config
from . import auth as auth_cli
from . import config as config_cli
from . import device as device_cli
from . import prop as prop_cli
from . import semantic as semantic_cli
from . import spec_cmd as spec_cli
from . import doctor as doctor_cli
from .context import AppContext, OutputOption, default_profile_name

app = typer.Typer(
    help="命令行控制米家设备",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)
app.add_typer(auth_cli.app, name="auth")
app.add_typer(auth_cli.profile_app, name="profile")
app.add_typer(device_cli.app, name="device")
app.add_typer(device_cli.home_app, name="home")
app.add_typer(device_cli.room_app, name="room")
app.add_typer(spec_cli.app, name="spec")
app.add_typer(config_cli.app, name="config")

# 高频命令挂在顶层：mi get / mi set / mi action / mi on|off|toggle
app.command("get")(prop_cli.get)
app.command("set")(prop_cli.set_)
app.command("action")(prop_cli.action)
app.command("on")(prop_cli.on)
app.command("off")(prop_cli.off)
app.command("toggle")(prop_cli.toggle)
app.command("light")(semantic_cli.light)
app.command("climate")(semantic_cli.climate)
app.command("cover")(semantic_cli.cover)
app.command("fan")(semantic_cli.fan)


@app.callback()
def main_callback(
    ctx: typer.Context,
    profile: Annotated[
        Optional[str],
        typer.Option("--profile", "-p", envvar="MI_PROFILE", help="使用哪个 profile"),
    ] = None,
    region: Annotated[
        Optional[str],
        typer.Option(
            "--region", "-r", envvar="MI_REGION",
            help="区域：" + "/".join(const.CLOUD_SERVERS),
        ),
    ] = None,
    output: Annotated[
        Optional[OutputFormat],
        typer.Option("--output", "-o", envvar="MI_OUTPUT", help="输出格式"),
    ] = None,
    timeout: Annotated[
        float, typer.Option("--timeout", help="单次请求超时（秒）")
    ] = float(const.HTTP_TIMEOUT),
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
    quiet: Annotated[bool, typer.Option("--quiet", "-q")] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="只解析和校验，不真的下发")
    ] = False,
    all_homes: Annotated[
        bool, typer.Option("--all-homes", help="忽略默认家庭，跨所有家庭操作")
    ] = False,
) -> None:
    root: Path = config_dir()
    config = read_config(root)
    ctx.obj = AppContext(
        profile_name=profile or default_profile_name(root),
        output=output or OutputFormat(config.get("output", "table")),
        timeout=timeout,
        verbose=verbose,
        quiet=quiet,
        region=region or config.get("region"),
        root=root,
        dry_run=dry_run,
        all_homes=all_homes,
    )


@app.command()
def version() -> None:
    """打印版本。"""
    print(f"mi-home-cli {__version__}")


@app.command()
def doctor(ctx: typer.Context, output: OutputOption = None) -> None:
    """检查登录所需的环境（端口、域名解析、网络、时钟）。"""
    doctor_cli.run(ctx, output)


def main() -> None:
    try:
        app()
    except MiCliError as err:
        render.error(err.message)
        if err.hint:
            render.info(f"[dim]提示：{err.hint}[/dim]")
        sys.exit(err.exit_code)
    except KeyboardInterrupt:
        render.info("\n已取消")
        sys.exit(130)


if __name__ == "__main__":
    main()
