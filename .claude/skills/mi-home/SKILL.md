---
name: mi-home
description: 用本仓库的 mi 命令行工具控制米家（Xiaomi Home）智能家居设备——开关灯、调亮度色温、读温湿度和空气质量、查设备状态、调用设备动作、订阅实时事件。凡是用户提到家里的灯、插座、空调、风扇、窗帘、净化器、扫地机、摄像机、传感器，或者说「把客厅灯打开」「屋里多少度」「净化器什么模式」「有人回家时提醒我」这类话，都应该用这个 skill，即使用户没有提到「米家」或「mi 命令」。控制真实设备会产生物理效果，这个 skill 也说明了该怎么谨慎操作。
---

# 用 mi 控制米家设备

`mi` 是本仓库提供的命令行工具，走小米云端 OAuth2 接口操作真实设备。

## 先确认能用

```bash
mi auth status          # 未登录会以退出码 10 失败
```

没登录就停下来告诉用户跑 `mi auth login`（要在浏览器里完成授权），**不要**替用户尝试登录流程。

## 核心循环：定位设备 → 查能力 → 操作

```bash
mi device list --search 灯 -o json     # 有哪些设备
mi spec show <设备> --writable         # 这台设备能改什么，取值范围是什么
mi get <设备> [属性...]                 # 读
mi set <设备> <属性=值>...              # 写
```

**不要凭记忆猜 `siid.piid`。** 不同型号编号不同，`mi spec show` 会给出准确的 id、
取值范围和枚举值。属性可以直接用名字（`brightness`、`light.on`），比数字更稳。

## 给 agent 的几条要点

### 用 JSON 解析，别解析表格

```bash
mi -o json device list             # 结构化，字段名是英文，可靠
mi -o plain get 台灯 brightness     # 单值，输出 "60"，适合直接用
```

默认的表格输出是给人看的，会按终端宽度截断，不要拿去解析。JSON 里的字段名和
类型是稳定的：

```json
{"did": "305792037", "name": "33的台灯", "alias": null, "model": "yeelink.light.lamp4",
 "home": "我家", "room": "书房", "online": true, "connect_type": 0}
```

`mi get <设备> -o json` 返回的是 `{"属性全名": 值}`，比如
`{"light.on": true, "light.brightness": 60}`。

### 退出码就是分类结果，照着分支处理

| 码 | 含义 | 该怎么办 |
| --- | --- | --- |
| 0 | 成功 | — |
| 3 | 设备不存在 | 用 `mi device list` 确认名字 |
| 4 | 设备名有歧义 | **报错信息里列出了候选，把它们呈现给用户让他选**，别自己挑 |
| 5 | 设备离线 | 告诉用户设备不在线 |
| 6 | 属性不存在 | 用 `mi spec show` 查这台设备到底支持什么 |
| 7 | 值非法（越界/只读） | 报错里有正确范围，据此修正 |
| 10 | 未登录 | 让用户跑 `mi auth login` |

歧义（4）尤其重要：用户家里常有多盏「吸顶灯」「台灯」。**替用户猜一个去开关，
是这个工具最容易造成实际损害的方式**——可能开的是别人卧室的灯。

### 写操作有物理效果

一条 `mi set` 会真的点亮某个房间的灯、启动某台电器。所以：

- **拿不准就先 `--dry-run`**：只解析校验、不下发，用来确认解析到的是哪台设备、
  写的是哪个属性、值转成了什么。

  ```bash
  mi set 台灯 brightness=60 --dry-run
  ```
- **写完把「旧值 → 新值」报给用户**：`mi set` 本来就会打印，转述它。改错了用户能
  照着回滚。
- **深夜、卧室、别人的房间**这类场景，动手前先确认一次。用户说「关灯」时，
  如果匹配到多盏，见上面的退出码 4。
- 不要为了「验证成功」去反复开关设备。要确凿结论加 `--verify`（写完回读一次）。
- **有些设备被写入参数时会自己启动**：比如 Yeelight 台灯，灯关着时写
  `brightness=60`，它会顺带把灯点亮。所以「只想改个参数、别把设备打开」这种意图，
  写完要回读确认状态，并把实际结果如实告诉用户。

### 一次命令做完，别拆成多条

```bash
mi set 台灯 on=true brightness=60 color-temperature=4000    # 好：一次批量下发
```

比连发三条 `mi set` 快，也避免设备状态中间态。

### 值可以写人话

```bash
mi set 净化器 mode=睡眠        # 枚举值直接写中文描述（英文原文也认）
mi set 台灯 on=开             # on/off、true/false、1/0、开/关 都行
mi light 台灯 --color 红       # 颜色支持 #ff8800 和常见颜色名
```

数值超范围、写只读属性，会在发请求前就被拦下并告诉你正确范围——不用自己校验。

### 语义命令更省事

```bash
mi on|off|toggle <设备>
mi light <设备> [--on|--off] [--brightness 60] [--ct 4000] [--color 暖]
mi climate <设备> [--on|--off] [--mode 制冷] [--temp 26]
mi fan <设备> [--speed 2] [--mode 自动]
mi cover <设备> [--open|--close|--position 50]
```

不带选项时显示当前状态。设备不支持某个选项会明确报错，不会默默忽略。

### 等事件用 watch，不要轮询

```bash
mi watch <设备> --events --exit-after 1     # 阻塞到第一条事件就返回
mi watch <设备> -o json --duration 60       # 逐行 JSON，可以管道处理
```

走 MQTT 长连接。**不要写 `while true; do mi get ...; done` 这种轮询**——慢、吵、
还可能触发限流。

## 常见任务

**「屋里多少度」「空气怎么样」**
```bash
mi get 净化器 -o json        # 温湿度、PM2.5、甲醛都在可读属性里
```
先 `mi device list --search 温` / `--search 净化` 找到带传感器的设备。

**「把客厅的灯都关了」**
```bash
mi device list --room 客厅 --search 灯 -o json     # 先列出来
mi off <逐个设备>                                  # 再逐台关
```
多台设备时**先把清单给用户确认**再动手。

**「有人回家时…」**
```bash
mi watch 门锁 --events -o json --exit-after 1
```

**用户家里有多个家庭**（`mi home list` 能看到）时，建议设默认家庭，能大幅减少歧义：
```bash
mi home use 我家
```

## 更多

- 完整命令与参数：本仓库 `docs/cli-spec.md`
- 设计与协议细节、已知限制：`docs/design.md`
- 摄像机推流地址支持情况：`docs/camera-stream.md`
- 已知做不到的事：米家场景/自动化、摄像机录像回放（需要账号密码那套身份，
  本项目只用 OAuth2）；多数摄像机拿不到实时画面
