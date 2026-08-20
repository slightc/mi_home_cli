# mi_home_cli

命令行控制米家（Xiaomi Home）设备。协议参考 [XiaoMi/ha_xiaomi_home](https://github.com/XiaoMi/ha_xiaomi_home) 的行为独立实现，非小米官方项目。

## 安装

```bash
pip install -e '.[mdns]'      # mdns 是可选依赖，能提高登录时自动接收回调的成功率
```

## 登录

```bash
mi doctor        # 先看看端口、域名解析、网络、时钟有没有问题
mi auth login
mi auth status
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
mi auth login|status|refresh|whoami|logout
mi profile list|use|remove|path
mi doctor
mi version
```

设备控制相关命令（`mi device list`、`mi get`、`mi set`、`mi action`、`mi watch`）
按 [docs/cli-spec.md](docs/cli-spec.md) 分期实现中。

全局参数（`--profile` / `--region` / `--output` / `--timeout`）放在子命令之前，
例如 `mi -o json auth status`；`-o` 也可以直接跟在子命令后面，或用环境变量
`MI_PROFILE` / `MI_REGION` / `MI_OUTPUT`。

## 开发

```bash
python -m venv .venv && .venv/bin/pip install -e '.[dev,mdns]'
.venv/bin/python -m pytest
```

测试全部离线运行，不需要小米账号。

## 文档

- [实现方案设计](docs/design.md) — 认证、云端 API、spec、局域网、MQTT、分层与分期
- [命令清单](docs/cli-spec.md) — CLI 的全部命令与参数

## 声明

本项目与小米公司无关。账号凭据仅保存在本机，不上传任何第三方服务。
本项目不包含 `ha_xiaomi_home` 的任何源码或资源文件，仅参考其公开的接口行为。
