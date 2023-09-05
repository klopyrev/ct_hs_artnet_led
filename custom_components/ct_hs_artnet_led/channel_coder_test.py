from .channel_coder import ChannelCoder, ChannelCoders
from .state import DmxLightState

import yaml


def test_basic():
    coder = ChannelCoder(1, min_value=0, max_value=100)
    values = [0, 0, 0]

    expected_pairs = [
        (0, 0),
        (25, 64),
        (50, 128),
        (100, 255),
    ]
    for value, channel_value in expected_pairs:
        coder.encode(values, value)
        assert values[1] == channel_value
    assert values[0] == 0
    assert values[2] == 0


def test_offset():
    coder = ChannelCoder(0, min_value=2000, max_value=10000, offset=11)
    values = [0]

    expected_pairs = [
        (2000, 11),
        (4000, 72),
        (10000, 255),
    ]
    for value, channel_value in expected_pairs:
        coder.encode(values, value)
        assert values[0] == channel_value


def test_hue_8bit():
    coder = ChannelCoder(0, min_value=0, max_value=360, is_hue=True)
    values = [0]

    expected_pairs = [
        (0, 0),
        (40, 28),
        (357, 254),
        (359, 255),
        (360, 0),
    ]
    for value, channel_value in expected_pairs:
        coder.encode(values, value)
        assert values[0] == channel_value


def test_hue_16bit():
    coder = ChannelCoder(
        0, min_value=0, max_value=360, is_hue=True, num_bytes=2, endianness="big"
    )
    values = [0, 0]

    expected_pairs = [
        (0, [0, 0]),
        (40, [28, 114]),  # 7282
        (357, [253, 222]),  # 64990
        (359, [255, 74]),  # 65354
        (360, [0, 0]),
    ]
    for value, channel_value in expected_pairs:
        coder.encode(values, value)
        assert values == channel_value


def test_correction():
    coder = ChannelCoder(
        0,
        min_value=0,
        max_value=100,
        correction_polynomial=[0, -0.0586, 0.0267, -0.000565, 0.00000406],
    )
    values = [0]

    expected_pairs = [
        (0, 0),
        (23, 18),
        (55, 53),
        (100, 255),
    ]
    for value, channel_value in expected_pairs:
        coder.encode(values, value)
        assert values[0] == channel_value


def test_astera():
    with open("test_config.yaml", encoding="ascii") as f:
        config = yaml.safe_load(f)
        type_config = config["light"][0]["types"][0]
        assert type_config["name"] == "astera"
        coders = ChannelCoders(type_config)

        state = DmxLightState(
            brightness=70,
            color_temp_kelvin=5500,
            hue=40,
            saturation=79,
        )
        values = [1] * 7
        coders.encode(values, state)

        assert values == [179, 50, 117, 0, 28, 114, 106]


def test_aputure():
    with open("test_config.yaml", encoding="ascii") as f:
        config = yaml.safe_load(f)
        type_config = config["light"][0]["types"][1]
        assert type_config["name"] == "aputure"
        coders = ChannelCoders(type_config)

        state = DmxLightState(
            brightness=70,
            color_temp_kelvin=5500,
            hue=40,
            saturation=79,
        )
        values = [1] * 5
        coders.encode(values, state)

        assert values == [178, 28, 171, 118, 0]
