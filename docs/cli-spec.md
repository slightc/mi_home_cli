# 米家 CLI — 命令清单

可执行文件名：`mi`（备用别名 `mihome`）。设计原则：**高频操作短、低频操作全**。
`mi set 客厅灯 on=true` 这类日常操作不需要记 did，也不需要记 siid/piid（解析规则见 [design.md §4](./design.md#4-两个好用度核心设备解析与属性解析)）。

## 全局参数

| 参数 | 说明 |
| --- | --- |
| `--profile <name>` | 账号/区域配置隔离，默认 `default` |
| `--region <cn\|de\|i2\|ru\|sg\|us>` | 覆盖当前 profile 的区域 |
| `-o, --output <table\|json\|yaml\|plain>` | 输出格式，默认 `table`；非 TTY 自动去色 |
| `--channel <cloud\|auto\|lan>` | 控制通道，默认 `cloud`；`auto` 为局域网优先、失败回落云端 |
| `--timeout <sec>` | 单次请求超时，默认 15 |
| `--no-cache` / `--refresh` | 跳过本地缓存 / 强制刷新缓存 |
| `--dry-run` | 只解析与校验，不发出真实请求 |
| `-v, --verbose` / `-q, --quiet` | 打印完整请求响应 / 只输出结果 |
| `--yes` | 跳过危险操作确认 |
| `--all-homes` | 忽略默认家庭，跨所有家庭操作 |
| `--verify` | 写入后回读一次，用设备真实状态确认（默认不回读，只看返回码） |
| `--show-secrets` | 输出中不打码 device token 等敏感字段 |

---

## 1. 认证 `mi auth`

```bash
mi auth login [--region cn] [--manual] [--no-browser] [--no-mdns]
              [--redirect-url URL] [--wait 300] [--skip-confirm]
mi auth status [--check]       # 登录状态、uid、昵称、token 剩余有效期
mi auth exchange <code|URL>    # 用授权码换 token（换取失败时拿同一个码重试）
mi auth refresh                # 手动刷新 access_token
mi auth logout [--purge]       # 退出；--purge 连同设备缓存一起删
mi auth whoami                 # 打印当前账号信息（实时查询）
mi profile list|use|remove|path
```

小米对 `redirect_uri` 有白名单，host 必须是 `homeassistant.local:8123`
（实测见 [design.md §3.2](./design.md#32-登录流程cli-版本的难点)），
所以登录时 CLI 会监听本机 8123 并尝试用 mDNS 把这个域名指向本机；
不成功也没关系，把浏览器地址栏里的整段地址粘回终端即可。

- `--manual`：不监听端口，只走粘贴。
- `--no-mdns`：不广播 mDNS（局域网里有真的 Home Assistant 时用）。
- `--wait`：等待授权的秒数，默认 300。
- `--device-id`：覆盖 `device_id`（排查用；默认按 HA 的形态生成）。

授权和换 token 两步里的 `client_id` / `redirect_uri` / `device_id` 必须完全一致，
否则服务端返回 `96002 invalid request`，所以这三个值按 profile 固定下来。
加 `-v` 可以看到换 token 时实际发出的请求。

## 2. 家庭 / 房间 / 设备

```bash
mi home list                                   # 家庭列表（含共享家庭），默认家庭标 *
mi home use [<家庭>] [--clear]                 # 设置/查看/取消默认家庭
mi room list [--home <家>]                     # 房间列表

mi device list [--home H] [--room R] [--model M] [--online|--offline]
               [--search 关键词] [--with-sub] [--sort name|room|model]
mi device show <device> [--props]              # 详情；--props 顺带读一遍当前属性值
mi device sync                                 # 重新拉取设备清单缓存
mi device alias set <device> <别名>
mi device alias list|rm
mi device token <device>                       # 打印局域网 token（默认打码）
```

`device list` 默认输出：别名/名称、房间、model、在线、did（缩略）。

## 3. 能力查询 `mi spec`

```bash
mi spec show <device|urn> [--siid N] [--readable|--writable|--notify]
mi spec search <device> <关键词>       # 在该设备的属性/动作里搜
mi spec dump <device|urn> [-o json]    # 完整 spec，喂给脚本或 LLM
mi spec cache clear|info
```

`spec show` 的表格是用户查 `siid.piid`、取值范围、枚举值的入口：

```
SERVICE            PROP                 ID    ACCESS  TYPE    RANGE / VALUES        UNIT
light              on                   2.1   rw n    bool    -                     -
light              brightness           2.2   rw n    uint8   1~100 step 1          %
light              color-temperature    2.3   rw n    uint32  2700~6500 step 1      K
light              mode                 2.4   rw n    uint8   0=Day 1=Night 2=Warm  -
```

## 4. 读写属性

```bash
mi get <device> [prop...]              # 不带 prop 则读所有可读属性
mi get 客厅灯 on brightness
mi get 客厅灯 2.1 -o plain             # 只输出 "true"，方便 shell 取值

mi set <device> <prop=value>...        # 支持一次多条，同一请求批量下发
mi set 客厅灯 on=true brightness=60
mi set 空调 mode=制冷 target-temperature=26

mi watch [device...] [--prop P]... [--events/--no-events] [--state/--no-state]
                     [--exit-after N] [--duration 秒] [-o json]
```

- 值写法：`on/off/true/false/1/0`、数字、枚举描述文本、`"带空格的字符串"`。
- `watch` 走云端 MQTT 长连接，一条变化一行，`Ctrl-C` 退出。不指定设备就盯默认
  家庭里的全部设备。
- `--exit-after N` 收到 N 条后退出、`--duration` 盯多少秒后退出，便于脚本等待
  某个事件。
- 属性变化会显示「旧值 → 新值」（旧值来自本次会话里见过的上一条）。
- 断线自动重连，重连前会用最新的 access_token 换掉密码。

## 5. 调用动作

```bash
mi action <device> <action> [--in 值]... [--param 名=值]...
mi action 扫地机 vacuum.start-sweep
mi action 音箱 play-control.play --param volume=30
mi action list <device>                # 列出该设备所有可调用动作
```

## 6. 语义快捷命令

在 spec 之上的一层「人话映射」。能跨型号工作是因为 MIoT spec 里属性的 urn 名
是标准化的（`on`、`brightness`、`color-temperature`、`target-temperature`、
`fan-level`、`motor-control`…），语义层只认名字不认型号。

设备没有对应属性时直接报错并提示去 `mi spec show --writable` 查，不会默默忽略。
不带任何选项时显示这台设备的当前状态。

```bash
mi on <device>          # 等价于把该设备的 on 属性置 true
mi off <device>
mi toggle <device>

mi light <device> [--on|--off] [--brightness 60] [--ct 4000] [--color '#ff8800'] [--mode 睡眠]
mi climate <device> [--on|--off] [--mode 制冷] [--temp 26] [--fan 2]
mi cover <device> [--open|--close|--stop|--position 50]
mi fan <device> [--on|--off] [--speed 3] [--swing]
```

## 7. 局域网 `mi lan`

```bash
mi lan list                                    # 清单里哪些设备支持直连
mi lan discover [--timeout 3] [--address ...]  # UDP 广播扫描并缓存 IP
mi lan status <device>                         # 是否可直连、IP、延迟
mi lan raw <device> <method> [--params JSON]   # 直接发一条 miIO 请求（排查用）
```

控制命令加 `--channel lan` 就走直连，`--channel auto` 是「能直连就直连，
不成立静默回落云端」。默认是 `cloud`——局域网的探活和回落成本不该由默认承担；
想默认走局域网用 `mi config set channel auto`。

## 8. 原始接口 `mi raw`（逃生舱）

不经过 spec 解析，直接按协议字段发，用于调试和上游改字段时的临时绕行：

```bash
mi raw get <did> <siid> <piid>
mi raw set <did> <siid> <piid> <value>
mi raw action <did> <siid> <aiid> [--in JSON]
mi raw api <METHOD> <path> [--data JSON]        # 直接打云端 API
```

## 9. 其他

```bash
mi config list|get|set|unset|path   # profile / region / output / home / channel
mi doctor                        # 检查登录态、时钟偏差、网络连通、端口占用、文件权限
mi --install-completion     # typer 内置，装当前 shell 的补全
mi repl                          # 交互模式：选中设备后直接 get/set，带 Tab 补全（可选）
mi version
```

---

## 命令与实现阶段对应

| 阶段 | 命令 |
| --- | --- |
| M1 | `auth *`、`profile *`、`home list`、`room list`、`device list/show/sync`、`version` |
| M2 | `spec *`、`get`、`set`、`action`、`raw *`、`-o json` |
| M3 | `on/off/toggle`、`light/climate/cover/fan`、`device alias`、`completion`、`doctor`、`config` |
| M4 | `watch` |
| M5 | `lan *`、`--channel auto` 回落 |
| 可选 | `repl`、场景相关命令（接口需先验证） |
