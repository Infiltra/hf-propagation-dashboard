import math
from datetime import datetime, timezone


# ---------- MUF ----------
def muf_estimate(flux, kp, xray, bz=None):

    if not flux:
        return None

    foF2 = 4.0 + 0.05 * flux
    muf = foF2 * 3.5

    # geomagnetic depression
    if kp:
        muf -= kp * 1.2

    # flare absorption
    if xray and ("M" in xray or "X" in xray):
        muf -= 4

    # 🔥 NEW: IMF Bz coupling (very important)
    if bz:
        if bz < -10:
            muf -= 3
        elif bz < -5:
            muf -= 1.5

    return round(max(muf, 1), 1)

# ---------- GREYLINE ----------
def greyline_points():

    now = datetime.utcnow().replace(tzinfo=timezone.utc)

    # solar declination approximation
    day_of_year = now.timetuple().tm_yday
    decl = 23.44 * math.sin(math.radians((360/365) * (day_of_year - 81)))

    points = []

    for lon in range(-180, 181, 2):

        # hour angle
        time_utc = now.hour + now.minute/60
        ha = (time_utc - lon/15) * 15

        # latitude of terminator
        lat = math.degrees(math.atan(-math.cos(math.radians(ha)) / math.tan(math.radians(decl))))

        points.append({"lat": lat, "lon": lon})

    return points