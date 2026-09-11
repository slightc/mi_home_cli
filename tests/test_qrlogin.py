"""扫码登录的离线测试：整条 HTTP 链路用 httpx.MockTransport 回放。"""
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from mi_home_cli.core import const
from mi_home_cli.core.oauth import state_for_device
from mi_home_cli.core.qrlogin import QrChallenge, QrLoginClient, render_qr
from mi_home_cli.errors import CloudError, MiCliError, NetworkError

REDIRECT = const.redirect_url("1234567890")
DEVICE_ID = "ha.abcdef0123456789abcdef0123456789"
STATE = state_for_device(DEVICE_ID)

# serviceLogin?_json=true 的最小可用响应（真实响应字段更多）。
SERVICE_LOGIN = {
    "code": 70016,  # 未登录，正常
    "description": "登录验证失败",
    "_sign": "2&V1_oauth2.0&SIGN=",
    "sid": "oauth2.0",
    "qs": "%3Fcallback%3Dxxx",
    "callback": "https://account.xiaomi.com/sts/oauth?sign=x&followup=y&sid=oauth2.0",
    "serviceParam": '{"checkSafePhone":false}',
}
LOGIN_URL = "https://sgp.account.xiaomi.com/longPolling/login?ticket=lp_abc&dc=sgp"
LP_URL = "https://sgp.lp.account.xiaomi.com/lp/s?k=lp_abc"
LONG_POLLING = {
    "code": 0,
    "result": "ok",
    "loginUrl": LOGIN_URL,
    "lp": LP_URL,
    "qr": "https://account.xiaomi.com/pass/qr/login?ticket=lp_abc",
    "timeout": 300,
}
# 确认后跳转链：sts → authorize → redirect_uri?code=...
STS_LOCATION = "https://account.xiaomi.com/sts?ticket=abc&followup=z"
AUTHORIZE_BACK = "https://account.xiaomi.com/oauth2/authorize?resume=1"
FINAL_REDIRECT = f"{REDIRECT}?code=THE-CODE&state={STATE}"


def _client(handler) -> httpx.Client:
    # 复刻真实用法：不自动跟随跳转，我们自己解析每一跳。
    return httpx.Client(
        transport=httpx.MockTransport(handler), follow_redirects=False
    )


def _qr(handler) -> QrLoginClient:
    return QrLoginClient(
        redirect_url=REDIRECT,
        device_id=DEVICE_ID,
        state=STATE,
        client=_client(handler),
    )


def test_start_builds_challenge():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/oauth2/authorize":
            return httpx.Response(
                302, headers={"location": "https://account.xiaomi.com/pass/serviceLogin?sid=oauth2.0"}
            )
        if path == "/pass/serviceLogin":
            assert request.url.params.get("_json") == "true"
            return httpx.Response(200, json=SERVICE_LOGIN)
        if path == "/longPolling/loginUrl":
            # serviceLogin 拿到的登录参数要原样带上
            assert request.url.params["_sign"] == SERVICE_LOGIN["_sign"]
            assert request.url.params["sid"] == "oauth2.0"
            assert request.url.params["callback"] == SERVICE_LOGIN["callback"]
            return httpx.Response(200, json=LONG_POLLING)
        raise AssertionError(f"未预期的请求：{path}")

    with _qr(handler) as client:
        challenge = client.start()
    assert challenge.login_url == LOGIN_URL
    assert challenge.lp_url == LP_URL
    assert challenge.timeout == 300


def test_start_strips_json_prefix():
    import json as _json

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/oauth2/authorize":
            return httpx.Response(302, headers={"location": "https://account.xiaomi.com/pass/serviceLogin"})
        if path == "/pass/serviceLogin":
            # passport 常在 JSON 前塞一段防劫持前缀
            body = const.XIAOMI_JSON_PREFIX + _json.dumps(SERVICE_LOGIN)
            return httpx.Response(200, text=body)
        if path == "/longPolling/loginUrl":
            return httpx.Response(200, text=const.XIAOMI_JSON_PREFIX + _json.dumps(LONG_POLLING))
        raise AssertionError(path)

    with _qr(handler) as client:
        challenge = client.start()
    assert challenge.login_url == LOGIN_URL


def test_start_missing_login_redirect():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="just a page, no redirect")

    with _qr(handler) as client:
        with pytest.raises(CloudError) as excinfo:
            client.start()
    assert "登录页" in str(excinfo.value)


def test_start_missing_sign_field():
    incomplete = {k: v for k, v in SERVICE_LOGIN.items() if k != "_sign"}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/authorize":
            return httpx.Response(302, headers={"location": "https://account.xiaomi.com/pass/serviceLogin"})
        if request.url.path == "/pass/serviceLogin":
            return httpx.Response(200, json=incomplete)
        raise AssertionError(request.url.path)

    with _qr(handler) as client:
        with pytest.raises(CloudError) as excinfo:
            client.start()
    assert "_sign" in str(excinfo.value)


def _challenge() -> QrChallenge:
    return QrChallenge(
        login_url=LOGIN_URL, lp_url=LP_URL, qr_image_url="", timeout=300
    )


def test_poll_returns_location_on_confirm():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "location": STS_LOCATION})

    with _qr(handler) as client:
        location = client.poll(_challenge(), deadline=_deadline(), poll_interval=0)
    assert location == STS_LOCATION


def test_poll_waits_through_intermediate_state():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            # 已扫未确认：code=0 但还没有 location
            return httpx.Response(200, json={"code": 0, "result": "ok"})
        return httpx.Response(200, json={"code": 0, "location": STS_LOCATION})

    with _qr(handler) as client:
        location = client.poll(_challenge(), deadline=_deadline(), poll_interval=0)
    assert location == STS_LOCATION
    assert calls["n"] == 2


def test_poll_reports_error_code():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 70003, "desc": "二维码已失效"})

    with _qr(handler) as client:
        with pytest.raises(CloudError) as excinfo:
            client.poll(_challenge(), deadline=_deadline(), poll_interval=0)
    assert "二维码已失效" in str(excinfo.value)


def test_poll_timeout():
    def handler(request: httpx.Request) -> httpx.Response:
        # 一直是中间态，等到 deadline
        return httpx.Response(200, json={"code": 0})

    with _qr(handler) as client:
        with pytest.raises(MiCliError) as excinfo:
            # deadline 已过，poll 一进去就超时
            client.poll(_challenge(), deadline=0.0, poll_interval=0)
    assert "超时" in str(excinfo.value)


def test_resolve_code_follows_chain_to_redirect():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "account.xiaomi.com" and request.url.path == "/sts":
            return httpx.Response(302, headers={"location": AUTHORIZE_BACK})
        if request.url.path == "/oauth2/authorize":
            return httpx.Response(302, headers={"location": FINAL_REDIRECT})
        raise AssertionError(f"不该请求 {request.url}")

    with _qr(handler) as client:
        result = client.resolve_code(STS_LOCATION)
    assert result.code == "THE-CODE"
    assert result.state == STATE


def test_resolve_code_does_not_request_redirect_host():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        if request.url.path == "/sts":
            return httpx.Response(302, headers={"location": FINAL_REDIRECT})
        raise AssertionError(f"不该请求 {request.url}")

    with _qr(handler) as client:
        client.resolve_code(STS_LOCATION)
    # homeassistant.local 那一跳只解析、不真正请求
    assert const.REDIRECT_HOST not in seen


def test_resolve_code_rejects_state_mismatch():
    bad = f"{REDIRECT}?code=X&state=WRONG"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/sts":
            return httpx.Response(302, headers={"location": bad})
        raise AssertionError(request.url)

    with _qr(handler) as client:
        with pytest.raises(MiCliError) as excinfo:
            client.resolve_code(STS_LOCATION)
    assert "state" in str(excinfo.value)


def test_resolve_code_dead_ends_without_code():
    def handler(request: httpx.Request) -> httpx.Response:
        # 停在一个没有再跳转的页面（比如需要手机确认的授权页）
        return httpx.Response(200, text="<html>consent</html>")

    with _qr(handler) as client:
        with pytest.raises(CloudError) as excinfo:
            client.resolve_code(STS_LOCATION)
    assert "手机" in str(excinfo.value)


def test_network_error_wrapped():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    with _qr(handler) as client:
        with pytest.raises(NetworkError):
            client.start()


def test_render_qr_returns_scannable_block():
    out = render_qr("https://example.com/scan")
    # segno 在依赖里，应当能画出来
    assert out is not None
    # 上半块 ▀，前景=上模块 / 背景=下模块，黑白显式着色（不反色）
    assert "▀" in out
    # 暗模块用黑（30/40），亮模块用白（37/47），四种组合都可能出现
    assert "\x1b[30;40m" in out or "\x1b[30;47m" in out
    assert "\x1b[37;47m" in out or "\x1b[37;40m" in out
    # 半块把高度折半：每行的可见模块列数 ≈ 行数 * 2（二维码是方阵）
    import re

    lines = out.splitlines()
    cols = {len(re.sub(r"\x1b\[[0-9;]*m", "", ln)) for ln in lines}
    assert len(cols) == 1  # 每行等宽
    width = cols.pop()
    assert abs(width - len(lines) * 2) <= 2


def test_render_qr_returns_none_when_too_wide(monkeypatch):
    import os

    # 终端只有 10 列，二维码宽度必然超出 → 宁可不画（否则换行折断扫不出）
    monkeypatch.setattr(
        "shutil.get_terminal_size", lambda fallback=(80, 24): os.terminal_size((10, 24))
    )
    assert render_qr("https://example.com/some/long/enough/scan/url") is None


def test_render_qr_ok_when_wide_enough(monkeypatch):
    import os

    monkeypatch.setattr(
        "shutil.get_terminal_size", lambda fallback=(80, 24): os.terminal_size((200, 50))
    )
    assert render_qr("https://example.com/scan") is not None


def _deadline() -> float:
    import time

    return time.monotonic() + 30
