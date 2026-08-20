"""扫描 miot-spec 上所有摄像机/门铃型号，找出带官方推流服务的那些。

结论会随小米发布新 spec 变化，所以这份脚本比一张静态表更有用：

    uv run python tools/scan_stream_models.py            # 打印表格
    uv run python tools/scan_stream_models.py --json     # 输出 JSON
"""
from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor

import httpx

INSTANCES = "https://miot-spec.org/miot-spec-v2/instances"
INSTANCE = "https://miot-spec.org/miot-spec-v2/instance"
DEVICE_TYPES = (":device:camera:", ":device:video-doorbell:")
# 这两个服务本来是给 Google Home / Alexa 用的，但它们是普通的 spec 服务，
# 任何能调 spec 动作的客户端都能用
STREAM_SERVICE_PREFIX = "camera-stream-for-"


def scan(timeout: float = 30.0, workers: int = 16) -> dict[str, dict]:
    client = httpx.Client(timeout=timeout)
    instances = client.get(INSTANCES, params={"status": "released"}).json()["instances"]
    cameras = [
        item
        for item in instances
        if any(kind in item["type"] for kind in DEVICE_TYPES)
    ]

    def check(item: dict) -> tuple[str, dict] | None:
        try:
            spec = client.get(INSTANCE, params={"type": item["type"]}).json()
        except (httpx.HTTPError, ValueError):
            return None
        actions: set[str] = set()
        for service in spec.get("services", []):
            name = service["type"].split(":")[3]
            if not name.startswith(STREAM_SERVICE_PREFIX):
                continue
            actions.update(
                action["type"].split(":")[3] for action in service.get("actions", [])
            )
        if not actions:
            return None
        return item["model"], {"actions": sorted(actions)}

    found: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for result in pool.map(check, cameras):
            if result:
                model, info = result
                found.setdefault(model, {"actions": []})
                info["actions"] = sorted(
                    set(found[model]["actions"]) | set(info["actions"])
                )
                found[model] = info
    for info in found.values():
        starts = [a for a in info["actions"] if a.startswith("start-")]
        # 拿得到地址的（start-rtsp/hls-stream）和要走 WebRTC 协商的，用法完全不同
        info["kind"] = "url" if starts else "webrtc"
    return dict(sorted(found.items()))


def main() -> None:
    found = scan()
    if "--json" in sys.argv:
        print(json.dumps(found, ensure_ascii=False, indent=2))
        return
    url_models = {k: v for k, v in found.items() if v["kind"] == "url"}
    webrtc = {k: v for k, v in found.items() if v["kind"] == "webrtc"}
    print(f"能直接拿到推流地址的型号（{len(url_models)}）：")
    for model, info in url_models.items():
        print(f"  {model:30} {'、'.join(info['actions'])}")
    print(f"\n只提供 WebRTC 协商的型号（{len(webrtc)}，拿不到现成地址）：")
    for model in webrtc:
        print(f"  {model}")


if __name__ == "__main__":
    main()
