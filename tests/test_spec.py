"""spec 解析与取值转换。"""
import pytest

from mi_home_cli.core.spec import (
    DeviceSpec,
    format_value,
    parse_value,
    unit_symbol,
    urn_name,
)
from mi_home_cli.errors import AmbiguousReference, InvalidValue, SpecNotFound

INSTANCE = {
    "type": "urn:miot-spec-v2:device:air-purifier:0000A007:xiaomi-ma6:1",
    "description": "Air Purifier",
    "services": [
        {
            "iid": 2,
            "type": "urn:miot-spec-v2:service:air-purifier:00007811:x:1",
            "properties": [
                {
                    "iid": 1,
                    "type": "urn:miot-spec-v2:property:on:00000006:x:1",
                    "format": "bool",
                    "access": ["read", "write", "notify"],
                },
                {
                    "iid": 3,
                    "type": "urn:miot-spec-v2:property:mode:00000008:x:1",
                    "description": "Mode",
                    "format": "uint8",
                    "access": ["read", "write"],
                    "value-list": [
                        {"value": 0, "description": "Auto"},
                        {"value": 3, "description": "Sleep"},
                    ],
                },
                {
                    "iid": 4,
                    "type": "urn:miot-spec-v2:property:fault:00000009:x:1",
                    "format": "uint8",
                    "access": ["read"],
                },
            ],
            "actions": [
                {
                    "iid": 1,
                    "type": "urn:miot-spec-v2:action:toggle:00002811:x:1",
                    "description": "Toggle",
                    "in": [3],
                }
            ],
            "events": [
                {
                    "iid": 1,
                    "type": "urn:miot-spec-v2:event:low-battery:0000200x:x:1",
                    "description": "Low Battery",
                }
            ],
        },
        {
            "iid": 6,
            "type": "urn:miot-spec-v2:service:screen:00007806:x:1",
            "properties": [
                {
                    "iid": 1,
                    "type": "urn:miot-spec-v2:property:on:00000006:x:1",
                    "format": "bool",
                    "access": ["read", "write"],
                },
                {
                    "iid": 2,
                    "type": "urn:miot-spec-v2:property:brightness:0000000D:x:1",
                    "format": "uint8",
                    "access": ["read", "write"],
                    "unit": "percentage",
                    "value-range": [1, 100, 1],
                },
            ],
        },
    ],
}

TRANSLATIONS = {
    "service:002:property:003": "模式",
    "service:002:property:003:valuelist:000": "自动",
    "service:002:property:003:valuelist:001": "睡眠",
    "service:002:action:001": "切换",
}


@pytest.fixture()
def spec() -> DeviceSpec:
    return DeviceSpec(INSTANCE["type"], INSTANCE, TRANSLATIONS)


def test_urn_name():
    assert urn_name("urn:miot-spec-v2:property:brightness:0000000D:x:1") == "brightness"


def test_parses_services_properties_actions_events(spec: DeviceSpec):
    assert spec.name == "air-purifier"
    assert len(spec.properties) == 5
    assert len(spec.actions) == 1 and len(spec.events) == 1
    prop = spec.find_property("2.3")
    assert prop.full_name == "air-purifier.mode"
    assert prop.access_text() == "rw"
    assert prop.readable and prop.writable and not prop.notifiable


def test_applies_chinese_translations(spec: DeviceSpec):
    prop = spec.find_property("2.3")
    assert prop.description == "模式"
    assert [item.description for item in prop.value_list] == ["自动", "睡眠"]
    assert spec.find_action("2.1").description == "切换"


def test_find_property_by_full_name_and_bare_name(spec: DeviceSpec):
    assert spec.find_property("screen.brightness").ref == "6.2"
    assert spec.find_property("brightness").ref == "6.2"
    assert spec.find_property("模式").ref == "2.3"


def test_bare_name_prefers_primary_service(spec: DeviceSpec):
    """`on` 在主体和屏幕上都有，设备类型是 air-purifier，取主体那个。"""
    assert spec.find_property("on").ref == "2.1"
    assert spec.find_property("screen.on").ref == "6.1"


def test_ambiguous_reference_raises(spec: DeviceSpec):
    # 两个服务都叫 x 的情况：构造一个没有主服务偏好的 spec
    instance = {**INSTANCE, "type": "urn:miot-spec-v2:device:gateway:0:x:1"}
    other = DeviceSpec(instance["type"], instance, {})
    with pytest.raises(AmbiguousReference):
        other.find_property("on")


def test_unknown_property(spec: DeviceSpec):
    with pytest.raises(SpecNotFound):
        spec.find_property("nope")
    with pytest.raises(SpecNotFound):
        spec.find_property("9.9")


def test_parse_bool_accepts_common_words(spec: DeviceSpec):
    prop = spec.find_property("2.1")
    for word in ("on", "true", "1", "开", "打开"):
        assert parse_value(prop, word) is True
    for word in ("off", "false", "0", "关"):
        assert parse_value(prop, word) is False
    with pytest.raises(InvalidValue):
        parse_value(prop, "maybe")


def test_parse_enum_by_description_or_value(spec: DeviceSpec):
    prop = spec.find_property("2.3")
    assert parse_value(prop, "睡眠") == 3
    assert parse_value(prop, "Sleep") == 3  # 英文原文也认
    assert parse_value(prop, "3") == 3
    with pytest.raises(InvalidValue):
        parse_value(prop, "狂暴")


def test_parse_number_checks_range(spec: DeviceSpec):
    prop = spec.find_property("screen.brightness")
    assert parse_value(prop, "60") == 60
    with pytest.raises(InvalidValue):
        parse_value(prop, "300")
    with pytest.raises(InvalidValue):
        parse_value(prop, "abc")


def test_format_value(spec: DeviceSpec):
    assert format_value(spec.find_property("2.1"), True) == "开"
    assert format_value(spec.find_property("2.3"), 3) == "睡眠(3)"
    assert format_value(spec.find_property("screen.brightness"), 60) == "60%"
    assert format_value(spec.find_property("2.4"), None) == "-"


def test_unit_symbol():
    assert unit_symbol("percentage") == "%"
    assert unit_symbol("kelvin") == "K"
    assert unit_symbol("none") == ""
    assert unit_symbol(None) == ""
    assert unit_symbol("怪单位") == "怪单位"


def test_range_text(spec: DeviceSpec):
    assert spec.find_property("screen.brightness").range_text() == "1~100 step 1"
    assert spec.find_property("2.3").range_text() == "0=自动 3=睡眠"
    assert spec.find_property("2.1").range_text() == "-"


def test_verify_after_write_reports_actual_values(monkeypatch, spec):
    """写完回读：以设备的真实状态为准，而不是只信返回码。"""
    from mi_home_cli.cli.prop import verify_after_write

    prop = spec.find_property("screen.brightness")
    calls = []

    class FakeApi:
        def get_props(self, params):
            calls.append(params)
            # 第一次还是旧值，第二次才跟上——模拟上报延迟
            value = 30 if len(calls) == 1 else 60
            return [{"siid": 6, "piid": 2, "value": value, "code": 0}]

    monkeypatch.setattr("time.sleep", lambda _: None)
    actual = verify_after_write(FakeApi(), "d1", [(prop, 60)], delay=0)
    assert actual[(6, 2)] == 60
    assert len(calls) == 2  # 第一次不一致，重试了一次


def test_verify_skips_unreadable_properties(monkeypatch, spec):
    from mi_home_cli.cli.prop import verify_after_write

    write_only = spec.find_property("2.3")
    write_only.access = ["write"]

    class Boom:
        def get_props(self, params):
            raise AssertionError("不该为只写属性发读请求")

    monkeypatch.setattr("time.sleep", lambda _: None)
    assert verify_after_write(Boom(), "d1", [(write_only, 3)], delay=0) == {}


def test_float_is_shown_with_significant_digits(spec):
    """设备回的温度是 23.700001，原样显示既难看又像精度错觉。

    用有效数字而不是固定小数位：固定 2 位会把 0.019 mg/m³ 舍成 0.02。
    """
    from mi_home_cli.core.spec import Property, format_value

    temperature = Property(
        siid=3, piid=2, name="temperature", description="温度", format="float",
        access=["read"], service="environment", unit="celsius",
    )
    assert format_value(temperature, 23.700001) == "23.7℃"
    assert format_value(temperature, 57.0) == "57℃"

    hcho = Property(
        siid=3, piid=6, name="hcho-density", description="甲醛", format="float",
        access=["read"], service="environment", unit="mg/m3",
    )
    assert format_value(hcho, 0.019) == "0.019 mg/m³"
