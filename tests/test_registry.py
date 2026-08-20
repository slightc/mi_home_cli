"""设备解析：CLI 好不好用全看这里。"""
import pytest

from mi_home_cli.core.registry import Registry
from mi_home_cli.errors import AmbiguousReference, DeviceNotFound

DATA = {
    "uid": "1",
    "synced_at": 1700000000,
    "homes": [
        {"home_id": "h1", "home_name": "我家", "rooms": []},
        {"home_id": "h2", "home_name": "老家", "rooms": []},
    ],
    "devices": {
        "100": {
            "did": "100", "name": "客厅吸顶灯", "model": "yeelink.light.ml13",
            "urn": "urn:1", "online": True, "home_id": "h1", "home_name": "我家",
            "room_id": "r1", "room_name": "客厅",
        },
        "200": {
            "did": "200", "name": "卧室吸顶灯", "model": "yeelink.light.ml13",
            "urn": "urn:1", "online": False, "home_id": "h1", "home_name": "我家",
            "room_id": "r2", "room_name": "卧室",
        },
        "300": {
            "did": "300", "name": "空气净化器", "model": "xiaomi.airp.ma6",
            "urn": "urn:2", "online": True, "home_id": "h1", "home_name": "我家",
            "room_id": "r1", "room_name": "客厅",
        },
    },
}


@pytest.fixture()
def registry() -> Registry:
    return Registry(DATA, {"净化": "300"})


def test_resolve_by_did(registry: Registry):
    assert registry.resolve("200").name == "卧室吸顶灯"


def test_resolve_by_alias(registry: Registry):
    device = registry.resolve("净化")
    assert device.did == "300"
    assert device.label == "净化"  # 有别名就显示别名


def test_resolve_by_exact_name(registry: Registry):
    assert registry.resolve("客厅吸顶灯").did == "100"


def test_resolve_by_room_slash_name(registry: Registry):
    assert registry.resolve("卧室/卧室吸顶灯").did == "200"


def test_resolve_by_substring(registry: Registry):
    assert registry.resolve("净化器").did == "300"


def test_resolve_by_model(registry: Registry):
    assert registry.resolve("xiaomi.airp").did == "300"


def test_ambiguous_lists_candidates(registry: Registry):
    with pytest.raises(AmbiguousReference) as excinfo:
        registry.resolve("吸顶灯")
    message = str(excinfo.value)
    assert "客厅吸顶灯" in message and "卧室吸顶灯" in message
    assert excinfo.value.exit_code == 4


def test_filters_narrow_ambiguity(registry: Registry):
    assert registry.resolve("吸顶灯", room="客厅").did == "100"


def test_unknown_device(registry: Registry):
    with pytest.raises(DeviceNotFound) as excinfo:
        registry.resolve("洗衣机")
    assert excinfo.value.exit_code == 3


def test_exact_name_beats_substring():
    """名字是另一个名字的前缀时，精确匹配优先，不该报歧义。"""
    data = {
        **DATA,
        "devices": {
            "1": {"did": "1", "name": "灯", "model": "m", "urn": "u", "online": True},
            "2": {"did": "2", "name": "灯带", "model": "m", "urn": "u", "online": True},
        },
    }
    assert Registry(data).resolve("灯").did == "1"


def test_filter_by_online_and_search(registry: Registry):
    assert [d.did for d in registry.filter(online=False)] == ["200"]
    assert {d.did for d in registry.filter(search="灯")} == {"100", "200"}
    assert [d.did for d in registry.filter(room="客厅", model="airp")] == ["300"]


def test_find_home_by_name_and_id(registry: Registry):
    assert registry.find_home("我家")["home_id"] == "h1"
    assert registry.find_home("h2")["home_name"] == "老家"
    assert registry.find_home("老")["home_id"] == "h2"


def test_find_home_unknown(registry: Registry):
    with pytest.raises(DeviceNotFound):
        registry.find_home("别人家")


def test_find_home_ambiguous():
    data = {
        **DATA,
        "homes": [
            {"home_id": "h1", "home_name": "我家一号", "rooms": []},
            {"home_id": "h2", "home_name": "我家二号", "rooms": []},
        ],
    }
    with pytest.raises(AmbiguousReference):
        Registry(data).find_home("我家")


def test_filter_by_home_id_is_exact(registry: Registry):
    """按 id 过滤，家庭改名了也不受影响。"""
    assert len(registry.filter(home_id="h1")) == 3
    assert registry.filter(home_id="h2") == []


def test_default_home_scope_removes_ambiguity():
    """两个家庭各有一盏同名的灯，限定家庭后就不歧义了。"""
    data = {
        **DATA,
        "devices": {
            "1": {
                "did": "1", "name": "台灯", "model": "m", "urn": "u",
                "online": True, "home_id": "h1", "home_name": "我家",
            },
            "2": {
                "did": "2", "name": "台灯", "model": "m", "urn": "u",
                "online": True, "home_id": "h2", "home_name": "老家",
            },
        },
    }
    registry = Registry(data)
    with pytest.raises(AmbiguousReference):
        registry.resolve("台灯")
    assert registry.resolve("台灯", home_id="h1").did == "1"
