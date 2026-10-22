"""Running calculations and unit formatting. The only place pace/speed/units are derived."""

M_PER_KM = 1000.0
M_PER_MI = 1609.344


def pace_s_per_km(distance_m: float, duration_s: float) -> float | None:
    if not distance_m or not duration_s or distance_m <= 0 or duration_s <= 0:
        return None
    return duration_s / (distance_m / M_PER_KM)


def speed_kmh(distance_m: float, duration_s: float) -> float | None:
    if not distance_m or not duration_s or distance_m <= 0 or duration_s <= 0:
        return None
    return (distance_m / M_PER_KM) / (duration_s / 3600.0)


def format_duration(seconds: float) -> str:
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def format_distance(distance_m: float, units: str = "km") -> str:
    if units == "mi":
        return f"{distance_m / M_PER_MI:.2f} mi"
    return f"{distance_m / M_PER_KM:.2f} km"


def format_pace(sec_per_km: float | None, units: str = "km") -> str:
    if not sec_per_km:
        return "–"
    per_unit = sec_per_km * (M_PER_MI / M_PER_KM) if units == "mi" else sec_per_km
    s = int(round(per_unit))
    return f"{s // 60}:{s % 60:02d}/{'mi' if units == 'mi' else 'km'}"
