"""云端接口的数据组装。"""
import httpx
import pytest

from mi_home_cli.core.cloud import CloudApi
from mi_home_cli.core.session import Session
from mi_home_cli.store import AuthData, Profile

HOME_RESULT = {
    "code": 0,
    "result": {
        "homelist": [
            {
                "id": 111, "name": "我家", "uid": 999, "dids": ["d-home"],
                "roomlist": [
                    {"id": 11, "name": "客厅", "dids": ["d1"]},
                    {"id": 12, "name": "卧室", "dids": ["d2", "d2.s1"]},
                ],
            }
        ],
        "share_home_list": [
            {"id": 222, "name": "朋友家", "uid": 888, "dids": [], "roomlist": []}
        ],
    },
}


def _device(did, name, **extra):
    return {
        "did": did, "name": name, "model": "vendor.light.v1",
        "spec_type": "urn:x", "isOnline": True, **extra,
    }


def _session(profile: Profile, handler) -> Session:
    profile.write_auth(
        AuthData("AT", "RT", "cn", "ha.abc", 1700000000, 10**9)
    )
    return Session(profile, client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_fetch_homes_flattens_rooms(profile: Profile):
    with _session(profile, lambda r: httpx.Response(200, json=HOME_RESULT)) as s:
        homes = CloudApi(s).fetch_homes()
    assert homes["uid"] == "999"
    assert [h["home_name"] for h in homes["homes"]] == ["我家", "朋友家"]
    assert homes["homes"][0]["shared"] is False
    assert homes["homes"][1]["shared"] is True
    assert [r["room_name"] for r in homes["homes"][0]["rooms"]] == ["客厅", "卧室"]


def test_device_list_page_follows_pagination(profile: Profile):
    pages = [
        {
            "code": 0,
            "result": {
                "list": [_device("d1", "灯一")],
                "has_more": True,
                "next_start_did": "d1",
            },
        },
        {"code": 0, "result": {"list": [_device("d2", "灯二")], "has_more": False}},
    ]
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=pages[len(calls) - 1])

    with _session(profile, handler) as s:
        devices = CloudApi(s).fetch_device_details(["d1", "d2"])
    assert sorted(devices) == ["d1", "d2"]
    assert len(calls) == 2


def test_fetch_all_merges_placement_and_folds_sub_devices(profile: Profile):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/homeroom/gethome"):
            return httpx.Response(200, json=HOME_RESULT)
        body = request.read().decode()
        if '"dids": []' in body.replace("\n", ""):  # 查单独分享设备那次
            return httpx.Response(200, json={"code": 0, "result": {"list": []}})
        return httpx.Response(
            200,
            json={
                "code": 0,
                "result": {
                    "list": [
                        _device("d1", "客厅灯"),
                        _device("d2", "网关"),
                        _device("d2.s1", "子设备"),
                        _device("d-home", "没分房间的"),
                    ]
                },
            },
        )

    with _session(profile, handler) as s:
        data = CloudApi(s).fetch_all()

    devices = data["devices"]
    assert devices["d1"]["room_name"] == "客厅"
    assert devices["d1"]["home_name"] == "我家"
    # 家庭直属设备没有房间
    assert devices["d-home"]["room_name"] == ""
    # 子设备折进父设备，不单独出现在列表里
    assert "d2.s1" not in devices
    assert devices["d2"]["sub_devices"]["s1"]["name"] == "子设备"


def test_ignores_miwifi_devices(profile: Profile):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "code": 0,
                "result": {"list": [_device("miwifi.1", "路由器"), _device("d1", "灯")]},
            },
        )

    with _session(profile, handler) as s:
        devices = CloudApi(s).fetch_device_details(["miwifi.1", "d1"])
    assert list(devices) == ["d1"]


def test_get_props_is_chunked(profile: Profile):
    sizes = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        sizes.append(len(_json.loads(request.read())["params"]))
        return httpx.Response(200, json={"code": 0, "result": []})

    params = [{"did": "d", "siid": 1, "piid": i} for i in range(210)]
    with _session(profile, handler) as s:
        CloudApi(s).get_props(params)
    assert sizes == [150, 60]


def test_action_sends_bare_value_list(profile: Profile):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        seen.update(_json.loads(request.read())["params"])
        return httpx.Response(200, json={"code": 0, "result": {"code": 0}})

    with _session(profile, handler) as s:
        CloudApi(s).call_action("d1", 2, 1, [5, "x"])
    # 这个接口要的是值的数组，不是 {piid, value}
    assert seen == {"did": "d1", "siid": 2, "aiid": 1, "in": [5, "x"]}


def test_set_result_code_1_is_accepted_not_failure():
    """有些设备（如 dwdz.switch.sw0a01）对每次写入都回 code=1，操作其实成功了。

    实测：读当前值 code=0，写回同一个值 code=1，设备状态正常；把非 0 一律
    当失败会让所有写操作都报错。
    """
    from mi_home_cli.cli.prop import ACCEPTED_CODES, _explain_code

    assert 0 in ACCEPTED_CODES and 1 in ACCEPTED_CODES
    assert -704010000 not in ACCEPTED_CODES
    assert _explain_code(1) == "已接受"
    assert "失败" in _explain_code(-704220025) or "不可写" in _explain_code(-704220025)
