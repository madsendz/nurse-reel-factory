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
    """Download the (free, OFL) Poppins font the first time it's needed."""
    if not path.exists():
        import requests
        path.parent.mkdir(parents=True, exist_ok=True)
        r = requests.get(FONT_URL, timeout=60)
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


def layout_blocks(texts: list[str], zone_px: tuple[int, int, int, int], cfg: dict, font_path: Path):
    """Find font sizes so hook + all beats fit inside the zone. Returns list of blocks."""
    r = cfg["render"]
    zx, zy, zw, zh = zone_px
    max_bottom = int(r["height"] * r.get("safe_bottom", 0.78))
    scratch = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    # First try to fit inside the zone at a readable size; otherwise let it grow
    # down toward the safe bottom edge; finally shrink as far as needed.
    room = max_bottom - zy
    attempts = [(min(zh, room), s / 100) for s in range(100, 71, -4)] + \
               [(room, s / 100) for s in range(100, 39, -4)]
    for avail_h, scale in attempts:
        hook_size, beat_size = int(70 * scale), int(52 * scale)
        pad, gap = int(26 * scale), int(18 * scale)
        blocks, total = [], 0
        for i, t in enumerate(texts):
            size = hook_size if i == 0 else beat_size
            font = ImageFont.truetype(str(font_path), size)
            t = clean_text(t, font)
            lines = _wrap(scratch, t, font, zw - 2 * pad)
            line_h = int(size * 1.22)
            bw = int(max(scratch.textlength(ln, font=font) for ln in lines)) + 2 * pad
            bh = line_h * len(lines) + 2 * pad - int(size * 0.18)
            blocks.append({"text": t, "lines": lines, "font": font, "size": size, "pad": pad,
                           "line_h": line_h, "w": min(bw, zw), "h": bh, "hook": i == 0})
            total += bh + (gap if i else 0)
        if total <= avail_h:
            break
    y = zy
    for i, b in enumerate(blocks):
        b["x"] = zx + (zw - b["w"]) // 2
        b["y"] = y
        y += b["h"] + gap
    return blocks


def draw_frame(blocks: list[dict], upto: int, cfg: dict) -> Image.Image:
    r = cfg["render"]
    img = Image.new("RGBA", (r["width"], r["height"]), (0, 0, 0, 0))
    shadow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    d = ImageDraw.Draw(img)
    for b in blocks[: upto + 1]:
        radius = int(b["size"] * 0.45)
        box = [b["x"], b["y"], b["x"] + b["w"], b["y"] + b["h"]]
        sd.rounded_rectangle([box[0] + 4, box[1] + 6, box[2] + 4, box[3] + 6], radius, fill=(0, 0, 0, 70))
        fill = _hex(r["accent_color"]) if b["hook"] else _hex(r["box_color"], r.get("box_opacity", 0.94))
        color = _hex(r["hook_text_color"]) if b["hook"] else _hex(r["text_color"])
        d.rounded_rectangle(box, radius, fill=fill)
        ty = b["y"] + b["pad"] - int(b["size"] * 0.12)
        for ln in b["lines"]:
            lw = d.textlength(ln, font=b["font"])
            d.text((b["x"] + (b["w"] - lw) / 2, ty), ln, font=b["font"], fill=color)
            ty += b["line_h"]
    return Image.alpha_composite(shadow, img)


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

    texts = [soften(item["hook"], cfg)] + [soften(b, cfg) for b in item.get("beats", [])]
    starts, total = plan_timing(len(texts) - 1, usable, cfg)
    blocks = layout_blocks(texts, zone_pixels(clip, cfg), cfg, font_path)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        pngs = []
        for k in range(len(blocks)):
            p = Path(td) / f"stage{k}.png"
            draw_frame(blocks, k, cfg).save(p)
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
        draw_frame(blocks, len(blocks) - 1, cfg).save(out_path.with_suffix(".overlay.png"))
    return {"seconds": total, "stages": starts, "looped": loop}


def render_thumbnail(reel_path: Path, at: float, out_png: Path) -> None:
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{at}", "-i", str(reel_path), "-frames:v", "1",
                    str(out_png)], check=True)
