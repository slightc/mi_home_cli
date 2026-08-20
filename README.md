# mi_home_cli

命令行控制米家（Xiaomi Home）设备。协议参考 [XiaoMi/ha_xiaomi_home](https://github.com/XiaoMi/ha_xiaomi_home) 的行为独立实现，非小米官方项目。

## 安装

依赖用 [uv](https://docs.astral.sh/uv/) 管理。

```bash
uv sync --extra mdns          # mdns 是可选依赖，能提高登录时自动接收回调的成功率
uv run mi --help
```

想要一个全局可用的 `mi` 命令：

```bash
uv tool install --with zeroconf .
```

## 登录

```bash
uv run mi doctor        # 先看看端口、域名解析、网络、时钟有没有问题
uv run mi auth login
uv run mi auth status
```

小米的 OAuth 服务只接受 host 为 `homeassistant.local:8123` 的回调地址（实测结论见
[design.md §3.2](docs/design.md#32-登录流程cli-版本的难点)），因此 `mi auth login` 会：

1. 监听本机 8123 端口；
2. 尝试用 mDNS 把 `homeassistant.local` 指向本机，让浏览器能跳回来；
3. 上面两步不成功也不影响——授权后浏览器会停在一个打不开的地址上，
   把地址栏里的整段地址粘回终端即可（`mi auth login --manual` 直接走这条路）。

凭据保存在 `~/.config/mi-home-cli/profiles/<profile>/auth.json`，权限 0600，
可用 `MI_HOME_CONFIG_DIR` 改位置。多账号/多区域用 `--profile` 隔离。

## 已经能用的命令

```bash
# 登录
mi auth login|status|refresh|whoami|logout|exchange
mi profile list|use|remove|path

# 默认家庭（多个家庭时强烈建议设一个）
mi home use 我家              # 之后所有命令只看这个家庭
mi home use                   # 看当前默认
mi home use --clear           # 取消
mi config list|get|set|unset  # profile / region / output / home

# 看有什么
mi home list
mi room list [--home 我家]
mi device list [--home] [--room] [--model] [--search] [--online|--offline] [--wide]
mi device show <设备>
mi device sync
mi device alias set <设备> <别名>
mi spec show <设备> [--writable] [--actions]
mi spec search <设备> <关键词>
mi spec dump <设备>

# 控制
mi get <设备> [属性...]           # 不写属性就读全部可读属性
mi set <设备> <属性=值>...
mi action <设备> [动作] [--in 值]...
mi on|off|toggle <设备>

# 语义命令（不带选项时显示当前状态）
mi light <设备> [--on|--off] [--brightness 60] [--ct 4000] [--color 红] [--mode 日光]
mi climate <设备> [--on|--off] [--mode 制冷] [--temp 26] [--fan 2]
mi cover <设备> [--open|--close|--stop] [--position 50]
mi fan <设备> [--on|--off] [--speed 2] [--mode 自动] [--swing]

mi doctor
mi version
```

设了默认家庭之后，设备解析、`device list`、`room list` 都只看这个家庭，
跨家庭要加 `--all-homes`。设备在别的家庭时，报错会直接告诉你它在哪儿。

设备可以用名称、别名、`房间/名称`、did 或型号来指；属性可以写 `on`、
`light.brightness` 或 `2.1`，枚举值可以直接写中文描述（`mode=睡眠`）。
加 `--dry-run` 只解析校验不下发，加 `-o json` 输出给脚本用。

写操作会把「旧值 → 新值」一起打出来，改错了照着回滚就行。

Shell 补全用 typer 内置的：`mi --install-completion`（或 `mi --show-completion`
自己贴进 rc 文件）。

局域网控制和 `mi watch` 还没做，见 [docs/cli-spec.md](docs/cli-spec.md)。

全局参数（`--profile` / `--region` / `--output` / `--timeout`）放在子命令之前，
例如 `mi -o json auth status`；`-o` 也可以直接跟在子命令后面，或用环境变量
`MI_PROFILE` / `MI_REGION` / `MI_OUTPUT`。

## 开发

```bash
uv sync --extra mdns    # 创建 .venv 并按 uv.lock 装齐运行时依赖 + dev 依赖组
uv run pytest
```

`uv.lock` 已提交，用来锁定可复现的依赖版本；改了 `pyproject.toml` 后跑
`uv lock` 更新它。测试全部离线运行，不需要小米账号。

## 文档

- [实现方案设计](docs/design.md) — 认证、云端 API、spec、局域网、MQTT、分层与分期
- [命令清单](docs/cli-spec.md) — CLI 的全部命令与参数

## 声明

本项目与小米公司无关。账号凭据仅保存在本机，不上传任何第三方服务。
本项目不包含 `ha_xiaomi_home` 的任何源码或资源文件，仅参考其公开的接口行为。
