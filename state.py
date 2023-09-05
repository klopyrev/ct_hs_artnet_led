from dataclasses import dataclass


@dataclass
class DmxLightState:
    brightness: float  # Range: [0, 100]
    color_temp_kelvin: float
    hue: float  # Range: [0, 360]
    saturation: float  # Range: [0, 100]
