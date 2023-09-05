# Run from 1 step above the current directory using:
# python3 -m ct_hs_artnet_led.fade_tests

import asyncio
import copy
import logging
import yaml

import pyartnet

from . import ChannelCoders, DmxLightState, FadeController, Fader

CALC_FPS = 1000
FADE_FPS = 50
UPDATE_STATE_FREQUENCY_SECS = 1

# Hue fade crossing 0.
# START_BRIGHTNESS = 50
# START_COLOR_TEMP = 4000
# START_HUE = 0
# START_SATURATION = 100
# END_BRIGHTNESS = 50
# END_COLOR_TEMP = 4000
# END_HUE = 340
# END_SATURATION = 100
# FADE_DURATION_SECS = 5

# LCH hue crossing 0
# START_BRIGHTNESS = 50
# START_COLOR_TEMP = 4000
# START_HUE = 329
# START_SATURATION = 100
# END_BRIGHTNESS = 50
# END_COLOR_TEMP = 4000
# END_HUE = 359
# END_SATURATION = 100
# FADE_DURATION_SECS = 1
# WAIT_SECS = 5

# 180 degree fade
# START_BRIGHTNESS = 50
# START_COLOR_TEMP = 4000
# START_HUE = 0
# START_SATURATION = 100
# END_BRIGHTNESS = 50
# END_COLOR_TEMP = 4000
# END_HUE = 180
# END_SATURATION = 100
# FADE_DURATION_SECS = 1
# WAIT_SECS = 2

# Saturation fade
# START_BRIGHTNESS = 50
# START_COLOR_TEMP = 4000
# START_HUE = 0
# START_SATURATION = 10
# END_BRIGHTNESS = 50
# END_COLOR_TEMP = 4000
# END_HUE = 0
# END_SATURATION = 90
# FADE_DURATION_SECS = 5
# WAIT_SECS = 1

# Brightness and color temperature change.
# START_BRIGHTNESS = 3
# START_COLOR_TEMP = 2100
# START_HUE = 0
# START_SATURATION = 0
# END_BRIGHTNESS = 100
# END_COLOR_TEMP = 5500
# END_HUE = 0
# END_SATURATION = 0
# FADE_DURATION_SECS = 4
# WAIT_SECS = 1

# Saturated to unsaturated fade, and back.
START_BRIGHTNESS = 130
START_COLOR_TEMP = 2089
START_HUE = 22.824
START_SATURATION = 100.0
END_BRIGHTNESS = 255
END_COLOR_TEMP = 5516
END_HUE = 22.824
END_SATURATION = 0
FADE_DURATION_SECS = 1
WAIT_SECS = 1

# Off to on and back.
# START_BRIGHTNESS = 0
# START_COLOR_TEMP = 1750
# START_HUE = 0
# START_SATURATION = 0
# END_BRIGHTNESS = 70
# END_COLOR_TEMP = 4000
# END_HUE = 340
# END_SATURATION = 50
# FADE_DURATION_SECS = 5
# WAIT_SECS = 1


async def main():
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("ct_hs_artnet_led").setLevel(level=logging.DEBUG)
    # logging.getLogger("pyartnet").setLevel(level=logging.DEBUG)

    with open("ct_hs_artnet_led/test_config.yaml", encoding="ascii") as f:
        config = yaml.safe_load(f)
        type_config = config["light"][0]["types"][0]
        assert type_config["name"] == "astera"
        coders = ChannelCoders(type_config)

    node = pyartnet.ArtNetNode(
        ip="192.168.1.6",
        port=6454,
        max_fps=CALC_FPS,
        refresh_every=0,
        start_refresh_task=False,
    )
    universe = node.add_universe(0)
    channel = universe.add_channel(start=1, width=coders.num_channels(), byte_size=1)

    start_state = DmxLightState(
        brightness=START_BRIGHTNESS,
        color_temp_kelvin=START_COLOR_TEMP,
        hue=START_HUE,
        saturation=START_SATURATION,
    )
    end_state = DmxLightState(
        brightness=END_BRIGHTNESS,
        color_temp_kelvin=END_COLOR_TEMP,
        hue=END_HUE,
        saturation=END_SATURATION,
    )
    current_state = copy.deepcopy(start_state)

    values = [0] * coders.num_channels()
    coders.encode(values, start_state)
    channel.set_fade(values, 0)

    is_first = True
    for target_state in [end_state, start_state]:
        controller = FadeController(
            current_state,
            target_state,
            coders,
            FADE_FPS,
            FADE_DURATION_SECS,
            UPDATE_STATE_FREQUENCY_SECS,
            lambda: None,
        )
        faders = []
        for i in range(coders.num_channels()):
            faders.append(Fader(i, controller))

        channel.set_fade(faders, 1)

        await channel
        if is_first:
            print("")
            await asyncio.sleep(WAIT_SECS)
            is_first = False


if __name__ == "__main__":
    with asyncio.Runner() as runner:
        runner.run(main())
