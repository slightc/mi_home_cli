"""会话：鉴权头、自动续期、错误映射。"""
import time

import httpx
import pytest

from mi_home_cli.core.session import Session
from mi_home_cli.errors import CloudError, NotAuthenticated
from mi_home_cli.store import AuthData, Profile


def _auth(profile: Profile, *, age: int = 0, expires_in: int = 3600) -> AuthData:
    auth = AuthData(
        access_token="AT",
        refresh_token="RT",
        region="cn",
        device_id="cli.abc",
        obtained_at=int(time.time()) - age,
        expires_in=expires_in,
    )
    profile.write_auth(auth)
    return auth


def test_authorization_header_has_no_space_after_bearer(profile: Profile):
    """米家这套接口就是这么校验的，加空格会 401。"""
    _auth(profile)
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, json={"code": 0, "result": {}})

    with Session(profile, client=httpx.Client(transport=httpx.MockTransport(handler))) as s:
        s.api_post("/app/v2/home/device_list_page", {})
    assert seen["authorization"] == "BearerAT"
    assert seen["x-client-bizid"] == "haapi"


def test_token_refreshed_when_past_ratio(profile: Profile):
    _auth(profile, age=3000)  # 已经过了 70%
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/oauth/get_token"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "result": {
                        "access_token": "AT2",
                        "refresh_token": "RT2",
                        "expires_in": 3600,
                    },
                },
            )
        return httpx.Response(200, json={"code": 0, "result": {}})

    with Session(profile, client=httpx.Client(transport=httpx.MockTransport(handler))) as s:
        s.api_post("/app/v2/miotspec/prop/get", {})
    assert calls[0].endswith("/oauth/get_token")
    # 新 token 要落盘，否则下次又刷一遍
    assert profile.require_auth().access_token == "AT2"


def test_401_triggers_forced_refresh_and_retry(profile: Profile):
    _auth(profile)
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/oauth/get_token"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "result": {
                        "access_token": "AT2",
                        "refresh_token": "RT2",
                        "expires_in": 3600,
                    },
                },
            )
        if request.headers["authorization"] == "BearerAT":
            return httpx.Response(401)
        return httpx.Response(200, json={"code": 0, "result": {"ok": True}})

    with Session(profile, client=httpx.Client(transport=httpx.MockTransport(handler))) as s:
        result = s.api_post("/app/v2/miotspec/prop/get", {})
    assert result["result"] == {"ok": True}
    assert paths.count("/app/v2/miotspec/prop/get") == 2


def test_second_401_gives_up(profile: Profile):
    _auth(profile)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth/get_token"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "result": {
                        "access_token": "AT2",
                        "refresh_token": "RT2",
                        "expires_in": 3600,
                    },
                },
            )
        return httpx.Response(401)

    with Session(profile, client=httpx.Client(transport=httpx.MockTransport(handler))) as s:
        with pytest.raises(NotAuthenticated):
            s.api_post("/app/v2/miotspec/prop/get", {})


def test_non_zero_code_is_cloud_error(profile: Profile):
    _auth(profile)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": -1, "message": "bad request"})

    with Session(profile, client=httpx.Client(transport=httpx.MockTransport(handler))) as s:
        with pytest.raises(CloudError) as excinfo:
            s.api_post("/app/v2/miotspec/prop/get", {})
    assert "bad request" in str(excinfo.value)


def test_region_switches_host(profile: Profile):
    auth = AuthData(
        access_token="AT", refresh_token="RT", region="sg",
        device_id="cli.abc", obtained_at=int(time.time()), expires_in=3600,
    )
    profile.write_auth(auth)
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        return httpx.Response(200, json={"code": 0, "result": {}})

    with Session(profile, client=httpx.Client(transport=httpx.MockTransport(handler))) as s:
        s.api_get("/app/v2/home/device_list_page")
    assert hosts == ["sg.ha.api.io.mi.com"]
