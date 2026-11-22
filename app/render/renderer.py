"""Draws the banner and badges onto a poster with Pillow. A port of the Emby plugin's SkiaSharp renderer."""
from __future__ import annotations

import io
import os
from functools import lru_cache
from typing import Callable, Optional

from PIL import Image, ImageDraw, ImageFont

from .spec import Badge, Spec

FONT_DIR = os.path.join(os.path.dirname(__file__), "fonts")
MARKER = b"posteroverlay"  # written into every JPEG we produce, so our own output is never taken for an original

IconLookup = Callable[[Optional[str]], Optional[Image.Image]]


@lru_cache(maxsize=64)
def font(size: int, wide: bool) -> ImageFont.FreeTypeFont:
    name = "DejaVuSans-Bold.ttf" if wide else "DejaVuSansCondensed-Bold.ttf"
    return ImageFont.truetype(os.path.join(FONT_DIR, name), max(6, size))


def cap_height(f: ImageFont.FreeTypeFont) -> float:
    box = f.getbbox("H", anchor="ls")
    return float(-box[1]) if box[1] < 0 else f.size * 0.7


def render(poster: Image.Image, spec: Spec, icons: IconLookup) -> Image.Image:
    img = poster.convert("RGBA")
    w, h = img.size
    if spec.empty or w < 50 or h < 50:
        return img
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)

    banner_top = banner_bottom = 0.0
    has_banner = bool(spec.banner_text)
    if has_banner:
        bh = round(h * spec.banner_height)
        banner_top = 0 if spec.banner_top else h - bh
        banner_bottom = banner_top + bh
        _banner(draw, (0, banner_top, w, banner_bottom), spec)

    if spec.badges:
        badges = Image.new("RGBA", img.size, (0, 0, 0, 0))
        area_top = banner_bottom if has_banner and spec.banner_top else 0
        area_bottom = banner_top if has_banner and not spec.banner_top else h
        _draw_badges(badges, w, h, spec, icons, has_banner, banner_top, banner_bottom)
        box = badges.getbbox()
        if box:
            pos = spec.badge_position
            if pos == "Auto":
                pos = choose_position(img, box, w, area_top, area_bottom)
            dx, dy = _offset(pos, box, w, area_top, area_bottom)
            if dx or dy:
                moved = Image.new("RGBA", img.size, (0, 0, 0, 0))
                moved.alpha_composite(badges.crop(box), (box[0] + dx, box[1] + dy))
                badges = moved
            layer.alpha_composite(badges)

    return Image.alpha_composite(img, layer)


POSITIONS = ["TopLeft", "TopRight", "MiddleLeft", "BottomLeft", "BottomRight"]


def _offset(pos: str, box, w: int, area_top: float, area_bottom: float):
    """Move badges drawn in the top-left corner to another spot, keeping the same gap to the edges."""
    x0, y0, x1, y1 = box
    gap_x, gap_y = x0, y0 - area_top
    dx = (w - gap_x) - x1 if pos in ("TopRight", "BottomRight") else 0
    if pos == "MiddleLeft":
        dy = (area_top + area_bottom) / 2 - (y0 + y1) / 2
    elif pos in ("BottomLeft", "BottomRight"):
        dy = (area_bottom - gap_y) - y1
    else:
        dy = 0
    return round(dx), round(dy)


def choose_position(img: Image.Image, box, w: int, area_top: float, area_bottom: float) -> str:
    """Top-left unless text (a title, a tagline) is behind it; then the spot with the least text."""
    from .textdetect import text_mask, text_share
    mask = text_mask(img)
    if mask is None:
        return "TopLeft"
    scores = {}
    for pos in POSITIONS:
        dx, dy = _offset(pos, box, w, area_top, area_bottom)
        scores[pos] = text_share(mask, (box[0] + dx, box[1] + dy, box[2] + dx, box[3] + dy))
    if scores["TopLeft"] < AUTO_CLEAR:
        return "TopLeft"
    return min(POSITIONS, key=lambda p: (round(scores[p], 2), POSITIONS.index(p)))


AUTO_CLEAR = 0.015    # top-left is kept while text covers less than this share of the badges


def _draw_badges(layer: Image.Image, w: int, h: int, spec: Spec, icons: IconLookup, has_banner: bool,
                 banner_top: float, banner_bottom: float) -> None:
    """Badges in the top-left corner (render() then moves them to the chosen spot)."""
    if spec.badges:
        margin = w * spec.badge_margin
        unit = max(14.0, h * spec.badge_height)
        y = banner_bottom + margin if has_banner and spec.banner_top else margin
        limit = banner_top - margin if has_banner and not spec.banner_top else h - margin
        if spec.column:
            area_top = banner_bottom if has_banner and spec.banner_top else 0
            area_bottom = banner_top if has_banner and not spec.banner_top else h
            _column(layer, w, margin, y, limit, area_top, area_bottom, unit, spec, icons)
        else:
            gap = unit * 0.22
            drawn = 0
            for badge in spec.badges:
                if drawn >= spec.max_badges or y + unit * max(1.0, spec.icon_scale) > limit:
                    break
                icon = icons(badge.icon) if spec.use_icons else None
                used = _pill(layer, margin, y, unit, w * 0.6, badge, spec, icon)
                y += used + gap
                drawn += 1


def _banner(draw: ImageDraw.ImageDraw, rect, spec: Spec) -> None:
    x0, y0, x1, y1 = rect
    draw.rectangle(rect, fill=spec.banner_color)
    if spec.banner_color[3] == 255:
        edge_y = y1 if spec.banner_top else y0
        draw.line([(x0, edge_y), (x1, edge_y)], fill=(0, 0, 0, 70), width=max(1, round((y1 - y0) * 0.04)))
    text = spec.banner_text.upper()
    height = y1 - y0
    size = height * (0.68 if spec.wide_font else 0.56)
    f = font(round(size), spec.wide_font)
    tracking = size * spec.banner_tracking
    width = _tracked(f, text, tracking)
    available = (x1 - x0) * 0.9
    if width > available:
        scale = available / width
        size *= scale
        f = font(max(6, round(size)), spec.wide_font)
        tracking *= scale
        width = _tracked(f, text, tracking)
    x = (x0 + x1) / 2 - width / 2
    baseline = (y0 + y1) / 2 + cap_height(f) / 2
    for ch in text:
        draw.text((x, baseline), ch, font=f, fill=spec.banner_text_color, anchor="ls")
        x += f.getlength(ch) + tracking


def _tracked(f, text: str, tracking: float) -> float:
    return sum(f.getlength(c) + tracking for c in text) - (tracking if text else 0)


def _scaled_icon(icon: Image.Image, height: float, max_width: float):
    ih = height
    iw = icon.width * (ih / icon.height)
    if iw > max_width > 0:
        ih *= max_width / iw
        iw = max_width
    return iw, ih


def _paste_icon(layer: Image.Image, icon: Image.Image, x: float, y: float, w: float, h: float) -> None:
    resized = icon.resize((max(1, round(w)), max(1, round(h))), Image.LANCZOS)
    layer.alpha_composite(resized, (round(x), round(y)))


def _pill(layer: Image.Image, x: float, y: float, h: float, max_width: float, badge: Badge, spec: Spec,
          icon: Optional[Image.Image]) -> float:
    draw = ImageDraw.Draw(layer)
    pad = h * 0.2
    inner = h - 2 * pad
    f = font(round(h * 0.56), spec.wide_font)
    iw = ih = 0.0
    if icon is not None:
        ih = (inner * 0.92 if badge.value is None else inner * 1.08) * spec.icon_scale
        icon_max = max_width - 2 * pad - ((f.getlength(badge.value) + pad) if badge.value else 0)
        iw, ih = _scaled_icon(icon, ih, icon_max)
    label = badge.label if icon is None else None
    label_w = f.getlength(label) if label else 0
    value_w = f.getlength(badge.value) if badge.value else 0
    spacing = pad * 0.8
    content = iw + label_w + value_w + (spacing if (iw + label_w) > 0 and value_w > 0 else 0)
    bw = min(max_width, content + 2 * pad)
    pill_h = max(h, ih + pad)
    _rounded(layer, (x, y, x + bw, y + pill_h), h * 0.22, spec.badge_bg)
    cx = x + pad
    mid = y + pill_h / 2
    baseline = mid + cap_height(f) / 2
    if icon is not None:
        _paste_icon(layer, icon, cx, mid - ih / 2, iw, ih)
        cx += iw
    elif label:
        draw.text((cx, baseline), label, font=f, fill=badge.label_color or (255, 255, 255, 255), anchor="ls")
        cx += label_w
    if badge.value:
        cx += spacing if (iw + label_w) > 0 else 0
        draw.text((cx, baseline), badge.value, font=f, fill=(255, 255, 255, 255), anchor="ls")
    return pill_h


def _column(layer: Image.Image, width: int, margin: float, top: float, limit: float, area_top: float,
            area_bottom: float, unit: float, spec: Spec, icons: IconLookup) -> None:
    pad, item_gap, value_gap = unit * 0.28, unit * 0.32, unit * 0.1
    max_inner = width * 0.2
    f = font(round(unit * 0.62), spec.wide_font)
    cap = cap_height(f)
    items = []
    used = 2 * pad
    for badge in spec.badges:
        if len(items) >= spec.max_badges:
            break
        icon = icons(badge.icon) if spec.use_icons else None
        iw = ih = 0.0
        if icon is not None:
            iw, ih = _scaled_icon(icon, unit * (0.58 if badge.value else 0.62) * spec.icon_scale, max_inner)
            head_w, head_h = iw, ih
        else:
            head_w, head_h = min(max_inner, f.getlength(badge.label or "")), cap
        value_w = f.getlength(badge.value) if badge.value else 0
        hgt = head_h + (value_gap + cap if badge.value else 0)
        extra = hgt + (item_gap if items else 0)
        if top + used + extra > limit:
            break
        used += extra
        items.append((badge, icon, iw, ih, max(head_w, value_w)))
    if not items:
        return
    inner_w = max(i[4] for i in items)
    panel = (margin, top, margin + inner_w + 2 * pad, top + used)
    radius = unit * 0.3
    draw_box = [panel[0] - (radius if margin < 0.5 else 0), panel[1] - (radius if top - area_top < 0.5 else 0),
                panel[2], panel[3]]
    # Edges touching the poster edge or the banner were pushed outside the visible area; clip so they stay square.
    _rounded(layer, draw_box, radius, spec.badge_bg, clip=(0, area_top, width, area_bottom))

    draw = ImageDraw.Draw(layer)
    y = top + pad
    cx = (panel[0] + panel[2]) / 2
    for badge, icon, iw, ih, _ in items:
        if icon is not None:
            _paste_icon(layer, icon, cx - iw / 2, y, iw, ih)
            y += ih
        else:
            label = badge.label or ""
            draw.text((cx - f.getlength(label) / 2, y + cap), label, font=f,
                      fill=badge.label_color or (255, 255, 255, 255), anchor="ls")
            y += cap
        if badge.value:
            y += value_gap
            draw.text((cx - f.getlength(badge.value) / 2, y + cap), badge.value, font=f, fill=(255, 255, 255, 255),
                      anchor="ls")
            y += cap
        y += item_gap


def _rounded(layer: Image.Image, box, radius: float, fill, clip=None) -> None:
    """Rounded rectangle with a translucent fill, blended onto the layer (ImageDraw alone would overwrite).
    Drawn on a canvas just big enough for the box; clip limits it to (x0, y0, x1, y1)."""
    x0, y0, x1, y1 = box
    cx0, cy0, cx1, cy1 = clip or (0, 0, layer.width, layer.height)
    ox, oy = int(x0) - 1, int(y0) - 1
    w, h = int(x1 - ox) + 2, int(y1 - oy) + 2
    if w <= 0 or h <= 0:
        return
    tmp = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(tmp).rounded_rectangle([x0 - ox, y0 - oy, x1 - ox, y1 - oy], radius=max(0, radius), fill=fill)
    # Visible part, in layer coordinates.
    vx0, vy0 = max(ox, int(round(cx0)), 0), max(oy, int(round(cy0)), 0)
    vx1, vy1 = min(ox + w, int(round(cx1)), layer.width), min(oy + h, int(round(cy1)), layer.height)
    if vx1 <= vx0 or vy1 <= vy0:
        return
    part = tmp.crop((vx0 - ox, vy0 - oy, vx1 - ox, vy1 - oy))
    layer.alpha_composite(part, (vx0, vy0))


def to_jpeg(img: Image.Image, key: str, quality: int = 92) -> bytes:
    """JPEG with a comment marking it as ours (and what was drawn)."""
    out = io.BytesIO()
    img.convert("RGB").save(out, "JPEG", quality=quality, optimize=True,
                            comment=MARKER + b":" + key.encode("utf-8", "replace")[:200])
    return out.getvalue()


def is_ours(image: bytes) -> bool:
    """True when the image was written by this app (its JPEG comment carries our marker)."""
    head = image[:65536]
    return MARKER + b":" in head


def open_image(data: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(data))
    img.load()
    return img
