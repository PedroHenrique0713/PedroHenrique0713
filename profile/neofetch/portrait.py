"""Turn avatar.png into the colored ASCII portrait used by build.py.

Run locally whenever the photo changes (needs Pillow, NumPy and OpenCV); the daily
build only reads portrait.json, so Actions stays stdlib-only.
"""
import json
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

HERE = Path(__file__).parent
COLS, ROWS = 62, 53
CELL_W, CELL_H = 6, 12  # 10px monospace cell in the card
RAMP = " .,:;i1tfLCG08@"
PALETTE_SIZE = 24
# Box around the person for GrabCut, in avatar pixels (x, y, w, h).
SUBJECT = (40, 12, 250, 310)
# Crop kept for the portrait: top and bottom rows, width follows the cell aspect.
CROP_TOP, CROP_BOTTOM, CROP_CENTER_X = 14, 276, 161
BACKGROUND_INK = {"dark": 0.22, "light": 0.0}
CLAHE_CLIP = 0  # local contrast; it flattens the face into texture at this size
GAMMA = 0.9


def load():
    bgr = cv2.imread(str(HERE / "avatar.png"))
    mask = np.zeros(bgr.shape[:2], np.uint8)
    bg_model, fg_model = np.zeros((1, 65), np.float64), np.zeros((1, 65), np.float64)
    cv2.grabCut(bgr, mask, SUBJECT, bg_model, fg_model, 8, cv2.GC_INIT_WITH_RECT)
    subject = np.where((mask == cv2.GC_FGD) | (mask == cv2.GC_PR_FGD), 1.0, 0.0).astype(np.float32)
    subject = cv2.GaussianBlur(subject, (9, 9), 0)

    if CLAHE_CLIP:
        lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB)
        lab[..., 0] = cv2.createCLAHE(clipLimit=CLAHE_CLIP, tileGridSize=(4, 4)).apply(lab[..., 0])
        bgr = cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)

    crop_h = CROP_BOTTOM - CROP_TOP
    crop_w = round(crop_h * (COLS * CELL_W) / (ROWS * CELL_H))
    x0 = CROP_CENTER_X - crop_w // 2
    region = (slice(CROP_TOP, CROP_BOTTOM), slice(x0, x0 + crop_w))
    rgb = cv2.cvtColor(bgr[region], cv2.COLOR_BGR2RGB)
    rgb = cv2.resize(rgb, (COLS, ROWS), interpolation=cv2.INTER_AREA).astype(np.float32)
    subject = cv2.resize(subject[region], (COLS, ROWS), interpolation=cv2.INTER_AREA)
    luma = rgb @ np.array([0.299, 0.587, 0.114], np.float32) / 255
    lo, hi = np.percentile(luma[subject > 0.5], (2, 98))
    luma = np.clip((luma - lo) / (hi - lo), 0, 1)
    return rgb, luma, subject


def render(rgb, luma, subject, theme):
    ink = luma if theme == "dark" else 1 - luma
    ink = ink ** GAMMA * (subject + (1 - subject) * BACKGROUND_INK[theme])
    peak = np.maximum(rgb.max(axis=2, keepdims=True), 1)
    if theme == "dark":
        scale = (0.42 + 0.58 * luma[..., None]) * 255 / peak
    else:
        scale = (0.10 + 0.50 * luma[..., None]) * 255 / peak
    shown = np.clip(rgb * scale, 0, 255).astype(np.uint8)

    quant = Image.fromarray(shown).quantize(colors=PALETTE_SIZE, method=Image.Quantize.MEDIANCUT)
    flat = quant.getpalette()[: PALETTE_SIZE * 3]
    palette = ["#%02x%02x%02x" % tuple(flat[i : i + 3]) for i in range(0, len(flat), 3)]
    classes = np.array(quant)

    rows = []
    for y in range(ROWS):
        runs = []
        for x in range(COLS):
            ch = RAMP[int(round(ink[y, x] * (len(RAMP) - 1)))]
            cls = int(classes[y, x])
            # A space has no color, so it rides along with whatever run is open.
            if runs and (ch == " " or runs[-1][1] == cls):
                runs[-1][0] += ch
            elif runs and runs[-1][0].strip() == "":
                runs[-1] = [runs[-1][0] + ch, cls]
            else:
                runs.append([ch, cls])
        rows.append(runs)
    return {"palette": palette, "rows": rows}


if __name__ == "__main__":
    rgb, luma, subject = load()
    out = {"cols": COLS, "rows": ROWS, "cell": [CELL_W, CELL_H]}
    for theme in ("dark", "light"):
        out[theme] = render(rgb, luma, subject, theme)
    (HERE / "portrait.json").write_text(json.dumps(out, separators=(",", ":")))
    print("portrait.json:", COLS, "x", ROWS)
