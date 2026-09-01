from fastapi import FastAPI
from fastapi.responses import FileResponse
import requests, threading, time
from datetime import datetime
import pytz
import statistics
import math
from app.propagation import muf_estimate, greyline_points
from app.propagation_regional import muf_regional, muf_grid
from app.path_engine import path_prediction
from app.path_engine import distance_km
from app.propagation import muf_estimate, greyline_points
import math

app = FastAPI()
cache = {"data": None}


# ---------------- POLAR / AURORAL ----------------

def auroral_oval_lat(kp: float):
    # simple mapping: higher Kp → oval moves equatorward
    if kp is None:
        return 67
    return 67 - min(max(kp, 0), 9) * 2.0  # ~67 → ~49

def polar_absorption(lat: float, kp: float):
    lat_abs = abs(lat)
    if kp is None:
        kp = 2

    # base polar loss
    loss = 0.0
    if lat_abs > 60:
        loss += 5 + (lat_abs - 60) * 0.5  # grows with latitude

    # storm-time enhancement
    loss += kp * 2.5

    return loss  # dB-ish penalty in your scoring domain


def auroral_scatter_gain(lat: float, kp: float, d_km: float):
    """
    Weak, broad gain near auroral oval. Helps “fill” otherwise dead paths.
    """
    if kp is None:
        return 1.0

    oval = auroral_oval_lat(kp)
    lat_abs = abs(lat)

    # proximity to oval (bell-shaped)
    prox = math.exp(-((lat_abs - oval) / 6.0) ** 2)

    # distance preference (auroral paths often 1–4 Mm)
    d_pref = math.exp(-((d_km - 2500) / 1500) ** 2)

    gain = 1.0 + 0.25 * prox * d_pref * (kp / 5.0)
    return gain  # ~1.0–1.25

# ---------------- SCATTER MODES ----------------

def sporadic_e_boost(freq_mhz: float, lat: float, hour_utc: float, d_km: float):
    """
    Es favors 10–50 MHz, mid-lats, daytime/early evening, 800–2200 km.
    """
    # frequency window
    if not (10 <= freq_mhz <= 50):
        return 1.0

    lat_abs = abs(lat)

    # mid-lat preference (20–55)
    lat_w = math.exp(-((lat_abs - 35) / 15) ** 2)

    # time: peaks ~12–20 LT
    local_time = (hour_utc + 0) % 24  # lon can be added if you want
    time_w = math.exp(-((local_time - 16) / 4) ** 2)

    # distance window
    d_w = math.exp(-((d_km - 1500) / 700) ** 2)

    boost = 1.0 + 0.6 * lat_w * time_w * d_w
    return boost  # up to ~1.6


def tep_boost(lat_tx: float, lat_rx: float, freq_mhz: float, hour_utc: float, d_km: float):
    """
    Transequatorial propagation: N–S paths straddling equator, 3–7 Mm, 10–30 MHz, evening.
    """
    if not (10 <= freq_mhz <= 30):
        return 1.0

    # must straddle equator
    if lat_tx * lat_rx >= 0:
        return 1.0

    # distance window
    d_w = math.exp(-((d_km - 4500) / 2000) ** 2)

    # time window (evening ~17–22 LT)
    lt = (hour_utc + 0) % 24
    t_w = math.exp(-((lt - 19) / 3.5) ** 2)

    # equatorial proximity
    eq_w = math.exp(-((abs(lat_tx) + abs(lat_rx)) / 40) ** 2)

    boost = 1.0 + 0.5 * d_w * t_w * eq_w
    return boost  # up to ~1.5

def noise_floor(freq_mhz, lat, lon, hour_utc):

    # ---------- BASE NOISE ----------
    # lower freq = higher noise
    if freq_mhz < 5:
        base = 40
    elif freq_mhz < 10:
        base = 30
    elif freq_mhz < 20:
        base = 20
    else:
        base = 12

    # ---------- TIME (night noisier at low freq) ----------
    local_time = (hour_utc + lon / 15) % 24

    if local_time < 6 or local_time > 20:
        base += 8
    else:
        base -= 3

    # ---------- LOCATION ----------
    # crude model
    if -60 < lat < 60:
        if -160 < lon < -30 or 20 < lon < 150:
            base -= 5   # ocean quieter
        else:
            base += 5   # land noisier

    # ---------- POLAR ----------
    if abs(lat) > 60:
        base += 10

    return max(base, 5)

def reflection_height(freq_mhz, muf):

    # base F-layer height
    base = 250

    # frequency scaling
    if muf <= 0:
        return base

    ratio = freq_mhz / muf

    # higher freq → higher reflection
    height = base + (150 * ratio)

    # clamp realistic bounds
    return max(180, min(height, 450))

def ground_conductivity(lat, lon):

    # ITU-like simplified zones

    # ocean (best)
    if -60 < lat < 60:
        if -160 < lon < -30:
            return 5.0   # Pacific
        if -30 < lon < 20:
            return 4.0   # Atlantic
        if 20 < lon < 150:
            return 4.5   # Indian

    # deserts (good)
    if 10 < lat < 35 and 20 < lon < 60:
        return 2.5   # Sahara / Middle East

    # forests / inland (poor)
    return 1.0

# ---------------- DEBUG ----------------
def log(tag, msg):
    print(f"[{tag}] {msg}")


# ---------------- FETCH ----------------
def fetch_json(url):
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log("ERROR", f"{url} -> {e}")
        return None


# ---------------- KP ----------------
def kp_json():
    d = fetch_json("https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json")
    if not d:
        return None
    val = float(d[-1]["Kp"])
    log("KP_JSON", val)
    return val


def kp_text():
    try:
        txt = requests.get(
            "https://services.swpc.noaa.gov/text/daily-geomagnetic-indices.txt",
            timeout=10
        ).text

        for line in reversed(txt.splitlines()):
            if not line[:4].isdigit():
                continue

            vals = []
            for v in line.split()[3:11]:
                if "-" in v:
                    vals.extend([float(x) for x in v.split("-") if x])
                elif v:
                    vals.append(float(v))

            nz = [v for v in vals if v > 0]
            val = nz[-1] if nz else max(vals)

            log("KP_TEXT", val)
            return val

    except Exception as e:
        log("KP_TEXT_ERR", e)

    return None


#PATH ENGINE
def muf_wrapper(lat, lon):

    d = cache.get("data", {})

    # -------- FIX KP --------
    kp_raw = d.get("kp")

    if isinstance(kp_raw, dict):
        kp = kp_raw.get("final") or kp_raw.get("json") or kp_raw.get("text")
    else:
        kp = kp_raw

    # -------- OTHER VALUES --------
    sc = d.get("solar_cycle") or {}
    flux = sc.get("flux")

    xray = (d.get("xray") or {}).get("display")

    mag = d.get("solar_wind_mag") or {}
    bz = mag.get("bz")

    if not flux:
        return 5  # safe fallback

    return muf_regional(lat, lon, flux, kp, xray, bz)


def greyline_wrapper():
    return greyline_points()

# ---------------- XRAY ----------------
def xray_class(f):
    if f < 1e-8: return "A"
    if f < 1e-7: return f"A{f/1e-8:.1f}"
    if f < 1e-6: return f"B{f/1e-7:.1f}"   # B1.0–B9.9
    if f < 1e-5: return f"C{f/1e-6:.1f}"   # C1.0–C9.9  ← was /1e-5
    if f < 1e-4: return f"M{f/1e-5:.1f}"   # M1.0–M9.9  ← was /1e-4
    return f"X{f/1e-4:.1f}"                # X1.0+       ← was /1e-3


# ---------------- EXTRA ----------------
def solar_cycle():
    # Daily F10.7 — updated every day
    flux_data = fetch_json("https://services.swpc.noaa.gov/json/solar-cycle/f10-7cm-flux.json")
    
    
    # Daily sunspots
    ssn_data = fetch_json("https://services.swpc.noaa.gov/json/solar-cycle/swpc_observed_ssn.json")
    # log("SSN DATA", ssn_data)

    flux = None
    ssn = None

    if flux_data:
        # Find last entry where value is not -1.0
        valid = [x for x in flux_data if x.get("f10.7", -1) > 0]
        if valid:
            flux = valid[-1]["f10.7"]

    if ssn_data:
        valid = [x for x in ssn_data if x.get("swpc_ssn", -1) >= 0]
        # log("SSN: ", valid)
        if valid:
            ssn = valid[-1]["swpc_ssn"]

    return {"sunspots": ssn, "flux": flux}


# ---------------- SOLAR WIND (IMPROVED) ----------------
def solar_wind():

    d = fetch_json("https://services.swpc.noaa.gov/products/solar-wind/plasma-1-day.json")
    if not d or len(d) < 10:
        log("SOLAR_WIND", "NO DATA")
        return None

    rows = []

    for x in d:
        try:
            density = float(x[1])
            speed = float(x[2])

            if density > 0 and speed > 0:
                rows.append((density, speed))

        except:
            continue

    if not rows:
        log("SOLAR_WIND", "NO VALID ROWS")
        return None

    recent = rows[-20:]

    densities = [r[0] for r in recent]
    speeds = [r[1] for r in recent]

    if not densities or not speeds:
        log("SOLAR_WIND", "EMPTY SERIES")
        return None

    speed = statistics.mean(speeds)
    density = statistics.mean(densities)

    log("SOLAR_SPEED", speed)
    log("SOLAR_DENSITY", density)

    return {
        "speed": round(speed, 1),
        "density": round(density, 1)
    }


def solar_wind_mag():

    d = fetch_json("https://services.swpc.noaa.gov/products/solar-wind/mag-1-day.json")
    if not d or len(d) < 10:
        log("SOLAR_MAG", "NO DATA")
        return None

    rows = []

    for x in d:
        try:
            bt = float(x[3])
            bz = float(x[6])

            rows.append((bt, bz))

        except:
            continue

    if not rows:
        log("SOLAR_MAG", "NO VALID ROWS")
        return None

    recent = rows[-20:]

    bt_vals = [r[0] for r in recent]
    bz_vals = [r[1] for r in recent]

    bt = statistics.mean(bt_vals)
    bz = statistics.mean(bz_vals)

    neg = [v for v in bz_vals if v < 0]
    south_bias = sum(neg) / len(neg) if neg else 0

    return {
        "bt": round(bt, 2),
        "bz": round(bz, 2),
        "bz_trend": round(south_bias, 2)
    }

def solar_wind_impact(speed, bz, bz_trend=None):

    if speed and speed > 600 and bz and bz < -10:
        return "Strong"

    if speed and speed > 500 and bz and bz < -5:
        return "Moderate"

    if bz_trend and bz_trend < -5:
        return "Elevated"

    return "Quiet"

def global_propagation_grid():

    print("[GLOBAL] START")

    data = cache["data"]

    # flux = data.get("solar", {}).get("flux")
    flux = data.get("solar_cycle", {}).get("flux")
    kp = data.get("kp") if not isinstance(data.get("kp"), dict) else data.get("kp", {}).get("final")
    xray = data.get("xray", {}).get("display")
    # bz = data.get("solar_wind", {}).get("bz")
    bz = data.get("geomagnetic", {}).get("bz")
    print(f"[GLOBAL INPUT] flux={flux} kp={kp} xray={xray} bz={bz}")

    grid = []

    # resolution (you can tune later)
    LAT_STEP = 3
    LON_STEP = 3

    for lat in range(-60, 61, LAT_STEP):
        for lon in range(-180, 181, LON_STEP):

            muf = muf_regional(lat, lon, flux, kp, xray, bz)

            if muf is None:
                continue

            # ---------- SIMPLE SNR MODEL ----------
            snr = 50

            # MUF contribution
            if muf > 20:
                snr += 20
            elif muf > 10:
                snr += 10
            elif muf < 5:
                snr -= 20

            # geomagnetic penalty
            if kp:
                snr -= kp * 3

            # flare penalty
            if xray and ("M" in xray or "X" in xray):
                snr -= 15

            # clamp
            snr = max(min(snr, 95), 5)

            # ---------- BAND ----------
            if muf >= 28:
                band = "10m"
            elif muf >= 21:
                band = "15m"
            elif muf >= 14:
                band = "20m"
            elif muf >= 7:
                band = "40m"
            else:
                band = "80m"

            grid.append({
                "lat": lat,
                "lon": lon,
                "muf": round(muf, 1),
                "snr": snr,
                "band": band
            })

    print(f"[GLOBAL] points={len(grid)}")

    return grid

# ---------------- SCALES ----------------
def g_scale(kp):
    if kp is None: return "N/A"
    if kp >= 9: return "G5"
    if kp >= 8: return "G4"
    if kp >= 7: return "G3"
    if kp >= 6: return "G2"
    if kp >= 5: return "G1"
    return "Quiet"


def r_scale(xray):
    if not xray: return "R0"
    if "X" in xray: return "R3+"
    if "M" in xray: return "R2"
    if "C" in xray: return "R1"
    return "R0"


def s_scale(proton):
    if not proton: return "S0"
    if proton > 100: return "S3"
    if proton > 10: return "S2"
    if proton > 1: return "S1"
    return "S0"


def aurora_level(kp, bz):
    if kp is None:
        return "None"
    if kp >= 7:
        return "High"
    if kp >= 5:
        return "Moderate"
    if kp >= 4:
        return "Low"
    return "None"


def proton_storm(p):
    if not p:
        return "S0"
    if p > 100:
        return "S3"
    if p > 10:
        return "S2"
    if p > 1:
        return "S1"
    return "S0"


# def ap_index(kp):
#     if kp is None:
#         return None
#     return int((kp ** 2) * 2)

KP_TO_AP = {
    0: 0, 0.33: 2, 0.67: 3, 1: 4, 1.33: 5, 1.67: 6,
    2: 7, 2.33: 9, 2.67: 12, 3: 15, 3.33: 18, 3.67: 22,
    4: 27, 4.33: 32, 4.67: 39, 5: 48, 5.33: 56, 5.67: 67,
    6: 80, 6.33: 94, 6.67: 111, 7: 132, 7.33: 154, 7.67: 179,
    8: 207, 8.33: 236, 8.67: 300, 9: 400
}

def ap_index(kp):
    if kp is None:
        return None
    # find nearest key
    closest = min(KP_TO_AP.keys(), key=lambda k: abs(k - kp))
    return KP_TO_AP[closest]


def hf_blackout(xray, kp, proton, bz=None):

    if xray and ("X" in xray):
        return "Severe"

    if xray and ("M" in xray) and bz and bz < -5:
        return "High"

    if kp and kp >= 5:
        return "Moderate"

    if proton and proton > 10:
        return "Moderate"

    return "Low"
# ---------------- EVENTS ----------------
def events(kp, bz):
    return {
        "aurora": kp and kp >= 5,
        "hf_degradation": kp and kp >= 4,
        "polar_absorption": bz and bz < -10
    }


# ---------------- HF BANDS ----------------
def band_conditions(kp, flux, xray):

    s = 0

    if flux:
        if flux > 150: s += 2
        elif flux > 100: s += 1

    if kp:
        if kp >= 5: s -= 2
        elif kp >= 3: s -= 1

    if xray and ("M" in xray or "X" in xray):
        s -= 2

    def label(v):
        if v >= 2: return "Excellent"
        if v == 1: return "Good"
        if v == 0: return "Fair"
        if v == -1: return "Poor"
        return "Bad"

    return {
        "80m": label(s + 1),
        "40m": label(s + 1),
        "20m": label(s),
        "15m": label(s - 1),
        "10m": label(s - 2),
    }


# ---------------- MAIN ----------------
def fetch():

    log("FETCH", "START")

    kpj = kp_json()
    kpt = kp_text()
    kp_final = max([v for v in [kpj, kpt] if v is not None], default=None)
    log("KP_FINAL", kp_final)

    xr = fetch_json("https://services.swpc.noaa.gov/json/goes/primary/xrays-1-day.json")
    pr = fetch_json("https://services.swpc.noaa.gov/json/goes/primary/integral-protons-1-day.json")
    er = fetch_json("https://services.swpc.noaa.gov/json/goes/primary/integral-electrons-1-day.json")
    # ---------------- OTHER ----------------
    sc = solar_cycle()
    log("GOT SOLAR CYCLE", sc)
    sw = solar_wind()
    mag = solar_wind_mag()
    
    # REGIONAL MUF CODE
    flux = sc.get("flux") if sc else None
    bz = mag.get("bz") if mag else None
    

    # ---------------- XRAY (STABLE + PHYSICS AWARE) ----------------
    x_series, x_disp = [], None

    if xr:
        valid = [
            x for x in xr
            if x.get("flux") and x.get("energy") == "0.1-0.8nm"
        ]

        recent = valid[-120:]  # ~2 hrs

        if recent:
            flux_vals = [x["flux"] for x in recent]

            baseline = statistics.median(flux_vals[-30:])
            peak = max(flux_vals[-10:])
            latest = flux_vals[-1]

            ratio = peak / baseline if baseline > 0 else 1

            #  smooth transition instead of binary switch
            weight = min(max((ratio - 1), 0), 1)
            effective = baseline * (1 - weight) + peak * weight

            # prevent lag during flare rise
            effective = max(effective, latest * 0.9)

            x_series = flux_vals[-60:]
            x_disp = xray_class(effective)

            log("XRAY_BASE", baseline)
            log("XRAY_PEAK", peak)
            log("XRAY_RATIO", ratio)
            log("XRAY_EFFECTIVE", effective)
            log("XRAY_CLASS", x_disp)
    muf = muf_estimate(
    sc.get("flux") if sc else None,
    kp_final,
    x_disp,
    mag["bz"] if mag else None
    )

    regional = muf_grid(
        flux,
        kp_final,
        x_disp,
        bz
    )

    # ---------------- PROTON ----------------
    p_series, p_raw, p_median, p_scaled = [], None, None, None

    if pr:
        valid = [x["flux"] for x in pr 
        if x.get("flux") is not None 
        and str(x.get("energy")) ==  ">=10 MeV"]
        # log("PROTON VALID:", valid)
        recent = valid[-60:]

        if recent:
            p_series = recent

            p_raw = recent[-1]                                #  TRUE RAW
            p_median = statistics.median(p_series[-10:])      #  SMOOTHED
            p_scaled = round(math.log10(p_median + 1), 3)     #  UI SCALE

            log("PROTON_RAW", p_raw)
            log("PROTON_MEDIAN", p_median)
            log("PROTON_SCALED", p_scaled)

    # ---------------- ELECTRON ----------------
    e_series, e_raw, e_median, e_scaled = [], None, None, None

    if er:
        valid = [x["flux"] for x in er 
        if x.get("flux") is not None 
        and str(x.get("energy")) == ">=2 MeV"]
        # log("ELECTRON VALID:", valid)
        recent = valid[-60:]

        if recent:
            e_series = recent

            e_raw = recent[-1]
            e_median = statistics.median(e_series[-10:])
            e_scaled = int(e_median * 5)   # your display scaling

            log("ELECTRON_RAW", e_raw)
            log("ELECTRON_MEDIAN", e_median)
            log("ELECTRON_SCALED", e_scaled)

    

    data = {
    "kp": {"json": kpj, "text": kpt, "final": kp_final},

    "geomagnetic": {
        "g": g_scale(kp_final),
        "r": r_scale(x_disp),
        "s": s_scale(p_median),
        "ap": ap_index(kp_final),
        "bt": mag["bt"] if mag else None,
        "bz": mag["bz"] if mag else None
    },

    "aurora": {
        "level": aurora_level(kp_final, mag["bz"] if mag else None),
        "kp": kp_final,
        "bz": mag["bz"] if mag else None
    },

    "storms": {
        "proton": proton_storm(p_median),
        "radiation": s_scale(p_median),
        "radio_blackout": r_scale(x_disp)
    },

    "xray": {
        "display": x_disp,
        "series": x_series,
        "thresholds": {
            "A": 1e-7,
            "B": 1e-6,
            "C": 1e-5,
            "M": 1e-4,
            "X": 1e-3
        }
    },

    "proton": {
        "raw": p_raw,
        "median": p_median,
        "scaled": p_scaled,
        "series": p_series,
        "thresholds": {
            "S1": 1,
            "S2": 10,
            "S3": 100
        }
    },

    "electron": {
        "raw": e_raw,
        "median": e_median,
        "scaled": e_scaled,
        "series": e_series,
        "thresholds": {
            "disturbance": 1000
        }
    },
    "hf": {
    "blackout": hf_blackout(
        x_disp,
        kp_final,
        p_median,
        mag["bz"] if mag else None
    )
},
    "propagation": {
    "muf": muf
    },

    "solar_cycle": sc,
    "solar_wind": sw,

    "wind_impact": solar_wind_impact(
    sw["speed"] if sw else None,
    mag["bz"] if mag else None,
    mag["bz_trend"] if mag else None
),

    "regional_propagation": {
        "grid": regional
    },

    "bands": band_conditions(kp_final, sc.get("flux") if sc else None, x_disp),

    "timestamp": time.time()
    }

    log("FETCH", "DONE")
    return data


def worker():
    while True:
        cache["data"] = fetch()
        time.sleep(900)


cache["data"] = fetch()
threading.Thread(target=worker, daemon=True).start()


@app.get("/solar")
def solar():
    return cache


@app.get("/")
def home():
    return FileResponse("app/static/index.html")


@app.get("/graphs")
def graphs():
    return FileResponse("app/static/graphs.html")

@app.get("/greyline")
def greyline():
    return {"points": greyline_points()}

@app.get("/propagation")
def propagation():
    return FileResponse("app/static/propagation.html")

@app.get("/muf_map")
def muf_map():
    return cache["data"].get("regional_propagation", {})

@app.get("/mufmap")
def mufmap():
    return FileResponse("app/static/muf_map.html")

@app.get("/path.html")
def path_page():
    return FileResponse("app/static/path.html")

@app.get("/path")
def path(tx_lat: float, tx_lon: float, rx_lat: float, rx_lon: float):

    result = path_prediction(
            (tx_lat, tx_lon),
            (rx_lat, rx_lon),
            muf_wrapper,
            greyline_wrapper,
            xray=cache["data"]["xray"]["display"],
            kp=cache["data"]["kp"]["final"] if isinstance(cache["data"]["kp"], dict) else cache["data"]["kp"]
    )
    return result

@app.get("/propagation/global")
def propagation_global():
    return global_propagation_grid()

@app.get("/global")
def global_page():
    return FileResponse("app/static/global.html")

def antenna_gain(tx_lat, tx_lon, rx_lat, rx_lon, azimuth, beamwidth):

    # bearing calculation
    dLon = math.radians(rx_lon - tx_lon)
    lat1 = math.radians(tx_lat)
    lat2 = math.radians(rx_lat)

    y = math.sin(dLon) * math.cos(lat2)
    x = math.cos(lat1)*math.sin(lat2) - math.sin(lat1)*math.cos(lat2)*math.cos(dLon)

    bearing = (math.degrees(math.atan2(y, x)) + 360) % 360

    # angular difference
    diff = abs(bearing - azimuth)
    diff = min(diff, 360 - diff)

    # gain pattern (cosine falloff)
    if diff > beamwidth:
        return 0.2  # sidelobe

    gain = math.cos(math.radians(diff / beamwidth * 90))

    return max(gain, 0.1)

def solar_zenith(lat, lon, timestamp=None):

    if timestamp:
        now = datetime.utcfromtimestamp(timestamp)
    else:
        now = datetime.utcnow()

    hour = now.hour + now.minute / 60

    subsolar_lon = (hour - 12) * 15

    day_of_year = now.timetuple().tm_yday
    decl = -23.44 * math.cos(math.radians((360/365) * (day_of_year + 10)))

    lat_rad = math.radians(lat)
    decl_rad = math.radians(decl)
    hour_angle = math.radians(lon - subsolar_lon)

    cos_zenith = (
        math.sin(lat_rad) * math.sin(decl_rad) +
        math.cos(lat_rad) * math.cos(decl_rad) * math.cos(hour_angle)
    )

    cos_zenith = max(min(cos_zenith, 1), -1)

    return math.degrees(math.acos(cos_zenith))

def ionosphere_layers(lat, lon, flux, timestamp=None):

    zenith = solar_zenith(lat, lon, timestamp)

    # daylight factor
    daylight = max(math.cos(math.radians(zenith)), 0)

    # ---- E LAYER ----
    # strong only in daytime
    foE = 1.5 + 0.02 * flux * daylight
    muf_E = foE * 2.5

    # ---- F1 LAYER ----
    foF1 = 2 + 0.03 * flux * daylight
    muf_F1 = foF1 * 3

    # ---- F2 LAYER ----
    foF2 = 3 + 0.04 * flux * daylight
    muf_F2 = foF2 * 3.5

    return {
        "E": muf_E,
        "F1": muf_F1,
        "F2": muf_F2,
        "zenith": zenith
    }

def fast_path_score(tx_lat, tx_lon, lat, lon, muf_func, xray, kp, timestamp=None):

    pts = []
    steps = 20

    total_d = distance_km(tx_lat, tx_lon, lat, lon)
    # ---------- FREQUENCY BASE ----------
    base_muf = muf_func(lat, lon)
    freq = min(max(base_muf * 0.6, 3), 30)

    h_reflect = reflection_height(freq, base_muf)

    # ---------- TAKEOFF ANGLE ----------
    takeoff_angle = max(3, 30 - (10 * 0.7))  # keep consistent with your model

    # ---------- CURVATURE FACTOR ----------
    # higher angle = flatter path, lower angle = higher arc
    curvature = (h_reflect / 400)

    for i in range(steps + 1):
        f = i / steps

        # linear interpolation
        p_lat = tx_lat + (lat - tx_lat) * f
        p_lon = tx_lon + (lon - tx_lon) * f

        # ---------- RAY BENDING (VERTICAL ARC) ----------
        # create an arc (parabolic)
        height_factor = 4 * f * (1 - f)   # parabola peak at midpoint
        bend = curvature * height_factor

        # simulate ionospheric reflection height effect
        # we perturb latitude slightly to simulate curved path
        # stronger bending for higher reflection
        p_lat += bend * (h_reflect / 200.0)

        pts.append((p_lat, p_lon, f))

    muf_vals = []
    loss_vals = []

    for (p_lat, p_lon, frac) in pts:

        # ---------- LAYER MODEL ----------
        layers = ionosphere_layers(
            p_lat,
            p_lon,
            cache["data"]["solar_cycle"]["flux"],
            timestamp
        )

        muf_E = layers["E"]
        muf_F1 = layers["F1"]
        muf_F2 = layers["F2"]
        zenith = layers["zenith"]

        d = total_d * frac

        # ---------- SMOOTH LAYER BLENDING ----------
        if d < 1500:
            muf = muf_E
        elif d < 3000:
            w = (d - 1500) / 1500
            muf = (1 - w) * muf_E + w * muf_F1
        elif d < 6000:
            w = (d - 3000) / 3000
            muf = (1 - w) * muf_F1 + w * muf_F2
        else:
            muf = muf_F2

        # ---------- ABSORPTION ----------
        loss = 0

        # D-layer (daytime)
        if zenith < 90:
            loss += (90 - zenith) * 0.25

        # E-layer absorption (short path)
        if d < 2000:
            loss += 5

        # X-ray
        if xray:
            if "X" in xray:
                loss += 30
            elif "M" in xray:
                loss += 15

        # geomagnetic
        if kp:
            loss += kp * 2

        # ---------- CURVATURE LOSS ----------
        # longer arc = more spreading loss
        loss += curvature * 10

        effective_muf = max(muf - (loss * 0.1), 1)

        muf_vals.append(effective_muf)
        loss_vals.append(loss)

    if not muf_vals:
        return 5

    min_muf = min(muf_vals)
    avg_muf = sum(muf_vals) / len(muf_vals)
    avg_loss = sum(loss_vals) / len(loss_vals)

    # ---------- FINAL SCORE ----------
    score = 50

    if avg_muf > 14:
        score += 20
    elif avg_muf > 10:
        score += 10
    else:
        score -= 20

    score -= avg_loss * 0.3

    return max(min(score, 95), 5)


@app.get("/propagation/tx")
def tx_propagation(tx_lat: float, tx_lon: float, 
            antenna_h: float = 10, msl: float = 10,
            azimuth: float = 0, beamwidth: float = 90, timestamp: float = None):

    data = cache["data"]

    flux = data.get("solar_cycle", {}).get("flux")

    kp_raw = data.get("kp")
    kp = kp_raw if not isinstance(kp_raw, dict) else kp_raw.get("final")

    xray = data.get("xray", {}).get("display")
    bz = data.get("geomagnetic", {}).get("bz")

    print(f"[TX PROP] tx=({tx_lat},{tx_lon}) flux={flux}")

    results = []

    LAT_STEP = 1
    LON_STEP = 1

    for lat in range(-90, 91, LAT_STEP):
        for lon in range(-180, 181, LON_STEP):

            d = distance_km(tx_lat, tx_lon, lat, lon)

            muf = muf_regional(lat, lon, flux, kp, xray, bz)

            if muf is None:
                continue

            # -------- BASE SCORE --------
            score = fast_path_score(
                tx_lat, tx_lon,
                lat, lon,
                muf_wrapper,
                xray,
                kp, timestamp
            )
            # ---------- NOISE FLOOR (NEW) ----------
            hour = (timestamp / 3600.0) % 24 if timestamp else datetime.utcnow().hour

            freq_base = min(max(muf * 0.6, 3), 30)
            noise = noise_floor(freq_base, lat, lon, hour)

            score = score - noise * 0.5
            # -------- SKIP ZONE --------
            if d < 500:
                score *= 0.2

            # -------- HOP SHAPING --------
            freq = min(max(muf * 0.6, 3), 30)
            h_reflect = reflection_height(freq, muf)

            takeoff_angle = max(3, 30 - (antenna_h * 0.7))

            hop_length = 2 * h_reflect * math.tan(math.radians(90 - takeoff_angle))
            hop_length *= 1.2

            hops = max(1, int(d / hop_length))
            ideal = hops * hop_length
            delta = abs(d - ideal)

            width = 1200
            hop_gain = max(0, 1 - (delta / width))

            score *= (0.6 + 0.4 * hop_gain)

            # -------- MUF --------
            if muf < 3:
                score -= 40
            elif muf < 7:
                score -= 20
            elif muf > 18:
                score += 10

            # -------- POLAR ABSORPTION (NEW) --------
            score -= polar_absorption(lat, kp)

            # -------- GEOMAG --------
            if kp:
                score -= kp * 3

            # -------- XRAY --------
            if xray:
                if "X" in xray:
                    score -= 25
                elif "M" in xray:
                    score -= 15

            # -------- ALTITUDE --------
            if msl > 3000:
                score += 10
            elif msl > 1000:
                score += 5

            # -------- GREYLINE --------
            try:
                zenith = solar_zenith(lat, lon, timestamp)
                delta_z = abs(zenith - 90)

                if delta_z < 10:
                    score += int(25 * (1 - delta_z / 10))
            except:
                pass

            # ---------- AURORAL SCATTER (NEW) ----------
            score *= auroral_scatter_gain(lat, kp, d)

            # ---------- CLAMP ----------
            score = max(min(score + 10, 95), 5)

            # ---------- ANTENNA ----------
            gain = antenna_gain(tx_lat, tx_lon, lat, lon, azimuth, beamwidth)

            # ---------- GROUND ----------
            cond = ground_conductivity(lat, lon)
            score *= (0.75 + (cond / 5.0))

            # ---------- BAND SNR ----------
            band_snr = {}

            BANDS = [
                ("10m", 28),
                ("15m", 21),
                ("20m", 14),
                ("40m", 7),
                ("80m", 3.5)
            ]

            # UTC hour for scatter models
            hour = (timestamp / 3600.0) % 24 if timestamp else datetime.utcnow().hour

            for name, freq in BANDS:

                b_score = score

                # ---------- REFLECTION HEIGHT ----------
                h_reflect_band = reflection_height(freq, muf)

                hop_len_band = 2 * h_reflect_band * math.tan(math.radians(90 - takeoff_angle))
                hop_len_band *= 1.2

                delta_band = abs(d - hop_len_band)
                width = 1500

                hop_factor = max(0.3, 1 - (delta_band / width))
                b_score *= hop_factor

                # ---------- MUF ----------
                ratio = muf / freq

                if ratio < 0.8:
                    b_score -= 40
                elif ratio < 1.0:
                    b_score -= 20
                elif ratio > 1.5:
                    b_score += 10

                # ---------- ABSORPTION ----------
                b_score -= (freq / 10) * 2

                # ---------- NOISE PER BAND (NEW) ----------
                noise_b = noise_floor(freq, lat, lon, hour)
                b_score -= noise_b * 0.6

                # ---------- SPORADIC E (NEW) ----------
                b_score *= sporadic_e_boost(freq, lat, hour, d)

                # ---------- TEP (NEW) ----------
                b_score *= tep_boost(tx_lat, lat, freq, hour, d)

                # ---------- AURORAL (FINE) ----------
                b_score *= auroral_scatter_gain(lat, kp, d)

                # ---------- CLAMP ----------
                b_score = max(min(b_score, 95), 5)

                band_snr[name] = round(b_score, 1)

            results.append({
                "lat": lat,
                "lon": lon,
                "snr": round(score, 1),
                "bands": band_snr,
                "dist": round(d, 1),
                "muf": muf
            })

    return results

def detect_events(data):

    result = {
        "flare": {},
        "geomagnetic": {},
        "impact": {}
    }

    # ---------- X-RAY ----------
    xray = data.get("xray", {}).get("display", "")

    if "X" in xray:
        result["flare"] = {
            "level": "X-Class",
            "severity": "Severe",
            "impact": "HF Blackout Likely"
        }
    elif "M" in xray:
        result["flare"] = {
            "level": "M-Class",
            "severity": "Strong",
            "impact": "HF Degradation"
        }
    elif "C" in xray:
        result["flare"] = {
            "level": "C-Class",
            "severity": "Moderate",
            "impact": "Minor Effects"
        }
    else:
        result["flare"] = {
            "level": "Quiet",
            "severity": "None",
            "impact": "Stable"
        }

    # ---------- KP ----------
    kp = data.get("kp")
    if isinstance(kp, dict):
        kp = kp.get("final")

    if kp is None:
        kp = 0

    if kp >= 7:
        storm = "Strong Storm"
    elif kp >= 5:
        storm = "Moderate Storm"
    elif kp >= 3:
        storm = "Unsettled"
    else:
        storm = "Quiet"

    result["geomagnetic"] = {
        "kp": kp,
        "condition": storm
    }

    # ---------- Bz ----------
    bz = data.get("geomagnetic", {}).get("bz")

    if bz is not None:
        if bz < -10:
            bz_state = "Southward (Storm Enhancing)"
        elif bz < 0:
            bz_state = "Weak Southward"
        else:
            bz_state = "Northward (Stable)"
    else:
        bz_state = "Unknown"

    result["geomagnetic"]["bz"] = bz_state

    # ---------- HF IMPACT ----------
    impact = []

    if "X" in xray:
        impact.append("HF blackout on sunlit side")
    elif "M" in xray:
        impact.append("HF signal degradation")

    if kp >= 5:
        impact.append("Polar path disruption")

    if bz and bz < -5:
        impact.append("Auroral absorption likely")

    result["impact"]["summary"] = impact if impact else ["Stable conditions"]

    return result

@app.get("/space-events")
def space_events():
    data = cache["data"]
    return detect_events(data)

@app.get("/space-weather")
def space_weather():
    return FileResponse("app/static/space_weather.html")

from app.timelapse_service import start_worker_once

# start worker when app starts
start_worker_once()

import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

@app.get("/timelapse")
def timelapse_page():
    return FileResponse("app/static/sdo_timelapse.html")

@app.get("/timelapse/{band}.gif")
def get_timelapse(band: str):
    if band not in ["0171", "0304", "0131"]:
        return {"error": "invalid band"}

    path = os.path.join(BASE_DIR, "data", "gifs", f"{band}.gif")

    if not os.path.exists(path):
        return {"error": f"gif not found: {path}"}

    return FileResponse(path, media_type="image/gif")

@app.get("/favicon.ico")
def favicon():
    return FileResponse("app/static/favicon.ico")