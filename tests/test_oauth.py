"""OAuth 授权流程的离线测试。"""
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from mi_home_cli.core import const
from mi_home_cli.core.oauth import OAuthClient, build_auth_url, new_state
from mi_home_cli.errors import CloudError, NetworkError


def test_auth_url_contains_required_params():
    url = build_auth_url(
        redirect_url=const.DEFAULT_REDIRECT_URL,
        device_id="cli.abc",
        state="s1",
    )
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    assert parsed.netloc == "account.xiaomi.com"
    assert params["client_id"] == [const.CLIENT_ID]
    assert params["response_type"] == ["code"]
    assert params["device_id"] == ["cli.abc"]
    assert params["state"] == ["s1"]
    assert params["skip_confirm"] == ["false"]
    # host 必须是白名单里的那个，换了小米会拒
    assert params["redirect_uri"] == [
        f"http://{const.REDIRECT_HOST}:{const.REDIRECT_PORT}"
        f"{const.DEFAULT_REDIRECT_PATH}"
    ]


def test_state_is_random():
    assert new_state() != new_state()
    assert len(new_state()) == 32


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_exchange_code_success():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/app/v2/ha/oauth/get_token"
        assert "code" in request.url.params["data"]
        return httpx.Response(
            200,
            json={
                "code": 0,
                "result": {
                    "access_token": "AT",
                    "refresh_token": "RT",
                    "expires_in": 3600,
                },
            },
        )

    with OAuthClient("cn", client=_client(handler)) as client:
        token = client.exchange_code("the-code", "cli.abc")
    assert token.access_token == "AT"
    assert token.expires_in == 3600

    auth = token.to_auth("cn", "cli.abc")
    # 有效期用掉 70% 就该刷新
    assert auth.refresh_at == auth.obtained_at + 2520
    assert not auth.needs_refresh
    assert not auth.expired


def test_exchange_code_invalid_code_message():
    """小米把真正的错误藏在 message 的 JSON 字符串里，要翻出来。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "code": -6,
                "message": (
                    '{"error":96013,"error_description":'
                    '"invalid authorization code"}'
                ),
                "result": {
                    "error": 96013,
                    "error_description": "invalid authorization code",
                },
            },
        )

    with OAuthClient("cn", client=_client(handler)) as client:
        with pytest.raises(CloudError) as excinfo:
            client.exchange_code("bad", "cli.abc")
    assert "invalid authorization code" in str(excinfo.value)
    assert excinfo.value.exit_code == 9
    assert excinfo.value.hint is not None


def test_unauthorized_is_cloud_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="nope")

    with OAuthClient("cn", client=_client(handler)) as client:
        with pytest.raises(CloudError):
            client.refresh("RT")


def test_network_failure_maps_to_network_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    with OAuthClient("cn", client=_client(handler)) as client:
        with pytest.raises(NetworkError) as excinfo:
            client.refresh("RT")
    assert excinfo.value.exit_code == 8


def test_region_host_selection():
    assert const.api_host("cn") == "ha.api.io.mi.com"
    assert const.api_host("sg") == "sg.ha.api.io.mi.com"
    assert const.api_base_url("de") == "https://de.ha.api.io.mi.com"
