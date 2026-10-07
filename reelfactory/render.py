"""Turn a pointing clip + a topic into a finished 9:16 reel.

Layout: a colored hook box appears first, then each fact line pops in
underneath it, stacked inside the clip's "zone" (where Lexi points).
"""
from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .config import ROOT

# Characters some fonts lack -> safe replacements
_FALLBACK_CHARS = {"≠": "isn't", "→": "->", "…": "...", "–": "-", "—": "-", "’": "'", "“": '"', "”": '"'}


FONT_URL = "https://raw.githubusercontent.com/google/fonts/main/ofl/poppins/Poppins-Bold.ttf"


def ensure_font(path: Path) -> Path:
    """Download the free (OFL) fonts the first time they're needed."""
    if not path.exists():
        import requests
        path.parent.mkdir(parents=True, exist_ok=True)
        url = FONT_URLS.get(path.name, FONT_URL) if "FONT_URLS" in globals() else FONT_URL
        r = requests.get(url, timeout=60)
        r.raise_for_status()
        path.write_bytes(r.content)
    return path


def probe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    info = json.loads(out)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    has_audio = any(s["codec_type"] == "audio" for s in info["streams"])
    w, h = int(v["width"]), int(v["height"])
    rot = 0
    for sd in v.get("side_data_list", []) or []:
        if "rotation" in sd:
            rot = int(float(sd["rotation"]))
    if "rotate" in (v.get("tags") or {}):
        rot = int(v["tags"]["rotate"])
    if abs(rot) % 180 == 90:
        w, h = h, w
    return {"width": w, "height": h, "duration": float(info["format"]["duration"]), "has_audio": has_audio}


def soften(text: str, cfg: dict) -> str:
    r = cfg["render"]
    if not r.get("soften_words"):
        return text
    for word, repl in (r.get("soften_map") or {}).items():
        text = re.sub(rf"\b{re.escape(word)}\b", repl, text, flags=re.IGNORECASE)
    return text


def _font_supports(font: ImageFont.FreeTypeFont, ch: str) -> bool:
    try:
        return font.getmask(ch).getbbox() is not None or ch.isspace()
    except Exception:
        return False


def clean_text(text: str, font: ImageFont.FreeTypeFont) -> str:
    out = []
    for ch in text:
        if ch in _FALLBACK_CHARS and not _font_supports(font, ch):
            out.append(_FALLBACK_CHARS[ch])
        else:
            out.append(ch)
    return "".join(out)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_w: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if draw.textlength(trial, font=font) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _hex(c: str, alpha: float = 1.0) -> tuple:
    c = c.lstrip("#")
    return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16), int(255 * alpha))


# ───────────────────────── title (hook) styling ─────────────────────────
FONT_URLS = {
    "Poppins-Bold.ttf": "https://raw.githubusercontent.com/google/fonts/main/ofl/poppins/Poppins-Bold.ttf",
    "InstrumentSerif-Regular.ttf":
        "https://raw.githubusercontent.com/google/fonts/main/ofl/instrumentserif/InstrumentSerif-Regular.ttf",
}
EMOJI_FONTS = ["/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf", "/usr/share/fonts/noto/NotoColorEmoji.ttf"]


def _is_emoji(ch: str) -> bool:
    o = ord(ch)
    return o >= 0x1F000 or 0x2600 <= o <= 0x27BF or o in (0xFE0F, 0x200D) or 0x1F1E6 <= o <= 0x1F1FF


def style_case(text: str, mode: str) -> str:
    """lower_keep_caps: 'Is a curve NORMAL?' -> 'is a curve NORMAL?' (keeps ALL-CAPS words for emphasis)."""
    if mode != "lower_keep_caps":
        return text
    out = []
    for w in text.split(" "):
        letters = [c for c in w if c.isalpha()]
        keep = sum(c.isupper() for c in letters) >= 2  # ALL-CAPS + acronyms like STIs, PrEP
        out.append(w if keep else w.lower())
    return " ".join(out)


def _runs(text: str):
    """Split into [(is_emoji, chunk)] runs."""
    runs = []
    for ch in text:
        e = _is_emoji(ch)
        if runs and runs[-1][0] == e:
            runs[-1][1] += ch
        else:
            runs.append([e, ch])
    return runs


class TitleStyle:
    def __init__(self, cfg: dict, size: int):
        r = cfg["render"]
        self.size = size
        path = ROOT / r.get("hook_font", "assets/fonts/InstrumentSerif-Regular.ttf")
        self.font = ImageFont.truetype(str(ensure_font(path)), size)
        self.color = _hex(r.get("hook_color", "#FFFFFF"))
        self.outline = max(1, int(size * r.get("hook_outline", 0.075)))
        off = r.get("hook_offset", [0.05, 0.07])
        self.offset = (int(size * off[0]), int(size * off[1]))
        self.shadow = _hex(r.get("hook_outline_color", "#000000"))
        self.emoji_font = None
        for f in EMOJI_FONTS:
            if Path(f).exists():
                try:
                    self.emoji_font = ImageFont.truetype(f, 109)  # Noto Color Emoji only renders at 109px
                except OSError:
                    pass
                break

    def _emoji_img(self, chunk: str):
        if not self.emoji_font:
            return None
        tmp = Image.new("RGBA", (140 * len(chunk), 140), (0, 0, 0, 0))
        ImageDraw.Draw(tmp).text((0, 0), chunk, font=self.emoji_font, embedded_color=True)
        bbox = tmp.getbbox()
        if not bbox:
            return None
        tmp = tmp.crop(bbox)
        h = int(self.size * 0.82)
        return tmp.resize((max(1, int(tmp.width * h / tmp.height)), h), Image.LANCZOS)

    def width(self, line: str, d: ImageDraw.ImageDraw) -> int:
        w = 0
        for is_e, chunk in _runs(line):
            if is_e:
                im = self._emoji_img(chunk.replace("\ufe0f", ""))
                w += (im.width + int(self.size * 0.08)) if im else 0
            else:
                w += int(d.textlength(chunk, font=self.font))
        return w + self.outline * 2

    def draw_line(self, img: Image.Image, line: str, cx: int, y: int) -> None:
        d = ImageDraw.Draw(img)
        x = cx - self.width(line, d) // 2 + self.outline
        for is_e, chunk in _runs(line):
            if is_e:
                im = self._emoji_img(chunk.replace("\ufe0f", ""))
                if im:
                    img.alpha_composite(im, (x + int(self.size * 0.04), y + int(self.size * 0.12)))
                    x += im.width + int(self.size * 0.08)
                continue
            ox, oy = self.offset
            d.text((x + ox, y + oy), chunk, font=self.font, fill=self.shadow,
                   stroke_width=self.outline, stroke_fill=self.shadow)  # block offset
            d.text((x, y), chunk, font=self.font, fill=self.color,
                   stroke_width=self.outline, stroke_fill=self.shadow)  # outlined text
            x += int(d.textlength(chunk, font=self.font))


def _wrap_title(style: TitleStyle, text: str, max_w: int) -> list[str]:
    d = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if style.width(trial, d) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def layout_title(text: str, clip: dict, cfg: dict) -> dict:
    """Title is centered horizontally; vertically centered on the frame (hook_center_y),
    pushed below the face if it would cover it."""
    r = cfg["render"]
    W, H = r["width"], r["height"]
    max_w = int(W * r.get("hook_width", 0.86))
    for size in range(int(r.get("hook_size", 104)), 47, -6):
        style = TitleStyle(cfg, size)
        lines = _wrap_title(style, text, max_w)
        if len(lines) <= 4:
            break
    line_h = int(style.size * r.get("hook_line_height", 0.98))
    h = line_h * len(lines) + style.offset[1] + style.outline
    cy = r.get("hook_center_y", 0.5) * H
    y = int(cy - h / 2)
    face = (clip.get("auto") or {}).get("face")
    if face:
        f_top, f_bot = face[1] * H, (face[1] + face[3]) * H
        if y < f_bot and y + h > f_top:  # overlaps the face -> move just below it
            y = int(f_bot + 0.015 * H)
    y = max(int(0.12 * H), min(y, int(r.get("safe_bottom", 0.78) * H) - h))
    return {"style": style, "lines": lines, "line_h": line_h, "y": y, "h": h}


def draw_title(img: Image.Image, title: dict, cfg: dict) -> None:
    cx = cfg["render"]["width"] // 2
    y = title["y"]
    for ln in title["lines"]:
        title["style"].draw_line(img, ln, cx, y)
        y += title["line_h"]


# ───────────────────────── subtitles (facts) ─────────────────────────
def layout_blocks(texts: list[str], zone_px: tuple[int, int, int, int], cfg: dict, font_path: Path,
                  title: dict | None = None):
    """Fact boxes, stacked where Lexi points (the clip's zone), never overlapping the title."""
    r = cfg["render"]
    H = r["height"]
    zx, zy, zw, zh = zone_px
    top_min = int(H * r.get("sub_top_min", 0.16))
    max_bottom = int(H * r.get("safe_bottom", 0.78))
    zy = max(zy, top_min)
    scratch = ImageDraw.Draw(Image.new("RGBA", (10, 10)))

    def measure(scale):
        size = int(r.get("sub_size", 50) * scale)
        pad, gap = int(24 * scale), int(16 * scale)
        font = ImageFont.truetype(str(font_path), size)
        blocks, total = [], 0
        for i, t in enumerate(texts):
            t = clean_text(t, font)
            lines = _wrap(scratch, t, font, zw - 2 * pad)
            line_h = int(size * 1.22)
            bw = int(max(scratch.textlength(ln, font=font) for ln in lines)) + 2 * pad
            bh = line_h * len(lines) + 2 * pad - int(size * 0.18)
            blocks.append({"text": t, "lines": lines, "font": font, "size": size, "pad": pad,
                           "line_h": line_h, "w": min(bw, zw), "h": bh, "hook": False})
            total += bh + (gap if i else 0)
        return blocks, total, gap

    # free vertical spans (outside the title)
    spans = [(top_min, max_bottom)]
    if title:
        t0, t1 = title["y"] - int(0.015 * H), title["y"] + title["h"] + int(0.015 * H)
        spans = [(top_min, max(top_min, t0)), (min(max_bottom, t1), max_bottom)]
    zone_mid = zy + zh / 2
    # prefer the span containing where she points, else the closest, else the biggest
    spans_sorted = sorted(spans, key=lambda s: (not (s[0] <= zone_mid <= s[1]),
                                                min(abs(zone_mid - s[0]), abs(zone_mid - s[1])), -(s[1] - s[0])))
    for s0, s1 in spans_sorted + [max(spans, key=lambda s: s[1] - s[0])]:
        for scale in [x / 100 for x in range(100, 59, -5)]:
            blocks, total, gap = measure(scale)
            if total <= s1 - s0:
                y = int(min(max(zy, s0), s1 - total))
                for b in blocks:
                    b["x"] = zx + (zw - b["w"]) // 2
                    b["y"] = y
                    y += b["h"] + gap
                return blocks
    blocks, total, gap = measure(0.6)
    y = max(top_min, max_bottom - total)
    for b in blocks:
        b["x"], b["y"] = zx + (zw - b["w"]) // 2, y
        y += b["h"] + gap
    return blocks


def draw_frame(blocks: list[dict], upto: int, cfg: dict, title: dict | None = None) -> Image.Image:
    """Stage 0 = title only; stage k adds fact k."""
    r = cfg["render"]
    img = Image.new("RGBA", (r["width"], r["height"]), (0, 0, 0, 0))
    shadow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    d = ImageDraw.Draw(img)
    for b in blocks[: upto]:
        radius = int(b["size"] * 0.45)
        box = [b["x"], b["y"], b["x"] + b["w"], b["y"] + b["h"]]
        sd.rounded_rectangle([box[0] + 4, box[1] + 6, box[2] + 4, box[3] + 6], radius, fill=(0, 0, 0, 70))
        d.rounded_rectangle(box, radius, fill=_hex(r["box_color"], r.get("box_opacity", 0.94)))
        ty = b["y"] + b["pad"] - int(b["size"] * 0.12)
        for ln in b["lines"]:
            lw = d.textlength(ln, font=b["font"])
            d.text((b["x"] + (b["w"] - lw) / 2, ty), ln, font=b["font"], fill=_hex(r["text_color"]))
            ty += b["line_h"]
    out = Image.alpha_composite(shadow, img)
    if title:
        draw_title(out, title, cfg)
    return out


def zone_pixels(clip: dict, cfg: dict) -> tuple[int, int, int, int]:
    r = cfg["render"]
    z = clip.get("zone") or {"x": 0.08, "y": 0.14, "w": 0.84, "h": 0.40}
    return (int(z["x"] * r["width"]), int(z["y"] * r["height"]), int(z["w"] * r["width"]), int(z["h"] * r["height"]))


def plan_timing(n_beats: int, clip_len: float, cfg: dict) -> tuple[list[float], float]:
    """Return start time of each text stage (hook=0) and total reel length."""
    r = cfg["render"]
    spb = r.get("seconds_per_beat", 2.6)
    hook_hold = spb * 0.9
    starts = [0.0] + [hook_hold + i * spb for i in range(n_beats)]
    needed = starts[-1] + spb + 1.2  # hold the full card a bit at the end
    total = max(r.get("min_seconds", 6), min(r.get("max_seconds", 30), max(needed, min(clip_len, needed + 2))))
    return starts, round(total, 2)


def render_reel(item: dict, clip: dict, clip_path: Path, out_path: Path, cfg: dict) -> dict:
    r = cfg["render"]
    font_path = ensure_font(ROOT / r["font"])
    info = probe(clip_path)
    start = float(clip.get("start") or 0)
    end = float(clip.get("end") or info["duration"])
    usable = max(0.5, end - start)

    hook = style_case(soften(item["hook"], cfg), r.get("hook_case", "natural"))
    if r.get("hook_emoji") and not any(_is_emoji(c) for c in hook):
        hook = f"{hook} {r['hook_emoji']}"
    beats = [soften(b, cfg) for b in item.get("beats", [])]
    starts, total = plan_timing(len(beats), usable, cfg)
    title = layout_title(hook, clip, cfg)
    blocks = layout_blocks(beats, zone_pixels(clip, cfg), cfg, font_path, title=title)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        pngs = []
        for k in range(len(blocks) + 1):
            p = Path(td) / f"stage{k}.png"
            draw_frame(blocks, k, cfg, title).save(p)
            pngs.append(p)

        W, H, fps = r["width"], r["height"], r.get("fps", 30)
        loop = usable < total
        cmd = ["ffmpeg", "-y", "-loglevel", "error"]
        if loop:
            cmd += ["-stream_loop", "-1"]
        cmd += ["-ss", f"{start}", "-i", str(clip_path)]
        for p in pngs:
            cmd += ["-i", str(p)]
        chains = [f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps={fps},setsar=1[v0]"]
        for k in range(len(pngs)):
            t0 = starts[k]
            t1 = starts[k + 1] if k + 1 < len(pngs) else total + 1
            chains.append(f"[v{k}][{k + 1}:v]overlay=0:0:enable='between(t,{t0:.2f},{t1:.2f})'[v{k + 1}]")
        filt = ";".join(chains)
        cmd += ["-filter_complex", filt, "-map", f"[v{len(pngs)}]"]
        if info["has_audio"] and r.get("audio", "keep") == "keep":
            cmd += ["-map", "0:a:0", "-c:a", "aac", "-b:a", "128k", "-ar", "44100"]
        else:
            cmd += ["-an"]
        cmd += ["-t", f"{total}", "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
                "-profile:v", "high", "-movflags", "+faststart", str(out_path)]
        subprocess.run(cmd, check=True)
        # keep a thumbnail of the full card for previews
        draw_frame(blocks, len(blocks), cfg, title).save(out_path.with_suffix(".overlay.png"))
    return {"seconds": total, "stages": starts, "looped": loop}


def render_thumbnail(reel_path: Path, at: float, out_png: Path) -> None:
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{at}", "-i", str(reel_path), "-frames:v", "1",
                    str(out_png)], check=True)
