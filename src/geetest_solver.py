"""
GeeTest V4 验证码自动求解器（滑块 + 语序点选 phrase）

  playwright_solve() — Playwright 浏览器内自动求解：
    · slide  — 背景差分定位缺口并拖动滑块
    · phrase — 按 geetest_ques_tips 提示图在背景上依次点选（优先购常用）

安装依赖（conda activate ibox 环境下）：
    pip install playwright Pillow numpy
    playwright install chromium
"""

from __future__ import annotations

import asyncio
import base64
import io
import itertools
import random
import time
import uuid
from typing import Optional

# ---- optional deps (fail gracefully) ----------------------------------------

try:
    from PIL import Image
    import numpy as np
    _PILLOW_OK = True
except ImportError:
    _PILLOW_OK = False

try:
    from playwright.async_api import async_playwright
    _PLAYWRIGHT_OK = True
except ImportError:
    _PLAYWRIGHT_OK = False

try:
    import ddddocr
    _DDDDOCR_OK = True
except ImportError:
    _DDDDOCR_OK = False

try:
    import cv2
    _CV2_OK = True
except ImportError:
    _CV2_OK = False

_ddddocr_det = None
_ddddocr_ocr = None

# ── Constants ─────────────────────────────────────────────────────────────────

CAPTCHA_ID_IBOX = "0d4b08eac1cbdcad36bbf607c5bf3e1b"
_MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 12; M2006J10C Build/SP1A.210812.016; wv) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/99.0.4844.88 "
    "Mobile Safari/537.36"
)

# The HTML page served to Playwright – mimics iBox's WebView GeeTest init
_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <style>
    body{{margin:0;background:#f5f5f5;display:flex;justify-content:center;
         align-items:center;min-height:100vh;}}
  </style>
</head>
<body>
  <div id="captcha-box"></div>
  <script src="https://static.geetest.com/v4/gt4.js"></script>
  <script>
    window._captchaResult = null;
    window._captchaError  = null;
    initGeetest4({{
      captchaId: '{captcha_id}',
      product:   'bind',
      language:  'zh-cn'
    }}, function(captchaObj) {{
      window._captchaObj = captchaObj;
      captchaObj.appendTo('#captcha-box');
      captchaObj.showCaptcha();
      captchaObj.onSuccess(function() {{
        window._captchaResult = captchaObj.getValidate();
        document.title = '__solved__';
      }});
      captchaObj.onError(function(err) {{
        window._captchaError = typeof err === 'object' ? JSON.stringify(err) : String(err);
        document.title = '__error__';
      }});
    }});
  </script>
</body>
</html>
"""


# ── Image gap detection ───────────────────────────────────────────────────────

def find_gap_by_diff(fullbg_bytes: bytes, bg_bytes: bytes) -> int:
    """
    Precisely locate the gap by diffing the full background image (no gap)
    against the challenge background image (with gap cut out).

    Returns the x-coordinate of the gap CENTER in native image pixels.
    This is the definitive method; no heuristics needed.
    """
    if not _PILLOW_OK:
        return 120

    full = Image.open(io.BytesIO(fullbg_bytes)).convert("RGB")
    gap  = Image.open(io.BytesIO(bg_bytes)).convert("RGB")

    # Normalise to same size (fullbg and bg are usually identical dimensions)
    if full.size != gap.size:
        full = full.resize(gap.size, Image.LANCZOS)

    full_arr = np.array(full, dtype=np.float32)
    gap_arr  = np.array(gap,  dtype=np.float32)

    diff = np.abs(full_arr - gap_arr).sum(axis=2)   # (H, W)
    col_diff = diff.mean(axis=0)                     # (W,)

    W = len(col_diff)
    # Ignore leftmost 10 % (the slider piece starting position adds noise)
    # and rightmost 5 %
    col_diff[:max(1, W // 10)] = 0
    col_diff[-max(1, W // 20):] = 0

    return int(np.argmax(col_diff))


def find_gap_x_from_screenshot(bg_bytes: bytes) -> int:
    """
    Locate the gap in a GeeTest slider background using shadow detection.

    The gap shadow is uniquely dark AND uniform (low std-dev) compared with
    the surrounding background photo.  Scoring on both signals together is
    far more reliable than a pure-brightness window search.
    """
    if not _PILLOW_OK:
        return 120

    img = Image.open(io.BytesIO(bg_bytes)).convert("L")  # grayscale
    arr = np.array(img, dtype=np.float32)
    H, W = arr.shape[:2]

    # Analyse a central vertical strip where the gap shadow is most visible
    y0, y1 = H // 6, H * 5 // 6
    strip = arr[y0:y1, :]
    col_mean = strip.mean(axis=0)   # average brightness per column
    col_std  = strip.std(axis=0)    # std-dev per column

    # Normalise to [0, 1]
    col_mean_n = col_mean / 255.0
    col_std_n  = col_std / (col_std.max() + 1e-6)

    # Shadow score: dark (low mean) AND uniform (low std)
    shadow_score = (1.0 - col_mean_n) * 0.6 + (1.0 - col_std_n) * 0.4

    # Smooth over a narrow window for sub-pixel precision
    shadow_smooth = np.convolve(shadow_score, np.ones(7) / 7, mode="same")

    # Ignore leftmost 25 % — the slider piece spans ~15 % from the left; the
    # extra margin prevents its dark shadow from being mistaken for the gap.
    # Ignore rightmost 8 % (edge noise).
    skip_l = max(1, W // 4)
    skip_r = max(1, W // 12)
    shadow_smooth[:skip_l] = 0
    shadow_smooth[-skip_r:] = 0

    return int(np.argmax(shadow_smooth))


def _extract_gt_images(obj: object, target: dict, _depth: int = 0) -> None:
    """Recursively search a parsed GeeTest /load JSON for bg / fullbg keys."""
    if _depth > 5 or not isinstance(obj, dict):
        return
    bg     = obj.get("bg")     or obj.get("bg_pic")
    fullbg = obj.get("fullbg") or obj.get("full_bg")
    static = obj.get("static_path") or obj.get("staticPath")
    if bg and fullbg:
        target["bg"]     = bg
        target["fullbg"] = fullbg
        if static:
            target["static_path"] = static
    imgs = obj.get("imgs")
    if imgs and "imgs" not in target:
        target["imgs"] = imgs
    captcha_type = obj.get("captcha_type") or obj.get("captchaType")
    if captcha_type and "captcha_type" not in target:
        target["captcha_type"] = captcha_type
    if static and "static_path" not in target:
        target["static_path"] = static
    if target.get("bg") and target.get("fullbg") and target.get("imgs"):
        return
    for v in obj.values():
        if isinstance(v, dict):
            _extract_gt_images(v, target, _depth + 1)
        elif isinstance(v, list):
            for item in v:
                _extract_gt_images(item, target, _depth + 1)
        if target.get("bg") and target.get("fullbg") and target.get("imgs"):
            return


def _parse_geetest_json(body: str) -> dict:
    import json as _json_mod
    import re as _re_mod

    text = body.strip()
    if not text:
        raise ValueError("empty geetest response")
    if text[0] not in "{[":
        match = _re_mod.match(r"^[^(]+\((.*)\)\s*$", text, _re_mod.S)
        if match:
            text = match.group(1)
    return _json_mod.loads(text)


def _gt_static_url(static_path: str | None, rel_path: str) -> str:
    if rel_path.startswith("http"):
        return rel_path
    rel = rel_path.lstrip("/")
    # Phrase/bg/icon image assets are served from static.geetest.com root.
    if rel.startswith("captcha_v4/") or "/phrase/" in rel or rel.endswith((".jpg", ".png", ".webp")):
        return "https://static.geetest.com/" + rel
    base = (static_path or "https://static.geetest.com/").rstrip("/")
    if not base.startswith("http"):
        base = "https://static.geetest.com" + base
    return base + "/" + rel


def _get_ddddocr_det():
    global _ddddocr_det
    if not _DDDDOCR_OK:
        return None
    if _ddddocr_det is None:
        _ddddocr_det = ddddocr.DdddOcr(det=True, show_ad=False)
    return _ddddocr_det


def _get_ddddocr_ocr():
    global _ddddocr_ocr
    if not _DDDDOCR_OK:
        return None
    if _ddddocr_ocr is None:
        _ddddocr_ocr = ddddocr.DdddOcr(show_ad=False)
    return _ddddocr_ocr


def _match_template_score(bg_gray: np.ndarray, tmpl_gray: np.ndarray) -> tuple[int, int, float]:
    """Return (x, y, score) of best template match in bg_gray."""
    th, tw = tmpl_gray.shape[:2]
    ih, iw = bg_gray.shape[:2]
    if ih < th or iw < tw:
        return 0, 0, -1.0

    if _CV2_OK:
        res = cv2.matchTemplate(bg_gray, tmpl_gray, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(res)
        return int(max_loc[0]), int(max_loc[1]), float(max_val)

    tmpl = tmpl_gray.astype(np.float64)
    tmpl -= tmpl.mean()
    tmpl_std = tmpl.std() + 1e-8
    tmpl /= tmpl_std

    best_score = -2.0
    best_x = best_y = 0
    for y in range(ih - th + 1):
        for x in range(iw - tw + 1):
            patch = bg_gray[y : y + th, x : x + tw].astype(np.float64)
            patch -= patch.mean()
            patch_std = patch.std() + 1e-8
            patch /= patch_std
            score = float((patch * tmpl).mean())
            if score > best_score:
                best_score = score
                best_x, best_y = x, y
    return best_x, best_y, best_score


def _find_phrase_clicks_by_hints(
    bg_bytes: bytes,
    hint_bytes_list: list[bytes],
    *,
    min_score: float = 0.45,
) -> list[tuple[int, int]] | None:
    """Match each hint glyph image onto the phrase background; return click centers."""
    if not _PILLOW_OK or not hint_bytes_list:
        return None

    bg_img = Image.open(io.BytesIO(bg_bytes)).convert("L")
    bg_arr = np.array(bg_img, dtype=np.uint8)
    clicks: list[tuple[int, int]] = []
    used_boxes: list[tuple[int, int, int, int]] = []

    for hint_bytes in hint_bytes_list:
        tmpl_img = Image.open(io.BytesIO(hint_bytes)).convert("L")
        tmpl_arr = np.array(tmpl_img, dtype=np.uint8)
        th, tw = tmpl_arr.shape[:2]
        if th < 4 or tw < 4:
            return None

        best_x, best_y, best_score = _match_template_score(bg_arr, tmpl_arr)
        if best_score < min_score:
            print(f"[geetest] phrase hint match score too low: {best_score:.3f}")
            return None

        for ux, uy, uw, uh in used_boxes:
            if abs(best_x - ux) < max(tw, uw) // 2 and abs(best_y - uy) < max(th, uh) // 2:
                print("[geetest] phrase hint matched same region twice")
                return None

        used_boxes.append((best_x, best_y, tw, th))
        clicks.append((best_x + tw // 2, best_y + th // 2))

    return clicks or None


def _find_phrase_clicks_by_ocr(
    bg_bytes: bytes,
    target_chars: list[str],
) -> list[tuple[int, int]] | None:
    """Fallback: detect chars on bg with ddddocr and click in target order."""
    det = _get_ddddocr_det()
    ocr = _get_ddddocr_ocr()
    if det is None or ocr is None or not target_chars:
        return None

    boxes = det.detection(bg_bytes)
    if not boxes:
        return None

    img = Image.open(io.BytesIO(bg_bytes))
    detections: list[tuple[str, int, int]] = []
    for box in boxes:
        x1, y1, x2, y2 = box
        crop = img.crop((x1, y1, x2, y2))
        buf = io.BytesIO()
        crop.save(buf, format="PNG")
        char = (ocr.classification(buf.getvalue()) or "").strip()
        if char:
            detections.append((char, (x1 + x2) // 2, (y1 + y2) // 2))

    clicks: list[tuple[int, int]] = []
    used: set[tuple[int, int]] = set()
    for target in target_chars:
        matched = False
        for char, cx, cy in detections:
            if char != target or (cx, cy) in used:
                continue
            clicks.append((cx, cy))
            used.add((cx, cy))
            matched = True
            break
        if not matched:
            return None
    return clicks


def _parse_expected_phrase_count(title_text: str, *, default: int = 3) -> int:
    import re as _re_mod

    for token in _re_mod.findall(r"\d+", title_text or ""):
        value = int(token)
        if 2 <= value <= 6:
            return value
    return default


def _mser_char_blobs(
    gray: np.ndarray,
    *,
    min_area: int = 400,
    max_area: int = 5000,
) -> list[tuple[int, int, int, int, int]]:
    """Return merged char blobs as (cx, cy, area, x1, y1)."""
    if not _CV2_OK:
        return []
    mser = cv2.MSER_create()
    mser.setDelta(5)
    mser.setMinArea(80)
    mser.setMaxArea(8000)
    _, bboxes = mser.detectRegions(gray)
    merged: list[tuple[int, int, int, int, int]] = []
    for bbox in bboxes:
        x, y, bw, bh = bbox
        area = int(bw) * int(bh)
        if area < min_area or area > max_area or bw < 14 or bh < 14:
            continue
        cx, cy = int(x + bw // 2), int(y + bh // 2)
        if any(abs(cx - mx) < 35 and abs(cy - my) < 35 for mx, my, *_ in merged):
            continue
        merged.append((cx, cy, area, int(x), int(y)))
    return merged


def _find_phrase_clicks_by_hint_columns(
    bg_bytes: bytes,
    *,
    expected_count: int = 3,
    min_score: float = 0.28,
) -> list[tuple[int, int]] | None:
    """Match evenly-spaced hint glyphs in the top strip onto the phrase body."""
    if not _CV2_OK or not _PILLOW_OK:
        return None

    img = cv2.imdecode(np.frombuffer(bg_bytes, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return None
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape[:2]
    split_y = max(int(h * 0.22), 28)
    hint = gray[:split_y, :]
    body = gray[split_y:, :].copy()

    col_w = w // expected_count
    if col_w < 20:
        return None

    clicks: list[tuple[int, int]] = []
    used: set[tuple[int, int]] = set()
    for i in range(expected_count):
        col = hint[:, i * col_w : (i + 1) * col_w]
        mask = col < 240
        if mask.sum() < 30:
            return None
        ys, xs = np.where(mask)
        x1, x2 = int(xs.min()), int(xs.max())
        y1, y2 = int(ys.min()), int(ys.max())
        tmpl = col[y1 : y2 + 1, x1 : x2 + 1]
        th, tw = tmpl.shape[:2]
        if th < 6 or tw < 6:
            return None

        best_val = -1.0
        best_loc = (0, 0)
        for scale in (0.85, 1.0, 1.15):
            if scale != 1.0:
                scaled = cv2.resize(
                    tmpl,
                    (max(6, int(tw * scale)), max(6, int(th * scale))),
                    interpolation=cv2.INTER_LINEAR,
                )
            else:
                scaled = tmpl
            sth, stw = scaled.shape[:2]
            if body.shape[0] < sth or body.shape[1] < stw:
                continue
            res = cv2.matchTemplate(body, scaled, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(res)
            if max_val > best_val:
                best_val = float(max_val)
                best_loc = (max_loc[0], max_loc[1], stw, sth)

        if best_val < min_score:
            return None
        cx = int(best_loc[0] + best_loc[2] // 2)
        cy = int(best_loc[1] + best_loc[3] // 2 + split_y)
        if (cx, cy) in used:
            return None
        clicks.append((cx, cy))
        used.add((cx, cy))
        # Mask matched region before next hint.
        bx, by, bw, bh = best_loc[0], best_loc[1], best_loc[2], best_loc[3]
        body[by : min(body.shape[0], by + bh + 4), bx : min(body.shape[1], bx + bw + 4)] = 255

    return clicks if len(clicks) == expected_count else None


def _find_phrase_clicks_by_hint_strip(
    bg_bytes: bytes,
    *,
    expected_count: int = 3,
) -> list[tuple[int, int]] | None:
    """Match hint glyphs in the top strip onto the phrase body."""
    if not _CV2_OK or not _PILLOW_OK:
        return None

    img = cv2.imdecode(np.frombuffer(bg_bytes, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return None
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    h = gray.shape[0]
    split_y = max(int(h * 0.22), 28)

    hint_blobs = _mser_char_blobs(gray[:split_y, :], min_area=80, max_area=3500)
    if len(hint_blobs) < expected_count:
        return None
    if len(_mser_char_blobs(gray[split_y:, :])) < expected_count:
        return None

    hint_blobs.sort(key=lambda b: b[0])
    hints = hint_blobs[:expected_count]
    body_gray = gray[split_y:, :]
    clicks: list[tuple[int, int]] = []
    used: set[tuple[int, int]] = set()

    for hx, hy, _, x1, y1 in hints:
        pad = 4
        tmpl = gray[max(0, y1 - pad) : hy + pad, max(0, x1 - pad) : hx + pad]
        if tmpl.size == 0:
            return None
        th, tw = tmpl.shape[:2]
        if th < 8 or tw < 8:
            return None
        res = cv2.matchTemplate(body_gray, tmpl, cv2.TM_CCOEFF_NORMED)
        while True:
            _, max_val, _, max_loc = cv2.minMaxLoc(res)
            if max_val < 0.30:
                return None
            cx = int(max_loc[0] + tw // 2)
            cy = int(max_loc[1] + th // 2 + split_y)
            if (cx, cy) not in used:
                clicks.append((cx, cy))
                used.add((cx, cy))
                break
            x0 = max(0, max_loc[0] - tw)
            y0 = max(0, max_loc[1] - th)
            x1m = min(res.shape[1] - 1, max_loc[0] + tw)
            y1m = min(res.shape[0] - 1, max_loc[1] + th)
            res[y0:y1m, x0:x1m] = 0

    return clicks if len(clicks) == expected_count else None


def _find_phrase_clicks_by_reading_order(
    bg_bytes: bytes,
    *,
    expected_count: int = 3,
) -> list[tuple[int, int]] | None:
    """Detect glyph blobs on phrase bg and click in reading order (top→bottom, left→right)."""
    if not _CV2_OK or not _PILLOW_OK:
        return None

    img = cv2.imdecode(np.frombuffer(bg_bytes, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return None
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape[:2]

    mser = cv2.MSER_create()
    mser.setDelta(5)
    mser.setMinArea(80)
    mser.setMaxArea(8000)
    _, bboxes = mser.detectRegions(gray)

    centers: list[tuple[int, int, int]] = []
    for bbox in bboxes:
        x, y, bw, bh = bbox
        area = int(bw) * int(bh)
        if area < 400 or area > 5000:
            continue
        if bw < 14 or bh < 14:
            continue
        centers.append((int(x + bw // 2), int(y + bh // 2), area))

    merged: list[tuple[int, int, int]] = []
    for cx, cy, area in sorted(centers, key=lambda item: -item[2]):
        if any(abs(cx - mx) < 35 and abs(cy - my) < 35 for mx, my, _ in merged):
            continue
        merged.append((cx, cy, area))

    if len(merged) < expected_count:
        return None

    merged.sort(key=lambda item: -item[2])
    selected = merged[:expected_count]
    row_tol = max(h // 4, 30)
    selected.sort(key=lambda item: (item[1] // row_tol, item[0]))
    return [(cx, cy) for cx, cy, _ in selected]


def _parse_nine_select_count(title_text: str, *, default: int = 3) -> int:
    import re as _re_mod

    for token in _re_mod.findall(r"\d+", title_text or ""):
        value = int(token)
        if 2 <= value <= 6:
            return value
    return default


def _split_nine_grid_image(grid_bytes: bytes) -> list[bytes]:
    """Split a 3×3 GeeTest nine-grid JPG into nine tile PNG byte blobs."""
    if not _PILLOW_OK:
        return []
    img = Image.open(io.BytesIO(grid_bytes)).convert("RGB")
    w, h = img.size
    cw, ch = w // 3, h // 3
    tiles: list[bytes] = []
    for row in range(3):
        for col in range(3):
            crop = img.crop((col * cw, row * ch, (col + 1) * cw, (row + 1) * ch))
            buf = io.BytesIO()
            crop.save(buf, format="PNG")
            tiles.append(buf.getvalue())
    return tiles


def _center_crop_bgr(image: np.ndarray, margin: float = 0.12) -> np.ndarray:
    h, w = image.shape[:2]
    dx, dy = int(w * margin), int(h * margin)
    if dx * 2 >= w or dy * 2 >= h:
        return image
    return image[dy : h - dy, dx : w - dx]


def _compare_tile_to_prompt(prompt_bytes: bytes, tile_bytes: bytes) -> float:
    """Score how well a nine-grid tile matches the prompt photo (0..1)."""
    if not tile_bytes or not prompt_bytes or not _PILLOW_OK:
        return 0.0

    if _CV2_OK:
        prompt = cv2.imdecode(np.frombuffer(prompt_bytes, np.uint8), cv2.IMREAD_COLOR)
        tile = cv2.imdecode(np.frombuffer(tile_bytes, np.uint8), cv2.IMREAD_COLOR)
        if prompt is None or tile is None:
            return 0.0
        prompt = _center_crop_bgr(prompt)
        tile = _center_crop_bgr(tile)
        prompt = cv2.resize(prompt, (128, 128))
        tile = cv2.resize(tile, (128, 128))

        def _hist_correl(a_bgr: np.ndarray, b_bgr: np.ndarray, space: int) -> float:
            if space == cv2.COLOR_BGR2HSV:
                channels, ranges = [0, 1, 2], [0, 180, 0, 256, 0, 256]
                bins = [8, 8, 8]
            else:
                channels, ranges = [0, 1, 2], [0, 256, 0, 256, 0, 256]
                bins = [8, 8, 8]
            a_conv = cv2.cvtColor(a_bgr, space)
            b_conv = cv2.cvtColor(b_bgr, space)
            hist_a = cv2.calcHist([a_conv], channels, None, bins, ranges)
            hist_b = cv2.calcHist([b_conv], channels, None, bins, ranges)
            cv2.normalize(hist_a, hist_a, 0, 1, cv2.NORM_MINMAX)
            cv2.normalize(hist_b, hist_b, 0, 1, cv2.NORM_MINMAX)
            return float(cv2.compareHist(hist_a, hist_b, cv2.HISTCMP_CORREL))

        hist_hsv = _hist_correl(prompt, tile, cv2.COLOR_BGR2HSV)
        hist_lab = _hist_correl(prompt, tile, cv2.COLOR_BGR2LAB)

        gray_p = cv2.cvtColor(prompt, cv2.COLOR_BGR2GRAY)
        gray_t = cv2.cvtColor(tile, cv2.COLOR_BGR2GRAY)
        res = cv2.matchTemplate(gray_t, gray_p, cv2.TM_CCOEFF_NORMED)
        tmpl_score = float(res.max())

        akaze_score = 0.0
        try:
            akaze = cv2.AKAZE_create()
            kp1, des1 = akaze.detectAndCompute(gray_p, None)
            kp2, des2 = akaze.detectAndCompute(gray_t, None)
            if des1 is not None and des2 is not None and len(kp1) >= 4 and len(kp2) >= 4:
                bf = cv2.BFMatcher(cv2.NORM_HAMMING)
                pairs = bf.knnMatch(des1, des2, k=2)
                good = 0
                for pair in pairs:
                    if len(pair) < 2:
                        continue
                    m, n = pair
                    if m.distance < 0.75 * n.distance:
                        good += 1
                akaze_score = min(1.0, good / max(min(len(kp1), len(kp2)), 1))
        except Exception:
            pass

        return (
            max(0.0, hist_hsv) * 0.30
            + max(0.0, hist_lab) * 0.20
            + max(0.0, tmpl_score) * 0.20
            + akaze_score * 0.30
        )

    prompt = Image.open(io.BytesIO(prompt_bytes)).convert("RGB")
    tile = Image.open(io.BytesIO(tile_bytes)).convert("RGB")
    size = (72, 72)
    p_arr = np.array(prompt.resize(size, Image.LANCZOS), dtype=np.float32).reshape(-1)
    t_arr = np.array(tile.resize(size, Image.LANCZOS), dtype=np.float32).reshape(-1)
    p_arr -= p_arr.mean()
    t_arr -= t_arr.mean()
    return float(np.dot(p_arr, t_arr) / ((np.linalg.norm(p_arr) * np.linalg.norm(t_arr)) + 1e-8))


def _rank_nine_tiles(
    prompt_bytes: bytes,
    tile_bytes_list: list[bytes],
) -> list[tuple[float, int]]:
    scored = [
        (_compare_tile_to_prompt(prompt_bytes, tile_bytes), idx)
        for idx, tile_bytes in enumerate(tile_bytes_list)
        if tile_bytes
    ]
    scored.sort(key=lambda item: item[0], reverse=True)
    return scored


def _find_nine_matching_indices(
    prompt_bytes: bytes,
    tile_bytes_list: list[bytes],
    *,
    select_count: int = 3,
) -> list[int]:
    ranked = _rank_nine_tiles(prompt_bytes, tile_bytes_list)
    if len(ranked) < select_count:
        return []
    return [idx for _, idx in ranked[:select_count]]


def _nine_pick_combos(
    prompt_bytes: bytes,
    tile_bytes_list: list[bytes],
    *,
    select_count: int = 3,
    top_k: int = 5,
    max_combos: int = 12,
) -> list[list[int]]:
    ranked = _rank_nine_tiles(prompt_bytes, tile_bytes_list)
    if len(ranked) < select_count:
        return []
    candidates = ranked[: min(top_k, len(ranked))]
    idx_pool = [idx for _, idx in candidates]
    combos: list[tuple[float, list[int]]] = []
    for combo in itertools.combinations(idx_pool, select_count):
        score = sum(
            next(sc for sc, ix in ranked if ix == i)
            for i in combo
        )
        combos.append((score, list(combo)))
    combos.sort(key=lambda item: item[0], reverse=True)
    return [c for _, c in combos[:max_combos]]


# ── Human-like trajectory ────────────────────────────────────────────────────

def _make_trajectory(start_x: float, end_x: float, y: float, steps: int = 35):
    """
    Return a list of (x, y) waypoints that simulate a human slider drag:
    slow start → acceleration → slight overshoot → correction.
    """
    dist = end_x - start_x
    points: list[tuple[float, float]] = []
    for i in range(steps):
        t = i / (steps - 1)
        # Smooth ease-in-out (cubic)
        ease = t * t * (3.0 - 2.0 * t)
        # Very small overshoot at 90 % of the way
        if t > 0.9:
            ease += (t - 0.9) * 0.5 * random.uniform(-1, 1)
        x = start_x + dist * ease + random.uniform(-0.8, 0.8)
        jitter_y = y + random.uniform(-2.5, 2.5) if i > 0 else y
        points.append((x, jitter_y))
    return points


async def _solve_phrase_challenge(
    *,
    page,
    gt_data: dict,
    remaining_ms: int,
    max_attempts: int,
) -> dict | None:
    """Solve GeeTest phrase / click-order captcha (优先购常用)."""
    bg_sel = "[class*='geetest_bg']"
    await page.wait_for_function(
        """() => {
            var bg = document.querySelector('[class*="geetest_bg"]');
            if (!bg) return false;
            if (bg.classList.contains('geetest_freeze_action')) return false;
            if (bg.classList.contains('geetest_freeze_wait')) return false;
            var bi = window.getComputedStyle(bg).backgroundImage;
            if (bi && bi !== 'none' && bi.indexOf('url') !== -1) return true;
            var img = bg.querySelector('img');
            return !!(img && img.complete && img.naturalWidth > 0);
        }""",
        timeout=min(25_000, remaining_ms),
    )
    bg_el = page.locator(bg_sel).first
    bg_box = await bg_el.bounding_box()
    if not bg_box or bg_box["width"] == 0:
        raise RuntimeError("Could not locate geetest_bg element for phrase challenge")

    async def _fetch_bg_bytes() -> bytes:
        if gt_data.get("imgs"):
            url = _gt_static_url(None, str(gt_data["imgs"]))
            resp = await page.request.get(url)
            if resp.status >= 400:
                raise RuntimeError(f"Failed to fetch phrase image: HTTP {resp.status}")
            return await resp.body()
        screenshot = await page.screenshot(clip={
            "x": bg_box["x"], "y": bg_box["y"],
            "width": bg_box["width"], "height": bg_box["height"],
        })
        return screenshot

    async def _fetch_hint_bytes_list() -> list[bytes]:
        hint_urls = await page.evaluate("""() => {
            var tips = document.querySelector('[class*="geetest_ques_tips"]');
            if (!tips) return [];
            return Array.from(tips.querySelectorAll('img'))
                .map(function(i) { return i.src; })
                .filter(Boolean);
        }""")
        hints: list[bytes] = []
        for url in hint_urls or []:
            resp = await page.request.get(url)
            hints.append(await resp.body())
        return hints

    async def _fetch_target_chars() -> list[str]:
        meta = await page.evaluate("""() => {
            var tips = document.querySelector('[class*="geetest_ques_tips"]');
            if (!tips) return {chars: [], text: ''};
            var imgs = tips.querySelectorAll('img');
            if (imgs.length) return {chars: [], text: (tips.innerText || '').trim()};
            var text = (tips.innerText || tips.textContent || '').trim();
            var chars = [];
            var re = /[\u4e00-\u9fff]/g, m;
            while ((m = re.exec(text)) !== null) chars.push(m[0]);
            return {chars: chars, text: text};
        }""")
        return list(meta.get("chars") or [])

    async def _fetch_title_text() -> str:
        return await page.evaluate(
            """() => {
            var el = document.querySelector('[class*="geetest_text_tips"]');
            return el ? (el.innerText || el.textContent || '').trim() : '';
        }"""
        )

    async def _click_phrase_points(clicks_native: list[tuple[int, int]], img_w: int, img_h: int) -> None:
        box = await bg_el.bounding_box()
        if not box:
            raise RuntimeError("geetest_bg bounding box lost")
        for cx, cy in clicks_native:
            rel_x = (cx / img_w) * box["width"]
            rel_y = (cy / img_h) * box["height"]
            await bg_el.click(
                position={
                    "x": rel_x + random.uniform(-1.0, 1.0),
                    "y": rel_y + random.uniform(-1.0, 1.0),
                },
                timeout=5_000,
            )
            await asyncio.sleep(random.uniform(0.35, 0.75))

    async def _wait_phrase_result() -> str | None:
        try:
            await page.wait_for_function(
                "() => document.title === '__solved__' || document.title === '__error__'",
                timeout=6_000,
            )
        except Exception:
            pass
        title = await page.title()
        if title in {"__solved__", "__error__"}:
            return title
        # Wrong clicks often trigger shake without changing title immediately.
        await asyncio.sleep(0.8)
        return await page.title()

    async def _phrase_expected_counts(title_text: str, hint_bytes_list: list[bytes]) -> list[int]:
        counts: list[int] = []
        if hint_bytes_list:
            n = len(hint_bytes_list)
            if 2 <= n <= 6:
                counts.append(n)
        parsed = _parse_expected_phrase_count(title_text)
        if parsed not in counts:
            counts.append(parsed)
        if 3 not in counts:
            counts.append(3)
        return counts

    for attempt in range(1, max_attempts + 1):
        bg_bytes = await _fetch_bg_bytes()
        img = Image.open(io.BytesIO(bg_bytes))
        img_w, img_h = img.size

        hint_bytes_list = await _fetch_hint_bytes_list()
        title_text = await _fetch_title_text()
        expected_counts = await _phrase_expected_counts(title_text, hint_bytes_list)
        clicks = _find_phrase_clicks_by_hints(bg_bytes, hint_bytes_list)
        method = "hint-template"
        if clicks is None:
            for expected in expected_counts:
                clicks = _find_phrase_clicks_by_hint_columns(
                    bg_bytes,
                    expected_count=expected,
                )
                if clicks:
                    method = f"hint-columns({expected})"
                    break
        if clicks is None:
            target_chars = await _fetch_target_chars()
            clicks = _find_phrase_clicks_by_ocr(bg_bytes, target_chars)
            method = "ocr"
        if clicks is None:
            for expected in expected_counts:
                clicks = _find_phrase_clicks_by_hint_strip(
                    bg_bytes,
                    expected_count=expected,
                )
                if clicks:
                    method = f"hint-strip({expected})"
                    break
        if clicks is None:
            for expected in expected_counts:
                clicks = _find_phrase_clicks_by_reading_order(
                    bg_bytes,
                    expected_count=expected,
                )
                if clicks:
                    method = f"reading-order({expected})"
                    break

        click_orders: list[list[tuple[int, int]]] = []
        if clicks:
            if method.startswith("reading-order") and len(clicks) <= 4:
                click_orders = [list(p) for p in itertools.permutations(clicks, len(clicks))]
            else:
                click_orders = [clicks]

        if not click_orders:
            raise RuntimeError(
                "Could not resolve phrase click targets "
                f"(hints={len(hint_bytes_list)}, ddddocr={_DDDDOCR_OK}, cv2={_CV2_OK})"
            )

        for order_idx, click_order in enumerate(click_orders[:12], start=1):
            print(
                f"[geetest] phrase attempt {attempt}/{max_attempts} "
                f"({method}) order {order_idx}/{min(len(click_orders), 12)}: "
                f"{len(click_order)} clicks on {img_w}x{img_h}"
            )
            await _click_phrase_points(click_order, img_w, img_h)
            title = await _wait_phrase_result()
            if title == "__solved__":
                result = await page.evaluate("() => window._captchaResult")
                if result and result.get("lot_number"):
                    return result
                raise RuntimeError(f"Phrase solved but result empty: {result}")
            if title == "__error__":
                err = await page.evaluate("() => window._captchaError || 'unknown'")
                raise RuntimeError(f"GeeTest phrase error: {err}")
            if order_idx < min(len(click_orders), 12):
                await asyncio.sleep(0.6)

        if attempt < max_attempts:
            print(f"[geetest] Phrase attempt {attempt} failed, refreshing challenge…")
            gt_data.clear()
            refresh_sel = "[class*='geetest_refresh']"
            verify_btn_sel = "[class*='geetest_btn']"
            try:
                await page.locator(refresh_sel).first.click(timeout=3_000)
            except Exception:
                try:
                    await page.locator(verify_btn_sel).first.click(timeout=3_000)
                except Exception:
                    pass
            await asyncio.sleep(1.5)
            await page.wait_for_function(
                """() => {
                    var root = document.querySelector('[class*="geetest_captcha"]');
                    if (!root) return false;
                    var click = document.querySelector('[class*="geetest_subitem"][class*="geetest_click"]');
                    if (click) {
                        var bg = document.querySelector('[class*="geetest_bg"]');
                        if (bg) {
                            var bi = window.getComputedStyle(bg).backgroundImage;
                            if (bi && bi !== 'none') return true;
                        }
                    }
                    return !root.classList.contains('geetest_freeze_wait')
                        || root.classList.contains('geetest_nextReady');
                }""",
                timeout=10_000,
            )
            bg_box = await bg_el.bounding_box() or bg_box

    return None


async def _solve_nine_challenge(
    *,
    page,
    gt_data: dict,
    remaining_ms: int,
    max_attempts: int,
) -> dict | None:
    """Solve GeeTest nine-grid captcha: pick N tiles matching the prompt icon."""
    await page.wait_for_function(
        """() => {
            var prompt = document.querySelector('[class*="geetest_ques_tips"] img');
            if (!prompt || !prompt.src) return false;
            var tiles = document.querySelectorAll(
                '[class*="geetest_nine"] [class*="geetest_item_img"],'
                + '[class*="geetest_nine"] [class*="geetest_imgs"]'
            );
            if (tiles.length < 9) return false;
            var loaded = 0;
            for (var i = 0; i < tiles.length; i++) {
                var el = tiles[i];
                var bi = window.getComputedStyle(el).backgroundImage;
                if (bi && bi !== 'none' && bi.indexOf('url') !== -1) {
                    loaded++;
                    continue;
                }
                if (el.tagName === 'IMG' && el.complete && el.naturalWidth > 0) loaded++;
            }
            return loaded >= 9;
        }""",
        timeout=min(25_000, remaining_ms),
    )

    async def _capture_tile_bytes_list() -> list[bytes]:
        loc = page.locator(
            "[class*='geetest_nine'] [class*='geetest_item_img'], "
            "[class*='geetest_nine'] [class*='geetest_imgs']"
        )
        count = min(await loc.count(), 9)
        tiles: list[bytes] = []
        for i in range(count):
            el = loc.nth(i)
            box = await el.bounding_box()
            if not box or box["width"] == 0:
                tiles.append(b"")
                continue
            shot = await page.screenshot(
                clip={
                    "x": box["x"],
                    "y": box["y"],
                    "width": box["width"],
                    "height": box["height"],
                }
            )
            tiles.append(shot)
        return tiles

    async def _grid_snapshot() -> dict:
        return await page.evaluate(
            """() => {
            var promptEl = document.querySelector('[class*="geetest_ques_tips"] img');
            var prompt = promptEl ? promptEl.src : null;
            var titleEl = document.querySelector('[class*="geetest_text_tips"]');
            var title = titleEl ? (titleEl.innerText || titleEl.textContent || '').trim() : '';
            var tileCount = document.querySelectorAll(
                '[class*="geetest_nine"] [class*="geetest_item_img"],'
                + '[class*="geetest_nine"] [class*="geetest_imgs"]'
            ).length;
            return {prompt: prompt, title: title, tileCount: tileCount};
        }"""
        )

    async def _click_nine_cell(target_idx: int) -> None:
        await page.evaluate(
            """(targetIdx) => {
            var items = Array.from(
                document.querySelectorAll('[class*="geetest_nine"] [class*="geetest_item"]')
            ).filter(function(el) {
                return /geetest_[0-8]\\b/.test(el.className || '');
            }).sort(function(a, b) {
                var ma = (a.className || '').match(/geetest_([0-8])/);
                var mb = (b.className || '').match(/geetest_([0-8])/);
                return parseInt(ma[1], 10) - parseInt(mb[1], 10);
            });
            var el = items[targetIdx];
            if (!el) return;
            var wrap = el.querySelector('[class*="geetest_item_wrap"]') || el;
            wrap.click();
        }""",
            target_idx,
        )

    async def _wait_nine_result() -> str | None:
        try:
            await page.wait_for_function(
                "() => document.title === '__solved__' || document.title === '__error__'",
                timeout=8_000,
            )
        except Exception:
            pass
        title = await page.title()
        if title in {"__solved__", "__error__"}:
            return title
        await asyncio.sleep(0.8)
        return await page.title()

    async def _fetch_tile_bytes_list() -> list[bytes]:
        if gt_data.get("imgs"):
            url = _gt_static_url(None, str(gt_data["imgs"]))
            resp = await page.request.get(url)
            if resp.status < 400:
                split_tiles = _split_nine_grid_image(await resp.body())
                if len(split_tiles) == 9:
                    return split_tiles
        await asyncio.sleep(0.6)
        return await _capture_tile_bytes_list()

    async def _refresh_nine_grid() -> None:
        gt_data.clear()
        try:
            await page.locator("[class*='geetest_refresh']").first.click(timeout=3_000)
        except Exception:
            pass
        await asyncio.sleep(1.5)
        await page.wait_for_function(
            """() => {
                var tiles = document.querySelectorAll(
                    '[class*="geetest_nine"] [class*="geetest_item_img"],'
                    + '[class*="geetest_nine"] [class*="geetest_imgs"]'
                );
                return tiles.length >= 9;
            }""",
            timeout=10_000,
        )

    async def _submit_nine_selection() -> None:
        await page.evaluate(
            """() => {
            var btn = document.querySelector(
                '[class*="geetest_submit"], [class*="geetest_commit"]'
            );
            if (btn) btn.click();
        }"""
        )

    for attempt in range(1, max_attempts + 1):
        snap = await _grid_snapshot()
        prompt_url = snap.get("prompt")
        tile_count = int(snap.get("tileCount") or 0)
        if not prompt_url or tile_count < 9:
            raise RuntimeError(
                f"Nine-grid not ready (prompt={bool(prompt_url)} tiles={tile_count})"
            )

        select_count = _parse_nine_select_count(str(snap.get("title") or ""))
        prompt_bytes = await (await page.request.get(prompt_url)).body()
        tile_bytes_list = await _fetch_tile_bytes_list()
        if len([t for t in tile_bytes_list if t]) < 9:
            raise RuntimeError(f"Nine-grid tile images incomplete ({len(tile_bytes_list)})")

        ranked = _rank_nine_tiles(prompt_bytes, tile_bytes_list)
        pick_combos = _nine_pick_combos(
            prompt_bytes,
            tile_bytes_list,
            select_count=select_count,
            top_k=6,
            max_combos=1,
        )
        if not pick_combos:
            pick_combos = [_find_nine_matching_indices(
                prompt_bytes, tile_bytes_list, select_count=select_count
            )]

        for combo_idx, pick_indices in enumerate(pick_combos, start=1):
            if not pick_indices:
                continue
            scores = [
                next((sc for sc, ix in ranked if ix == i), 0.0)
                for i in pick_indices
            ]
            print(
                f"[geetest] nine attempt {attempt}/{max_attempts} "
                f"combo {combo_idx}/{len(pick_combos)}: "
                f"pick {pick_indices} scores={[f'{s:.3f}' for s in scores]}"
            )

            for idx in pick_indices:
                if idx >= len(tile_bytes_list):
                    continue
                await _click_nine_cell(idx)
                await asyncio.sleep(random.uniform(0.35, 0.75))

            await _submit_nine_selection()
            await asyncio.sleep(0.4)

            title = await _wait_nine_result()
            if title == "__solved__":
                result = await page.evaluate("() => window._captchaResult")
                if result and result.get("lot_number"):
                    return result
                raise RuntimeError(f"Nine-grid solved but result empty: {result}")
            if title == "__error__":
                err = await page.evaluate("() => window._captchaError || 'unknown'")
                raise RuntimeError(f"GeeTest nine-grid error: {err}")

        if attempt < max_attempts:
            print(f"[geetest] Nine-grid attempt {attempt} failed, refreshing…")
            await _refresh_nine_grid()

    return None


# ── Playwright async core ─────────────────────────────────────────────────────

async def _solve_async(
    captcha_id: str,
    timeout_ms: int,
    headed: bool,
    max_slider_attempts: int,
) -> dict:
    if not _PLAYWRIGHT_OK:
        raise ImportError(
            "playwright not installed.\n"
            "Run: conda activate ibox && pip install playwright && playwright install chromium"
        )
    if not _PILLOW_OK:
        raise ImportError(
            "Pillow / numpy not installed.\n"
            "Run: conda activate ibox && pip install Pillow numpy"
        )

    html = _HTML_TEMPLATE.format(captcha_id=captcha_id)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=not headed)
        ctx = await browser.new_context(
            user_agent=_MOBILE_UA,
            viewport={"width": 390, "height": 844},
            device_scale_factor=2,
            is_mobile=True,
        )
        # Minimal bot-evasion: hide the webdriver flag
        await ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = await ctx.new_page()

        # Serve the local HTML through Playwright's route interception so the
        # page has a proper https origin (avoids some CORS quirks).
        _PAGE_URL = "https://ibox-captcha.internal/"
        await page.route(
            _PAGE_URL,
            lambda r: r.fulfill(content_type="text/html; charset=utf-8", body=html),
        )
        # Capture page console/errors for diagnostics
        _page_errors: list[str] = []
        page.on("console", lambda msg: _page_errors.append(f"[js:{msg.type}] {msg.text}") if msg.type in ("error", "warning") else None)
        page.on("pageerror", lambda exc: _page_errors.append(f"[pageerror] {exc}"))

        # ── Intercept GeeTest /load API to obtain fullbg + bg image URLs ──────
        # GeeTest V4 fetches challenge data from gcaptcha4.geetest.com/load.
        # The JSON response contains 'bg' (with gap) and 'fullbg' (full image)
        # under 'data', plus 'static_path' as CDN base.  Diffing the two images
        # gives the exact gap position without any heuristics.
        _gt_data: dict = {}

        import json as _json_mod

        # Route-based interception is far more reliable than page.on("response"):
        # we are in the middleware chain so the response body is always readable,
        # whereas the "response" event fires after the browser has already
        # consumed the stream and response.text() silently returns nothing.
        async def _intercept_gt_load(route) -> None:
            try:
                req_url = route.request.url
                if "client_type=" not in req_url:
                    sep = "&" if "?" in req_url else "?"
                    req_url = f"{req_url}{sep}client_type=h5"
                response = await route.fetch(url=req_url)
                try:
                    body_bytes = await response.body()
                    body_text = body_bytes.decode("utf-8", errors="replace")
                    js = _parse_geetest_json(body_text)
                    _extract_gt_images(js, _gt_data)
                except Exception:
                    pass
                await route.fulfill(response=response)
            except Exception:
                await route.continue_()

        await page.route(
            lambda url: "geetest.com" in url and "/load" in url,
            _intercept_gt_load,
        )

        print("[geetest] Launching browser and loading GeeTest widget…")
        await page.goto(_PAGE_URL, wait_until="domcontentloaded")

        # ── Wait for GeeTest script to initialise (network check) ─────────────
        # Give the CDN script up to 30 s to load before waiting for the widget.
        t_init_start = time.monotonic()
        cdn_load_timeout = min(30_000, timeout_ms)
        try:
            await page.wait_for_function(
                "() => typeof window._captchaObj !== 'undefined' || document.title === '__error__'",
                timeout=cdn_load_timeout,
            )
            print("[geetest] GeeTest widget initialised.")
        except Exception:
            errs = "; ".join(_page_errors[-5:]) if _page_errors else "none"
            raise RuntimeError(
                f"GeeTest widget did not initialise within {cdn_load_timeout // 1000} s. "
                f"Check that static.geetest.com is reachable. Console errors: {errs}"
            )

        elapsed_ms = int((time.monotonic() - t_init_start) * 1000)
        remaining_ms = max(timeout_ms - elapsed_ms, 20_000)

        # ── GeeTest V4 'bind' product: click the initial verify button first ──
        # The widget renders a "click to verify" (geetest_btn) prompt; the
        # slide puzzle only appears after clicking it.
        click_btn_sel = "[class*='geetest_btn']"
        try:
            await page.wait_for_selector(click_btn_sel, state="visible", timeout=8_000)
            btn_el = page.locator(click_btn_sel).first
            print("[geetest] Clicking initial verify button…")
            await btn_el.click()
            await asyncio.sleep(1.0)
        except Exception:
            print("[geetest] No pre-click button found, continuing…")

        async def _detect_challenge_type() -> str:
            return await page.evaluate("""() => {
                if (document.querySelector('[class*="geetest_subitem"][class*="geetest_slide"]')) return 'slide';
                if (document.querySelector('[class*="geetest_subitem"][class*="geetest_nine"]')) return 'nine';
                if (document.querySelector('[class*="geetest_subitem"][class*="geetest_click"]')) return 'click';
                if (document.querySelector('[class*="geetest_subitem"][class*="geetest_icon"]')) return 'icon';
                var ct = document.querySelector('[class*="geetest_subitem"]');
                if (!ct) return 'unknown';
                if ((ct.className || '').indexOf('geetest_nine') !== -1) return 'nine';
                if ((ct.className || '').indexOf('geetest_click') !== -1) return 'click';
                return 'unknown';
            }""")

        async def _wait_challenge_ready() -> None:
            print("[geetest] Waiting for challenge to load…")
            await page.wait_for_function(
                """() => {
                    var root = document.querySelector('[class*="geetest_captcha"]');
                    if (!root) return false;
                    var nine = document.querySelector('[class*="geetest_subitem"][class*="geetest_nine"]');
                    if (nine) {
                        var prompt = document.querySelector('[class*="geetest_ques_tips"] img');
                        if (!prompt || !prompt.src) return false;
                        var tiles = document.querySelectorAll(
                            '[class*="geetest_nine"] [class*="geetest_item_img"],'
                            + '[class*="geetest_nine"] [class*="geetest_imgs"]'
                        );
                        if (tiles.length < 9) return false;
                        var loaded = 0;
                        for (var i = 0; i < tiles.length; i++) {
                            var el = tiles[i];
                            var bi = window.getComputedStyle(el).backgroundImage;
                            if (bi && bi !== 'none' && bi.indexOf('url') !== -1) {
                                loaded++;
                                continue;
                            }
                            if (el.tagName === 'IMG' && el.complete && el.naturalWidth > 0) loaded++;
                        }
                        return loaded >= 9;
                    }
                    var click = document.querySelector('[class*="geetest_subitem"][class*="geetest_click"]');
                    if (click) {
                        var bg = document.querySelector('[class*="geetest_bg"]');
                        if (bg) {
                            if (bg.classList.contains('geetest_freeze_action')) return false;
                            var bi = window.getComputedStyle(bg).backgroundImage;
                            if (bi && bi !== 'none' && bi.indexOf('url') !== -1) return true;
                        }
                        var tips = document.querySelector('[class*="geetest_ques_tips"]');
                        if (tips && tips.children.length) return true;
                        var img = document.querySelector('[class*="geetest_bg"] img');
                        if (img && img.complete && img.naturalWidth > 0) return true;
                    }
                    if (!root.classList.contains('geetest_freeze_wait')) return true;
                    if (root.classList.contains('geetest_nextReady')) return true;
                    return false;
                }""",
                timeout=min(25_000, remaining_ms),
            )

        await asyncio.sleep(1.0)
        challenge_type = await _detect_challenge_type()
        print(f"[geetest] Challenge type: {challenge_type!r} (api={_gt_data.get('captcha_type')!r})")

        try:
            await _wait_challenge_ready()
        except Exception:
            classes = await page.evaluate(
                "() => Array.from(document.querySelectorAll('[class]'))"
                ".map(e => e.className).filter(Boolean).slice(0, 20)"
            )
            raise RuntimeError(
                f"Challenge did not reach ready state within timeout. "
                f"Visible classes: {classes}"
            )

        challenge_type = await _detect_challenge_type()
        api_type = str(_gt_data.get("captcha_type") or "")
        is_nine = challenge_type == "nine" or api_type == "nine"
        is_phrase = (
            challenge_type in {"click", "icon"}
            or api_type == "phrase"
        )

        if is_nine:
            result = await _solve_nine_challenge(
                page=page,
                gt_data=_gt_data,
                remaining_ms=remaining_ms,
                max_attempts=max_slider_attempts,
            )
            await browser.close()
            if result and result.get("lot_number"):
                return result
            raise RuntimeError(f"Nine-grid challenge failed: {result}")

        if is_phrase:
            result = await _solve_phrase_challenge(
                page=page,
                gt_data=_gt_data,
                remaining_ms=remaining_ms,
                max_attempts=max_slider_attempts,
            )
            await browser.close()
            if result and result.get("lot_number"):
                return result
            raise RuntimeError(f"Phrase challenge failed: {result}")

        if challenge_type != "slide":
            raise RuntimeError(
                f"Unsupported GeeTest challenge type: {challenge_type!r}. "
                f"Try --captcha-mode app for manual App capture."
            )

        print("[geetest] Slide challenge confirmed, waiting for drag handle…")
        btn_sel = "[class*='geetest_slider'] [class*='geetest_btn']"
        await page.wait_for_selector(btn_sel, state="visible", timeout=8_000)
        print("[geetest] Drag handle ready, starting solver…")

        # ── Try to extract bg/fullbg URLs from DOM CSS (before first attempt) ──
        # GeeTest V4 renders the bg and fullbg as divs with background-image CSS.
        # If successful this is as accurate as the API-interception approach.
        async def _try_dom_extraction() -> None:
            try:
                dom_imgs = await page.evaluate("""
                    () => {
                        function extractUrl(el) {
                            if (!el) return null;
                            try {
                                // background-image CSS
                                var bi = window.getComputedStyle(el).backgroundImage;
                                var m = bi && bi.match(/url\\(["']?([^"')]+)["']?\\)/);
                                if (m) return m[1];
                                // <img src> fallback
                                if (el.tagName === 'IMG' && el.src) return el.src;
                            } catch(e) {}
                            return null;
                        }
                        function searchDoc(doc) {
                            var bgUrl = null, fullbgUrl = null;
                            var els = Array.from(doc.querySelectorAll('[class*="geetest_bg"], [class*="geetest_bg_"]'));
                            for (var el of els) {
                                var cls = el.className || '';
                                var url = extractUrl(el);
                                if (!url) continue;
                                if (!fullbgUrl && (cls.indexOf('fullbg') !== -1 || cls.indexOf('full_bg') !== -1)) {
                                    fullbgUrl = url;
                                } else if (!bgUrl && cls.indexOf('fullbg') === -1 && cls.indexOf('full_bg') === -1) {
                                    bgUrl = url;
                                }
                            }
                            return {bg: bgUrl, fullbg: fullbgUrl};
                        }
                        var result = searchDoc(document);
                        if (result.bg && result.fullbg) return result;
                        // Also search iframes (GeeTest sometimes sandboxes in an iframe)
                        try {
                            for (var i = 0; i < frames.length; i++) {
                                try {
                                    var r = searchDoc(frames[i].document);
                                    if (r.bg && r.fullbg) return r;
                                } catch(e) {}
                            }
                        } catch(e) {}
                        return result;
                    }
                """)
                if dom_imgs and dom_imgs.get("bg") and dom_imgs.get("fullbg"):
                    _gt_data["bg"]     = dom_imgs["bg"]
                    _gt_data["fullbg"] = dom_imgs["fullbg"]
                    _gt_data.pop("static_path", None)  # DOM URLs are already absolute
                    print("[geetest] Extracted image URLs from DOM CSS.")
            except Exception:
                pass

        if not (_gt_data.get("bg") and _gt_data.get("fullbg")):
            await _try_dom_extraction()

        # Grab bg element bounding box once (stable across retries)
        bg_el = page.locator("[class*='geetest_bg']").first
        bg_box = await bg_el.bounding_box()
        if not bg_box or bg_box["width"] == 0:
            raise RuntimeError("Could not locate geetest_bg element")

        for attempt in range(1, max_slider_attempts + 1):
            # ── Determine gap position ────────────────────────────────────────
            # Primary: diff fullbg vs bg from the GeeTest API response (exact).
            # Fallback: element screenshot + brightness heuristic.
            if _gt_data.get("bg") and _gt_data.get("fullbg"):
                static_path = (_gt_data.get("static_path") or "https://static.geetest.com/").rstrip("/") + "/"

                def _to_url(path: str) -> str:
                    return path if path.startswith("http") else static_path + path

                bg_url     = _to_url(_gt_data["bg"])
                fullbg_url = _to_url(_gt_data["fullbg"])

                # Use Playwright's request API (inherits browser context/cookies)
                bg_resp     = await page.request.get(bg_url)
                fullbg_resp = await page.request.get(fullbg_url)
                bg_bytes_img     = await bg_resp.body()
                fullbg_bytes_img = await fullbg_resp.body()

                gap_x_native = find_gap_by_diff(fullbg_bytes_img, bg_bytes_img)
                native_w = Image.open(io.BytesIO(bg_bytes_img)).width
                # Map native pixel → CSS pixel via element width ratio
                gap_x_css = (gap_x_native / native_w) * bg_box["width"]
                method = "diff"
            else:
                # Fallback: element screenshot
                bg_screenshot = await page.screenshot(clip={
                    "x": bg_box["x"], "y": bg_box["y"],
                    "width": bg_box["width"], "height": bg_box["height"],
                })
                gap_x_physical = find_gap_x_from_screenshot(bg_screenshot)
                # Infer actual device pixel ratio from the screenshot image width
                # vs the CSS bounding box width – avoids hardcoding /2.
                _sshot_pil = Image.open(io.BytesIO(bg_screenshot))
                actual_dpr = max(1.0, _sshot_pil.width / bg_box["width"])
                gap_x_css = gap_x_physical / actual_dpr
                method = "screenshot"

            # ── Get drag handle bounding box ──────────────────────────────────
            btn = page.locator(btn_sel).first
            box = await btn.bounding_box()
            if not box:
                raise RuntimeError("Slider button bounding box not found")

            # Drag mechanics:
            #   gap_x_css  = distance (in CSS px) from bg left edge to gap CENTER
            #   target_x   = absolute page x where the piece center should land
            #              = bg_box["x"] + gap_x_css
            #   sx         = current absolute page x of handle center
            sx = box["x"] + box["width"] / 2
            sy = box["y"] + box["height"] / 2
            target_x = bg_box["x"] + gap_x_css
            print(
                f"[geetest] attempt {attempt} ({method}): "
                f"gap_x_css={gap_x_css:.1f}  drag {sx:.1f}→{target_x:.1f}"
            )

            trajectory = _make_trajectory(sx, target_x, sy)
            await page.mouse.move(sx, sy)
            await asyncio.sleep(random.uniform(0.2, 0.4))
            await page.mouse.down()
            await asyncio.sleep(random.uniform(0.08, 0.15))

            for wpt_x, wpt_y in trajectory:
                await page.mouse.move(wpt_x, wpt_y)
                await asyncio.sleep(random.uniform(0.004, 0.018))

            await asyncio.sleep(random.uniform(0.05, 0.15))
            await page.mouse.up()

            # ── Wait for result ───────────────────────────────────────────────
            try:
                await page.wait_for_function(
                    "() => document.title === '__solved__' || document.title === '__error__'",
                    timeout=8_000,
                )
            except Exception:
                pass

            title = await page.title()
            if title == "__solved__":
                result = await page.evaluate("() => window._captchaResult")
                await browser.close()
                if result and result.get("lot_number"):
                    return result
                raise RuntimeError(f"Solved but result empty: {result}")

            if title == "__error__":
                err = await page.evaluate("() => window._captchaError || 'unknown'")
                raise RuntimeError(f"GeeTest reported error: {err}")

            # Wrong position – GeeTest shakes and reloads; retry
            if attempt < max_slider_attempts:
                print(f"[geetest] Slider attempt {attempt} missed, retrying…")
                # Clear stale image data so the next attempt fetches a fresh set
                _gt_data.clear()
                await asyncio.sleep(1.5)
                # Wait for ready state after reload
                await page.wait_for_function(
                    """() => {
                        var root = document.querySelector('[class*="geetest_captcha"]');
                        if (!root) return false;
                        if (!root.classList.contains('geetest_freeze_wait')) return true;
                        if (root.classList.contains('geetest_nextReady')) return true;
                        return false;
                    }""",
                    timeout=10_000,
                )
                await page.wait_for_selector(btn_sel, state="visible", timeout=10_000)
                # Attempt to re-extract image URLs for the reloaded challenge
                await _try_dom_extraction()
            else:
                raise RuntimeError(
                    f"Slider did not land correctly after {max_slider_attempts} attempts"
                )

        await browser.close()
        raise RuntimeError("Unreachable")


# ── Public API ────────────────────────────────────────────────────────────────

def playwright_solve(
    captcha_id: str = CAPTCHA_ID_IBOX,
    timeout: float = 60.0,
    headed: bool = False,
    max_retries: int = 3,
    max_slider_attempts: int = 6,
) -> dict:
    """
    Solve a GeeTest V4 slider captcha using a Playwright Chromium browser.

    Returns a dict with keys:
        lot_number, captcha_id, pass_token, gen_time, captcha_output

    Raises RuntimeError / ImportError on failure.

    Prerequisites (one-time setup):
        conda activate ibox
        pip install playwright Pillow numpy
        playwright install chromium
    """
    loop = asyncio.new_event_loop()
    try:
        last_err: Exception | None = None
        for attempt in range(1, max_retries + 1):
            try:
                return loop.run_until_complete(
                    _solve_async(
                        captcha_id=captcha_id,
                        timeout_ms=int(timeout * 1000),
                        headed=headed,
                        max_slider_attempts=max_slider_attempts,
                    )
                )
            except Exception as exc:
                last_err = exc
                print(f"[geetest] Playwright attempt {attempt}/{max_retries} failed: {exc}")
                if attempt < max_retries:
                    time.sleep(2 ** attempt)
        raise RuntimeError(f"All {max_retries} Playwright attempts failed") from last_err
    finally:
        loop.close()


def check_dependencies() -> tuple[bool, str]:
    """Return (ok, message) about optional dependency availability."""
    missing = []
    if not _PLAYWRIGHT_OK:
        missing.append("playwright  →  pip install playwright && playwright install chromium")
    if not _PILLOW_OK:
        missing.append("Pillow/numpy  →  pip install Pillow numpy")
    if missing:
        return False, "Missing dependencies:\n  " + "\n  ".join(missing)
    extras = []
    if not _CV2_OK:
        extras.append("opencv-python-headless (optional, faster phrase matching)")
    if not _DDDDOCR_OK:
        extras.append("ddddocr (optional, OCR fallback for phrase hints)")
    msg = "All core dependencies available"
    if extras:
        msg += "\n  Optional: " + ", ".join(extras)
    return True, msg
