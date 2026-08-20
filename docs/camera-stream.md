# 哪些摄像机能通过 OAuth2 拿到推流地址

部分型号的 spec 里带 `camera-stream-for-google-home` /
`camera-stream-for-amazon-alexa` 服务。这两个服务本来是给 Google Home 和 Alexa 用的，
但它们就是普通的 spec 服务——**任何能调 spec 动作的客户端都能用**，本项目的 OAuth2
身份足够，不需要账号密码那套。

```bash
mi action <摄像机>                        # 有 start-rtsp-stream 就能拿地址
mi action <摄像机> start-rtsp-stream --in 2
# ↳ stream-address    rtsp://...
# ↳ expiration-time   ...
```

地址有过期时间，用完记得 `stop-stream`。

## 支不支持是按 SKU 定的

同一款产品的不同 SKU 可能一个有一个没有，别按产品名判断：

| 型号 | 官方推流 |
| --- | --- |
| `chuangmi.camera.039c01`（智能摄像机2 云台版） | ❌ |
| `chuangmi.camera.039c04`（同产品另一 SKU） | ✅ |
| `chuangmi.camera.079ac1`（智能摄像机4 4K） | ❌ |
| `chuangmi.camera.079ae2`（同产品另一 SKU） | ✅ |

没有这个服务的型号只有 `p2p-stream`，只能靠
[go2rtc](https://go2rtc.org/internal/xiaomi/) 之类自己实现了 cs2 的项目取流，原因见下。

## 为什么 `p2p-stream` 在 OAuth2 下取不到画面

结论：**取不到**，而且不是"接口没找到"，是身份边界决定的。实测证据：

**1. 设备侧不给连接参数。** 调 `start-p2p-stream`（siid 9 / aiid 1）返回
`code: 0` 但 `out` 为空——会话开了，可地址、session id、密钥一个都没有：

```json
{"did":"...","siid":9,"aiid":1,"code":0,"exe_time":99}
```

**2. 密钥要单独换，而那个接口在另一套身份下。** go2rtc（MIT）的实现里
（`internal/xiaomi/xiaomi.go`），每次连接前要向云端换密钥：

```go
cloudUserRequest(url.User, "/v2/device/miss_get_vendor", params)
// {"app_pubkey":"<客户端公钥>","did":"...","support_vendors":"TUTK_CS2_MTP"}
// 老设备回退到 /device/devicepass
```

这些请求用 `ssecurity` 派生 `signedNonce` 做 RC4 签名，而 `ssecurity` 来自
**账号密码登录**（`serviceLogin` → `passToken` + `serviceToken`）。

**3. OAuth2 网关上没有这个接口。**

| 请求 | 结果 |
| --- | --- |
| `ha.api.io.mi.com/app/v2/device/miss_get_vendor` | 404 |
| `ha.api.io.mi.com/app/v2/device/devicepass` | 404 |
| `ha.api.io.mi.com/v2/device/miss_get_vendor` | 204 |
| `ha.api.io.mi.com/随便一个不存在的路径` | 204 |

**204 是这个网关对所有非 `/app/v2/*` 请求的兜底响应**，不代表接口存在——拿乱写的
路径对照过。

**4. 拿 OAuth token 去敲 App 的接口域名会被拒。**

```
api.io.mi.com/app/v2/device/miss_get_vendor → 401 {"code":0,"message":"auth error"}
```

它只认 `serviceToken` + RC4 签名。

所以：小米把**控制面**（spec 接口）开给了 OAuth2，**数据面**（p2p 密钥交换）留在账号
密码那套身份里。本项目只用 OAuth2，这条路就是死的——除非哪天小米把
`miss_get_vendor` 挂到 `/app/v2/*` 下面。

## 快照（2026-08-20，共 56 个型号）

名单会随小米发布新 spec 变化，用 `uv run python tools/scan_stream_models.py` 重新扫。

### 能直接拿到地址（43 个）

`start-rtsp-stream` / `start-hls-stream` 返回 URL。

- `chuangmi.camera.021a04`
- `chuangmi.camera.026c02`
- `chuangmi.camera.026c05`
- `chuangmi.camera.029a02`
- `chuangmi.camera.029b06`
- `chuangmi.camera.039a04`
- `chuangmi.camera.039c04`
- `chuangmi.camera.046a01`
- `chuangmi.camera.046c04`
- `chuangmi.camera.055c02`
- `chuangmi.camera.060a02`
- `chuangmi.camera.065ac1`
- `chuangmi.camera.068ac1`
- `chuangmi.camera.072ae2`
- `chuangmi.camera.075ae1`
- `chuangmi.camera.077ac1`
- `chuangmi.camera.079ae2`
- `chuangmi.camera.081ae2`
- `chuangmi.camera.111ae1`
- `chuangmi.camera.112ae1`
- `chuangmi.camera.115ac1`
- `chuangmi.camera.46e01`
- `chuangmi.camera.ipc009`
- `chuangmi.camera.ipc019`
- `chuangmi.camera.ipc019b`
- `chuangmi.camera.ipc021`
- `isa.camera.500dh`
- `isa.camera.hlc6`
- `isa.camera.hlc7`
- `isa.camera.hlc8a`
- `isa.camera.hlc9a`
- `mijia.camera.v1`
- `mijia.camera.v3`
- `mxiang.camera.c301`
- `mxiang.camera.c500os`
- `mxiang.camera.moc006`
- `mxiang.camera.moc008`
- `mxiang.camera.mod13`
- `mxiang.camera.mwc11`
- `xiaomi.camera.080ao1`
- `xiaomi.camera.c01a01`
- `xiaomi.camera.c302o`
- `xiaovv.camera.xvvi01`

### 只提供 WebRTC 协商（13 个）

只有 `initiate-webrtc-session`，要做 SDP offer/answer，拿不到现成地址，`mi action` 帮不上。

- `chuangmi.camera.061a03`
- `chuangmi.camera.120ae1`
- `isa.camera.700sa`
- `madv.cateye.mi3sg`
- `midr.camera.bw300`
- `midr.camera.bw400`
- `midr.camera.bw400b`
- `midr.camera.bw400g`
- `midr.camera.bw400p`
- `xiaomi.camera.083ae2`
- `xiaomi.camera.c201`
- `xiaomi.camera.c302n`
- `xiaomi.camera.c500a`
