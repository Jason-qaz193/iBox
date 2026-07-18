"""
Auto-solve GeeTest captcha on the phone screen via adb screencap + tap.

Tokens captured from the real iBox App WebView pass server-side risk checks;
standalone Playwright tokens often return HTTP 406 on sale-rush orders.
"""

from __future__ import annotations

import io
import re
import shlex
import subprocess
import time
import xml.etree.ElementTree as ET
from typing import Callable
from urllib.parse import quote

from .frida_client import peek_rpc_captcha, poll_captcha, rpc

try:
    from .device_bridge import run_adb as _run_adb
except ImportError:
    def _run_adb(*args: str, timeout: float = 15.0) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["adb", *args],
            capture_output=True,
            timeout=timeout,
        )

try:
    from PIL import Image

    _PILLOW_OK = True
except ImportError:
    _PILLOW_OK = False

try:
    import cv2

    _CV2_OK = True
except ImportError:
    _CV2_OK = False

try:
    import numpy as np

    _NUMPY_OK = True
except ImportError:
    _NUMPY_OK = False

try:
    from .geetest_solver import (
        _find_nine_matching_indices,
        _find_phrase_clicks_by_hint_columns,
        _find_phrase_clicks_by_hint_strip,
        _find_phrase_clicks_by_reading_order,
        _nine_pick_combos,
        _nine_combo_min_score,
        _phrase_click_orders,
        _rank_nine_tiles,
        _split_nine_grid_image,
        _MIN_NINE_TILE_SCORE,
    )

    _SOLVER_OK = True
except Exception:
    _SOLVER_OK = False

IBOX_PACKAGE = "com.box.art"
_IBOX_SCHEME_AUTH = "com.ibox.push"
_BUY_LABELS = ("立即购买", "确认购买", "去购买", "购买", "马上抢", "立即抢购")
_PAGE_BLOCK_LABELS = ("已售罄", "售罄", "已结束", "未开始", "即将开售", "暂无库存", "已抢光")


def _run_adb_cmd(*args: str, timeout: float = 15.0) -> subprocess.CompletedProcess:
    return _run_adb(*args, timeout=timeout)


def adb_screencap_png() -> bytes | None:
    proc = _run_adb_cmd("exec-out", "screencap", "-p", timeout=20.0)
    if proc.returncode != 0 or not proc.stdout:
        return None
    return proc.stdout


def adb_tap(x: int, y: int) -> None:
    _run_adb_cmd("shell", "input", "tap", str(int(x)), str(int(y)), timeout=5.0)


def adb_swipe(x1: int, y1: int, x2: int, y2: int, duration_ms: int = 200) -> None:
    _run_adb_cmd(
        "shell",
        "input",
        "swipe",
        str(int(x1)),
        str(int(y1)),
        str(int(x2)),
        str(int(y2)),
        str(int(duration_ms)),
        timeout=5.0,
    )


def adb_am_start_view_url(url: str, *, package: str = IBOX_PACKAGE) -> subprocess.CompletedProcess:
    """Launch a VIEW intent inside *package*; quote URL so shell '&' is not split."""
    target = str(url or "").strip()
    if not target:
        return subprocess.CompletedProcess(args=[], returncode=1)
    inner = (
        "am start -a android.intent.action.VIEW "
        f"-d {shlex.quote(target)} {shlex.quote(package)}"
    )
    return _run_adb_cmd("shell", inner, timeout=10.0)


def wake_ibox_app() -> None:
    _run_adb_cmd(
        "shell",
        "monkey",
        "-p",
        IBOX_PACKAGE,
        "-c",
        "android.intent.category.LAUNCHER",
        "1",
        timeout=10.0,
    )


def _iboxscheme_detail_url(
    *,
    h5_url: str = "",
    group_id: str = "",
    sale_id: str = "",
) -> str | None:
    h5 = str(h5_url or "").strip()
    if h5:
        return f"iboxscheme://{_IBOX_SCHEME_AUTH}/detail?url={quote(h5, safe='')}"
    gid = str(group_id or "").strip()
    sid = str(sale_id or "").strip()
    if gid and sid:
        return f"iboxscheme://{_IBOX_SCHEME_AUTH}/detail?groupId={gid}&saleId={sid}"
    return None


def build_sale_detail_urls(
    group_id: str,
    *,
    sale_id: str = "",
    sale_link: str = "",
) -> list[str]:
    gid = str(group_id or "").strip()
    sid = str(sale_id or "").strip()
    link = str(sale_link or "").strip()
    h5_candidates: list[str] = []
    urls: list[str] = []
    seen: set[str] = set()

    def _add_h5(url: str) -> None:
        u = url.strip()
        if u and u not in h5_candidates:
            h5_candidates.append(u)

    def _add(url: str) -> None:
        u = url.strip()
        if u and u not in seen:
            seen.add(u)
            urls.append(u)

    if gid and sid:
        _add_h5(
            "https://detail-page.ibox.art/index.html"
            f"#/first-publish/{gid}?1=1&is_full_screen=1&noBounce=1&saleId={sid}"
        )
        _add_h5(
            "https://detail-page.ibox.art/index.html"
            f"#/first-publish/{gid}/{sid}?1=1&is_full_screen=1&noBounce=1"
        )
        _add_h5(f"https://detail-page.ibox.art/#/first-publish/{gid}?saleId={sid}")
        _add_h5(f"https://detail-page.ibox.art/#/first-publish/{gid}/{sid}")
        _add_h5(f"https://detail-page.ibox.art/#/first-publish-detail/{sid}")
        _add_h5(f"https://detail-page.ibox.art/#/sale/{sid}")
    if link:
        if sid and "saleId" not in link and f"/{sid}" not in link:
            sep = "&" if "?" in link else "?"
            _add_h5(f"{link}{sep}saleId={sid}")
        _add_h5(link)
    if gid:
        _add_h5(f"https://detail-page.ibox.art/#/first-publish/{gid}")

    for h5 in h5_candidates:
        scheme = _iboxscheme_detail_url(h5_url=h5)
        if scheme:
            _add(scheme)
    if gid and sid:
        scheme = _iboxscheme_detail_url(group_id=gid, sale_id=sid)
        if scheme:
            _add(scheme)
    return urls


def _screencap_image() -> Image.Image | None:
    raw = adb_screencap_png()
    if not raw or not _PILLOW_OK:
        return None
    return Image.open(io.BytesIO(raw)).convert("RGB")


def _detail_page_stale() -> bool:
    texts = _collect_visible_text()
    compact = "".join(texts).replace(" ", "")
    joined = " | ".join(texts)
    if any(label in joined for label in _BUY_LABELS):
        return False
    img = _screencap_image()
    if img and _find_action_button_by_vision(
        img, region=_buy_search_region(img.size[0], img.size[1])
    ):
        return False
    if any(token in compact for token in ("发行0份", "流通0份", "¥--", "￥--")):
        return True
    if any(token in compact for token in ("藏品信息", "藏品故事", "STORY")):
        return True
    return False


def _buy_search_region(screen_w: int, screen_h: int) -> tuple[int, int, int, int]:
    return (0, int(screen_h * 0.55), screen_w, int(screen_h * 0.86))


def _collection_name_hints(collection_name: str) -> list[str]:
    name = str(collection_name or "").strip()
    hints: list[str] = []
    if name:
        hints.append(name)
    if name.endswith("7.12"):
        hints.extend(["7.12", "大美延吉", "延吉"])
    return [h for h in hints if h]


def _page_has_collection_hints(collection_name: str) -> bool:
    joined = " | ".join(_collect_visible_text())
    return any(hint in joined for hint in _collection_name_hints(collection_name))


def _page_on_home_page() -> bool:
    texts = _collect_visible_text()
    joined = " | ".join(texts)
    if any(token in joined for token in ("藏品信息", "藏品故事", "STORY", "藏品详情")):
        return False
    home_markers = ("活动中心", "游戏广场", "公告", "美好的事情将要发生", "首页")
    return sum(1 for marker in home_markers if marker in joined) >= 2


def _page_on_list_page() -> bool:
    texts = _collect_visible_text()
    joined = " | ".join(texts)
    if any(token in joined for token in ("藏品信息", "藏品故事", "STORY", "藏品详情")):
        return False
    if _page_on_home_page():
        return "发售记录" in joined
    return any(token in joined for token in ("发售记录", "价格排序", "时间倒序"))


def _page_on_sale_list_with_items() -> bool:
    if not _page_on_list_page() or _page_on_home_page():
        return False
    joined = " | ".join(_collect_visible_text())
    return "延吉" in joined or "7.12" in joined or "7.13" in joined or "售卖中" in joined


def _detail_page_ready(collection_name: str = "") -> bool:
    if _page_on_list_page() and not any(
        token in " | ".join(_collect_visible_text())
        for token in ("藏品信息", "藏品故事", "STORY", "藏品详情")
    ):
        return False
    texts = _collect_visible_text()
    joined = " | ".join(texts)
    if any(token in joined for token in ("藏品信息", "藏品故事", "STORY", "藏品详情")):
        if not _detail_page_stale():
            return True
        if collection_name and _page_has_collection_hints(collection_name):
            img = _screencap_image()
            if img and _find_action_button_by_vision(
                img, region=_buy_search_region(img.size[0], img.size[1])
            ):
                return True
    if collection_name and _page_has_collection_hints(collection_name) and not _detail_page_stale():
        return True
    img = _screencap_image()
    if img and _find_action_button_by_vision(img, region=_buy_search_region(img.size[0], img.size[1])):
        return not _detail_page_stale()
    return any(label in joined for label in _BUY_LABELS)


def adb_press_back() -> None:
    _run_adb_cmd("shell", "input", "keyevent", "4", timeout=3.0)


def _screen_size() -> tuple[int, int]:
    raw = adb_screencap_png()
    if raw and _PILLOW_OK:
        return Image.open(io.BytesIO(raw)).size
    return 1080, 2400


def _ensure_home_sale_records_visible() -> None:
    w, h = _screen_size()
    for _ in range(5):
        if _page_on_home_page() and _page_contains_text("发售记录"):
            return
        adb_swipe(w // 2, int(h * 0.86), w // 2, int(h * 0.50), 420)
        time.sleep(0.7)


def _home_sale_card_tap_points(screen_w: int, screen_h: int) -> list[tuple[int, int, str]]:
    max_y = int(screen_h * 0.86)
    points: list[tuple[int, int, str]] = []
    for row, yr in enumerate((0.84, 0.88)):
        cy = min(int(screen_h * yr), max_y - 6)
        for col, xr in enumerate((0.22, 0.52, 0.78)):
            points.append((int(screen_w * xr), cy, f"home-card:{row},{col}"))
    return points


def _navigate_via_home_card_grid(
    collection_name: str,
    *,
    max_passes: int = 6,
) -> bool:
    """Homepage sale-record cards live in a WebView without a11y text; tap by grid."""
    _ensure_home_sale_records_visible()
    w, h = _screen_size()
    for swipe_pass in range(max(1, int(max_passes))):
        for cx, cy, label in _home_sale_card_tap_points(w, h):
            adb_tap(cx, cy)
            print(f"[app-captcha] tapped {label} ({cx},{cy})", flush=True)
            time.sleep(2.0)
            if _detail_page_ready(collection_name):
                print("[app-captcha] opened sale detail from home cards ✓", flush=True)
                return True
            texts = _collect_visible_text()
            joined = " | ".join(texts)
            if any(token in joined for token in ("藏品信息", "STORY", "藏品故事")):
                if collection_name and not _page_has_collection_hints(collection_name):
                    print("[app-captcha] wrong sale card, going back…", flush=True)
                elif _detail_page_ready(collection_name):
                    return True
            adb_press_back()
            time.sleep(0.9)
        adb_swipe(int(w * 0.82), int(h * 0.90), int(w * 0.18), int(h * 0.90), 320)
        time.sleep(0.8)
        print(
            f"[app-captcha] home card swipe pass {swipe_pass + 1}/{max_passes}",
            flush=True,
        )
    return _detail_page_ready(collection_name)


def _try_rpc_open_urls(
    device_host: str,
    urls: list[str],
    collection_name: str = "",
) -> bool:
    try:
        from .frida_client import rpc_open_web
    except ImportError:
        return False
    h5_urls: list[str] = []
    for url in urls:
        if url.startswith("iboxscheme://") and "url=" in url:
            from urllib.parse import parse_qs, urlparse

            parsed = urlparse(url)
            query = parse_qs(parsed.query)
            raw = (query.get("url") or [""])[0]
            if raw:
                h5_urls.append(raw)
        elif url.startswith("http"):
            h5_urls.append(url)
    for h5 in h5_urls[:2]:
        print(f"[app-captcha] RPC open-web {h5[:100]}…", flush=True)
        if rpc_open_web(device_host, h5):
            time.sleep(4.0)
            if _detail_page_ready(collection_name):
                print("[app-captcha] sale detail page ready ✓ (RPC)", flush=True)
                return True
            if _try_select_sale_from_list(collection_name):
                return True
    return False


def _try_select_sale_from_list(collection_name: str) -> bool:
    if not collection_name:
        return False
    if _page_on_home_page():
        return False
    if not (_page_on_sale_list_with_items() or _page_contains_text(collection_name)):
        return False
    if adb_tap_collection_row(collection_name) and _detail_page_ready(collection_name):
        print("[app-captcha] opened sale detail from list ✓", flush=True)
        return True
    return False


def open_sale_purchase_page(
    group_id: str,
    *,
    sale_id: str = "",
    sale_link: str = "",
    collection_name: str = "",
    device_host: str = "",
) -> bool:
    """Open sale detail page; return True when buy-ready detail is shown."""
    if _try_select_sale_from_list(collection_name):
        return True
    if collection_name and _navigate_via_home_card_grid(collection_name, max_passes=4):
        return True

    urls = build_sale_detail_urls(group_id, sale_id=sale_id, sale_link=sale_link)
    if device_host and urls and _try_rpc_open_urls(device_host, urls[:2], collection_name):
        return True

    for url in urls[:1]:
        display = url if len(url) <= 120 else f"{url[:117]}…"
        print(f"[app-captcha] opening {display}", flush=True)
        proc = adb_am_start_view_url(url)
        if proc.returncode != 0:
            err = (proc.stderr or b"").decode("utf-8", errors="ignore").strip()
            if err:
                print(f"[app-captcha] WARN: am start failed: {err[:200]}", flush=True)
        time.sleep(3.0)
        if _detail_page_ready(collection_name):
            print("[app-captcha] sale detail page ready ✓", flush=True)
            return True
        if _try_select_sale_from_list(collection_name):
            return True

    if collection_name and _navigate_via_home_card_grid(collection_name, max_passes=6):
        return True
    return _detail_page_ready(collection_name)


def clear_rpc_captcha(device_host: str) -> None:
    try:
        rpc({"type": "captcha-clear"}, device_host=device_host, timeout=5.0)
    except Exception:
        pass


def _image_to_png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _parse_ui_bounds(bounds: str) -> tuple[int, int, int, int] | None:
    match = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds or "")
    if not match:
        return None
    x1, y1, x2, y2 = (int(v) for v in match.groups())
    if x2 <= x1 or y2 <= y1:
        return None
    return x1, y1, x2, y2


def _adb_uiautomator_dump_xml() -> str:
    dump_path = "/sdcard/ibox_uidump.xml"
    _run_adb_cmd("shell", "uiautomator", "dump", dump_path, timeout=12.0)
    proc = _run_adb_cmd("shell", "cat", dump_path, timeout=12.0)
    if proc.returncode != 0 or not proc.stdout:
        return ""
    text = proc.stdout.decode("utf-8", errors="ignore").strip()
    idx = text.find("<?xml")
    return text[idx:] if idx >= 0 else text


def adb_tap_by_ui_text(
    *labels,
    clickable_only: bool = False,
) -> tuple[bool, str]:
    """Find a visible UI node containing *labels* and tap its center."""
    xml_text = _adb_uiautomator_dump_xml()
    if not xml_text:
        return False, ""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return False, ""

    best: tuple[int, int, str] | None = None
    for node in root.iter("node"):
        text = f"{node.get('text') or ''}{node.get('content-desc') or ''}"
        if not text:
            continue
        if clickable_only and node.get("clickable") != "true":
            continue
        bounds = _parse_ui_bounds(node.get("bounds") or "")
        if not bounds:
            continue
        x1, y1, x2, y2 = bounds
        area = (x2 - x1) * (y2 - y1)
        for label in labels:
            if label not in text:
                continue
            cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
            if best is None or area < best[0]:
                best = (area, cx, cy, label)
            break

    if not best:
        return False, ""
    _, cx, cy, label = best
    adb_tap(cx, cy)
    return True, label


def _find_webview_bounds() -> tuple[int, int, int, int] | None:
    xml_text = _adb_uiautomator_dump_xml()
    if not xml_text:
        return None
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None

    best: tuple[int, tuple[int, int, int, int]] | None = None
    for node in root.iter("node"):
        cls = node.get("class") or ""
        if "WebView" not in cls:
            continue
        bounds = _parse_ui_bounds(node.get("bounds") or "")
        if not bounds:
            continue
        x1, y1, x2, y2 = bounds
        area = (x2 - x1) * (y2 - y1)
        if best is None or area > best[0]:
            best = (area, bounds)
    return best[1] if best else None


def _collect_visible_text() -> list[str]:
    xml_text = _adb_uiautomator_dump_xml()
    if not xml_text:
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    texts: list[str] = []
    for node in root.iter("node"):
        for attr in ("text", "content-desc"):
            value = (node.get(attr) or "").strip()
            if value and value not in texts:
                texts.append(value)
    return texts


def _page_contains_text(text: str) -> bool:
    needle = str(text or "").strip()
    if not needle:
        return False
    return any(needle in t for t in _collect_visible_text())


def _page_on_detail_page(collection_name: str = "") -> bool:
    if _page_on_list_page():
        return False
    return _detail_page_ready(collection_name) or _detail_page_stale()


def adb_scroll_sale_list_down() -> None:
    w, h = 1080, 2400
    raw = adb_screencap_png()
    if raw and _PILLOW_OK:
        w, h = Image.open(io.BytesIO(raw)).size
    adb_swipe(w // 2, int(h * 0.72), w // 2, int(h * 0.38), 380)


def adb_tap_collection_row(collection_name: str) -> bool:
    name = str(collection_name or "").strip()
    if not name:
        return False
    xml_text = _adb_uiautomator_dump_xml()
    if not xml_text:
        return False
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return False

    screen_h = _screen_height_guess()
    best: tuple[int, int, str] | None = None
    for node in root.iter("node"):
        text = f"{node.get('text') or ''}{node.get('content-desc') or ''}"
        if not text:
            continue
        if name not in text and not (name.endswith("7.12") and "7.12" in text):
            continue
        bounds = _parse_ui_bounds(node.get("bounds") or "")
        if not bounds:
            continue
        x1, y1, x2, y2 = bounds
        cy = (y1 + y2) // 2
        if cy < int(screen_h * 0.18) or cy > int(screen_h * 0.88):
            continue
        cx = (x1 + x2) // 2
        area = (x2 - x1) * (y2 - y1)
        if best is None or area > best[0]:
            best = (area, cx, cy, text[:48])
    if not best:
        return False
    _, cx, cy, label = best
    adb_tap(cx, cy)
    print(f"[app-captcha] tapped collection row «{label}» ({cx},{cy})", flush=True)
    time.sleep(2.0)
    return True


def navigate_to_sale_detail(
    collection_name: str,
    *,
    group_id: str = "",
    sale_id: str = "",
    sale_link: str = "",
    device_host: str = "",
) -> bool:
    if _try_select_sale_from_list(collection_name):
        return True
    if collection_name and _navigate_via_home_card_grid(collection_name, max_passes=4):
        return True
    if open_sale_purchase_page(
        group_id,
        sale_id=sale_id,
        sale_link=sale_link,
        collection_name=collection_name,
        device_host=device_host,
    ):
        return True
    if _page_on_sale_list_with_items():
        for attempt in range(6):
            if adb_tap_collection_row(collection_name) and _detail_page_ready(collection_name):
                return True
            adb_scroll_sale_list_down()
            time.sleep(1.0)
            print(
                f"[app-captcha] scroll list attempt {attempt + 1}/6 for {collection_name!r}",
                flush=True,
            )
    return _detail_page_ready(collection_name)


def _screen_height_guess() -> int:
    raw = adb_screencap_png()
    if raw and _PILLOW_OK:
        return Image.open(io.BytesIO(raw)).size[1]
    return 2400


def _is_tab_bar_region(cx: int, cy: int, screen_h: int) -> bool:
    return cy >= int(screen_h * 0.87)


def _log_page_hints(collection_name: str = "") -> None:
    texts = _collect_visible_text()
    if not texts:
        return
    joined = " | ".join(texts[:20])
    print(f"[app-captcha] page text: {joined[:240]}", flush=True)


def _save_debug_screenshot(tag: str) -> str | None:
    if not _PILLOW_OK:
        return None
    raw = adb_screencap_png()
    if not raw:
        return None
    import os
    from datetime import datetime

    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "logs"))
    os.makedirs(root, exist_ok=True)
    path = os.path.join(root, f"app-captcha-{tag}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.png")
    with open(path, "wb") as f:
        f.write(raw)
    print(f"[app-captcha] saved debug screenshot: {path}", flush=True)
    return path


def _find_native_bottom_button(screen_h: int) -> tuple[int, int, str] | None:
    xml_text = _adb_uiautomator_dump_xml()
    if not xml_text:
        return None
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None

    bottom_y = int(screen_h * 0.68)
    tab_y = int(screen_h * 0.87)
    best: tuple[int, int, int, str] | None = None
    for node in root.iter("node"):
        text = f"{node.get('text') or ''}{node.get('content-desc') or ''}"
        bounds = _parse_ui_bounds(node.get("bounds") or "")
        if not bounds:
            continue
        x1, y1, x2, y2 = bounds
        if y1 < bottom_y or y2 >= tab_y:
            continue
        cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
        if _is_tab_bar_region(cx, cy, screen_h):
            continue
        area = (x2 - x1) * (y2 - y1)
        for label in _BUY_LABELS:
            if label in text:
                return cx, cy, label
        if node.get("clickable") == "true" and area > 8000:
            if best is None or area < best[0]:
                best = (area, cx, cy, text[:20] or "clickable")
    if best:
        _, cx, cy, label = best
        return cx, cy, label
    return None


def _find_action_button_by_vision(
    img: Image.Image,
    region: tuple[int, int, int, int] | None = None,
) -> tuple[int, int] | None:
    """Detect orange/red CTA button in the bottom action bar."""
    if not (_CV2_OK and _NUMPY_OK):
        return None
    w, h = img.size
    if region:
        x1, y1, x2, y2 = region
    else:
        x1, y1, x2, y2 = 0, int(h * 0.72), w, h
    crop = img.crop((x1, y1, x2, y2))
    arr = cv2.cvtColor(np.array(crop), cv2.COLOR_RGB2BGR)
    hsv = cv2.cvtColor(arr, cv2.COLOR_BGR2HSV)
    masks = (
        cv2.inRange(hsv, (5, 80, 120), (25, 255, 255)),
        cv2.inRange(hsv, (0, 70, 120), (10, 255, 255)),
    )
    mask = masks[0]
    for extra in masks[1:]:
        mask = cv2.bitwise_or(mask, extra)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    best: tuple[float, int, int] | None = None
    crop_w, crop_h = crop.size
    min_area = max(1200, int(crop_w * crop_h * 0.01))
    for contour in contours:
        area = cv2.contourArea(contour)
        if area < min_area:
            continue
        bx, by, bw, bh = cv2.boundingRect(contour)
        if bw < crop_w * 0.12 or bh < crop_h * 0.18:
            continue
        cx = x1 + bx + bw // 2
        cy = y1 + by + bh // 2
        score = area + (bx / max(crop_w, 1)) * 500
        if best is None or score > best[0]:
            best = (score, cx, cy)
    return (best[1], best[2]) if best else None


def _buy_tap_points(
    img: Image.Image,
    webview: tuple[int, int, int, int] | None,
) -> list[tuple[int, int, str]]:
    w, h = img.size
    points: list[tuple[int, int, str]] = []
    seen: set[tuple[int, int]] = set()

    def _add(cx: int, cy: int, label: str) -> None:
        key = (cx // 8, cy // 8)
        if key in seen:
            return
        seen.add(key)
        points.append((cx, cy, label))

    native = _find_native_bottom_button(h)
    if native:
        _add(native[0], native[1], f"native:{native[2]}")

    vision = _find_action_button_by_vision(img, _buy_search_region(w, h))
    if vision:
        _add(vision[0], vision[1], "vision:orange-btn")

    if webview:
        x1, y1, x2, y2 = webview
        wv_w, wv_h = x2 - x1, y2 - y1
        for xr in (0.82, 0.75, 0.68):
            for yr in (0.86, 0.82, 0.78):
                _add(x1 + int(wv_w * xr), y1 + int(wv_h * yr), f"webview:{xr:.2f},{yr:.2f}")

    for xr in (0.82, 0.75):
        for yr in (0.86, 0.82, 0.78):
            _add(int(w * xr), int(h * yr), f"screen:{xr:.2f},{yr:.2f}")
    return points


def adb_tap_buy_button() -> bool:
    tapped, label = adb_tap_by_ui_text(*_BUY_LABELS)
    if tapped:
        print(f"[app-captcha] tapped buy button «{label}» via uiautomator", flush=True)
        return True

    raw = adb_screencap_png()
    if not raw or not _PILLOW_OK:
        return False
    img = Image.open(io.BytesIO(raw)).convert("RGB")
    webview = _find_webview_bounds()
    if webview:
        print(f"[app-captcha] WebView bounds={webview}", flush=True)

    tapped_any = False
    for cx, cy, label in _buy_tap_points(img, webview)[:3]:
        adb_tap(cx, cy)
        print(f"[app-captcha] tapped buy {label} ({cx},{cy})", flush=True)
        tapped_any = True
        time.sleep(0.35)
    return tapped_any


def _phrase_split_y(crop_h: int) -> int:
    return max(int(crop_h * 0.22), 28)


def _phrase_clicks_in_body(clicks: list[tuple[int, int]], crop_h: int) -> bool:
    split_y = _phrase_split_y(crop_h)
    return bool(clicks) and all(cy > split_y + 8 for _, cy in clicks)


def _phrase_click_candidates_for_crop(
    crop: Image.Image,
    *,
    strict: bool = False,
) -> list[tuple[str, list[tuple[int, int]]]]:
    if not _SOLVER_OK:
        return []
    png = _image_to_png_bytes(crop)
    w, h = crop.size
    min_score = 0.34 if strict else 0.22
    candidates: list[tuple[str, list[tuple[int, int]]]] = []
    for expected in ((3,) if strict else (3, 2, 4)):
        clicks = _find_phrase_clicks_by_hint_columns(
            png,
            expected_count=expected,
            min_score=min_score,
        )
        if not clicks or not _phrase_clicks_in_body(clicks, h):
            continue
        xs = [c[0] for c in clicks]
        ys = [c[1] for c in clicks]
        if max(xs) - min(xs) < w * (0.18 if strict else 0.12):
            continue
        if strict and len(set(ys)) < 2:
            continue
        candidates.append((f"hint-columns({expected})", clicks))
        if strict:
            break
    if strict:
        return candidates
    for expected in (3, 2, 4):
        for method, finder in (
            (f"hint-strip({expected})", lambda e=expected: _find_phrase_clicks_by_hint_strip(png, expected_count=e)),
            (f"reading-order({expected})", lambda e=expected: _find_phrase_clicks_by_reading_order(png, expected_count=e)),
        ):
            clicks = finder()
            if _phrase_clicks_in_body(clicks or [], h):
                candidates.append((method, clicks))
    return candidates


def _fullscreen_has_dim_overlay(img: Image.Image) -> bool:
    if not _NUMPY_OK:
        return False
    w, h = img.size
    arr = np.array(img.convert("L"), dtype=np.float32)
    margin = max(int(min(w, h) * 0.06), 12)
    edge = np.concatenate(
        [
            arr[:margin, :].ravel(),
            arr[-margin:, :].ravel(),
            arr[:, :margin].ravel(),
            arr[:, -margin:].ravel(),
        ]
    )
    center = arr[h // 4 : 3 * h // 4, w // 4 : 3 * w // 4]
    return float(edge.mean()) + 12 < float(center.mean()) and float(edge.mean()) < 120


def _crop_has_solveable_geetest(crop: Image.Image, *, strict: bool = True) -> bool:
    if _phrase_click_candidates_for_crop(crop, strict=strict):
        return True
    if strict:
        return False
    w, h = crop.size
    prompt_h = max(int(h * 0.22), 40)
    if w < 120 or h < prompt_h + 90:
        return False
    if not _SOLVER_OK or not _PILLOW_OK:
        return False
    grid = crop.crop((0, prompt_h, w, h))
    tiles = _split_nine_grid_image(_image_to_png_bytes(grid))
    return len(tiles) == 9


def _find_geetest_modal(img: Image.Image, *, strict: bool = True) -> tuple[Image.Image, int, int] | None:
    if strict and not _fullscreen_has_dim_overlay(img):
        return None
    for crop, ox, oy in _candidate_modal_crops(img):
        if _crop_has_solveable_geetest(crop, strict=strict):
            return crop, ox, oy
    return None


def _screen_has_geetest_modal() -> tuple[Image.Image, int, int] | None:
    raw = adb_screencap_png()
    if not raw or not _PILLOW_OK:
        return None
    img = Image.open(io.BytesIO(raw)).convert("RGB")
    return _find_geetest_modal(img, strict=True)


def _tap_geetest_refresh(crop: Image.Image, offset_x: int, offset_y: int) -> None:
    w, h = crop.size
    adb_tap(offset_x + int(w * 0.92), offset_y + int(h * 0.08))


def _looks_like_geetest_modal(crop: Image.Image) -> bool:
    if not _NUMPY_OK:
        return crop.size[0] >= 220 and crop.size[1] >= 140
    arr = np.array(crop.convert("L"), dtype=np.float32)
    h, w = arr.shape[:2]
    if h < 120 or w < 180:
        return False
    split_y = max(int(h * 0.22), 20)
    top = arr[:split_y, :]
    body = arr[split_y:, :]
    return float(top.std()) > 12 and float(body.std()) > 18


def _candidate_modal_crops(img: Image.Image) -> list[tuple[Image.Image, int, int]]:
    w, h = img.size
    presets = (
        (0.05, 0.24, 0.95, 0.76),
        (0.06, 0.28, 0.94, 0.74),
        (0.08, 0.30, 0.92, 0.72),
        (0.10, 0.32, 0.90, 0.70),
    )
    crops: list[tuple[Image.Image, int, int]] = []
    for x0r, y0r, x1r, y1r in presets:
        x0, y0 = int(w * x0r), int(h * y0r)
        x1, y1 = int(w * x1r), int(h * y1r)
        if x1 - x0 < 180 or y1 - y0 < 120:
            continue
        crop = img.crop((x0, y0, x1, y1))
        if _looks_like_geetest_modal(crop):
            crops.append((crop, x0, y0))
    if not crops:
        x0, y0 = int(w * 0.06), int(h * 0.28)
        crops.append((img.crop((x0, y0, int(w * 0.94), int(h * 0.74))), x0, y0))
    return crops


def _try_phrase_taps_on_crop(
    crop: Image.Image,
    offset_x: int,
    offset_y: int,
    tap_fn: Callable[[int, int], None],
    *,
    device_host: str,
) -> bool:
    if not _SOLVER_OK:
        return False
    candidates = _phrase_click_candidates_for_crop(crop, strict=True)
    if not candidates:
        candidates = _phrase_click_candidates_for_crop(crop, strict=False)
    for method, clicks in candidates[:6]:
        for order in _phrase_click_orders(clicks, method=method)[:8]:
            print(
                f"[app-captcha] phrase {method} clicks={order}",
                flush=True,
            )
            for cx, cy in order:
                tap_fn(offset_x + cx, offset_y + cy)
                time.sleep(0.42)
            tap_fn(offset_x + int(crop.size[0] * 0.82), offset_y + int(crop.size[1] * 0.94))
            time.sleep(1.2)
            cached = peek_rpc_captcha(device_host)
            if cached and cached.get("lot_number"):
                return True
    return False


def _try_nine_taps_on_crop(
    crop: Image.Image,
    offset_x: int,
    offset_y: int,
    tap_fn: Callable[[int, int], None],
    *,
    device_host: str,
) -> bool:
    if not _SOLVER_OK or not _PILLOW_OK:
        return False
    w, h = crop.size
    if w < 120 or h < 120:
        return False

    prompt_h = max(int(h * 0.22), 40)
    prompt = crop.crop((0, 0, w, prompt_h))
    grid = crop.crop((0, prompt_h, w, h))
    gw, gh = grid.size
    if gw < 90 or gh < 90:
        return False

    prompt_bytes = _image_to_png_bytes(prompt)
    grid_bytes = _image_to_png_bytes(grid)
    tiles = _split_nine_grid_image(grid_bytes)
    if len(tiles) != 9:
        return False

    ranked = _rank_nine_tiles(prompt_bytes, tiles)
    combos = _nine_pick_combos(
        prompt_bytes,
        tiles,
        select_count=2,
        top_k=8,
        max_combos=6,
    )
    combos = [
        combo
        for combo in combos
        if _nine_combo_min_score(ranked, combo) >= max(0.20, _MIN_NINE_TILE_SCORE)
    ]
    if not combos:
        combo = _find_nine_matching_indices(prompt_bytes, tiles, select_count=2)
        if combo and _nine_combo_min_score(ranked, combo) >= max(0.18, _MIN_NINE_TILE_SCORE - 0.05):
            combos = [combo]
    if not combos:
        return False

    cell_w, cell_h = gw // 3, gh // 3
    for pick in combos[:4]:
        print(f"[app-captcha] nine-grid pick={pick}", flush=True)
        for idx in pick:
            row, col = divmod(idx, 3)
            cx = offset_x + col * cell_w + cell_w // 2
            cy = offset_y + prompt_h + row * cell_h + cell_h // 2
            tap_fn(cx, cy)
            time.sleep(0.4)
        submit_x = offset_x + int(w * 0.82)
        submit_y = offset_y + int(h * 0.94)
        tap_fn(submit_x, submit_y)
        time.sleep(1.2)
        cached = peek_rpc_captcha(device_host)
        if cached and cached.get("lot_number"):
            return True
        _tap_geetest_refresh(crop, offset_x, offset_y)
        time.sleep(0.8)
    return False


def _attempt_screen_solve(
    tap_fn: Callable[[int, int], None],
    *,
    device_host: str,
    modal: tuple[Image.Image, int, int] | None = None,
) -> bool:
    if not _PILLOW_OK:
        return False
    if modal is None:
        modal = _screen_has_geetest_modal()
    if not modal:
        return False
    crop, ox, oy = modal
    if _try_phrase_taps_on_crop(crop, ox, oy, tap_fn, device_host=device_host):
        return True
    if _try_nine_taps_on_crop(crop, ox, oy, tap_fn, device_host=device_host):
        return True
    return False


def solve_captcha_on_device(
    device_host: str,
    *,
    timeout: float = 90.0,
    wake_app: bool = True,
    tap_interval: float = 3.0,
    group_id: str = "",
    sale_id: str = "",
    sale_link: str = "",
    collection_name: str = "",
) -> dict | None:
    """
    Fully automated sale-rush captcha on device:
    open purchase page → auto-tap buy → auto-solve GeeTest modal → RPC capture token.
    """
    if not _PILLOW_OK:
        print("[app-captcha] Pillow not installed; cannot auto-tap device screen", flush=True)
        return None

    clear_rpc_captcha(device_host)
    if wake_app:
        print("[app-captcha] waking iBox app…", flush=True)
        wake_ibox_app()
        time.sleep(1.2)
        if group_id:
            print(
                f"[app-captcha] opening purchase page group_id={group_id} "
                f"sale_id={sale_id or '-'} name={collection_name!r}…",
                flush=True,
            )
            if not navigate_to_sale_detail(
                collection_name,
                group_id=group_id,
                sale_id=sale_id,
                sale_link=sale_link,
                device_host=device_host,
            ):
                print(
                    f"[app-captcha] WARN: could not open buy-ready detail for {collection_name!r}",
                    flush=True,
                )
            _log_page_hints(collection_name)

    print("[app-captcha] 全自动：自动点击购买并识别验证码…", flush=True)

    deadline = time.monotonic() + max(timeout, 5.0)
    last_buy_tap_at = 0.0
    last_solve_at = 0.0
    buy_attempts = 0
    min_buy_before_solve = 2

    while time.monotonic() < deadline:
        cached = peek_rpc_captcha(device_host)
        if cached and cached.get("lot_number"):
            print(
                f"[app-captcha] captured from App ✓ lot_number={cached['lot_number'][:8]}…",
                flush=True,
            )
            return cached

        now = time.monotonic()
        if collection_name and (_page_on_home_page() or _page_on_sale_list_with_items()):
            if _navigate_via_home_card_grid(collection_name, max_passes=1) or _try_select_sale_from_list(
                collection_name
            ):
                last_buy_tap_at = 0.0
                buy_attempts = 0
                continue

        if not _detail_page_ready(collection_name):
            time.sleep(0.45)
            continue

        if buy_attempts < min_buy_before_solve or now - last_buy_tap_at >= 4.0:
            if buy_attempts < 12:
                try:
                    if adb_tap_buy_button():
                        buy_attempts += 1
                        time.sleep(2.0)
                        _log_page_hints(collection_name)
                except Exception as exc:
                    print(f"[app-captcha] buy tap error: {exc}", flush=True)
                last_buy_tap_at = now
                if buy_attempts < min_buy_before_solve:
                    continue

        modal = _screen_has_geetest_modal()
        if modal and buy_attempts >= min_buy_before_solve and now - last_solve_at >= 2.0:
            try:
                print("[app-captcha] geetest modal detected, solving…", flush=True)
                if _attempt_screen_solve(adb_tap, device_host=device_host, modal=modal):
                    cached = peek_rpc_captcha(device_host)
                    if cached and cached.get("lot_number"):
                        print(
                            f"[app-captcha] captured after auto-solve ✓ "
                            f"lot_number={cached['lot_number'][:8]}…",
                            flush=True,
                        )
                        return cached
                crop, ox, oy = modal
                _tap_geetest_refresh(crop, ox, oy)
            except Exception as exc:
                print(f"[app-captcha] screen solve error: {exc}", flush=True)
            last_solve_at = now

        time.sleep(0.45)

    _log_page_hints(collection_name)
    _save_debug_screenshot("timeout")
    return poll_captcha(device_host=device_host, timeout=3.0, clear_before=False)
