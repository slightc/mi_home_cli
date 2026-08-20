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

    @property
    def profile(self) -> Profile:
        return Profile(self.profile_name, root=self.root)

    def session(self) -> Session:
        return Session(self.profile, region=self.region, timeout=self.timeout)

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


def pick_output(ctx: AppContext, override: OutputFormat | None) -> OutputFormat:
    return override or ctx.output
