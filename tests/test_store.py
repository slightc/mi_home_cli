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


def test_identity_is_stable_and_ha_shaped(profile: Profile):
    identity = profile.identity("cn")
    # 形态必须和 Home Assistant 一致，否则换 token 会被判 96002
    assert identity.device_id.startswith("ha.")
    assert len(identity.device_id) == 35
    assert identity.webhook_id.isdigit()
    assert identity.redirect_url == (
        f"http://homeassistant.local:8123/api/webhook/{identity.webhook_id}"
    )
    # 同一个 profile 反复取要稳定，否则重试时参数对不上
    again = profile.identity("cn")
    assert (again.device_id, again.redirect_url) == (
        identity.device_id,
        identity.redirect_url,
    )


def test_identity_differs_per_region(profile: Profile):
    assert profile.identity("cn").device_id != profile.identity("sg").device_id
    # webhook_id 不随区域变
    assert profile.identity("cn").webhook_id == profile.identity("sg").webhook_id


def test_pending_login_roundtrip(profile: Profile):
    assert profile.read_pending() is None
    profile.write_pending({"state": "s1", "region": "cn"})
    assert profile.read_pending()["state"] == "s1"
    assert file_is_private(profile.pending_path)
    profile.clear_pending()
    assert profile.read_pending() is None


def test_clear_and_purge(profile: Profile):
    profile.write_auth(_auth())
    profile.clear_auth()
    assert not profile.auth_path.exists() and profile.path.exists()
    profile.write_auth(_auth())
    profile.purge()
    assert not profile.path.exists()
