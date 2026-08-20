"""本地存储：权限、过期计算、device_id 持久化。"""
import json
import os
import time

import pytest

from mi_home_cli.errors import NotAuthenticated
from mi_home_cli.store import AuthData, Profile, file_is_private


def _auth(**kwargs) -> AuthData:
    base = dict(
        access_token="AT",
        refresh_token="RT",
        region="cn",
        device_id="cli.abc",
        obtained_at=int(time.time()),
        expires_in=3600,
    )
    base.update(kwargs)
    return AuthData(**base)


def test_auth_file_is_owner_only(profile: Profile):
    profile.write_auth(_auth())
    assert file_is_private(profile.auth_path)
    assert os.stat(profile.auth_path).st_mode & 0o777 == 0o600
    assert os.stat(profile.path).st_mode & 0o777 == 0o700


def test_roundtrip(profile: Profile):
    profile.write_auth(_auth(uid="123", nickname="小明"))
    loaded = profile.require_auth()
    assert loaded.access_token == "AT"
    assert loaded.uid == "123"
    assert loaded.nickname == "小明"


def test_require_auth_without_login(profile: Profile):
    with pytest.raises(NotAuthenticated) as excinfo:
        profile.require_auth()
    assert excinfo.value.exit_code == 10


def test_broken_auth_file_reports_missing_field(profile: Profile):
    profile.path.mkdir(parents=True)
    profile.auth_path.write_text(json.dumps({"access_token": "AT"}))
    with pytest.raises(NotAuthenticated):
        profile.require_auth()


def test_expiry_math():
    now = int(time.time())
    fresh = _auth(obtained_at=now, expires_in=1000)
    assert not fresh.needs_refresh and not fresh.expired

    stale = _auth(obtained_at=now - 800, expires_in=1000)
    assert stale.needs_refresh and not stale.expired  # 过了 70%，但还没到期

    dead = _auth(obtained_at=now - 2000, expires_in=1000)
    assert dead.expired


def test_device_id_is_stable(profile: Profile):
    first = profile.device_id()
    assert first.startswith("cli.")
    assert profile.device_id() == first
    # 登录之后以 auth.json 里记的为准
    profile.write_auth(_auth(device_id="cli.from-auth"))
    assert profile.device_id() == "cli.from-auth"


def test_clear_and_purge(profile: Profile):
    profile.write_auth(_auth())
    profile.clear_auth()
    assert not profile.auth_path.exists() and profile.path.exists()
    profile.write_auth(_auth())
    profile.purge()
    assert not profile.path.exists()
