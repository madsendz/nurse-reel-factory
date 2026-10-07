"""Payoff text boxes that pop up exactly when Lexi points.

Each clip has a list of pointing moments ("events": time + where her hand is).
A reel plays the clip ONCE (no looping). The topic's facts are grouped into as
many boxes as she has pointing moments, each box appears on its moment, next to
her hand, and never on top of her face or the title. Clips with no pointing get
their last payoff 1-2 seconds before the end so people watch to the end.
"""
from __future__ import annotations

from statistics import median

from PIL import Image, ImageDraw, ImageFont


# ───────────────────────── pointing detection (pure, testable) ─────────────────────────
def _hands(lm):
    for name, w, i in (("L", 15, 19), ("R", 16, 20)):
        W, I = lm[w], lm[i]
        if min(W[2], I[2]) >= 0.35 and -0.05 < I[0] < 1.05 and -0.05 < I[1] < 1.08:
            yield name, W, I


def _rel(lm, P):
    """Point relative to the shoulders, in shoulder widths (cancels camera movement)."""
    cx, cy = (lm[11][0] + lm[12][0]) / 2, (lm[11][1] + lm[12][1]) / 2
    sw = max(abs(lm[11][0] - lm[12][0]), 0.05)
    return ((P[0] - cx) / sw, (P[1] - cy) / sw)


def face_box(lm) -> list[float] | None:
    """Face box (x, y, w, h normalized) from the pose's eye/ear/mouth points."""
    if min(lm[0][2], lm[2][2], lm[5][2]) < 0.4:
        return None
    pts = [lm[i] for i in range(11)]
    xs = [p[0] for p in pts]
    w = max(max(xs) - min(xs), 0.04)
    eye_y = (lm[2][1] + lm[5][1]) / 2
    mouth_y = (lm[9][1] + lm[10][1]) / 2
    em = max(mouth_y - eye_y, w * 0.25)
    top = eye_y - em * 1.3           # forehead (hair may be covered)
    bottom = mouth_y + em * 1.1      # chin
    x0 = min(xs) - w * 0.2
    return [round(x0, 3), round(top, 3), round(w * 1.4, 3), round(bottom - top, 3)]


def events_from_landmarks(rows, min_hold=0.5, merge_gap=0.45, move=0.5, max_events=4):
    """rows: [(t, landmarks|None)], landmarks = 33 BlazePose points [x, y, visibility] (frame-normalized,
    same crop as the finished reel). Returns pointing moments [{t, end, dir, x, y}]."""
    good = [(t, lm) for t, lm in rows if lm]
    if len(good) < 4:
        return []
    first = good[0][0]
    rest = {}
    for t, lm in [g for g in good if g[0] <= first + 0.3] or good[:2]:
        for h, W, I in _hands(lm):
            rest.setdefault(h, []).append(_rel(lm, I))
    rest = {h: (median(p[0] for p in v), median(p[1] for p in v)) for h, v in rest.items()}
    labels = []
    for t, lm in good:
        sh = (lm[11][1] + lm[12][1]) / 2
        hip = (lm[23][1] + lm[24][1]) / 2
        nose = lm[0]
        ups, lows = [], []
        for h, W, I in _hands(lm):
            r = rest.get(h)
            p = _rel(lm, I)
            dev = ((p[0] - r[0]) ** 2 + (p[1] - r[1]) ** 2) ** 0.5 if r else 0
            if I[1] < sh - 0.06 and (I[1] < nose[1] + 0.03 or abs(I[0] - nose[0]) > 0.12):
                ups.append((sh - I[1], I))
            elif dev > move and W[2] >= 0.5:
                lows.append((dev, I))
        if ups:
            lab, I = "up", max(ups)[1]
        elif len(lows) == 2:
            lab, I = "center", [(lows[0][1][0] + lows[1][1][0]) / 2, (lows[0][1][1] + lows[1][1][1]) / 2]
        elif lows:
            I = lows[0][1]
            lab = "down" if I[1] > (sh + hip) / 2 else "center"
        else:
            lab, I = None, None
        labels.append((t, lab, I))
    segs = []
    for t, lab, I in labels:
        if segs and segs[-1]["dir"] == lab and t - segs[-1]["end"] <= merge_gap:
            segs[-1]["end"] = t
            segs[-1]["pts"].append(I)
        elif lab:
            segs.append({"dir": lab, "t": t, "end": t, "pts": [I]})
    out = []
    for s in segs:
        if s["end"] - s["t"] < min_hold:
            continue
        out.append({"t": round(s["t"], 2), "end": round(s["end"], 2), "dir": s["dir"],
                    "x": round(median(p[0] for p in s["pts"]), 3), "y": round(median(p[1] for p in s["pts"]), 3)})
    if len(out) > max_events:  # keep the longest holds
        out = sorted(sorted(out, key=lambda e: e["end"] - e["t"], reverse=True)[:max_events], key=lambda e: e["t"])
    return out


def face_track_from_landmarks(rows, step=0.5) -> list[list[float]]:
    """[[t, x, y, w, h], ...] every `step` seconds."""
    out, nxt = [], -1.0
    for t, lm in rows:
        if lm and t >= nxt:
            f = face_box(lm)
            if f:
                out.append([round(t, 2)] + f)
                nxt = t + step
    return out


# ───────────────────────── planning ─────────────────────────
def clip_events(clip: dict) -> list[dict]:
    """Manual events (set by a human) win over auto-detected ones."""
    ev = clip.get("events_manual")
    if ev is None:
        ev = (clip.get("auto") or {}).get("events") or []
    return sorted(ev, key=lambda e: float(e["t"]))


def slots_for(clip: dict) -> int:
    """How many payoff boxes this clip wants (0 = no pointing, flexible)."""
    return len(clip_events(clip))


def group_beats(beats: list[str], k: int) -> list[list[str]]:
    """Split beats into k consecutive groups, earlier groups get the extras: 5->3 = [2,2,1]."""
    k = max(1, min(k, len(beats)))
    base, extra = divmod(len(beats), k)
    out, i = [], 0
    for g in range(k):
        n = base + (1 if g < extra else 0)
        out.append(beats[i:i + n])
        i += n
    return out


def plan(beats: list[str], clip: dict, duration: float, cfg: dict) -> list[dict]:
    """Return payoff boxes: [{lines: [beat, ...], t: show_time, event: {...}|None}]."""
    r = cfg["render"]
    if not beats:
        return []
    events = clip_events(clip)
    events = [e for e in events if float(e["t"]) < duration - 0.3]
    if events:
        groups = group_beats(beats, len(events))
        use = events[-len(groups):]  # fewer facts than points -> use the LAST points (payoff late)
        return [{"lines": g, "t": round(float(e["t"]) + r.get("point_lead", 0.1), 2), "event": e}
                for g, e in zip(groups, use)]
    # no pointing: last payoff 1-2 s before the end, the rest spread out after the title has been read
    groups = group_beats(beats, min(len(beats), r.get("max_boxes_no_point", 3)))
    last = max(1.0, duration - r.get("payoff_before_end", 1.5))
    first = min(r.get("title_read_seconds", 2.5), last)
    n = len(groups)
    times = [last] if n == 1 else [first + (last - first) * i / (n - 1) for i in range(n)]
    return [{"lines": g, "t": round(t, 2), "event": None} for g, t in zip(groups, times)]


# ───────────────────────── placement ─────────────────────────
def _overlap(a, b) -> float:
    """overlap area of two (x0, y0, x1, y1) boxes."""
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return w * h if w > 0 and h > 0 else 0.0


def faces_between(clip: dict, t0: float, t1: float, W: int, H: int, pad: float = 0.02) -> list[tuple]:
    """Face boxes (pixels) seen while a box is on screen."""
    track = (clip.get("auto") or {}).get("face_track") or []
    boxes = [f[1:] for f in track if t0 - 0.5 <= f[0] <= t1 + 0.25]
    if not boxes:
        f = (clip.get("auto") or {}).get("face")
        boxes = [f] if f else []
    if len(boxes) >= 3:  # ignore odd frames (camera swings) where the face suddenly fills the screen
        areas = sorted(b[2] * b[3] for b in boxes)
        med = areas[len(areas) // 2]
        boxes = [b for b in boxes if b[2] * b[3] <= med * 1.8]
    out = []
    for x, y, w, h in boxes:
        out.append(((x - pad) * W, (y - pad) * H, (x + w + pad) * W, (y + h + pad) * H))
    return out


def measure_box(lines: list[str], font_path, size: int, max_w: int):
    scratch = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    from .render import _wrap, clean_text
    font = ImageFont.truetype(str(font_path), size)
    pad = int(size * 0.48)
    line_h = int(size * 1.22)
    wrapped = []
    for i, b in enumerate(lines):
        if i:
            wrapped.append("")  # small gap between grouped facts
        wrapped += _wrap(scratch, clean_text(b, font), font, max_w - 2 * pad)
    widths = [scratch.textlength(ln, font=font) for ln in wrapped if ln]
    w = int(max(widths)) + 2 * pad
    gaps = sum(1 for ln in wrapped if not ln)
    h = line_h * (len(wrapped) - gaps) + int(line_h * 0.45) * gaps + 2 * pad - int(size * 0.18)
    return {"lines": wrapped, "font": font, "size": size, "pad": pad, "line_h": line_h, "w": w, "h": h}


def place(payoffs: list[dict], clip: dict, duration: float, cfg: dict, font_path, title: dict | None) -> list[dict]:
    """Give each payoff box an (x, y): near her hand, off her face, off the title and earlier boxes."""
    r = cfg["render"]
    W, H = r["width"], r["height"]
    top_min, bottom_max = int(H * r.get("payoff_top_min", 0.10)), int(H * r.get("safe_bottom", 0.80))
    side = int(W * 0.03)
    taken = []
    if title:
        taken.append((W * 0.05, title["y"] - 0.01 * H, W * 0.95, title["y"] + title["h"] + 0.01 * H))
    out = []
    base = r.get("sub_size", 50)
    for p in payoffs:
        ev = p["event"]
        faces = faces_between(clip, p["t"], duration, W, H)
        if ev:
            ax, ay = float(ev["x"]) * W, float(ev["y"]) * H
            if ev.get("dir") == "up":
                ay -= 0.07 * H  # text sits just above the raised finger
        else:
            ax = W / 2
            ay = (title["y"] + title["h"] + 0.09 * H) if title else 0.62 * H
        best = None
        for scale in (1.0, 0.9, 0.8, 0.7, 0.62):
            for wfrac in (0.84, 0.7, 0.58, 0.46, 0.38, 0.32, 0.27):
                b = measure_box(p["lines"], font_path, int(base * scale), int(W * wfrac))
                bw, bh = b["w"], b["h"]
                if bh > bottom_max - top_min:
                    continue
                for y in range(top_min, bottom_max - bh + 1, 12):
                    for x in range(side, W - side - bw + 1, 18):
                        rect = (x, y, x + bw, y + bh)
                        if any(_overlap(rect, o) > 0 for o in taken):
                            continue
                        face_hit = sum(_overlap(rect, f) for f in faces) / (bw * bh)
                        cx, cy = x + bw / 2, y + bh / 2
                        dist = ((cx - ax) ** 2 + ((cy - ay) * 1.3) ** 2) ** 0.5 / H
                        cost = dist + face_hit * 4 + (1 - scale) * 0.35
                        if best is None or cost < best[0]:
                            best = (cost, x, y, b)
            if best and best[0] < 0.25:  # good enough at this size
                break
        if best is None:  # nothing fits: smallest box at the bottom
            b = measure_box(p["lines"], font_path, int(base * 0.6), int(W * 0.84))
            best = (9, (W - b["w"]) // 2, bottom_max - b["h"], b)
        _, x, y, b = best
        b.update({"x": x, "y": y, "t": p["t"]})
        taken.append((x - 8, y - 8, x + b["w"] + 8, y + b["h"] + 8))
        out.append(b)
    return out
