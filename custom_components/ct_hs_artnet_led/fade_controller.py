from collections.abc import Callable
import colormath.color_objects
import colormath.color_conversions
import copy
from enum import Enum
import logging
from time import time

import pyartnet

from . import ChannelCoders
from . import DmxLightState


log = logging.getLogger(__name__)


class FadeType(Enum):
    OFF_TO_ON = 1
    ON_TO_OFF = 2
    UNSATURATED_TO_SATURATED = 3
    SATURATED_TO_UNSATURATED = 4
    HUE_FADE = 5
    OTHER = 6


class FadeController:
    def __init__(
        self,
        mutable_current_state: DmxLightState,
        target_state: DmxLightState,
        coders: ChannelCoders,
        fps: int,
        fade_duration_secs: float,
        update_state_frequency_secs: float,
        update_state_callback: Callable[[], None],
    ) -> None:
        self._start_time_secs = time()
        self._end_time_secs = self._start_time_secs + fade_duration_secs
        self._is_done = False

        self._last_state_change_time_secs = self._start_time_secs
        self._change_delay_secs = 1 / fps
        self._num_changes = 0

        self._last_home_assistant_update_time_secs = self._start_time_secs
        self._home_assistant_update_state_frequency_secs = update_state_frequency_secs
        self._home_assistant_update_state_callback = update_state_callback

        self._current_state = mutable_current_state
        self._start_state = copy.deepcopy(mutable_current_state)
        self._target_state = target_state

        self._values = [0] * coders.num_channels()
        coders.encode(self._values, mutable_current_state)
        self._coders = coders

        if self._start_state.brightness == 0 and self._target_state.brightness > 0:
            self._fade_type = FadeType.OFF_TO_ON
        elif self._start_state.brightness > 0 and self._target_state.brightness == 0:
            self._fade_type = FadeType.ON_TO_OFF
        elif self._start_state.saturation == 0 and self._target_state.saturation > 0:
            self._fade_type = FadeType.UNSATURATED_TO_SATURATED
        elif self._start_state.saturation > 0 and self._target_state.saturation == 0:
            self._fade_type = FadeType.SATURATED_TO_UNSATURATED
        elif self._start_state.hue != self._target_state.hue:
            self._fade_type = FadeType.HUE_FADE
            self._init_hue_fade()
        else:
            self._fade_type = FadeType.OTHER

    # The code is set up so that this function will get called once for each
    # channel in incremental order, very frequently, until all channels return
    # True for the second return value.
    def calc_next_value(self, channel_i) -> tuple[float, bool]:
        if channel_i == 0:
            self._maybe_update_state()
            self._maybe_send_update_to_home_assistant()

        return self._values[channel_i], self._is_done

    def _maybe_update_state(self):
        current_time_secs = time()
        if (
            current_time_secs - self._last_state_change_time_secs
            < self._change_delay_secs
        ):
            return

        change_time_secs = self._last_state_change_time_secs + self._change_delay_secs
        while change_time_secs + self._change_delay_secs < current_time_secs:
            # We may occassionally skip frames if the CPU isn't able to keep up.
            change_time_secs += self._change_delay_secs

        is_first_step = self._last_state_change_time_secs == self._start_time_secs
        is_last_step = change_time_secs >= self._end_time_secs

        if is_first_step:
            self._debug_print_state()

        self._compute_new_state(change_time_secs, is_first_step, is_last_step)
        self._coders.encode(self._values, self._current_state)

        if is_first_step:
            self._debug_print_state()

        self._last_state_change_time_secs = change_time_secs
        self._num_changes += 1
        self._is_done = is_last_step

    def _maybe_send_update_to_home_assistant(self):
        current_time_secs = time()
        if self._is_done or (
            current_time_secs - self._last_home_assistant_update_time_secs
            >= self._home_assistant_update_state_frequency_secs
        ):
            self._home_assistant_update_state_callback()
            self._last_home_assistant_update_time_secs = current_time_secs
            self._debug_print_state()

    def _debug_print_state(self):
        if self._fade_type == FadeType.HUE_FADE:
            log.debug(
                f"bright={round(self._current_state.brightness, 1)}, "
                f"ct={round(self._current_state.color_temp_kelvin)}, "
                f"hue={round(self._current_state.hue)}, "
                f"sat={round(self._current_state.saturation, 1)}, "
                f"l={round(self._current_lch.lch_l, 1)}, "
                f"c={round(self._current_lch.lch_c, 1)}, "
                f"h={round(self._current_lch.lch_h, 1)}"
            )
        else:
            log.debug(
                f"bright={round(self._current_state.brightness, 1)}, "
                f"ct={round(self._current_state.color_temp_kelvin)}, "
                f"hue={round(self._current_state.hue)}, "
                f"sat={round(self._current_state.saturation, 1)}"
            )

        if self._is_done:
            intended_fps = round(1 / self._change_delay_secs)
            actual_fps = round(
                self._num_changes / (self._end_time_secs - self._start_time_secs)
            )
            log.debug(f"{intended_fps=}, {actual_fps=}")

    def _compute_new_state(
        self, change_time_secs: float, is_first_step: bool, is_last_step: bool
    ):
        travel_fraction = (change_time_secs - self._last_state_change_time_secs) / (
            self._end_time_secs - self._last_state_change_time_secs
        )
        travel_fraction = min(1, travel_fraction)

        if self._fade_type == FadeType.OFF_TO_ON:
            self._off_to_on_fade(travel_fraction, is_first_step, is_last_step)
        elif self._fade_type == FadeType.ON_TO_OFF:
            self._on_to_off_fade(travel_fraction, is_first_step, is_last_step)
        elif self._fade_type == FadeType.UNSATURATED_TO_SATURATED:
            self._unsaturated_to_saturated_fade(
                travel_fraction, is_first_step, is_last_step
            )
        elif self._fade_type == FadeType.SATURATED_TO_UNSATURATED:
            self._saturated_to_unsaturated_fade(
                travel_fraction, is_first_step, is_last_step
            )
        elif self._fade_type == FadeType.HUE_FADE:
            self._hue_fade(travel_fraction, is_first_step, is_last_step)
        else:
            self._other_fade(travel_fraction, is_first_step, is_last_step)

    def _off_to_on_fade(self, travel_fraction, is_first_step, is_last_step):
        if is_first_step:
            self._current_state.color_temp_kelvin = self._target_state.color_temp_kelvin
            self._current_state.hue = self._target_state.hue
            self._current_state.saturation = self._target_state.saturation

        self._linear_fade_brightness(travel_fraction)

    def _on_to_off_fade(self, travel_fraction, is_first_step, is_last_step):
        self._linear_fade_brightness(travel_fraction)

        if is_last_step:
            self._current_state.color_temp_kelvin = self._target_state.color_temp_kelvin
            self._current_state.hue = self._target_state.hue
            self._current_state.saturation = self._target_state.saturation

    def _unsaturated_to_saturated_fade(
        self, travel_fraction, is_first_step, is_last_step
    ):
        if is_first_step:
            self._current_state.hue = self._target_state.hue

        self._linear_fade_brightness(travel_fraction)
        self._linear_fade_color_temp(travel_fraction)
        self._linear_fade_saturation(travel_fraction)

    def _saturated_to_unsaturated_fade(
        self, travel_fraction, is_first_step, is_last_step
    ):
        self._linear_fade_brightness(travel_fraction)
        self._linear_fade_color_temp(travel_fraction)
        self._linear_fade_saturation(travel_fraction)

        if is_last_step:
            self._current_state.hue = self._target_state.hue

    def _init_hue_fade(self):
        current_hsv = colormath.color_objects.HSVColor(
            self._current_state.hue,
            self._current_state.saturation / 100,
            self._current_state.brightness / 100,
        )
        self._current_lch = colormath.color_conversions.convert_color(
            current_hsv, colormath.color_objects.LCHabColor
        )
        target_hsv = colormath.color_objects.HSVColor(
            self._target_state.hue,
            self._target_state.saturation / 100,
            self._target_state.brightness / 100,
        )
        self._target_lch = colormath.color_conversions.convert_color(
            target_hsv, colormath.color_objects.LCHabColor
        )

    def _hue_fade(self, travel_fraction, is_first_step, is_last_step):
        # Color temperature fades independently.
        self._linear_fade_color_temp(travel_fraction)

        self._current_lch.lch_l = self._compute_linear_update(
            self._current_lch.lch_l, self._target_lch.lch_l, travel_fraction
        )
        self._current_lch.lch_c = self._compute_linear_update(
            self._current_lch.lch_c, self._target_lch.lch_c, travel_fraction
        )
        self._current_lch.lch_h = self._compute_hue_linear_update(
            self._current_lch.lch_h, self._target_lch.lch_h, travel_fraction
        )
        current_hsv = colormath.color_conversions.convert_color(
            self._current_lch, colormath.color_objects.HSVColor
        )
        self._current_state.hue = current_hsv.hsv_h
        self._current_state.saturation = current_hsv.hsv_s * 100
        self._current_state.brightness = current_hsv.hsv_v * 100

    def _other_fade(self, travel_fraction, is_first_step, is_last_step):
        self._linear_fade_brightness(travel_fraction)
        self._linear_fade_color_temp(travel_fraction)
        self._linear_fade_saturation(travel_fraction)

        assert self._current_state.hue == self._target_state.hue

    def _linear_fade_brightness(self, travel_fraction):
        if self._current_state.brightness != self._target_state.brightness:
            self._current_state.brightness = self._compute_linear_update(
                self._current_state.brightness,
                self._target_state.brightness,
                travel_fraction,
            )

    def _linear_fade_color_temp(self, travel_fraction):
        if (
            self._current_state.color_temp_kelvin
            != self._target_state.color_temp_kelvin
        ):
            self._current_state.color_temp_kelvin = self._compute_linear_update(
                self._current_state.color_temp_kelvin,
                self._target_state.color_temp_kelvin,
                travel_fraction,
            )

    def _linear_fade_saturation(self, travel_fraction):
        if self._current_state.saturation != self._target_state.saturation:
            self._current_state.saturation = self._compute_linear_update(
                self._current_state.saturation,
                self._target_state.saturation,
                travel_fraction,
            )

    def _compute_linear_update(self, current_value, target_value, travel_fraction):
        return current_value + (target_value - current_value) * travel_fraction

    def _compute_hue_linear_update(self, current_value, target_value, travel_fraction):
        if target_value - current_value > 180:
            current_value += 360
        elif current_value - target_value > 180:
            current_value -= 360

        new_value = self._compute_linear_update(
            current_value, target_value, travel_fraction
        )

        if new_value >= 360:
            new_value -= 360
        elif new_value < 0:
            new_value += 360
        return new_value


class Fader(pyartnet.fades.FadeBase):
    def __init__(self, channel_i: int, controller: FadeController) -> None:
        super().__init__()
        self._channel_i = channel_i
        self._controller = controller

    # Needed because of broken "not 0 <= target <= self._value_max" check.
    def __le__(self, other):
        return True

    # Needed because of broken "not 0 <= target <= self._value_max" check.
    def __ge__(self, other):
        return True

    def initialize(self, current: int, target: int, steps: int):
        pass

    def calc_next_value(self) -> float:
        next_value, is_done = self._controller.calc_next_value(self._channel_i)
        self.is_done = is_done
        return next_value
