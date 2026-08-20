"""命令间共享的全局上下文。"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Optional

import typer

from ..core import const
from ..core.session import Session
from ..render import OutputFormat
from ..store import Profile, config_dir, read_config


@dataclass
class AppContext:
    profile_name: str
    output: OutputFormat
    timeout: float
    verbose: bool
    quiet: bool
    region: str | None
    root: Path
    dry_run: bool = False
    all_homes: bool = False
    verify: bool = False
    # 默认走云端：局域网要先探活、失败还要回落，首次命中的等待都算在用户头上。
    # 想要低延迟的人显式选 auto/lan，或 `mi config set channel auto`。
    channel: str = "cloud"

    @property
    def profile(self) -> Profile:
        return Profile(self.profile_name, root=self.root)

    def session(self) -> Session:
        return Session(self.profile, region=self.region, timeout=self.timeout)

    def default_home(self) -> dict[str, str] | None:
        """配置里设的默认家庭，`--all-homes` 可临时忽略。"""
        if self.all_homes:
            return None
        home = read_config(self.root).get("home")
        if isinstance(home, dict) and home.get("id"):
            return home
        return None

    def home_filter(self) -> dict[str, str]:
        """喂给 Registry.filter / resolve 的家庭过滤条件。"""
        home = self.default_home()
        return {"home_id": home["id"]} if home else {}

    def resolved_region(self) -> str:
        """命令行 > 已登录的 profile > 配置文件 > cn。"""
        if self.region:
            return self.region
        auth = self.profile.read_auth()
        if auth:
            return auth.region
        return read_config(self.root).get("region", const.DEFAULT_REGION)


def default_profile_name(root: Path | None = None) -> str:
    return read_config(root or config_dir()).get("profile", "default")


# 子命令上的 -o：不写就沿用全局值。click 的分组只认子命令之前的全局参数，
# 而 `mi auth status -o json` 是更自然的写法，所以这里重复声明一次。
OutputOption = Annotated[
    Optional[OutputFormat],
    typer.Option("--output", "-o", envvar="MI_OUTPUT", help="输出格式"),
]


def pick_output(
    ctx: AppContext,
    override: OutputFormat | None,
    default: OutputFormat | None = None,
) -> OutputFormat:
    """子命令上的 -o 优先；没写就用全局值。

    default 用于「表格没意义」的命令（比如 spec dump），此时只有用户显式指定
    过全局 -o 才尊重全局值。
    """
    if override:
        return override
    if default and ctx.output is OutputFormat.table:
        return default
    return ctx.output


# 写操作的两个开关同样重复声明在子命令上。`mi set 灯 on=true --dry-run` 是任何人
# （和任何 agent）都会自然写出的形式，只能放在子命令前面属于反直觉。
DryRunOption = Annotated[
    Optional[bool],
    typer.Option("--dry-run/--no-dry-run", help="只解析和校验，不真的下发"),
]
VerifyOption = Annotated[
    Optional[bool],
    typer.Option("--verify/--no-verify", help="写入后回读确认真实状态"),
]


def apply_write_flags(
    ctx: AppContext, dry_run: bool | None = None, verify: bool | None = None
) -> AppContext:
    """子命令上的开关覆盖全局值。"""
    if dry_run is not None:
        ctx.dry_run = dry_run
    if verify is not None:
        ctx.verify = verify
    return ctx
