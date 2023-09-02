import asyncio
import copy
from dataclasses import dataclass
import logging
import math
import time

import pyartnet

from homeassistant.components.light import (
    ATTR_RGBWW_COLOR,
    ATTR_HS_COLOR,
    ATTR_RGB_COLOR,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_WHITE,
    ATTR_BRIGHTNESS,
    ATTR_TRANSITION,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.const import (
    STATE_ON,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
import homeassistant.util.color as color_util


log = logging.getLogger(__name__)


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
):
    pyartnet.base.CREATE_TASK = hass.async_create_task

    node = pyartnet.ArtNetNode(
        ip=config["ip"],
        port=config["port"],
        max_fps=config["max_fps"],
        refresh_every=config["refresh_every"],
        start_refresh_task=config["refresh_every"] > 0,
    )
    universe = node.add_universe(config["universe"])

    types = {}
    for type_config in config["types"]:
        types[type_config["name"]] = type_config

    entities = []
    for entity_config in config["entities"]:
        entities.append(DmxLight(entity_config, types[entity_config["type"]], universe))

    async_add_entities(entities)


@dataclass
class DmxLightState:
    brightness: int = 0  # Range: [0, 255]
    color_temp_kelvin: float = 0
    hue: float = 0  # Range: [0, 360]
    saturation: float = 0  # Range: [0, 100]


class DmxLight(LightEntity, RestoreEntity):
    def __init__(
        self, entity_config, type_config, universe: pyartnet.base.BaseUniverse
    ) -> None:
        self._attr_has_entity_name = True  # Required in new code.
        self._attr_name = entity_config["name"]
        escaped_name = self._attr_name.replace(" ", "_").lower()
        self.entity_id = "light." + escaped_name
        # Using the name as the unique ID is frowned upon in Home Assistant
        # Developer documentation. However, that makes it easier to switch
        # DMX channels for the lights, which I've had to do several times.
        self._attr_unique_id = escaped_name

        self._attr_should_poll = False

        self._attr_color_mode = ColorMode.RGBWW
        self._attr_supported_color_modes = set(
            [
                ColorMode.RGB,
                ColorMode.RGBWW,
                ColorMode.COLOR_TEMP,
                ColorMode.HS,
                ColorMode.WHITE,
            ]
        )

        self._attr_supported_features = LightEntityFeature.TRANSITION

        self._attr_min_color_temp_kelvin = type_config["min_color_temp_kelvin"]
        self._attr_max_color_temp_kelvin = type_config["max_color_temp_kelvin"]
        self._default_color_temp_kelvin = type_config["default_color_temp_kelvin"]

        self._default_transition_secs = type_config.get("default_transition_secs", 0)

        self._previous_state = None
        self._state = DmxLightState()
        self._fade_sequence_number = 0

        channel_number = entity_config["channel"]
        self._brightness_channel = None
        self._color_temp_channel = None
        self._hue_channel = None
        self._saturation_channel = None
        for channel_config in type_config["channels"]:
            num_bytes = channel_config.get("bytes", 1)
            endianness = channel_config.get("endianness", "little")
            pyartnet_channel = universe.add_channel(
                start=channel_number,
                width=1,
                byte_size=num_bytes,
                byte_order=endianness,
            )
            channel_number += num_bytes

            if channel_config["type"] == "constant":
                pyartnet_channel.set_values([channel_config["value"]])
            elif channel_config["type"] == "brightness":
                assert self._brightness_channel is None
                self._brightness_channel = DmxChannel(
                    pyartnet_channel,
                    channel_config,
                    min_value=0,
                    max_value=255,
                    is_hue=False,
                )
            elif channel_config["type"] == "color_temp_kelvin":
                assert self._color_temp_channel is None
                self._color_temp_channel = DmxChannel(
                    pyartnet_channel,
                    channel_config,
                    min_value=self._attr_min_color_temp_kelvin,
                    max_value=self._attr_max_color_temp_kelvin,
                    is_hue=False,
                )
            elif channel_config["type"] == "hue":
                assert self._hue_channel is None
                self._hue_channel = DmxChannel(
                    pyartnet_channel,
                    channel_config,
                    min_value=0,
                    max_value=360,
                    is_hue=True,
                )
            elif channel_config["type"] == "saturation":
                assert self._saturation_channel is None
                self._saturation_channel = DmxChannel(
                    pyartnet_channel,
                    channel_config,
                    min_value=0,
                    max_value=100,
                    is_hue=False,
                )
            else:
                assert False
        assert self._brightness_channel is not None
        assert self._color_temp_channel is not None
        assert self._hue_channel is not None
        assert self._saturation_channel is not None

    @property
    def is_on(self) -> bool | None:
        return self._state.brightness > 0

    @property
    def brightness(self) -> int | None:
        return self._state.brightness

    @property
    def rgbww_color(self) -> tuple[int, int, int, int, int] | None:
        r, g, b = color_util.color_hs_to_RGB(self._state.hue, self._state.saturation)
        _, _, _, c, w = color_util.color_temperature_to_rgbww(
            self._state.color_temp_kelvin,
            255,
            self.min_color_temp_kelvin,
            self.max_color_temp_kelvin,
        )
        return (r, g, b, c, w)

    async def async_added_to_hass(self) -> None:
        old_state = await self.async_get_last_state()
        if old_state is not None:
            log.debug(f"DmxLight.async_added_to_hass({old_state})")

            if old_state.state == STATE_ON:
                await self.async_turn_on(
                    **{
                        ATTR_BRIGHTNESS: old_state.attributes.get(ATTR_BRIGHTNESS),
                        ATTR_RGBWW_COLOR: old_state.attributes.get(ATTR_RGBWW_COLOR),
                    }
                )
            else:
                await self.async_turn_off()

    async def async_turn_on(self, **kwargs):
        log.debug(f"DmxLight.async_turn_on({kwargs})")

        transition_secs = kwargs.get(ATTR_TRANSITION, self._default_transition_secs)

        old_state = copy.deepcopy(self._state)
        new_state = copy.deepcopy(self._state)

        if ATTR_BRIGHTNESS in kwargs:
            new_state.brightness = kwargs[ATTR_BRIGHTNESS]
        elif old_state.brightness == 0:
            # Default to full on.
            new_state.brightness = 255

        has_color_temp_kelvin = False
        if ATTR_WHITE in kwargs:
            new_state.hue = 0
            new_state.saturation = 0
        elif ATTR_HS_COLOR in kwargs:
            new_state.hue, new_state.saturation = kwargs[ATTR_HS_COLOR]
        elif ATTR_RGB_COLOR in kwargs:
            new_state.hue, new_state.saturation = color_util.color_RGB_to_hs(
                *kwargs[ATTR_RGB_COLOR]
            )
        elif ATTR_COLOR_TEMP_KELVIN in kwargs:
            new_state.color_temp_kelvin = kwargs[ATTR_COLOR_TEMP_KELVIN]
            has_color_temp_kelvin = True
        elif ATTR_RGBWW_COLOR in kwargs:
            new_state.hue, new_state.saturation = color_util.color_RGB_to_hs(
                *kwargs[ATTR_RGBWW_COLOR][0:3]
            )
            new_state.color_temp_kelvin = color_util.rgbww_to_color_temperature(
                kwargs[ATTR_RGBWW_COLOR],
                self.min_color_temp_kelvin,
                self.max_color_temp_kelvin,
            )[0]
            has_color_temp_kelvin = True

        if old_state.brightness == 0 and not has_color_temp_kelvin:
            # Set default color temperature.
            new_state.color_temp_kelvin = self._default_color_temp_kelvin

        # Recover a previous state if you turn off and then on a light.
        if (
            not kwargs
            and self._previous_state is not None
            and self._previous_state.brightness > 0
        ):
            new_state = self._previous_state
            self._previous_state = None

        # We update to a very low brightness value right away, so that a very
        # long running fade can be turned off.
        if transition_secs > 0 and old_state.brightness == 0:
            self._state = copy.deepcopy(new_state)
            self._state.brightness = 1
            self.async_schedule_update_ha_state()

        await self._run_fade(old_state, new_state, transition_secs)

        self.async_schedule_update_ha_state()

    async def async_turn_off(self, **kwargs):
        log.debug(f"DmxLight.async_turn_off({kwargs})")

        transition_secs = kwargs.get(ATTR_TRANSITION, self._default_transition_secs)

        old_state = copy.deepcopy(self._state)
        self._previous_state = old_state
        new_state = DmxLightState()

        await self._run_fade(old_state, new_state, transition_secs)

        self.async_schedule_update_ha_state()

    async def _run_fade(
        self,
        old_state: DmxLightState,
        new_state: DmxLightState,
        transition_secs: float,
    ) -> None:
        log.debug(f"DmxLight._run_fade(")
        log.debug(f"    {old_state=},")
        log.debug(f"    {new_state=},")
        log.debug(f"    {transition_secs=})")

        self._fade_sequence_number += 1
        this_fade_sequence_number = self._fade_sequence_number
        is_fade_overridden = (
            lambda: this_fade_sequence_number != self._fade_sequence_number
        )

        fade_ms = transition_secs * 1000

        if fade_ms > 0 and old_state.brightness == 0 and new_state.brightness > 0:
            await self._off_to_on_fade(
                old_state, new_state, fade_ms, is_fade_overridden
            )
        elif fade_ms > 0 and old_state.brightness > 0 and new_state.brightness == 0:
            await self._on_to_off_fade(
                old_state, new_state, fade_ms, is_fade_overridden
            )
        elif fade_ms > 0 and old_state.saturation == 0 and new_state.saturation > 0:
            await self._unsaturated_to_saturated_fade(
                old_state, new_state, fade_ms, is_fade_overridden
            )
        elif fade_ms > 0 and old_state.saturation > 0 and new_state.saturation == 0:
            await self._saturated_to_unsaturated_fade(
                old_state, new_state, fade_ms, is_fade_overridden
            )
        elif fade_ms > 0 and old_state.hue != new_state.hue and fade_ms > 0:
            await self._hue_fade(old_state, new_state, fade_ms, is_fade_overridden)
        else:
            await self._simple_fade(old_state, new_state, fade_ms, is_fade_overridden)

    async def _off_to_on_fade(
        self,
        old_state: DmxLightState,
        new_state: DmxLightState,
        fade_ms: float,
        is_fade_overridden,
    ):
        log.debug(f" off to on")
        self._color_temp_channel.set_fade(new_state.color_temp_kelvin, 0)
        self._hue_channel.set_fade(new_state.hue, 0)
        self._saturation_channel.set_fade(new_state.saturation, 0)

        self._state.color_temp_kelvin = new_state.color_temp_kelvin
        self._state.hue = new_state.hue
        self._state.saturation = new_state.saturation

        self._brightness_channel.set_fade(new_state.brightness, fade_ms)

        await self._brightness_channel.await_fade()

        if is_fade_overridden():
            return

        self._state.brightness = new_state.brightness

    async def _on_to_off_fade(
        self,
        old_state: DmxLightState,
        new_state: DmxLightState,
        fade_ms: float,
        is_fade_overridden,
    ):
        log.debug(f" on to off")
        self._brightness_channel.set_fade(new_state.brightness, fade_ms)

        await self._brightness_channel.await_fade()

        if is_fade_overridden():
            return

        self._state.brightness = new_state.brightness

        # Give some time for fades to propagate to the light. It doesn't
        # matter that it's long, since the main fade is done.
        await asyncio.sleep(1)

        if is_fade_overridden():
            return

        self._color_temp_channel.set_fade(new_state.color_temp_kelvin, 0)
        self._hue_channel.set_fade(new_state.hue, 0)
        self._saturation_channel.set_fade(new_state.saturation, 0)

        self._state.color_temp_kelvin = new_state.color_temp_kelvin
        self._state.hue = new_state.hue
        self._state.saturation = new_state.saturation

    async def _unsaturated_to_saturated_fade(
        self,
        old_state: DmxLightState,
        new_state: DmxLightState,
        fade_ms: float,
        is_fade_overridden,
    ):
        log.debug(f" unsaturated to saturated")
        self._hue_channel.set_fade(new_state.hue, 0)

        self._state.hue = new_state.hue

        self._brightness_channel.set_fade(new_state.brightness, fade_ms)
        self._color_temp_channel.set_fade(new_state.color_temp_kelvin, fade_ms)
        self._saturation_channel.set_fade(new_state.saturation, fade_ms)

        await self._brightness_channel.await_fade()
        await self._color_temp_channel.await_fade()
        await self._saturation_channel.await_fade()

        if is_fade_overridden():
            return

        self._state.brightness = new_state.brightness
        self._state.color_temp_kelvin = new_state.color_temp_kelvin
        self._state.saturation = new_state.saturation

    async def _saturated_to_unsaturated_fade(
        self,
        old_state: DmxLightState,
        new_state: DmxLightState,
        fade_ms: float,
        is_fade_overridden,
    ):
        log.debug(f" saturated to unsaturated")
        self._brightness_channel.set_fade(new_state.brightness, fade_ms)
        self._color_temp_channel.set_fade(new_state.color_temp_kelvin, fade_ms)
        self._saturation_channel.set_fade(new_state.saturation, fade_ms)

        await self._brightness_channel.await_fade()
        await self._color_temp_channel.await_fade()
        await self._saturation_channel.await_fade()

        if is_fade_overridden():
            return

        self._state.brightness = new_state.brightness
        self._state.color_temp_kelvin = new_state.color_temp_kelvin
        self._state.saturation = new_state.saturation

        # Give some time for fades to propagate to the light. It doesn't
        # matter that it's long, since the main fade is done.
        #
        # Otherwise, I have seen the Astera light do something funny in this
        # case.
        await asyncio.sleep(1)

        if is_fade_overridden():
            return

        self._hue_channel.set_fade(new_state.hue, 0)

        self._state.hue = new_state.hue

    async def _hue_fade(
        self,
        old_state: DmxLightState,
        new_state: DmxLightState,
        fade_ms: float,
        is_fade_overridden,
    ):
        log.debug(f" hue change")
        self._brightness_channel.set_fade(new_state.brightness, fade_ms)
        self._color_temp_channel.set_fade(new_state.color_temp_kelvin, fade_ms)

        start_time_ms = time.time() * 1000

        FADE_STEP_MS = 100
        num_fade_steps = math.ceil(fade_ms / FADE_STEP_MS)

        old_rgb = list(color_util.color_hs_to_RGB(old_state.hue, old_state.saturation))
        new_rgb = list(color_util.color_hs_to_RGB(new_state.hue, new_state.saturation))
        for step in range(num_fade_steps):
            intermediate_rgb = [
                old_rgb[i] + (new_rgb[i] - old_rgb[i]) * (step + 1) / num_fade_steps
                for i in range(3)
            ]
            intermediate_hue, intermediate_saturation = color_util.color_RGB_to_hs(
                *intermediate_rgb
            )

            intended_end_time_ms = start_time_ms + (step + 1) * FADE_STEP_MS
            actual_fade_step_ms = max(0, intended_end_time_ms - time.time() * 1000)

            self._saturation_channel.set_fade(
                intermediate_saturation, actual_fade_step_ms
            )

            if (
                intermediate_hue > self._state.hue
                and intermediate_hue - self._state.hue > 180
            ):
                first_hue_diff = self._state.hue
                second_hue_diff = 360 - intermediate_hue
                first_fade_step_ms = (
                    first_hue_diff
                    / (first_hue_diff + second_hue_diff)
                    * actual_fade_step_ms
                )

                self._hue_channel.set_fade(0, first_fade_step_ms)
                await self._hue_channel.await_fade()
                if is_fade_overridden():
                    return
                self._state.hue = 0

                self._hue_channel.set_fade(360, 0)
                self._state.hue = 360

                actual_fade_step_ms = max(0, intended_end_time_ms - time.time() * 1000)
                self._hue_channel.set_fade(intermediate_hue, actual_fade_step_ms)
            elif (
                self._state.hue > intermediate_hue
                and self._state.hue - intermediate_hue > 180
            ):
                first_hue_diff = 360 - self._state.hue
                second_hue_diff = intermediate_hue
                first_fade_step_ms = (
                    first_hue_diff
                    / (first_hue_diff + second_hue_diff)
                    * actual_fade_step_ms
                )

                self._hue_channel.set_fade(360, first_fade_step_ms)
                await self._hue_channel.await_fade()
                if is_fade_overridden():
                    return
                self._state.hue = 360

                self._hue_channel.set_fade(0, 0)
                self._state.hue = 0

                actual_fade_step_ms = max(0, intended_end_time_ms - time.time() * 1000)
                self._hue_channel.set_fade(intermediate_hue, actual_fade_step_ms)
            else:
                self._hue_channel.set_fade(intermediate_hue, actual_fade_step_ms)

            await self._hue_channel.await_fade()
            await self._saturation_channel.await_fade()

            if is_fade_overridden():
                return

            self._state.hue = intermediate_hue
            self._state.saturation = intermediate_saturation

        await self._brightness_channel.await_fade()
        await self._color_temp_channel.await_fade()

        if is_fade_overridden():
            return

        self._state.brightness = new_state.brightness
        self._state.color_temp_kelvin = new_state.color_temp_kelvin

    async def _simple_fade(
        self,
        old_state: DmxLightState,
        new_state: DmxLightState,
        fade_ms: float,
        is_fade_overridden,
    ):
        log.debug(f" other")
        self._brightness_channel.set_fade(new_state.brightness, fade_ms)
        self._color_temp_channel.set_fade(new_state.color_temp_kelvin, fade_ms)
        self._hue_channel.set_fade(new_state.hue, fade_ms)
        self._saturation_channel.set_fade(new_state.saturation, fade_ms)

        if fade_ms > 0:
            await self._brightness_channel.await_fade()
            await self._color_temp_channel.await_fade()
            await self._hue_channel.await_fade()
            await self._saturation_channel.await_fade()

            if is_fade_overridden():
                return

        self._state.brightness = new_state.brightness
        self._state.color_temp_kelvin = new_state.color_temp_kelvin
        self._state.hue = new_state.hue
        self._state.saturation = new_state.saturation


class DmxChannel:
    def __init__(
        self,
        channel: pyartnet.base.Channel,
        config,
        min_value: float,
        max_value: float,
        is_hue: bool,
    ) -> None:
        self._type = config["type"]
        self._channel = channel
        self._min_value = min_value
        self._max_value = max_value
        self._is_hue = is_hue

        self._channel_min_value = config.get("offset", 0)
        self._channel_max_value = 1
        for _ in range(config.get("bytes", 1)):
            self._channel_max_value *= 256
        self._channel_max_value -= 1

        self._correction_polynomial = config.get("correction_polynomial")

    def set_fade(self, value: float, fade_ms: int):
        if self._correction_polynomial is not None:
            value = self._apply_correction(value, self._correction_polynomial)
        value = min(value, self._max_value)
        value = max(value, self._min_value)

        value_range = self._max_value - self._min_value
        channel_range = self._channel_max_value - self._channel_min_value
        if self._is_hue:
            # Hue is special, because a value of 360 is equivalent to 0.
            channel_range += 1

        channel_value = (
            value - self._min_value
        ) / value_range * channel_range + self._channel_min_value
        channel_value = min(channel_value, self._channel_max_value)
        channel_value = max(channel_value, self._channel_min_value)
        channel_value = int(round(channel_value))

        log.debug(
            f"{self._type} fade to {value} over {fade_ms} ms, channel value {channel_value}"
        )
        if fade_ms > 0:
            self._channel.set_fade([int(channel_value)], fade_ms)
        else:
            # We do both here: One to update the value immediately, and
            # one to cancel any running fades.
            self._channel.set_values([int(channel_value)])
            self._channel.set_fade([int(channel_value)], fade_ms)

    async def await_fade(self):
        await self._channel

    def _apply_correction(self, value: float, coefficients: list[float]) -> float:
        corrected_value = 0
        for exponent, coefficient in enumerate(coefficients):
            term = coefficient
            for i in range(exponent):
                term *= value
            corrected_value += term
        return corrected_value
