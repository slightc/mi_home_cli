# mi_home_cli

命令行控制米家（Xiaomi Home）设备。

```bash
mi device list --room 客厅          # 看看有什么
mi set 台灯 on=true brightness=60   # 控制
mi light 台灯 --ct 4000 --color 暖   # 或者用人话
mi watch 净化器 -o json | jq        # 实时盯着，喂给脚本
```

协议行为参考 [XiaoMi/ha_xiaomi_home](https://github.com/XiaoMi/ha_xiaomi_home)（小米官方的
Home Assistant 集成）独立实现，**不包含其任何源码或资源文件**，也与小米公司无关。

---

## 特点

- **不用记 did**：设备可以用名称、别名、`房间/名称`、型号来指；属性可以写 `on`、
  `light.brightness` 或 `2.1`，枚举值直接写中文（`mode=睡眠`）。
- **spec 驱动**：取值范围、枚举、读写权限都来自设备自己的 MIoT spec，越界和写只读属性
  在发请求之前就被拦下。
- **能当积木用**：`-o json` 输出稳定结构，`-o plain` 直接取值，退出码分类明确。
- **实时推送**：`mi watch` 走云端 MQTT 长连接，不是轮询。
- **局域网直连**：支持的设备可以绕开云端，延迟从 1~2 秒降到几十毫秒。
- **凭据只在本地**：`~/.config/mi-home-cli/`，文件 0600，不上传任何第三方服务。

## 安装

依赖用 [uv](https://docs.astral.sh/uv/) 管理。

```bash
git clone https://github.com/slightc/mi_home_cli && cd mi_home_cli
uv sync --extra mdns      # mdns 是可选依赖，能提高登录时自动接收回调的成功率
uv run mi --help
```

想要全局可用的 `mi` 命令：

```bash
uv tool install --with zeroconf .
```

需要 Python 3.11+。

## 快速开始

```bash
uv run mi doctor          # 先体检：端口、域名解析、网络、证书、时钟
uv run mi auth login      # 浏览器登录小米账号
uv run mi device sync     # 拉取家庭、房间、设备清单
uv run mi device list

uv run mi home use 我家    # 多个家庭时强烈建议设一个默认家庭
uv run mi device alias set "米家空气净化器 6" 净化器

uv run mi get 净化器       # 读所有可读属性
uv run mi set 净化器 mode=睡眠
uv run mi fan 净化器 --speed 2
```

### 关于登录

小米的 OAuth 服务只接受 host 为 `homeassistant.local:8123` 的回调地址
（实测结论见 [design.md §3.2](docs/design.md)），所以 `mi auth login` 会：

1. 监听本机 8123 端口；
2. 尝试用 mDNS 把 `homeassistant.local` 指向本机，让浏览器能跳回来；
3. 上面两步都不成也不影响——授权后浏览器会停在一个打不开的地址上，
   **把地址栏里的整段地址粘回终端**即可（`mi auth login --manual` 直接走这条路）。

本地回调和粘贴是同时等待的，谁先到用谁。token 用掉 70% 有效期后自动续期。

## 核心概念

### 设备怎么指

按精确度从高到低匹配，某一级命中就不再往下找：

```
did → 别名 → 名称精确匹配 → 房间/名称 → 名称子串 → 型号
```

同一级命中多个就报歧义并列出候选（退出码 4），可以用 `--home` / `--room` / `--model`
缩小范围，或者干脆起个别名：

```bash
mi device alias set "石头自清洁扫拖机器人G10" 扫地机
mi on 扫地机
```

### 属性怎么写

三种写法等价，都由设备的 spec 解析：

```bash
mi get 台灯 2.2                  # siid.piid
mi get 台灯 light.brightness     # 服务.属性
mi get 台灯 brightness           # 裸名（该设备内唯一才行）
```

裸名在多个服务里都存在时，优先设备的主服务——空气净化器的 `on` 指主体开关而不是
屏幕，想要屏幕的写 `screen.on`。

值也一样宽松：`on=true` / `on=开` / `on=1` 等价，枚举值 `mode=睡眠` / `mode=Sleep` /
`mode=3` 等价。

### 默认家庭

家庭多了设备重名很常见。设一个默认家庭之后，设备解析、`device list`、`room list`
都只看这个家庭：

```bash
mi home use 我家
mi home use --clear      # 取消
mi --all-homes ...       # 临时跨家庭
```

设备在别的家庭时不会静默跨家庭操作，而是告诉你它在哪：

```
✗ 默认家庭「我家」里没有 `筒射灯 8`，但在 观湖园19栋301/进门走廊 的 筒射灯 8 找到了
提示：加 --all-homes 跨家庭操作，或用 `mi home use` 换默认家庭
```

### 控制通道

| 通道 | 行为 |
| --- | --- |
| `cloud`（默认） | 走米家云端，任何设备都能用 |
| `lan` | 只走局域网直连，不成立就报错说清楚卡在哪 |
| `auto` | 能直连就直连，任何一步不成立（包括调用本身失败）静默回落云端 |

```bash
mi --channel lan get 净化器 on
mi config set channel auto     # 想默认走局域网
```

默认是 `cloud`：局域网首次命中要付一次探活加一次可能失败的调用，这份等待不该由
默认承担。

## 命令一览

完整参数见 [docs/cli-spec.md](docs/cli-spec.md)。

### 账号

```bash
mi auth login [--manual] [--no-browser] [--region cn]
mi auth status [--check]     # 登录状态、token 剩余有效期
mi auth exchange <code|URL>  # 换 token 那步失败时，用同一个授权码重试
mi auth refresh | whoami | logout
mi profile list|use|remove|path
```

### 查看

```bash
mi home list
mi home use [<家庭>] [--clear]
mi room list [--home 我家]
mi device list [--home] [--room] [--model] [--search] [--online|--offline] [--wide]
mi device show <设备>
mi device sync
mi device alias set|list|rm
mi device token <设备> [--show-secrets]
```

### 能力

```bash
mi spec show <设备|urn> [--siid N] [--writable] [--actions]
mi spec search <设备> <关键词>
mi spec dump <设备>            # 完整 spec，喂给脚本或 LLM
mi spec cache info|clear
```

`mi spec show` 是查 `siid.piid`、取值范围、枚举值的入口：

```
服务    属性               id    权限  类型    取值            单位  说明
light   on                 2.1   rwn   bool    -               -     开关
light   brightness         2.2   rwn   uint8   1~100 step 1    %     亮度
light   color-temperature  2.3   rwn   uint16  2700~6000       K     色温
light   mode               2.7   rwn   uint8   0=空场景 4=日光…  -     模式
```

### 控制

```bash
mi get <设备> [属性...]              # 不写属性就读全部可读属性
mi set <设备> <属性=值>...           # 一次多条，批量下发
mi action <设备> [动作] [--in 值]...  # 不给动作名就列出所有动作
mi on|off|toggle <设备>

# 语义命令（不带选项时显示当前状态）
mi light <设备> [--on|--off] [--brightness 60] [--ct 4000] [--color 红] [--mode 日光]
mi climate <设备> [--on|--off] [--mode 制冷] [--temp 26] [--fan 2]
mi cover <设备> [--open|--close|--stop] [--position 50]
mi fan <设备> [--on|--off] [--speed 2] [--mode 自动] [--swing]
```

写操作会打印「旧值 → 新值 → 结果」，改错了照着回滚。加 `--verify` 会在写完后回读一次，
以设备真实状态为准（多一次往返）。

### 实时

```bash
mi watch [设备...] [--prop on] [--no-events] [--no-state]
                   [--all-updates] [--exit-after N] [--duration 秒]
```

不指定设备就盯默认家庭的全部设备。设备会周期性重报同一个值，默认跳过，
`--all-updates` 全显示。

```bash
# 当成自动化触发器用
mi watch 门锁 -o json | while read -r line; do
  echo "$line" | jq -r 'select(.kind=="event")'
done
```

### 局域网

```bash
mi lan list                  # 清单里哪些设备支持直连
mi lan discover              # 广播扫描并缓存 IP
mi lan status <设备>          # 可达性、IP、延迟
mi lan raw <设备> miIO.info   # 直接发一条 miIO 请求（排查用）
```

### 其他

```bash
mi config list|get|set|unset|path    # profile / region / output / home / channel
mi doctor                            # 环境体检
mi --install-completion              # shell 补全（typer 内置）
mi version
```

## 脚本化

全局参数放在子命令之前（`-o` 也可以跟在子命令后面）：

| 参数 | 说明 |
| --- | --- |
| `-o, --output` | `table`（默认）/ `json` / `yaml` / `plain` |
| `-p, --profile` | 多账号隔离 |
| `--channel` | `cloud`（默认）/ `auto` / `lan` |
| `--home` / `--all-homes` | 限定或忽略默认家庭 |
| `--dry-run` | 只解析校验，不下发 |
| `--verify` | 写入后回读确认 |
| `-v, --verbose` | 打印请求细节、走了哪条通道 |

也支持环境变量 `MI_PROFILE` / `MI_REGION` / `MI_OUTPUT` / `MI_CHANNEL`。

```bash
mi -o plain get 台灯 brightness        # 只输出 "60"，可以直接 $(...)
mi -o json device list | jq '.[] | select(.在线=="是")'
```

退出码：

| 码 | 含义 | 码 | 含义 |
| --- | --- | --- | --- |
| 0 | 成功 | 6 | 属性/动作不存在 |
| 1 | 通用错误 | 7 | 值非法（越界、不可写） |
| 2 | 参数错误 | 8 | 网络错误 |
| 3 | 设备不存在 | 9 | 云端返回错误 |
| 4 | 设备引用有歧义 | 10 | 未登录或登录失效 |
| 5 | 设备离线/不可达 | 130 | Ctrl-C 中断 |

## 数据存放

```
~/.config/mi-home-cli/
├── config.json                 # 默认 profile / 区域 / 输出格式 / 家庭 / 通道
└── profiles/<name>/
    ├── auth.json               # token（0600）
    ├── identity.json           # OAuth 用的 device_id、回调 id
    ├── devices.json            # 设备清单缓存，含局域网 token（0600）
    ├── aliases.json            # 自定义别名
    ├── lan.json                # 局域网地址缓存
    └── spec/                   # spec 缓存，14 天
```

用 `MI_HOME_CONFIG_DIR` 可以改位置。设备 token 等价于局域网控制权，任何输出里默认打码，
`--show-secrets` 才明文。

## 常见问题

<details>
<summary><b>登录时浏览器跳到一个打不开的地址</b></summary>

正常。小米只认 `homeassistant.local:8123` 这个回调 host，而你机器上多半没有 Home
Assistant。把浏览器地址栏里的整段地址粘回终端即可，CLI 一直在等。
</details>

<details>
<summary><b>macOS 上 <code>mi lan discover</code> 扫不到任何设备</b></summary>

macOS 14+ 会拦截终端发出的局域网广播，而且**静默丢弃、不报错**。去
`系统设置 → 隐私与安全性 → 本地网络` 放行你的终端（Terminal / iTerm / VS Code）。

其次检查：和设备在同一网段？路由器开了 AP 隔离？
</details>

<details>
<summary><b><code>mi watch</code> 报证书校验失败</b></summary>

两种原因：

1. **系统根证书库是空的**。uv 装的 Python 在 macOS 上常见（`ssl.create_default_context()`
   里 0 张证书）。CLI 会自动退回 certifi，能连上；想一劳永逸就在 shell 配置里加
   `export SSL_CERT_FILE=$(python -m certifi)`。
2. **代理在做中间人**。Clash / Surge 的 fake-IP 会把域名解析到 `198.18.x.x`。给
   `mqtt.io.mi.com` 加一条直连规则，或用 `MI_CA_BUNDLE=/path/to/ca.pem` 指定代理根证书。

`mi doctor` 会把这两种情况分别指出来。
</details>

<details>
<summary><b>哪些设备能局域网直连</b></summary>

只有直连路由器的 WiFi/有线设备（`connect_type ∈ {0,8,12,23}` 且有 token）。蓝牙 Mesh、
ZigBee 这些经网关接入的走不了——实测某账号 72 台设备里只有 21 台够格。`mi lan list` 能
看出哪些可以。

**「扫得到」也不等于「能用」**：设备固件对 MIoT spec 方法的支持不一样。实测米家空气净化器 6
的 `get_properties` 走局域网完全可用，而老固件的 Yeelight 台灯 `miIO.info` 通、
`get_properties` 却回 `user ack timeout`。用 `mi lan raw <设备> miIO.info` 可以区分
「协议不通」和「这个方法设备不支持」。`auto` 通道遇到这种设备会自动回落云端，并把结果记下来，
后续命令不再白试。
</details>

<details>
<summary><b>写入返回 <code>code=1</code> 是失败吗</b></summary>

不是。`0` 是成功，`1` 是「已接受、设备执行中」——部分设备（实测 `dwdz.switch.sw0a01`）
对每次写入都回 `1`，操作其实成功了。想要确凿结论加 `--verify`，它会在写完后回读一次。
</details>

## 开发

```bash
uv sync --extra mdns
uv run pytest
```

测试全部离线运行，不需要小米账号：云端接口用 `httpx.MockTransport` 回放，局域网协议用一个
独立实现组包逻辑的假设备跑完整链路，MQTT 用假客户端验证接线。

改了 `pyproject.toml` 后跑 `uv lock` 更新锁文件。

## 实现范围

| | 状态 |
| --- | --- |
| OAuth 登录、token 续期、多账号 | ✅ |
| 家庭 / 房间 / 设备清单 | ✅ |
| spec 获取与缓存、属性读写、动作调用 | ✅ |
| 语义命令、别名、默认家庭 | ✅ |
| 云端 MQTT 实时推送 | ✅ |
| 局域网直连（miIO） | ✅ |
| 米家场景 / 自动化 | ❌ 上游未使用相关接口，需自行调研 |
| 中枢网关本地控制 | ❌ 需要证书，仅 cn 区，复杂度高 |

设计与协议细节见 [docs/design.md](docs/design.md)，完整命令参考见
[docs/cli-spec.md](docs/cli-spec.md)。

## 声明

本项目与小米公司无关，`app/v2/*` 这些接口是小米提供给 Home Assistant 集成使用的，
不是对外承诺的契约，随时可能变化。

账号凭据仅保存在本机，不上传任何第三方服务。本项目不包含 `ha_xiaomi_home` 的任何源码或
资源文件，仅参考其公开的接口行为。
