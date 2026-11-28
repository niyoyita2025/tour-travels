"""
The environmental measurements the website understands.

Adding a new sensor type means adding one line here with its unit and the range a real sensor can report.
Values outside that range are refused, so a faulty sensor cannot fill the database with nonsense.
"""

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Metric:
    key: str
    label: str
    unit: str
    low: float    # the smallest value a real sensor reports
    high: float   # the largest value a real sensor reports


METRICS = {m.key: m for m in (
    Metric('temperature', 'Temperature', '°C', -40, 85),
    Metric('humidity', 'Relative humidity', '%', 0, 100),
    Metric('pressure', 'Air pressure', 'hPa', 300, 1100),
    Metric('co2', 'Carbon dioxide (CO2)', 'ppm', 0, 10000),
    Metric('pm25', 'Fine dust (PM2.5)', 'µg/m³', 0, 1000),
    Metric('pm10', 'Coarse dust (PM10)', 'µg/m³', 0, 2000),
    Metric('air_quality', 'Air quality index', 'AQI', 0, 500),
    Metric('voc', 'Volatile organic compounds', 'index', 0, 500),
    Metric('light', 'Light level', 'lux', 0, 200000),
)}


def choices():
    return [(key, metric.label) for key, metric in METRICS.items()]


def validate_value(key, value):
    """Return the value as a float. Raise ValueError with a plain reason when it cannot be stored."""
    metric = METRICS.get(key)
    if metric is None:
        raise ValueError(f'"{key}" is not a measurement this website records.')
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f'The {metric.label.lower()} must be a number.')
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f'The {metric.label.lower()} must be a finite number.')
    if not metric.low <= number <= metric.high:
        raise ValueError(f'The {metric.label.lower()} {number:g} {metric.unit} is outside the range this sensor '
                         f'can report ({metric.low:g} to {metric.high:g} {metric.unit}).')
    return number
