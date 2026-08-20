# 米家 CLI — 命令清单

可执行文件名：`mi`（备用别名 `mihome`）。设计原则：**高频操作短、低频操作全**。
`mi set 客厅灯 on=true` 这类日常操作不需要记 did，也不需要记 siid/piid（解析规则见 [design.md §4](./design.md#4-两个好用度核心设备解析与属性解析)）。

## 全局参数

| 参数 | 说明 |
| --- | --- |
| `--profile <name>` | 账号/区域配置隔离，默认 `default` |
| `--region <cn\|de\|i2\|ru\|sg\|us>` | 覆盖当前 profile 的区域 |
| `-o, --output <table\|json\|yaml\|plain>` | 输出格式，默认 `table`；非 TTY 自动去色 |
| `--channel <auto\|cloud\|lan>` | 控制通道，默认 `auto`（局域网可达优先，失败回落云端） |
| `--timeout <sec>` | 单次请求超时，默认 15 |
| `--no-cache` / `--refresh` | 跳过本地缓存 / 强制刷新缓存 |
| `--dry-run` | 只解析与校验，不发出真实请求 |
| `-v, --verbose` / `-q, --quiet` | 打印完整请求响应 / 只输出结果 |
| `--yes` | 跳过危险操作确认 |
| `--show-secrets` | 输出中不打码 device token 等敏感字段 |

---

## 1. 认证 `mi auth`

```bash
mi auth login [--region cn] [--port 9527] [--manual] [--redirect-url URL] [--no-browser]
mi auth status                 # 登录状态、uid、昵称、token 剩余有效期
mi auth refresh                # 手动刷新 access_token
mi auth logout [--purge]       # 退出；--purge 连同设备缓存一起删
mi auth whoami                 # 打印当前 profile 的账号信息
mi profile list|use|remove
```

`--manual`：不起本地服务，打印授权链接，用户粘回重定向后的完整 URL 或 `code`（`redirect_uri` 白名单兜底方案）。

## 2. 家庭 / 房间 / 设备

```bash
mi home list                                   # 家庭列表（含共享家庭）
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

mi watch [device...] [--all] [--prop P]... [--events] [--state] [--since]
                     [-o json] [--exit-after N]
```

- 值写法：`on/off/true/false/1/0`、数字、枚举描述文本、`"带空格的字符串"`。
- `watch` 走云端 MQTT 长连接，每行一条 JSON/表格记录，`Ctrl-C` 退出；`--exit-after N` 收到 N 条后退出，便于脚本等待某个事件。

## 5. 调用动作

```bash
mi action <device> <action> [--in 值]... [--param 名=值]...
mi action 扫地机 vacuum.start-sweep
mi action 音箱 play-control.play --param volume=30
mi action list <device>                # 列出该设备所有可调用动作
```

## 6. 语义快捷命令（M3）

在 spec 之上做的一层「人话映射」，找不到对应属性时报错并提示用 `mi set`：

```bash
mi on <device>          # 等价于把该设备的 on 属性置 true
mi off <device>
mi toggle <device>

mi light <device> [--on|--off] [--brightness 60] [--ct 4000] [--color '#ff8800'] [--mode 睡眠]
mi climate <device> [--on|--off] [--mode 制冷] [--temp 26] [--fan 2]
mi cover <device> [--open|--close|--stop|--position 50]
mi fan <device> [--on|--off] [--speed 3] [--swing]
```

## 7. 局域网 `mi lan`（M5）

```bash
mi lan discover [--iface eth0] [--timeout 5]   # UDP 广播扫同网段设备
mi lan status <device>                          # 是否可直连、IP、延迟
mi lan get/set/action ...                       # 等价于 --channel lan
```

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
mi config get|set|list|path      # region / output / channel / timeout / cache TTL
mi doctor                        # 检查登录态、时钟偏差、网络连通、端口占用、文件权限
mi completion bash|zsh|fish
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
