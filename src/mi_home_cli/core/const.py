"""接口常量。

这些值来自对 Home Assistant 米家集成（XiaoMi/ha_xiaomi_home）公开行为的观察，
不是小米对外承诺的接口契约，随时可能变化；集中放在这里方便跟进。
"""
from __future__ import annotations

# 米家给 Home Assistant 集成注册的 OAuth 客户端。
CLIENT_ID = "2882303761520251711"

OAUTH_AUTH_URL = "https://account.xiaomi.com/oauth2/authorize"
USER_PROFILE_URL = "https://open.account.xiaomi.com/user/profile"

# 小米 OAuth 服务端对 redirect_uri 做白名单校验，实测结论：
#   * host 必须是 homeassistant.local:8123，换端口或换成 localhost/127.0.0.1
#     都会返回 "invalid redirect uri"；
#   * scheme（http/https）与 path、query 不校验，可以自定义。
# 因此这里的 host 不可更改，只有路径是我们自己的。
# 换 token 时服务端还会拿 code 绑定的信息做二次比对，实测用自定义路径
# （/mi-home-cli/callback）+ 自定义 device_id（cli.xxx）会被判 96002
# invalid request，所以回调路径和 device_id 都照 Home Assistant 的形态来：
#   redirect_uri = http://homeassistant.local:8123/api/webhook/{webhook_id}
#   device_id    = ha.{32 位 hex}
REDIRECT_HOST = "homeassistant.local"
REDIRECT_PORT = 8123
REDIRECT_PATH_PREFIX = "/api/webhook/"
REDIRECT_ORIGIN = f"http://{REDIRECT_HOST}:{REDIRECT_PORT}"
DEVICE_ID_PREFIX = "ha."


def redirect_url(webhook_id: str) -> str:
    return f"{REDIRECT_ORIGIN}{REDIRECT_PATH_PREFIX}{webhook_id}"

DEFAULT_API_HOST = "ha.api.io.mi.com"

CLOUD_SERVERS: dict[str, str] = {
    "cn": "中国大陆",
    "de": "Europe",
    "i2": "India",
    "ru": "Russia",
    "sg": "Singapore",
    "us": "United States",
}

DEFAULT_REGION = "cn"

HTTP_TIMEOUT = 30
# access_token 用掉这个比例的有效期后就提前刷新，留足余量。
TOKEN_REFRESH_RATIO = 0.7

USER_AGENT = "mi-home-cli"


def api_host(region: str) -> str:
    """区域对应的 API host。"""
    if region == "cn":
        return DEFAULT_API_HOST
    return f"{region}.{DEFAULT_API_HOST}"


def api_base_url(region: str) -> str:
    return f"https://{api_host(region)}"
