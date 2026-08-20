"""小米账号 OAuth2 授权码流程。

流程：
  1. 打开 account.xiaomi.com/oauth2/authorize（带 redirect_uri / client_id /
     device_id / state），用户在浏览器里登录并授权；
  2. 浏览器被重定向到 redirect_uri?code=...&state=...；
  3. 拿 code 到 {api_host}/app/v2/ha/oauth/get_token 换 access_token；
  4. 过期前用 refresh_token 走同一个接口续期。
"""
from __future__ import annotations

import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlencode

import httpx

from ..errors import CloudError, NetworkError
from ..store import AuthData
from . import const


@dataclass
class TokenResult:
    access_token: str
    refresh_token: str
    expires_in: int
    obtained_at: int

    def to_auth(
        self, region: str, device_id: str, *, uid: str | None = None,
        nickname: str | None = None,
    ) -> AuthData:
        return AuthData(
            access_token=self.access_token,
            refresh_token=self.refresh_token,
            region=region,
            device_id=device_id,
            obtained_at=self.obtained_at,
            expires_in=self.expires_in,
            uid=uid,
            nickname=nickname,
        )


def new_state() -> str:
    return secrets.token_hex(16)


def state_for_device(device_id: str) -> str:
    """按 Home Assistant 米家集成的算法生成 state。

    state 本身只用于校验回调是不是本次登录发起的，小米不会在换 token 时再要
    它；这里保持同样的算法，纯粹是为了把「和 HA 不一致」这个变量从排查里去掉。
    """
    return hashlib.sha1(f"d={device_id}".encode("utf-8")).hexdigest()


def build_auth_url(
    *,
    redirect_url: str,
    device_id: str,
    state: str,
    scope: list[str] | None = None,
    skip_confirm: bool = False,
) -> str:
    """拼授权页 URL。

    skip_confirm=False 表示每次都显示授权确认页，对 CLI 更安全（用户能看清
    自己在授权什么），代价是多点一次。
    """
    params: dict[str, Any] = {
        "redirect_uri": redirect_url,
        "client_id": const.CLIENT_ID,
        "response_type": "code",
        "device_id": device_id,
        "state": state,
        "skip_confirm": "true" if skip_confirm else "false",
    }
    if scope:
        params["scope"] = " ".join(scope)
    return f"{const.OAUTH_AUTH_URL}?{urlencode(params)}"


def _unwrap_error(res_obj: dict[str, Any]) -> CloudError:
    """把小米的嵌套错误结构翻成一句人话。

    形如：{"code":-6,"message":"{\\"error\\":96013,
           \\"error_description\\":\\"invalid authorization code\\"}"}
    """
    code = res_obj.get("code")
    detail: dict[str, Any] = {}
    result = res_obj.get("result")
    if isinstance(result, dict):
        detail = result
    else:
        message = res_obj.get("message")
        if isinstance(message, str):
            try:
                parsed = json.loads(message)
                if isinstance(parsed, dict):
                    detail = parsed
            except json.JSONDecodeError:
                pass
    description = detail.get("error_description") or res_obj.get("message")
    inner = detail.get("error")
    hint = None
    if inner == 96013:
        hint = "授权码无效或已被用过，重新执行 `mi auth login`"
    elif inner == 96002:
        hint = (
            "换 token 的参数和授权时不一致（redirect_uri / client_id / "
            "device_id 三者必须完全相同），用 -v 看实际发出的请求"
        )
    elif code == 401 or inner == 401:
        hint = "凭据已失效，重新执行 `mi auth login`"
    text = f"获取 token 失败（code={code}"
    if inner is not None:
        text += f", error={inner}"
    text += f"）：{description}"
    return CloudError(text, code=code, description=str(description), hint=hint)


class OAuthClient:
    """token 的获取与刷新。"""

    def __init__(
        self,
        region: str = const.DEFAULT_REGION,
        *,
        redirect_url: str,
        timeout: float = const.HTTP_TIMEOUT,
        client: httpx.Client | None = None,
        trace: Callable[[str], None] | None = None,
    ) -> None:
        self.region = region
        self.redirect_url = redirect_url
        self._timeout = timeout
        self._trace = trace
        self._client = client or httpx.Client(
            timeout=timeout, headers={"User-Agent": const.USER_AGENT}
        )
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "OAuthClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _get_token(self, data: dict[str, Any]) -> TokenResult:
        url = f"{const.api_base_url(self.region)}/app/v2/ha/oauth/get_token"
        payload = json.dumps(data)
        if self._trace:
            self._trace(f"GET {url}?data={payload}")
        try:
            response = self._client.get(
                url,
                params={"data": payload},
                headers={"content-type": "application/x-www-form-urlencoded"},
            )
        except httpx.HTTPError as err:
            raise NetworkError(f"连接 {url} 失败：{err}") from err
        if response.status_code == 401:
            raise CloudError(
                "获取 token 失败：未授权（401）",
                code=401,
                hint="重新执行 `mi auth login`",
            )
        if response.status_code != 200:
            raise CloudError(
                f"获取 token 失败：HTTP {response.status_code}",
                code=response.status_code,
            )
        if self._trace:
            self._trace(f"<- HTTP {response.status_code} {response.text[:500]}")
        try:
            res_obj = response.json()
        except ValueError as err:
            raise CloudError(f"响应不是合法 JSON：{response.text[:200]}") from err
        if res_obj.get("code") != 0 or "result" not in res_obj:
            raise _unwrap_error(res_obj)
        result = res_obj["result"]
        missing = [
            key
            for key in ("access_token", "refresh_token", "expires_in")
            if key not in result
        ]
        if missing:
            raise CloudError(f"响应缺少字段：{', '.join(missing)}")
        return TokenResult(
            access_token=result["access_token"],
            refresh_token=result["refresh_token"],
            expires_in=int(result["expires_in"]),
            obtained_at=int(time.time()),
        )

    def exchange_code(self, code: str, device_id: str) -> TokenResult:
        return self._get_token(
            {
                "client_id": int(const.CLIENT_ID),
                "redirect_uri": self.redirect_url,
                "code": code,
                "device_id": device_id,
            }
        )

    def refresh(self, refresh_token: str) -> TokenResult:
        return self._get_token(
            {
                "client_id": int(const.CLIENT_ID),
                "redirect_uri": self.redirect_url,
                "refresh_token": refresh_token,
            }
        )

    def user_profile(self, access_token: str) -> dict[str, Any]:
        """账号昵称等基础信息，用于 `mi auth whoami`。"""
        try:
            response = self._client.get(
                const.USER_PROFILE_URL,
                params={"clientId": const.CLIENT_ID, "token": access_token},
            )
        except httpx.HTTPError as err:
            raise NetworkError(f"获取账号信息失败：{err}") from err
        try:
            res_obj = response.json()
        except ValueError as err:
            raise CloudError("账号信息响应不是合法 JSON") from err
        if res_obj.get("code") != 0 or "data" not in res_obj:
            raise CloudError(
                f"获取账号信息失败：{res_obj.get('description') or res_obj}",
                code=res_obj.get("code"),
            )
        return res_obj["data"]
