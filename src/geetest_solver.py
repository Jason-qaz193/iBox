"""
GeeTest V4 滑块验证码自动求解器

两种求解模式：
  1. playwright_solve()  — 使用无头浏览器 + 图像分析自动拖动滑块
  2. (fallback) poll_rpc_captcha() — 等待用户在 App 中手动滑动，
     LSPosed 模块捕获结果后由 Python 通过 RPC 取回

安装依赖（conda activate ibox 环境下）：
    pip install playwright Pillow numpy
    playwright install chromium
"""

from __future__ import annotations

import asyncio
import base64
import io
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
        return
    for v in obj.values():
        if isinstance(v, dict):
            _extract_gt_images(v, target, _depth + 1)
        elif isinstance(v, list):
            for item in v:
                _extract_gt_images(item, target, _depth + 1)
        if target.get("bg") and target.get("fullbg"):
            return


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
                response = await route.fetch()
                try:
                    body_bytes = await response.body()
                    js = _json_mod.loads(body_bytes.decode("utf-8", errors="replace"))
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
            print("[geetest] No pre-click button found, continuing to slider…")

        # ── Wait for challenge to be ready ────────────────────────────────────
        # freeze_wait = images still loading.  nextReady = images loaded,
        # puzzle ready for interaction (freeze_wait may remain alongside it).
        # We proceed when either freeze_wait clears OR nextReady appears.
        print("[geetest] Waiting for challenge to load…")
        try:
            await page.wait_for_function(
                """() => {
                    var root = document.querySelector('[class*="geetest_captcha"]');
                    if (!root) return false;
                    if (!root.classList.contains('geetest_freeze_wait')) return true;
                    if (root.classList.contains('geetest_nextReady')) return true;
                    return false;
                }""",
                timeout=min(20_000, remaining_ms),
            )
        except Exception:
            classes = await page.evaluate(
                "() => Array.from(document.querySelectorAll('[class]'))"
                ".map(e => e.className).filter(Boolean).slice(0, 20)"
            )
            raise RuntimeError(
                f"Challenge did not reach ready state within timeout. "
                f"Visible classes: {classes}"
            )

        # ── Confirm challenge type is slide ───────────────────────────────────
        # geetest_subitem will have class geetest_slide for slider,
        # geetest_click/geetest_icon for harder challenges (bot detected).
        challenge_type = await page.evaluate("""() => {
            if (document.querySelector('[class*="geetest_subitem"][class*="geetest_slide"]')) return 'slide';
            if (document.querySelector('[class*="geetest_subitem"][class*="geetest_click"]')) return 'click';
            if (document.querySelector('[class*="geetest_subitem"][class*="geetest_icon"]')) return 'icon';
            // Fall back: check class list of first subitem
            var sub = document.querySelector('[class*="geetest_subitem"]');
            return sub ? sub.className : 'unknown';
        }""")

        if challenge_type != "slide":
            screenshot_path = "/tmp/geetest_debug.png"
            try:
                await page.screenshot(path=screenshot_path, full_page=True)
                screenshot_msg = f" Screenshot saved to {screenshot_path}."
            except Exception:
                screenshot_msg = ""
            raise RuntimeError(
                f"GeeTest served a '{challenge_type}' challenge instead of slider "
                f"(likely bot-detection). Try --captcha-headed or --captcha-mode manual.{screenshot_msg}"
            )

        # ── GeeTest V4 slide: drag handle is geetest_btn inside geetest_slider ─
        # (NOT geetest_slider_btn — that is V3 naming)
        btn_sel = "[class*='geetest_slider'] [class*='geetest_btn']"
        print("[geetest] Slide challenge confirmed, waiting for drag handle…")
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
    return True, "All dependencies available"
