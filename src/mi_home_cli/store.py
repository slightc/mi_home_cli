"""本地配置与凭据存储。

目录结构见 docs/design.md §3.1。含密文件一律 0600、目录 0700。
"""
from __future__ import annotations

import json
import os
import stat
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .core.const import DEFAULT_REGION, TOKEN_REFRESH_RATIO
from .errors import MiCliError, NotAuthenticated

ENV_CONFIG_DIR = "MI_HOME_CONFIG_DIR"
APP_DIR_NAME = "mi-home-cli"


def config_dir() -> Path:
    """配置根目录，可用 MI_HOME_CONFIG_DIR 覆盖。"""
    override = os.environ.get(ENV_CONFIG_DIR)
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME")
    if base:
        return Path(base).expanduser() / APP_DIR_NAME
    return Path.home() / ".config" / APP_DIR_NAME


def _write_private_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_suffix(path.suffix + ".tmp")
    # 先以 0600 建文件再写，避免内容短暂可读。
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fp:
        json.dump(data, fp, ensure_ascii=False, indent=2)
        fp.write("\n")
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        with path.open(encoding="utf-8") as fp:
            return json.load(fp)
    except (OSError, json.JSONDecodeError) as err:
        raise MiCliError(f"读取 {path} 失败：{err}") from err


def file_is_private(path: Path) -> bool:
    """文件是否只有属主可读写。"""
    if not path.exists():
        return True
    mode = path.stat().st_mode
    return not mode & (stat.S_IRWXG | stat.S_IRWXO)


@dataclass
class AuthData:
    """一个 profile 的登录态。"""

    access_token: str
    refresh_token: str
    region: str
    device_id: str
    obtained_at: int
    expires_in: int
    uid: str | None = None
    nickname: str | None = None

    @property
    def expires_at(self) -> int:
        return self.obtained_at + self.expires_in

    @property
    def refresh_at(self) -> int:
        """到这个时间点就该刷新了。"""
        return self.obtained_at + int(self.expires_in * TOKEN_REFRESH_RATIO)

    @property
    def expired(self) -> bool:
        return time.time() >= self.expires_at

    @property
    def needs_refresh(self) -> bool:
        return time.time() >= self.refresh_at

    def dump(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "region": self.region,
            "device_id": self.device_id,
            "obtained_at": self.obtained_at,
            "expires_in": self.expires_in,
            "uid": self.uid,
            "nickname": self.nickname,
        }

    @staticmethod
    def load(data: dict[str, Any]) -> "AuthData":
        try:
            return AuthData(
                access_token=data["access_token"],
                refresh_token=data["refresh_token"],
                region=data.get("region", DEFAULT_REGION),
                device_id=data.get("device_id", ""),
                obtained_at=int(data.get("obtained_at", 0)),
                expires_in=int(data.get("expires_in", 0)),
                uid=data.get("uid"),
                nickname=data.get("nickname"),
            )
        except KeyError as err:
            raise NotAuthenticated(f"登录信息不完整，缺少 {err}") from err


@dataclass
class Profile:
    """一个账号/区域配置。"""

    name: str
    root: Path = field(default_factory=config_dir)

    @property
    def path(self) -> Path:
        return self.root / "profiles" / self.name

    @property
    def auth_path(self) -> Path:
        return self.path / "auth.json"

    @property
    def devices_path(self) -> Path:
        return self.path / "devices.json"

    @property
    def spec_dir(self) -> Path:
        return self.path / "spec"

    def exists(self) -> bool:
        return self.auth_path.exists()

    def read_auth(self) -> AuthData | None:
        data = _read_json(self.auth_path)
        if data is None:
            return None
        return AuthData.load(data)

    def require_auth(self) -> AuthData:
        auth = self.read_auth()
        if auth is None:
            raise NotAuthenticated(f"profile `{self.name}` 尚未登录")
        return auth

    def write_auth(self, auth: AuthData) -> None:
        _write_private_json(self.auth_path, auth.dump())

    def clear_auth(self) -> None:
        self.auth_path.unlink(missing_ok=True)

    def purge(self) -> None:
        import shutil

        if self.path.exists():
            shutil.rmtree(self.path)

    def device_id(self) -> str:
        """本机在小米 OAuth 侧的设备标识，首次使用时生成并固定下来。

        换 device_id 不影响登录，但会让小米侧多出一条设备记录，所以持久化。
        """
        auth = self.read_auth()
        if auth and auth.device_id:
            return auth.device_id
        marker = self.path / "device_id"
        if marker.exists():
            return marker.read_text(encoding="utf-8").strip()
        device_id = f"cli.{uuid.uuid4().hex}"
        self.path.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path, 0o700)
        marker.write_text(device_id + "\n", encoding="utf-8")
        return device_id


def list_profiles(root: Path | None = None) -> list[str]:
    base = (root or config_dir()) / "profiles"
    if not base.is_dir():
        return []
    return sorted(p.name for p in base.iterdir() if p.is_dir())


def read_config(root: Path | None = None) -> dict[str, Any]:
    return _read_json((root or config_dir()) / "config.json") or {}


def write_config(data: dict[str, Any], root: Path | None = None) -> None:
    _write_private_json((root or config_dir()) / "config.json", data)
