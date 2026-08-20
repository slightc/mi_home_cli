# mi_home_cli

命令行控制米家（Xiaomi Home）设备。协议参考 [XiaoMi/ha_xiaomi_home](https://github.com/XiaoMi/ha_xiaomi_home) 的行为独立实现，非小米官方项目。

```bash
mi auth login
mi device list
mi set 客厅灯 on=true brightness=60
mi watch 客厅灯 --events
```

## 文档

- [实现方案设计](docs/design.md) — 认证、云端 API、spec、局域网、MQTT、分层与分期
- [命令清单](docs/cli-spec.md) — CLI 的全部命令与参数

## 状态

设计阶段，尚未开始编码。

## 声明

本项目与小米公司无关。账号凭据仅保存在本机 `~/.config/mi-home-cli/`（权限 0600），不上传任何第三方服务。
本项目不包含 `ha_xiaomi_home` 的任何源码或资源文件，仅参考其公开的接口行为。
