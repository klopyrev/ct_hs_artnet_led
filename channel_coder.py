from . import DmxLightState


class ChannelCoders:
    def __init__(self, type_config) -> None:
        self._constant_channel_values = []
        self._brightness_coder = None
        self._color_temp_coder = None
        self._hue_coder = None
        self._saturation_coder = None

        channel_i = 0
        for channel_config in type_config["channels"]:
            num_bytes = channel_config.get("bytes", 1)
            endianness = channel_config.get("endianness", "big")
            offset = channel_config.get("offset", 0)
            correction_polynomial = channel_config.get("correction_polynomial", None)

            if channel_config["type"] == "constant":
                self._constant_channel_values.append(
                    (channel_i, channel_config["value"])
                )
            elif channel_config["type"] == "brightness":
                assert self._brightness_coder is None
                self._brightness_coder = ChannelCoder(
                    channel_i,
                    min_value=0,
                    max_value=100,
                    num_bytes=num_bytes,
                    endianness=endianness,
                    offset=offset,
                    is_hue=False,
                    correction_polynomial=correction_polynomial,
                )
            elif channel_config["type"] == "color_temp_kelvin":
                assert self._color_temp_coder is None
                self._color_temp_coder = ChannelCoder(
                    channel_i,
                    min_value=type_config["min_color_temp_kelvin"],
                    max_value=type_config["max_color_temp_kelvin"],
                    num_bytes=num_bytes,
                    endianness=endianness,
                    offset=offset,
                    is_hue=False,
                    correction_polynomial=correction_polynomial,
                )
            elif channel_config["type"] == "hue":
                assert self._hue_coder is None
                self._hue_coder = ChannelCoder(
                    channel_i,
                    min_value=0,
                    max_value=360,
                    num_bytes=num_bytes,
                    endianness=endianness,
                    offset=offset,
                    is_hue=True,
                    correction_polynomial=correction_polynomial,
                )
            elif channel_config["type"] == "saturation":
                assert self._saturation_coder is None
                self._saturation_coder = ChannelCoder(
                    channel_i,
                    min_value=0,
                    max_value=100,
                    num_bytes=num_bytes,
                    endianness=endianness,
                    offset=offset,
                    is_hue=False,
                    correction_polynomial=correction_polynomial,
                )
            else:
                assert False

            channel_i += num_bytes

        self._num_channels = channel_i

        assert self._brightness_coder is not None
        assert self._color_temp_coder is not None
        assert self._hue_coder is not None
        assert self._saturation_coder is not None

    def num_channels(self):
        return self._num_channels

    def encode(self, values: list[int], state: DmxLightState):
        self._brightness_coder.encode(values, state.brightness)
        self._color_temp_coder.encode(values, state.color_temp_kelvin)
        self._hue_coder.encode(values, state.hue)
        self._saturation_coder.encode(values, state.saturation)
        for (
            channel_i,
            value,
        ) in self._constant_channel_values:
            values[channel_i] = value


class ChannelCoder:
    def __init__(
        self,
        channel_i: int,
        max_value: float,
        min_value: float = 0,
        num_bytes: int = 1,
        endianness: str = "big",
        offset: int = 0,
        is_hue: bool = False,
        correction_polynomial: list[float] | None = None,
    ) -> None:
        self._channel_i = channel_i
        assert num_bytes in [1, 2]
        assert endianness in ["big", "little"]
        self._num_bytes = num_bytes
        self._big_endian = endianness == "big"

        self._min_value = min_value
        self._max_value = max_value
        self._value_range = max_value - min_value

        self._channel_min_value = offset
        self._channel_max_value = 1
        for _ in range(num_bytes):
            self._channel_max_value *= 256
        self._channel_max_value -= 1
        self._channel_range = self._channel_max_value - self._channel_min_value
        if is_hue:
            # Hue is special, because a value of 360 is equivalent to 0.
            self._channel_range += 1

        self._is_hue = is_hue
        self._correction_polynomial = correction_polynomial

    def encode(self, values: list[int], value: float):
        if self._correction_polynomial is not None:
            value = self._apply_correction(value, self._correction_polynomial)
        value = min(value, self._max_value)
        value = max(value, self._min_value)

        channel_value = int(
            round(
                (value - self._min_value) / self._value_range * self._channel_range
                + self._channel_min_value
            )
        )
        assert channel_value >= self._channel_min_value
        if self._is_hue:
            if channel_value > self._channel_max_value:
                channel_value = self._channel_min_value
        else:
            assert channel_value <= self._channel_max_value

        if self._num_bytes == 2:
            hi = int(channel_value / 256)
            lo = channel_value % 256
            if self._big_endian:
                values[self._channel_i] = hi
                values[self._channel_i + 1] = lo
            else:
                values[self._channel_i] = lo
                values[self._channel_i + 1] = hi
        else:
            values[self._channel_i] = channel_value

    def _apply_correction(self, value: float, coefficients: list[float]) -> float:
        corrected_value = 0
        for exponent, coefficient in enumerate(coefficients):
            term = coefficient
            for i in range(exponent):
                term *= value
            corrected_value += term
        return corrected_value
