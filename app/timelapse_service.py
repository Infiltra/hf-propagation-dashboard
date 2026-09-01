import os
import time
import threading
from datetime import datetime
from typing import List

import requests
from PIL import Image

# -------- CONFIG --------
BANDS = ["0171", "0304", "0131"]
BASE_URL = "https://sdo.gsfc.nasa.gov/assets/img/latest/latest_512_{}.jpg"
FRAME_DIR = "data/frames"
GIF_DIR = "data/gifs"

FETCH_INTERVAL_SEC = 60        # pull a new frame every minute
MAX_FRAMES = 30                # ~30 minutes window
GIF_DURATION_MS = 150          # frame delay in GIF

os.makedirs(FRAME_DIR, exist_ok=True)
os.makedirs(GIF_DIR, exist_ok=True)
for b in BANDS:
    os.makedirs(os.path.join(FRAME_DIR, b), exist_ok=True)


def fetch_frame(band: str) -> str:
    """Download latest SDO image for a band and save with timestamp."""
    url = BASE_URL.format(band)
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(FRAME_DIR, band, f"{ts}.jpg")

    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        with open(path, "wb") as f:
            f.write(r.content)
        return path
    except Exception:
        return ""


def trim_frames(band: str):
    """Keep only the last MAX_FRAMES files."""
    band_dir = os.path.join(FRAME_DIR, band)
    files = sorted(os.listdir(band_dir))
    if len(files) > MAX_FRAMES:
        for f in files[: len(files) - MAX_FRAMES]:
            try:
                os.remove(os.path.join(band_dir, f))
            except Exception:
                pass


def build_gif(band: str):
    """Build GIF from current frames."""
    band_dir = os.path.join(FRAME_DIR, band)
    files = sorted(os.listdir(band_dir))
    if len(files) < 2:
        return

    images: List[Image.Image] = []
    for f in files:
        p = os.path.join(band_dir, f)
        try:
            img = Image.open(p).convert("RGB")
            images.append(img)
        except Exception:
            continue

    if len(images) < 2:
        return

    out = os.path.join(GIF_DIR, f"{band}.gif")
    try:
        images[0].save(
            out,
            save_all=True,
            append_images=images[1:],
            duration=GIF_DURATION_MS,
            loop=0,
            optimize=False,
        )
    except Exception:
        pass


def worker_loop():
    """Background loop: fetch → trim → build GIFs."""
    while True:
        for band in BANDS:
            p = fetch_frame(band)
            if p:
                trim_frames(band)
                build_gif(band)
        time.sleep(FETCH_INTERVAL_SEC)


_worker_started = False


def start_worker_once():
    global _worker_started
    if _worker_started:
        return
    t = threading.Thread(target=worker_loop, daemon=True)
    t.start()
    _worker_started = os.environ.get("WORKER_STARTED") == "1"