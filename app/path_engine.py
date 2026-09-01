import math

BANDS = [
    ("10m", 28),
    ("15m", 21),
    ("20m", 14),
    ("40m", 7),
    ("80m", 3.5)
]

# ---------- HAVERSINE ----------
def distance_km(lat1, lon1, lat2, lon2):
    R = 6371
    dlat = math.radians(lat2-lat1)
    dlon = math.radians(lon2-lon1)

    a = (math.sin(dlat/2)**2 +
         math.cos(math.radians(lat1)) *
         math.cos(math.radians(lat2)) *
         math.sin(dlon/2)**2)

    return R * (2 * math.atan2(math.sqrt(a), math.sqrt(1-a)))


# ---------- PATH POINTS ----------
def path_points(lat1, lon1, lat2, lon2, steps=100):
    pts = []
    for i in range(steps+1):
        f = i/steps
        lat = lat1 + (lat2-lat1)*f
        lon = lon1 + (lon2-lon1)*f
        pts.append((lat, lon))
    return pts


# ---------- BAND ----------
def band_from_muf(muf):
    if muf >= 28: return "10m"
    if muf >= 21: return "15m"
    if muf >= 14: return "20m"
    if muf >= 7:  return "40m"
    return "NVIS"


def solar_zenith(lat, lon):

    import datetime

    now = datetime.datetime.utcnow()

    # fractional hour
    hour = now.hour + now.minute / 60

    # day of year
    day = now.timetuple().tm_yday

    # solar declination
    decl = 23.44 * math.sin(math.radians((360/365) * (day - 81)))

    # solar time correction
    solar_time = hour + (lon / 15)

    # hour angle
    ha = 15 * (solar_time - 12)

    # convert to radians
    lat_r = math.radians(lat)
    decl_r = math.radians(decl)
    ha_r = math.radians(ha)

    # zenith angle
    cos_zenith = (
        math.sin(lat_r) * math.sin(decl_r) +
        math.cos(lat_r) * math.cos(decl_r) * math.cos(ha_r)
    )

    cos_zenith = max(min(cos_zenith, 1), -1)

    zenith = math.degrees(math.acos(cos_zenith))

    return zenith

def absorption_loss(lat, lon, muf, xray, kp, zenith):

    loss = 0

    # ---------- D-LAYER (DAYTIME ABSORPTION) ----------
    # strongest near noon (zenith small)
    if zenith < 90:
        day_factor = max(0, (90 - zenith) / 90)
        loss += 20 * day_factor   # up to ~20% loss

    # ---------- XRAY FLARE ----------
    if xray:
        if "X" in xray:
            loss += 40
        elif "M" in xray:
            loss += 25
        elif "C" in xray:
            loss += 10

    # ---------- GEOMAGNETIC (Kp) ----------
    if kp:
        loss += kp * 3   # storms increase absorption

    # ---------- FREQUENCY DEPENDENCE ----------
    # lower MUF → lower freq → less D-layer absorption
    if muf < 7:
        loss *= 0.6
    elif muf < 14:
        loss *= 0.8
    else:
        loss *= 1.0

    return loss

def score_bands(min_muf, avg_loss, distance_km, grey_points):

    band_scores = {}

    for name, freq in BANDS:

        # ---------- BASE ----------
        score = 50

        # ---------- MUF vs BAND (SMOOTH, NOT BINARY) ----------
        muf_ratio = min_muf / freq

        if muf_ratio >= 1.5:
            score += 15
        elif muf_ratio >= 1.2:
            score += 10
        elif muf_ratio >= 1.0:
            score += 5
        elif muf_ratio >= 0.8:
            score -= 10
        else:
            score -= 30

        # ---------- DISTANCE SHAPING ----------
        if distance_km < 300:
            score -= 30   # near-field (bad for HF)
        elif distance_km < 1000:
            score -= 10
        elif distance_km < 4000:
            score += 10   # sweet spot
        elif distance_km > 8000:
            score -= 15

        # ---------- ABSORPTION (FREQ DEPENDENT) ----------
        # high freq suffers more
        freq_factor = freq / 10

        loss_penalty = avg_loss * freq_factor * 0.5
        score -= loss_penalty

        # ---------- GREYLINE BOOST ----------
        if grey_points:
            avg_weight = sum(p["weight"] for p in grey_points) / len(grey_points)
            score += int(25 * avg_weight)

        # ---------- CLAMP ----------
        score = max(min(score, 95), 5)

        # ---------- LABEL ----------
        if score > 80:
            label = "Excellent"
        elif score > 65:
            label = "Good"
        elif score > 50:
            label = "Fair"
        elif score > 30:
            label = "Poor"
        else:
            label = "Unusable"

        band_scores[name] = {
            "score": round(score, 1),
            "quality": label
        }

    return band_scores

# ---------- MAIN ENGINE ----------
def path_prediction(tx, rx, muf_func, greyline_func, xray=None, kp=None):

    lat1, lon1 = tx
    lat2, lon2 = rx

    dist = round(distance_km(lat1, lon1, lat2, lon2), 1)

    pts = path_points(lat1, lon1, lat2, lon2)

    muf_values = []
    absorption_values = []   # ✅ FIX
    grey_hits = 0
    grey_points = []

    grey_pts = greyline_func()

    for i, (lat, lon) in enumerate(pts):

        frac = i / (len(pts) - 1) if len(pts) > 1 else 0

        zenith = solar_zenith(lat, lon)

        muf = muf_func(lat, lon)

        loss = absorption_loss(
            lat, lon,
            muf,
            xray,
            kp,
            zenith
        )

        absorption_values.append(loss)   #  FIX

        effective_muf = max(muf - (loss * 0.1), 1)

        print(f"[ABS] lat={lat} muf={muf:.2f} loss={loss:.2f} eff={effective_muf:.2f}")

        muf_values.append(effective_muf)

        for g in grey_pts:
            if abs(lat - g["lat"]) < 3 and abs(lon - g["lon"]) < 3:

                grey_hits += 1

                dist_from_mid = abs(frac - 0.5)
                weight = max(0, 1 - (dist_from_mid * 2))

                grey_points.append({
                    "lat": lat,
                    "lon": lon,
                    "weight": round(weight, 2),
                    "pos": round(frac, 2)
                })

                break

    # ---------- FINAL METRICS ----------
    min_muf = min(muf_values)
    avg_muf = sum(muf_values) / len(muf_values)

    avg_loss = sum(absorption_values) / len(absorption_values) if absorption_values else 0
    max_loss = max(absorption_values) if absorption_values else 0
    min_loss = min(absorption_values) if absorption_values else 0

    band_scores = score_bands(
                min_muf,
                avg_loss,
                dist,
                grey_points
                )
    best_band = best_band = max(band_scores.items(), key=lambda x: x[1]["score"])[0]

    # ---------- RELIABILITY ----------
    reliability = 50

    if avg_muf > 14:
        reliability += 15
    elif avg_muf > 10:
        reliability += 8

    if min_muf < 5:
        reliability -= 20

    if grey_points:
        total_weight = sum(p["weight"] for p in grey_points)
        avg_weight = total_weight / len(grey_points)

        reliability += int(30 * avg_weight)

        if len(grey_points) > 3:
            reliability += 5
    
    # ---------- RETURN ----------
    return {
        "distance_km": dist,
        "min_muf": round(min_muf, 1),
        "avg_muf": round(avg_muf, 1),
        "best_band": best_band,
        "greyline_hits": grey_hits,
        "greyline_points": grey_points,
        "reliability": max(min(reliability, 95), 10),
        "bands": band_scores,
        "absorption": {   #  FIXED
            "avg_loss": round(avg_loss, 2),
            "max_loss": round(max_loss, 2),
            "min_loss": round(min_loss, 2)
        },
        "path": pts
    }