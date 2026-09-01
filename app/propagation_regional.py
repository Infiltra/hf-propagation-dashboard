import math
from datetime import datetime, timezone

def fof2_iri_like(lat, lon, flux, hour_utc):

    # -------- SOLAR CONTROL --------
    if not flux:
        return 5.0

    # -------- TIME (DIURNAL) --------
    # peak around 14 local time
    local_time = (hour_utc + lon/15) % 24
    time_factor = math.exp(-((local_time - 14)/5)**2)

    # -------- LATITUDE (EQUATORIAL ANOMALY) --------
    lat_abs = abs(lat)

    # equatorial trough + anomaly peaks (~±15°)
    eq_peak = math.exp(-((lat_abs - 15)/10)**2)
    mid_lat = math.exp(-((lat_abs - 40)/20)**2)

    lat_factor = 0.6 + 0.6 * eq_peak + 0.3 * mid_lat

    # -------- SOLAR FLUX --------
    flux_factor = 0.03 * flux

    # -------- BASE foF2 --------
    fof2 = 3 + flux_factor * time_factor * lat_factor

    # clamp
    return max(fof2, 1.5)

# ---------- SOLAR ZENITH ----------
def solar_zenith(lat, lon):

    now = datetime.utcnow().replace(tzinfo=timezone.utc)

    # day of year
    n = now.timetuple().tm_yday

    # solar declination
    decl = 23.44 * math.sin(math.radians((360/365)*(n-81)))

    # time offset
    time_utc = now.hour + now.minute/60
    lst = (time_utc + lon/15) % 24

    # hour angle
    ha = 15 * (lst - 12)

    # zenith angle
    cosz = (
        math.sin(math.radians(lat)) * math.sin(math.radians(decl)) +
        math.cos(math.radians(lat)) * math.cos(math.radians(decl)) * math.cos(math.radians(ha))
    )

    return math.degrees(math.acos(max(min(cosz,1),-1)))


# ---------- REGIONAL MUF ----------
def muf_regional(lat, lon, flux, kp, xray, bz):

    if not flux:
        return None

    zenith = solar_zenith(lat, lon)

    daylight = max(math.cos(math.radians(zenith)), 0.15)

    now = datetime.utcnow()
    hour = now.hour + now.minute / 60

    foF2 = fof2_iri_like(lat, lon, flux, hour)
    muf = foF2 * 3.5   # realistic MUF factor

    if kp:
        muf -= kp * 1.0

    if xray and ("M" in xray or "X" in xray):
        muf -= 4

    if bz:
        if bz < -10:
            muf -= 3
        elif bz < -5:
            muf -= 1.5

    if abs(lat) > 60:
        muf *= 0.75
        if kp:
            muf -= kp * 0.5

    # GREYLINE BOOST
    delta = abs(zenith - 90)
    if delta < 12:
        boost = (1 - (delta / 12))
        muf *= (1 + 0.25 * boost)

    return round(max(muf, 1), 1)


# ---------- GRID ----------
def muf_grid(flux, kp, xray, bz):

    grid = []

    for lat in range(-90, 91, 1):
        for lon in range(-180, 181, 8):

            muf = muf_regional(lat, lon, flux, kp, xray, bz)

            grid.append({
                "lat": lat,
                "lon": lon,
                "muf": muf
            })

    return grid