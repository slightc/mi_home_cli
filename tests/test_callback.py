"""回调解析与本地回调服务的测试。"""
import queue

import httpx
import pytest

from mi_home_cli.core.callback import CallbackServer, parse_pasted, port_available
from mi_home_cli.errors import UsageError


def test_parse_full_url():
    result = parse_pasted(
        "http://homeassistant.local:8123/mi-home-cli/callback?code=abc&state=s1",
        expected_state="s1",
    )
    assert (result.code, result.state, result.source) == ("abc", "s1", "paste")


def test_parse_query_only():
    assert parse_pasted("?code=abc&state=s1").code == "abc"
    assert parse_pasted("code=abc&state=s1").state == "s1"


def test_parse_bare_code():
    result = parse_pasted("  abc  ")
    assert result.code == "abc"
    assert result.state is None


def test_parse_strips_quotes():
    assert parse_pasted('"code=abc"').code == "abc"


def test_state_mismatch_rejected():
    with pytest.raises(UsageError):
        parse_pasted("code=abc&state=other", expected_state="s1")


def test_url_without_code_rejected():
    with pytest.raises(UsageError):
        parse_pasted("http://homeassistant.local:8123/x?error=access_denied")


def test_empty_input_rejected():
    with pytest.raises(UsageError):
        parse_pasted("   ")


def test_garbage_rejected():
    with pytest.raises(UsageError):
        parse_pasted("not a url at all")


def test_callback_server_captures_code():
    result_queue: "queue.Queue[object]" = queue.Queue()
    port = 18123
    assert port_available(port)
    server = CallbackServer(result_queue, bind_host="127.0.0.1", port=port)
    with server:
        response = httpx.get(
            f"http://127.0.0.1:{port}/mi-home-cli/callback",
            params={"code": "abc", "state": "s1"},
        )
    assert response.status_code == 200
    assert "登录成功" in response.text
    result = result_queue.get_nowait()
    assert (result.code, result.state, result.source) == ("abc", "s1", "server")


def test_callback_server_rejects_request_without_code():
    result_queue: "queue.Queue[object]" = queue.Queue()
    port = 18124
    server = CallbackServer(result_queue, bind_host="127.0.0.1", port=port)
    with server:
        response = httpx.get(f"http://127.0.0.1:{port}/x?error=denied")
    assert response.status_code == 400
    assert result_queue.empty()
