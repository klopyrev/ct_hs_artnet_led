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

from . import ChannelCoders, DmxLightState, FadeController, Fader


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
        max_fps=config["check_fps"],
        refresh_every=config["resend_universe_every_secs"],
        start_refresh_task=config["resend_universe_every_secs"] > 0,
    )
    universe = node.add_universe(config["universe"])

    types = {}
    for type_config in config["types"]:
        types[type_config["name"]] = type_config

    entities = []
    for entity_config in config["entities"]:
        entities.append(
            DmxLight(config, entity_config, types[entity_config["type"]], universe)
        )

    async_add_entities(entities)


class DmxLight(LightEntity, RestoreEntity):
    def __init__(
        self,
        config,
        entity_config,
        type_config,
        universe: pyartnet.base.BaseUniverse,
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
        self._default_color_temp_kelvin = config["default_color_temp_kelvin"]

        self._default_transition_secs = config.get("default_transition_secs", 0)

        self._ha_state_update_freq_secs = config[
            "home_assistant_state_update_frequency_secs"
        ]
        self._fade_fps = config["intended_fade_fps"]

        self._previous_state = None
        self._state = DmxLightState(
            brightness=0,
            color_temp_kelvin=self._attr_min_color_temp_kelvin,
            hue=0,
            saturation=0,
        )

        self._coders = ChannelCoders(type_config)
        self._channel = universe.add_channel(
            start=entity_config["channel"],
            width=self._coders.num_channels(),
            byte_size=1,
        )

    @property
    def is_on(self) -> bool | None:
        return self._state.brightness > 0

    @property
    def brightness(self) -> int | None:
        return int(self._state.brightness / 100 * 255)

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

        target_state = copy.deepcopy(self._state)

        if ATTR_BRIGHTNESS in kwargs:
            target_state.brightness = kwargs[ATTR_BRIGHTNESS] / 255
        elif self._state.brightness == 0:
            # Default to full on.
            target_state.brightness = 100

        has_color_temp_kelvin = False
        if ATTR_WHITE in kwargs:
            target_state.hue = 0
            target_state.saturation = 0
        elif ATTR_HS_COLOR in kwargs:
            target_state.hue, target_state.saturation = kwargs[ATTR_HS_COLOR]
        elif ATTR_RGB_COLOR in kwargs:
            target_state.hue, target_state.saturation = color_util.color_RGB_to_hs(
                *kwargs[ATTR_RGB_COLOR]
            )
        elif ATTR_COLOR_TEMP_KELVIN in kwargs:
            target_state.color_temp_kelvin = kwargs[ATTR_COLOR_TEMP_KELVIN]
            has_color_temp_kelvin = True
        elif ATTR_RGBWW_COLOR in kwargs:
            target_state.hue, target_state.saturation = color_util.color_RGB_to_hs(
                *kwargs[ATTR_RGBWW_COLOR][0:3]
            )
            target_state.color_temp_kelvin = color_util.rgbww_to_color_temperature(
                kwargs[ATTR_RGBWW_COLOR],
                self.min_color_temp_kelvin,
                self.max_color_temp_kelvin,
            )[0]
            has_color_temp_kelvin = True

        if self._state.brightness == 0 and not has_color_temp_kelvin:
            # Set default color temperature.
            target_state.color_temp_kelvin = self._default_color_temp_kelvin

        # Recover a previous state if you turn off and then on a light.
        if (
            not kwargs
            and self._previous_state is not None
            and self._previous_state.brightness > 0
        ):
            target_state = self._previous_state
            self._previous_state = None

        # This deals with a bug in Astera lights where if you fade from
        # saturated to unsaturated, even though the hue is updated only once
        # saturation is 0, the hue change is still visible.
        if target_state.saturation == 0:
            target_state.hue = self._state.hue

        await self._run_fade(target_state, transition_secs)

    async def async_turn_off(self, **kwargs):
        log.debug(f"DmxLight.async_turn_off({kwargs})")

        transition_secs = kwargs.get(ATTR_TRANSITION, self._default_transition_secs)

        self._previous_state = copy.deepcopy(self._state)
        target_state = DmxLightState(
            brightness=0,
            color_temp_kelvin=self._attr_min_color_temp_kelvin,
            hue=0,
            saturation=0,
        )

        await self._run_fade(target_state, transition_secs)

    async def _run_fade(
        self,
        target_state: DmxLightState,
        transition_secs: float,
    ) -> None:
        log.debug(f"DmxLight._run_fade(")
        log.debug(f"    current_state={self._state},")
        log.debug(f"    {target_state=},")
        log.debug(f"    {transition_secs=})")

        if transition_secs == 0:
            self._state = target_state
            values = [0] * self._coders.num_channels()
            self._coders.encode(values, self._state)
            self._channel.set_fade(values, 0)
            self.async_schedule_update_ha_state()
        else:
            controller = FadeController(
                self._state,
                target_state,
                self._coders,
                self._fade_fps,
                transition_secs,
                self._ha_state_update_freq_secs,
                self.async_schedule_update_ha_state,
            )
            faders = []
            for i in range(self._coders.num_channels()):
                faders.append(Fader(i, controller))

            self._channel.set_fade(faders, 1)
