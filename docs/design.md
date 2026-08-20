# 米家控制 CLI — 实现方案设计

> 参考对象：[XiaoMi/ha_xiaomi_home](https://github.com/XiaoMi/ha_xiaomi_home)（Home Assistant 米家集成）
> 本文只描述**如何实现**；命令清单见 [cli-spec.md](./cli-spec.md)。

---

## 0. 前置：许可证边界（必须先明确）

`ha_xiaomi_home` 的 License 头明确写着：授权仅限「为在 Home Assistant 中非商业使用」而复制、修改、分发，并且**不授权用于开发 APP、Web 服务及其他形式的软件**。

因此本项目的约束是：

- **不复制、不移植** `ha_xiaomi_home` 的源码、`specs/` 资源文件、翻译资源。
- 只把它当作**协议与接口行为的参考资料**（哪个 URL、哪些字段、哪种时序），自行独立实现。
- 局域网 miIO 协议部分若需要现成实现，使用 MIT 协议的 [`python-miio`](https://github.com/rytilahti/python-miio) 作为依赖，而不是抄上游代码。
- README 中声明本项目与小米官方无关，且账号数据仅保存在本地。

---

## 1. 上游是怎么跑通的（读码结论）

从 `custom_components/xiaomi_home/miot/` 读出的关键事实，是本 CLI 的实现依据：

### 1.1 认证：OAuth2 授权码模式

| 项 | 值 |
| --- | --- |
| 授权页 | `https://account.xiaomi.com/oauth2/authorize` |
| `client_id` | `2882303761520251711`（HA 集成公开使用的 ID） |
| 换 token | `GET https://{host}/app/v2/ha/oauth/get_token?data={json}` |
| API host | `ha.api.io.mi.com`（`cn`）/ `{region}.ha.api.io.mi.com`（其他区） |
| 参数 | `redirect_uri` / `client_id` / `response_type=code` / `device_id` / `state` / `skip_confirm` |
| `device_id` | 上游用 `ha.{uuid}`；本项目用 `cli.{uuid}` |
| 返回 | `access_token` / `refresh_token` / `expires_in` |

无 client_secret，刷新用 `refresh_token` 走同一个 `get_token` 接口。

### 1.2 云端 HTTP API

请求头：`Authorization: Bearer{access_token}`（**注意上游确实没有空格**）、`X-Client-BizId: haapi`、`X-Client-AppId: {client_id}`、`Content-Type: application/json`。响应统一是 `{"code":0,"result":...}`。

| 用途 | 接口 |
| --- | --- |
| 家庭/房间/设备归属 | `POST /app/v2/homeroom/gethome`（分页） |
| 设备详情 | `POST /app/v2/home/device_list_page`（`limit:200`，`start_did` 翻页） |
| 读属性 | `POST /app/v2/miotspec/prop/get`，body `{datasource:1, params:[{did,siid,piid}]}` |
| 写属性 | `POST /app/v2/miotspec/prop/set`，body `{params:[{did,siid,piid,value}]}` |
| 调用 action | `POST /app/v2/miotspec/action`，body `{params:{did,siid,aiid,in:[值...]}}` |
| 用户信息 | `GET https://open.account.xiaomi.com/user/profile` |
| 中枢证书 | `POST /app/v2/ha/oauth/get_central_crt` |

设备条目里可用的字段：`did`、`name`、`model`、`spec_type`(urn)、`isOnline`、`token`（局域网密钥）、`local_ip`、`pid`(connect_type)、`rssi`、`parent_id`、`extra.fw_version`。

工程要点：上游把 `prop/get` 做了 **0.2s 聚合 + 单请求最多 150 条**；`did` 形如 `600xxxxxx.s2` 的是子设备。

### 1.3 spec（设备能力描述）

- 实例：`https://miot-spec.org/miot-spec-v2/instance?type={urn}`
- 多语言：`https://miot-spec.org/instance/v2/multiLanguage`
- 标准库模板：`https://miot-spec.org/miot-spec-v2/template/list/...`

这些是**公开接口，不需要 token**，可以放心直接用。spec 决定了 `siid/piid/aiid`、`format`、`access`（read/write/notify）、`value-range`、`value-list`（枚举）、单位。

### 1.4 实时推送

> TLS 的根证书按「`MI_CA_BUNDLE` → 系统默认（含 `SSL_CERT_FILE`）→ certifi 兜底」
> 的顺序取。只用 certifi 会把配了企业 CA 的用户挡在门外；只用系统默认则在
> macOS 的某些 Python 上一张根证书都没有。另外代理的 fake-IP（`198.18.x.x`）
> 会做中间人，证书校验必然失败，`mi doctor` 会把这种情况点出来。


云端 MQTT：`{区域}-ha.mqtt.io.mi.com:8883`，TLS + MQTT v5，
**用户名是 OAuth 的 client_id、密码是 access_token**，
client_id 就是 OAuth 用的那个 `ha.{32 位 hex}`。订阅：

- 属性变化：`device/{did}/up/properties_changed/{siid}/{piid}`（或 `#`）
- 事件：`device/{did}/up/event_occured/{siid}/{eiid}`（上游此处拼写就是 `occured`）
- 在线状态：`device/{did}/state/#`

### 1.5 局域网控制

miIO 协议：UDP `54321`，包头 `0x2131`，密钥 = `md5(token)`，IV = `md5(md5(token)+token)`，AES-128-CBC + PKCS7，`[16:32]` 位是含 token 的 MD5 校验位。载荷是 JSON-RPC：`get_properties` / `set_properties` / `action`，参数与云端一致。

限制（上游文档明确写了）：只能控制**与本机同网段、直接联网的 WiFi/有线设备**，蓝牙 Mesh、ZigBee 等经网关接入的设备走不了局域网。中枢网关控制（mDNS 发现 + 证书）仅 `cn` 区支持，复杂度高。

---

## 2. 技术选型

**推荐：Python 3.11+**

- 与参考实现同语言，协议对照成本最低；`python-miio`（局域网）、`paho-mqtt`、`httpx`、`cryptography` 生态齐全。
- CLI 框架 `typer`（基于 click，自带补全生成），输出用 `rich`（表格/颜色/进度）。
- 依赖与打包统一用 `uv`：`uv sync` 建环境、`uv lock` 锁版本（`uv.lock` 入库），
  `uv run mi ...` 开发期调用，`uv tool install` 装成全局命令；入口命令 `mi`。

备选：Go（单二进制、启动快、分发省心，但 miIO 与 spec 解析要全部自己写）。若目标是「给别人分发的工具」而非「自己用的脚本」，可以选 Go；本设计按 Python 展开，分层设计对两者通用。

---

## 3. 架构分层

```
mi_home_cli/
├── cli/                     # 命令层：只做参数解析 + 调用 core + 渲染
│   ├── main.py              # 根命令、全局参数、异常→退出码
│   ├── auth.py device.py home.py spec.py prop.py action.py
│   ├── watch.py lan.py scene.py config.py doctor.py repl.py
│   └── render.py            # table / json / yaml / plain 四种输出
├── core/
│   ├── auth.py              # OAuth2：授权 URL、本地回环回调、token 刷新
│   ├── cloud.py             # HTTP API 封装（含 get 聚合、分页、重试）
│   ├── mqtt.py              # 云端 MQTT 订阅（watch 用）
│   ├── lan.py               # miIO UDP 直连（可选依赖 python-miio）
│   ├── spec.py              # spec 拉取/缓存/索引/值转换
│   ├── registry.py          # 设备清单缓存 + 名称/别名解析
│   ├── channel.py           # 通道路由：auto / cloud / lan
│   └── errors.py            # 错误码 → 人话 + 退出码
├── store.py                 # ~/.config/mi-home-cli 的读写（profile 隔离）
└── tests/                   # 全部离线，用 httpx.MockTransport 回放
```

**关键抽象 `Channel`**：`get_props / set_props / call_action` 三个方法，
`CloudChannel` 与 `LanChannel` 都实现它，`AutoChannel` 按「本机有 token +
设备在同网段 + 局域网可达」优先走 LAN、失败回落云端。CLI 层完全不感知走的哪条路。

**默认是 `cloud`**：局域网首次命中要付探活（unicast hello）加一次可能失败的
调用，这份等待不该由默认承担；想要低延迟的人显式 `--channel lan`，或
`mi config set channel auto` 设为默认。

### 3.1 本地存储

```
~/.config/mi-home-cli/
├── config.toml                 # 默认 profile、region、输出格式、超时、lan 开关
└── profiles/<name>/
    ├── auth.json               # access/refresh token、uid、过期时间   (chmod 600)
    ├── devices.json            # 设备清单缓存（含局域网 token）        (chmod 600)
    ├── aliases.json            # 用户自定义短名
    └── spec/<urn>.json         # spec 缓存（默认 14 天，与上游一致）
```

- 含密文件一律 `0600`，目录 `0700`；启动时校验权限，不对就警告。
- 局域网 token 默认在任何输出里打码，`--show-secrets` 才明文。
- 局域网地址缓存在 `lan.json`（did → IP + 发现时间），命中时先发一个 unicast
  hello 确认（几十毫秒），失败才重新广播扫描。
- 支持 `MI_HOME_CONFIG_DIR` 环境变量覆盖；支持 `--profile` 管理多账号/多区域。

### 3.2 登录流程（CLI 版本的难点）

**redirect_uri 是被服务端白名单锁死的**，这是整个 CLI 最硬的约束。

上游 `miot/const.py` 里写着：

```python
# Registered in Xiaomi OAuth 2.0 Service
# DO NOT CHANGE UNLESS YOU HAVE AN ADMINISTRATOR PERMISSION
OAUTH_REDIRECT_URL: str = 'http://homeassistant.local:8123'
```

HA 再拼上自己的 webhook 路径，最终是
`http://homeassistant.local:8123/api/webhook/{virtual_did}`。

实测授权接口对各种 redirect_uri 的反应：

| redirect_uri | 结果 |
| --- | --- |
| `http://homeassistant.local:8123/api/webhook/123456` | 302 → 登录页 ✅ |
| `http://homeassistant.local:8123/mi-home-cli/callback` | 302 ✅ |
| `http://homeassistant.local:8123/x?foo=bar` | 302 ✅ |
| `https://homeassistant.local:8123/x` | 302 ✅ |
| `http://homeassistant.local:9527/x` | `invalid redirect uri` ❌ |
| `http://localhost:8123/x` | `invalid redirect uri` ❌ |
| `http://127.0.0.1:9527` | `invalid redirect uri` ❌ |

**授权这一步的结论：host 必须是 `homeassistant.local:8123`；scheme 与 path、
query 不校验。**

但**换 token 那一步还会再比对一次**。用自定义路径
（`/mi-home-cli/callback`）和自定义 `device_id`（`cli.xxx`）拿到的真实授权码，
换 token 时返回 `96002 invalid request`；用假授权码测同样的参数却是
`96013 invalid authorization code`——说明服务端先查授权码，查到之后才校验
绑定信息，所以 96002 是「码本身有效、但参数不符合预期」。

因此身份三件套完全照 Home Assistant 的形态生成：

| 项 | 值 |
| --- | --- |
| `redirect_uri` | `http://homeassistant.local:8123/api/webhook/{webhook_id}`，`webhook_id` = 随机 64 位整数 |
| `device_id` | `ha.` + 32 位 hex（`sha256(install_id.webhook_id.region)[:32]`） |
| `state` | `sha1("d=" + device_id)`，与上游同算法 |

这三个值按 profile 持久化在 `identity.json` 里；授权与换 token 必须用同一套，
所以每次 `mi auth login` 会把它们连同 `state` 写进 `pending.json`，
换 token 万一失败可以用 `mi auth exchange <code>` 拿同一个授权码重试，
不必再走一遍浏览器。

于是要自动收到 code，得同时满足两件事：浏览器把 `homeassistant.local` 解析到
跑 CLI 的机器，且该机器的 8123 端口由我们监听。实现按下面的顺序尽力而为，
每一步失败都不阻断流程：

1. 端口 8123 能 bind → 起本地 HTTP 服务（任意路径都收，只认 `code`/`state`）。
   本机在跑 Home Assistant 时端口会被占，直接跳到粘贴方式。
2. `homeassistant.local` 已经解析到本机（hosts 或局域网里的 mDNS）→ 直接可用。
3. 否则装了 `zeroconf` 就自己广播一条 mDNS 记录，把 `homeassistant.local`
   指向本机 IP。局域网里有真 HA 时会撞名，撞了就放弃。
4. **粘贴兜底，永远可用**：浏览器跳到打不开的地址后，地址栏里仍然带着
   `?code=...&state=...`，让用户整段粘回终端。本地服务和粘贴同时等待，
   谁先到用谁；`--manual` 只走粘贴。

安全上：本地回调的 `state` 必须完全匹配才接受（不匹配就是别人往这个端口打的
请求）；粘贴内容缺 `state` 时给出警告。`state` 由 `device_id` 推导而来
（与上游同算法），不是每次随机——回调只在本机短暂监听，这点取舍换来的是
和上游完全一致的参数，少一个排查变量。

token 到期前（用掉 70% 有效期）自动续期；续期失败则提示重新 `mi auth login`，
退出码 `10`。

---

## 4. 两个「好用度」核心：设备解析与属性解析

CLI 的体验成败不在协议，在于**别让人记 did 和 siid/piid**。

### 4.1 设备引用（`<device>` 参数）

按顺序匹配，命中唯一即用：

1. `did`（纯数字或带 `.sN` 子设备后缀）
2. 用户别名（`mi device alias set 客厅灯 xxx`）
3. 设备名精确匹配（大小写不敏感）
4. `房间/设备名`，如 `客厅/吸顶灯`
5. 设备名子串模糊匹配
6. `model` 匹配（如 `yeelink.light.lamp4`）

多个候选 → 报错并列出候选表（带 did、房间、model），退出码 `4`。可用 `--home` /
`--room` / `--model` 缩小范围。

家里不止一个家庭时，`mi home use <家庭>` 设一个默认家庭，之后解析只在这个家庭
里进行——这是消除重名歧义最有效的一招。默认家庭按 `home_id` 匹配（家庭改名不
受影响）。设备在别的家庭时不静默跨家庭操作，而是报错并指出它在哪个家庭，
`--all-homes` 可临时跨家庭。

### 4.2 属性 / action 引用

`<prop>` 支持三种写法，从 spec 索引解析：

- 数字：`2.1`
- 全名：`light.brightness`（service 实例名.property 实例名）
- 短名：`brightness`（该设备内唯一才允许，否则报错并提示用全名）

值的处理全部由 spec 驱动：

- `bool`：`on/off/true/false/1/0/开/关`
- `uint8/int32/float`：按 `value-range` 校验 `min/max/step`，越界直接报错（不静默截断）
- 枚举 `value-list`：允许写描述文本（`Mode=Sleep` / `模式=睡眠`）或数字，输出时反查描述
- `string`：原样
- 只读属性写入 / 不可读属性读取 → 在发请求前就本地拦截报错

`action` 的入参按 spec 的 `in` 顺序，支持 `--in a --in b` 位置传参和 `--param name=value` 具名传参。

---

## 5. 输出与错误

- 默认 `table`（rich，自动适配终端宽度）；`-o json` 输出稳定 schema，**保证可被 `jq` 消费**（管道非 TTY 时自动关闭颜色）；另有 `-o yaml` / `-o plain`（只输出值，方便 `$(...)`）。
- 所有写操作打印「属性 → 旧值 → 新值 → 结果」：写之前先批量读一次旧值，
  多一次请求换来「改错了能照着回滚」。
- **返回码不等于生效**。`prop/set` 的 `code` 只是回执：`0` 是成功，`1` 是
  「已接受、设备执行中」——部分设备（实测 `dwdz.switch.sw0a01`）每次写入都
  回 `1`，操作其实是成功的。把非 0 一律当失败会让这类设备的每条命令都报错。
  因此 `0` 和 `1` 都算接受。想要确凿结论时加 `--verify`：写完回读一次，用真实
  状态说话；回读跟不上时提示「可能有延迟」而不是判失败。默认不回读——写操作
  多一次往返（约 0.6s）对交互式使用不划算，需要确认的场景再显式要。`--dry-run` 只解析不发请求，用于确认解析对不对。
- 退出码：`0` 成功 / `1` 通用错误 / `2` 参数错误 / `3` 设备不存在 / `4` 引用有歧义 / `5` 设备离线 / `6` 属性或 action 不存在 / `7` 值非法 / `8` 网络错误 / `9` 云端返回错误 / `10` 未登录或 token 失效。
- 错误信息把小米返回的 `code/message` 原样附在末尾（`--verbose` 时打印完整请求响应）。

---

## 6. 分期实现

| 阶段 | 内容 | 产出 |
| --- | --- | --- |
| **M1 打通** | ✅ OAuth 登录（本地回调 + mDNS + `--manual` 兜底）、token 存储/刷新、`auth *`、`profile *`、`doctor` | 能登录 |
| **M2 控制** | ✅ `home/room/device list`、设备清单缓存与解析、spec 拉取与缓存、`spec show/search/dump`、`get`/`set`/`action`、值转换、`on/off/toggle`、`-o json`、`--dry-run` | 核心可用 |
| **M3 顺手** | ✅ `light`/`climate`/`cover`/`fan` 语义命令、`device alias`、写操作显示旧值→新值、shell 补全（typer 内置） | 日常能用 |
| **M4 实时** | ✅ 云端 MQTT `watch`（属性/事件/上下线），流式输出、逐行 JSON | 可做脚本触发 |
| **M5 本地** | ✅ 局域网通道（自行实现 miIO）、`lan list/discover/status`、`--channel auto\|cloud\|lan` 回落 | 低延迟、断网可用 |
| 可选 | `repl` 交互模式、场景（`/app/v2/scene/*` 需自行探索验证，上游未使用）、中枢网关控制 | — |

## 6.5 明确的非目标：只用 OAuth2

米家生态里存在两套并行的身份：

| | OAuth2（本项目 / 官方 HA 集成） | 账号密码（米家 App、al-one/hass-xiaomi-miot） |
| --- | --- | --- |
| 拿到什么 | `access_token` | `serviceToken` + `ssecurity`，请求要 RC4 签名 |
| 能用的接口 | `/app/v2/*` 这套 spec 接口 | 上面全部，**外加** smartcamera、场景等私有接口 |
| 代价 | 授权页登录，全程不碰用户密码 | 要处理密码、二次验证、签名算法 |

**本项目只用 OAuth2。** 代价是下面这些做不了，属于已知且接受的缺口：

- **摄像机报警录像回放**：`business.smartcamera.api.io.mi.com` 的 `get/eventlist`
  与 `common/app/m3u8` 需要 `serviceToken` + `ssecurity` + RC4 签名。
- **米家场景 / 自动化**：同样在那套身份之下。

理由：不碰用户密码是这个 CLI 的安全底线之一——OAuth2 的授权在浏览器里完成，
CLI 只拿到一个 3 天有效期、可撤销的 token；换成账号密码就要在本地处理密码和
二次验证，风险和维护成本都不是这点功能换得来的。

需要那些能力的人，用 [al-one/hass-xiaomi-miot](https://github.com/al-one/hass-xiaomi-miot)
（Apache-2.0）更合适。

顺带澄清一个容易混淆的点：**摄像机的实时推流地址不在这条分界线上**。部分型号的
spec 里带 `camera-stream-for-google-home` / `camera-stream-for-amazon-alexa` 服务，
`start-hls-stream` / `start-rtsp-stream` 是普通的 spec 动作，OAuth2 就能调，
`mi action <设备> start-rtsp-stream` 直接可用，返回的地址会带上属性名。
没有这两个服务的型号（如 `chuangmi.camera.039c01`、`chuangmi.camera.079ac1`）
则只有 `p2p-stream`，那是小米私有的 cs2 协议，不在 miot-spec 的数据面里。
支不支持是按 SKU 定的，同一款产品的不同 SKU 可能一个有一个没有——完整名单和
扫描脚本见 [camera-stream.md](camera-stream.md)。

## 7. 测试策略

- 协议层用录制的响应做 fixture 回放测试（**fixture 必须脱敏**：did、uid、token、ssid/bssid、经纬度）。
- 解析层（设备解析、属性解析、值转换、退出码）纯单测，不碰网络。
- 一个 `--mock` 模式：用本地 fixture 跑完整命令，便于写文档示例和 CI。
- CI 只跑离线测试；真机测试用打了 `@pytest.mark.live` 的用例本地手动跑。

## 8. 已知风险

1. **接口非公开契约**：`app/v2/*` 是米家给 HA 集成用的接口，随时可能变；把 URL 和字段集中在 `core/cloud.py` 一处，方便跟进。
2. **`redirect_uri` 白名单**：已实测确认 host 锁死在 `homeassistant.local:8123`
   （见 3.2）。小米若调整白名单，自动回调会失效，粘贴方式不受影响。
3. **频率限制**：批量读属性要沿用「聚合 + 单请求 ≤150 条」的策略，`watch` 用 MQTT 而不是轮询。
4. **局域网覆盖有限**：只有 `connect_type ∈ {0,8,12,23}` 且有 token 的设备
   （直连路由器的 WiFi/有线设备）可以直连；蓝牙 Mesh、ZigBee 走网关的不行。
   实测某账号 72 台设备里只有 21 台够格。`--channel auto` 会在任何一步不成立时
   静默回落云端，所以覆盖有限不影响可用性。
5. **凭据安全**：device token 等价于局域网控制权，文件权限 + 输出打码 + 不写日志三重防护。
