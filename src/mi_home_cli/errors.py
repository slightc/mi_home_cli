"""统一错误类型与退出码。

退出码约定见 docs/design.md §5。
"""
from __future__ import annotations


class MiCliError(Exception):
    """所有可预期错误的基类，携带退出码。

    退出码由异常自己带着，cli/app.py 的 main() 负责打印并按它退出。
    （不继承 click 的异常：typer 0.27 起把 click 内置成了私有模块。）
    """

    exit_code: int = 1

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


class UsageError(MiCliError):
    exit_code = 2


class DeviceNotFound(MiCliError):
    exit_code = 3


class AmbiguousReference(MiCliError):
    exit_code = 4


class DeviceOffline(MiCliError):
    exit_code = 5


class SpecNotFound(MiCliError):
    exit_code = 6


class InvalidValue(MiCliError):
    exit_code = 7


class NetworkError(MiCliError):
    exit_code = 8


class CloudError(MiCliError):
    """云端返回了非 0 的 code。"""

    exit_code = 9

    def __init__(
        self,
        message: str,
        *,
        code: int | None = None,
        description: str | None = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(message, hint=hint)
        self.code = code
        self.description = description


class NotAuthenticated(MiCliError):
    exit_code = 10

    def __init__(
        self,
        message: str = "未登录或登录已失效",
        *,
        hint: str | None = "运行 `mi auth login` 重新登录",
    ) -> None:
        super().__init__(message, hint=hint)
