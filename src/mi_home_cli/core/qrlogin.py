"""小米账号扫码登录（QR scan login）。

**不引入无头浏览器**，纯 API 调用复刻「浏览器打开授权页后跳转到登录页」的整个
流程，最终拿到和浏览器登录完全一样的授权码 `code`，再交给既有的
`OAuthClient.exchange_code` 换 token——扫码只是换了一种「拿 code」的方式，后半段
和 `mi auth login` 一字不差。

跳转链（解析授权页跳转得到）：

  1. GET `oauth2/authorize`（`skip_confirm=true`）→ 302 到 `pass/serviceLogin`；
  2. GET `serviceLogin?_json=true` → 拿 `_sign` / `sid` / `qs` / `callback` /
     `serviceParam`（未登录时响应里 `code=70016 登录验证失败`，是正常状态）；
  3. GET `longPolling/loginUrl`（带上一步的参数）→ 拿 `loginUrl`（编码进二维码给
     用户扫的地址）、`lp`（长轮询地址）、`qr`（服务端渲染的二维码图片，兜底用）；
  4. 终端里把 `loginUrl` 渲染成二维码，用户用小米 App「我的 → 扫一扫」扫码并在
     手机上确认登录；
  5. 长轮询 `lp`，它会一直挂起直到手机确认，确认后返回一个 `location`；
  6. 携带一路攒下来的 cookie 跟随 `location` 的跳转链，走到 `redirect_uri` 时
     （那台 `homeassistant.local` 多半不可达，所以只解析不真正请求）从 query 里
     取出 `code` 和 `state`；
  7. `code` 交给 `OAuthClient.exchange_code`。

全程 cookie 由同一个 `httpx.Client` 维持（`deviceId` 等 cookie 在第 2 步种下，
后面每一步都要带着）。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlencode, urljoin, urlparse

import httpx

from ..errors import CloudError, MiCliError, NetworkError
from .callback import CallbackResult, parse_pasted
from .oauth import build_auth_url
from . import const

# 单次长轮询请求最多挂多久；挂到点没结果就重发（总时限由 deadline 控制）。
_LP_POLL_TIMEOUT = 30.0
# 跳转链最多跟几跳，防止异常情况下打转。
_MAX_REDIRECTS = 10
# 未登录时 serviceLogin 返回的 code，属正常状态而非错误。
_NOT_LOGGED_IN = 70016


@dataclass
class QrChallenge:
    """一次扫码登录的「待扫」状态。"""

    login_url: str  # 编码进二维码给用户扫的地址
    lp_url: str  # 长轮询地址
    qr_image_url: str  # 服务端渲染的二维码图片 URL（没装 segno 时的兜底）
    timeout: int  # 服务端给的二维码有效期（秒）


def _strip_prefix(text: str) -> str:
    text = text.lstrip()
    if text.startswith(const.XIAOMI_JSON_PREFIX):
        text = text[len(const.XIAOMI_JSON_PREFIX):]
    return text


def render_qr(data: str) -> str | None:
    """把内容渲染成终端二维码字符串；没装 `segno` 时返回 None。

    用「黑字白底 + 半块字符」渲染：每个字符单元上下叠两个模块，高度减半；显式给
    ANSI 黑前景白背景，保证暗模块在深色/浅色终端里都是暗的（不靠终端主题、也就
    不会出现反色扫不出的情况）。
    """
    try:
        import segno
    except ImportError:
        return None

    # error='m'：约 15% 纠错，二维码被终端字体略微拉伸也还扫得动。
    matrix = [list(row) for row in segno.make(data, error="m").matrix_iter(
        scale=1, border=4
    )]
    if len(matrix) % 2:  # 补一行全亮，好两两配对
        matrix.append([0] * len(matrix[0]))

    black_on_white = "\x1b[30;47m"
    reset = "\x1b[0m"
    glyph = {(1, 1): "█", (1, 0): "▀", (0, 1): "▄", (0, 0): " "}
    lines = []
    for top, bottom in zip(matrix[0::2], matrix[1::2]):
        cells = "".join(glyph[(t, b)] for t, b in zip(top, bottom))
        lines.append(f"{black_on_white}{cells}{reset}")
    return "\n".join(lines)


class QrLoginClient:
    """扫码登录的三步走：`start`（拿二维码）→ `poll`（等确认）→ `resolve_code`。

    HTTP 客户端可注入，方便离线测试；不注入时自建一个带 cookie 的 client。
    """

    def __init__(
        self,
        *,
        redirect_url: str,
        device_id: str,
        state: str,
        timeout: float = const.HTTP_TIMEOUT,
        client: httpx.Client | None = None,
        trace: Callable[[str], None] | None = None,
    ) -> None:
        self.redirect_url = redirect_url
        self.device_id = device_id
        self.state = state
        self._trace = trace
        self._client = client or httpx.Client(
            timeout=timeout,
            follow_redirects=False,
            headers={"User-Agent": const.WEB_USER_AGENT},
        )
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "QrLoginClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _get(self, url: str, *, timeout: float | None = None) -> httpx.Response:
        if self._trace:
            self._trace(f"GET {url}")
        try:
            if timeout is None:
                response = self._client.get(url)
            else:
                response = self._client.get(url, timeout=timeout)
        except httpx.HTTPError as err:
            raise NetworkError(f"连接 {urlparse(url).netloc} 失败：{err}") from err
        if self._trace:
            self._trace(f"<- HTTP {response.status_code}")
        return response

    def _json(self, response: httpx.Response) -> dict[str, Any]:
        try:
            obj = json.loads(_strip_prefix(response.text))
        except ValueError as err:
            raise CloudError(
                f"账号接口响应不是合法 JSON：{response.text[:200]}"
            ) from err
        if not isinstance(obj, dict):
            raise CloudError("账号接口响应结构异常")
        return obj

    def start(self, *, skip_confirm: bool = True) -> QrChallenge:
        """走完 authorize → serviceLogin → longPolling，拿到二维码和长轮询地址。"""
        authorize = build_auth_url(
            redirect_url=self.redirect_url,
            device_id=self.device_id,
            state=self.state,
            skip_confirm=skip_confirm,
        )
        login_page = self._get(authorize).headers.get("location")
        if not login_page:
            raise CloudError(
                "授权页没有跳转到登录页",
                hint="小米可能调整了登录流程，改用 `mi auth login`（浏览器/粘贴）",
            )

        sep = "&" if "?" in login_page else "?"
        service = self._json(self._get(login_page + sep + "_json=true"))
        # 未登录时 code=70016，正常；这一步要的是里面的登录参数，不是登录结果。
        code = service.get("code")
        if code not in (None, 0, _NOT_LOGGED_IN):
            raise CloudError(
                f"获取登录参数失败："
                f"{service.get('description') or service.get('desc') or code}",
                code=code,
            )
        missing = [
            key
            for key in ("_sign", "sid", "qs", "callback", "serviceParam")
            if not service.get(key)
        ]
        if missing:
            raise CloudError(
                f"登录页响应缺少字段：{', '.join(missing)}",
                hint="小米可能调整了登录流程，改用 `mi auth login`",
            )

        lp_params = {
            "_group": "DEFAULT",
            "_qrsize": "240",
            "qs": service["qs"],
            "bizDeviceType": "",
            "callback": service["callback"],
            "_hasLogo": "false",
            "theme": "",
            "sid": service["sid"],
            "needTheme": "false",
            "showActiveX": "false",
            "serviceParam": service["serviceParam"],
            "_locale": "zh_CN",
            "_sign": service["_sign"],
            "_dc": str(int(time.time() * 1000)),
        }
        obj = self._json(
            self._get(f"{const.LONG_POLLING_URL}?{urlencode(lp_params)}")
        )
        if obj.get("code") != 0 or not obj.get("loginUrl") or not obj.get("lp"):
            raise CloudError(
                f"获取二维码失败："
                f"{obj.get('description') or obj.get('desc') or obj.get('code')}",
                code=obj.get("code"),
            )
        return QrChallenge(
            login_url=obj["loginUrl"],
            lp_url=obj["lp"],
            qr_image_url=obj.get("qr", ""),
            timeout=int(obj.get("timeout", 300)),
        )

    def poll(
        self,
        challenge: QrChallenge,
        *,
        deadline: float,
        poll_interval: float = 1.0,
    ) -> str:
        """长轮询等手机确认，返回确认后的 `location`。超时抛错。

        `lp` 会一直挂起直到确认或服务端超时；挂到我们设的单次上限就重发，直到
        `deadline`。手机已扫但还没点确认时，服务端可能先返回一个没有 `location`
        的中间响应，这时接着等。
        """
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MiCliError(
                    "扫码登录超时（二维码已失效）",
                    hint="重新执行 `mi auth login --scan`",
                )
            try:
                response = self._client.get(
                    challenge.lp_url, timeout=min(remaining, _LP_POLL_TIMEOUT)
                )
            except httpx.ReadTimeout:
                continue  # 单次挂到点，重发
            except httpx.HTTPError as err:
                raise NetworkError(f"长轮询失败：{err}") from err

            obj = self._json(response)
            code = obj.get("code")
            location = obj.get("location")
            if code == 0 and location:
                return location
            if code in (0, None, _NOT_LOGGED_IN):
                # 已扫未确认之类的中间态，喘口气接着等（避免立即返回时空转）。
                if poll_interval:
                    time.sleep(poll_interval)
                continue
            raise CloudError(
                f"扫码登录失败："
                f"{obj.get('description') or obj.get('desc') or code}",
                code=code,
            )

    def resolve_code(self, location: str) -> CallbackResult:
        """跟随确认后的跳转链，走到 `redirect_uri` 时取出 `code` / `state`。"""
        url = location
        for _ in range(_MAX_REDIRECTS):
            host = urlparse(url).hostname
            if host == const.REDIRECT_HOST:
                # 这台机器多半不可达，不真正请求，直接从 URL 解析 code/state。
                # 复用粘贴那条路的解析（含 state 校验）。
                return parse_pasted(url, expected_state=self.state)
            response = self._get(url)
            nxt = response.headers.get("location")
            if not nxt:
                raise CloudError(
                    "跳转链没有回到回调地址就停了，可能需要在手机上完成授权确认",
                    code=response.status_code,
                    hint="在手机上点「同意/授权」后重试，或改用 `mi auth login`",
                )
            url = urljoin(url, nxt)
        raise CloudError("跳转次数过多，未能取到授权码")
