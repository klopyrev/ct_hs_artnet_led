# Run from 1 step above the current directory using:
# python3 -m ct_hs_artnet_led.fade_tests

import asyncio
import copy
from dataclasses import dataclass
import logging
import yaml

import pyartnet

from . import ChannelCoders, DmxLightState, FadeController, Fader

CALC_FPS = 1000
FADE_FPS = 50
UPDATE_STATE_FREQUENCY_SECS = 1


@dataclass
class FadeTest:
    name: str
    start_brightness: float
    start_color_temp: float
    start_hue: float
    start_saturation: float
    end_brightness: float
    end_color_temp: float
    end_hue: float
    end_saturation: float
    fade_duration_secs: float
    wait_secs: float = 1


TESTS = [
    FadeTest(
        name="Hue fade crossing 0",
        start_brightness=50,
        start_color_temp=4000,
        start_hue=0,
        start_saturation=100,
        end_brightness=50,
        end_color_temp=4000,
        end_hue=340,
        end_saturation=100,
        fade_duration_secs=5,
    ),
    FadeTest(
        name="LCH hue crossing 0",
        start_brightness=50,
        start_color_temp=4000,
        start_hue=329,
        start_saturation=100,
        end_brightness=50,
        end_color_temp=4000,
        end_hue=359,
        end_saturation=100,
        fade_duration_secs=1,
        wait_secs=5,
    ),
    FadeTest(
        name="180 degree fade",
        start_brightness=50,
        start_color_temp=4000,
        start_hue=0,
        start_saturation=100,
        end_brightness=50,
        end_color_temp=4000,
        end_hue=180,
        end_saturation=100,
        fade_duration_secs=1,
        wait_secs=2,
    ),
    FadeTest(
        name="Saturation fade",
        start_brightness=50,
        start_color_temp=4000,
        start_hue=0,
        start_saturation=10,
        end_brightness=50,
        end_color_temp=4000,
        end_hue=0,
        end_saturation=90,
        fade_duration_secs=5,
    ),
    FadeTest(
        name="Brightness and color temperature change",
        start_brightness=3,
        start_color_temp=2100,
        start_hue=0,
        start_saturation=0,
        end_brightness=100,
        end_color_temp=5500,
        end_hue=0,
        end_saturation=0,
        fade_duration_secs=4,
    ),
    FadeTest(
        name="Saturated to unsaturated fade, and back",
        start_brightness=130,
        start_color_temp=2089,
        start_hue=22.824,
        start_saturation=100.0,
        end_brightness=255,
        end_color_temp=5516,
        end_hue=22.824,
        end_saturation=0,
        fade_duration_secs=1,
    ),
    FadeTest(
        name="Off to on and back",
        start_brightness=0,
        start_color_temp=1750,
        start_hue=0,
        start_saturation=0,
        end_brightness=70,
        end_color_temp=4000,
        end_hue=340,
        end_saturation=50,
        fade_duration_secs=5,
    ),
    # ── Additional hue fade cases (exercises colormath path) ─────────
    FadeTest(
        name="Small hue change (no zero crossing)",
        start_brightness=50,
        start_color_temp=4000,
        start_hue=30,
        start_saturation=100,
        end_brightness=50,
        end_color_temp=4000,
        end_hue=60,
        end_saturation=100,
        fade_duration_secs=2,
    ),
    FadeTest(
        name="Hue fade green to blue",
        start_brightness=50,
        start_color_temp=4000,
        start_hue=120,
        start_saturation=100,
        end_brightness=50,
        end_color_temp=4000,
        end_hue=240,
        end_saturation=100,
        fade_duration_secs=3,
    ),
    FadeTest(
        name="Hue fade at partial saturation",
        start_brightness=50,
        start_color_temp=4000,
        start_hue=0,
        start_saturation=50,
        end_brightness=50,
        end_color_temp=4000,
        end_hue=180,
        end_saturation=50,
        fade_duration_secs=2,
    ),
    FadeTest(
        name="Hue fade at low brightness",
        start_brightness=20,
        start_color_temp=4000,
        start_hue=0,
        start_saturation=100,
        end_brightness=20,
        end_color_temp=4000,
        end_hue=240,
        end_saturation=100,
        fade_duration_secs=3,
    ),
]


async def run_test(test: FadeTest, coders: ChannelCoders, channel):
    print(f"\n{'='*60}")
    print(f"Running: {test.name}")
    print(f"{'='*60}")
    await asyncio.sleep(3)

    start_state = DmxLightState(
        brightness=test.start_brightness,
        color_temp_kelvin=test.start_color_temp,
        hue=test.start_hue,
        saturation=test.start_saturation,
    )
    end_state = DmxLightState(
        brightness=test.end_brightness,
        color_temp_kelvin=test.end_color_temp,
        hue=test.end_hue,
        saturation=test.end_saturation,
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
            test.fade_duration_secs,
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
            await asyncio.sleep(test.wait_secs)
            is_first = False


async def main():
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("ct_hs_artnet_led").setLevel(level=logging.DEBUG)
    # logging.getLogger("pyartnet").setLevel(level=logging.DEBUG)

    with open("ct_hs_artnet_led/test_config.yaml", encoding="ascii") as f:
        config = yaml.safe_load(f)
        type_config = config["light"][0]["types"][0]
        assert type_config["name"] == "astera"
        coders = ChannelCoders(type_config)

    entity_config = config["light"][0]["entities"][0]

    async with pyartnet.ArtNetNode.create(
        host=config["light"][0]["ip"],
        port=config["light"][0]["port"],
        max_fps=CALC_FPS,
        refresh_every=0,
    ) as node:
        await node.stop_refresh()
        universe = node.add_universe(config["light"][0]["universe"])
        channel = universe.add_channel(
            start=entity_config["channel"],
            width=coders.num_channels(),
            byte_size=1,
        )

        for test in TESTS:
            await run_test(test, coders, channel)

        print(f"\n{'='*60}")
        print(f"All {len(TESTS)} tests completed.")
        print(f"{'='*60}")


if __name__ == "__main__":
    with asyncio.Runner() as runner:
        runner.run(main())
