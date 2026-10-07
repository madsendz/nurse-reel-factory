"""Auto-tag clips: figure out where Lexi is pointing and where her face is,
then choose a text zone that sits where she points without covering her face.

Uses MediaPipe pose (arm direction) when the model file is available, and
OpenCV face detection as a fallback. Also makes contact sheets so a human
(or Claude) can double-check each clip's zone at a glance.
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from statistics import median

from PIL import Image, ImageDraw, ImageFont

from .config import ROOT
from .render import ensure_font, probe

POSE_MODEL = ROOT / ".cache" / "pose_landmarker_lite.task"
SAFE_TOP, SAFE_BOTTOM = 0.10, 0.78
MIN_H = 0.22          # preferred minimum text area height
MIN_POINTED = 0.15    # smallest band we'll still use if she's pointing into it


# ───────────────────────── geometry (pure, testable) ─────────────────────────
def compute_zone(face: tuple | None, target: tuple | None) -> dict:
    """face = (x, y, w, h) normalized; target = (x, y) normalized point she points at.
    Returns zone {x, y, w, h} normalized to the 1080x1920 frame."""
    # vertical bands available above and below the face
    if face:
        fx, fy, fw, fh = face
        top_band = (SAFE_TOP, max(SAFE_TOP, fy - 0.02))
        bot_band = (min(SAFE_BOTTOM, fy + fh + 0.02), SAFE_BOTTOM)
    else:
        top_band, bot_band = (SAFE_TOP, 0.5), (0.5, SAFE_BOTTOM)

    def size(b):
        return b[1] - b[0]

    if target:
        tx, ty = target
        # pick the band she's pointing into (or the one closest to the target)
        if face and fy <= ty <= fy + fh:  # pointing at face level -> side of face
            band = top_band if size(top_band) >= size(bot_band) else bot_band
            side = "left" if tx < fx + fw / 2 else "right"
        else:
            band = top_band if ty < (face[1] + face[3] / 2 if face else 0.5) else bot_band
            if size(band) < MIN_POINTED:
                band = top_band if band is bot_band else bot_band
            side = "left" if tx < 0.38 else "right" if tx > 0.62 else "center"
    else:
        band = top_band if size(top_band) >= size(bot_band) else bot_band
        side = "center"

    y0, y1 = band
    if y1 - y0 < MIN_POINTED:  # tiny band: take the bigger one and accept a little overlap
        y0, y1 = max([top_band, bot_band], key=size)
        y1 = max(y1, min(SAFE_BOTTOM, y0 + MIN_H))
    h = min(y1 - y0, 0.42)
    # anchor near the target vertically inside the band
    if target:
        y = min(max(target[1] - h / 2, y0), y1 - h)
    else:
        y = y0 if band is top_band else y1 - h
    if side == "left":
        x, w = 0.05, 0.62
    elif side == "right":
        x, w = 0.33, 0.62
    else:
        x, w = 0.08, 0.84
    r = lambda v: round(float(v), 3)
    return {"x": r(x), "y": r(y), "w": r(w), "h": r(h)}


# ───────────────────────── detection ─────────────────────────
def extract_frames(clip_path: Path, out_dir: Path, n: int = 5, start: float = 0, end: float | None = None,
                   width: int = 540, height: int = 960) -> list[Path]:
    """Grab n frames, scaled/cropped exactly like the renderer does (so coords line up)."""
    info = probe(clip_path)
    end = end or info["duration"]
    frames = []
    for k in range(n):
        t = start + (end - start) * (k + 1) / (n + 1)
        p = out_dir / f"f{k}.jpg"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", str(clip_path), "-frames:v", "1",
                        "-vf", f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}",
                        str(p)], check=True)
        frames.append(p)
    return frames


def detect_face(img_path: Path):
    import cv2
    img = cv2.imread(str(img_path))
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    det = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    faces = det.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=6, minSize=(60, 60))
    if len(faces) == 0:
        return None
    x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
    H, W = gray.shape
    # pad: hair + chin
    return (x / W - 0.02, max(0, y / H - 0.04), w / W + 0.04, h / H + 0.08)


_landmarker = None


def _pose():
    global _landmarker
    if _landmarker is None and POSE_MODEL.exists():
        from mediapipe.tasks.python import BaseOptions, vision
        _landmarker = vision.PoseLandmarker.create_from_options(vision.PoseLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=str(POSE_MODEL)), running_mode=vision.RunningMode.IMAGE))
    return _landmarker


def detect_pointing(img_path: Path):
    """Return ((target_x, target_y), face_box) from body pose, normalized; or (None, None)."""
    lm = _pose()
    if lm is None:
        return None, None
    import mediapipe as mp
    res = lm.detect(mp.Image.create_from_file(str(img_path)))
    if not res.pose_landmarks:
        return None, None
    P = res.pose_landmarks[0]
    # face box from nose/eyes/ears/mouth (landmarks 0-10)
    fxs = [P[i].x for i in range(11)]
    fys = [P[i].y for i in range(11)]
    fw = max(fxs) - min(fxs)
    face = (min(fxs) - fw * 0.35, min(fys) - fw * 0.9, fw * 1.7, (max(fys) - min(fys)) + fw * 1.6)
    best = None
    for elbow, wrist, index, shoulder in ((13, 15, 19, 11), (14, 16, 20, 12)):
        e, w, i, s = P[elbow], P[wrist], P[index], P[shoulder]
        if min(w.visibility, i.visibility) < 0.4:
            continue
        # how "pointy" the arm is: hand away from the shoulder
        reach = ((i.x - s.x) ** 2 + (i.y - s.y) ** 2) ** 0.5
        if best is None or reach > best[0]:
            dx, dy = i.x - e.x, i.y - e.y
            norm = (dx * dx + dy * dy) ** 0.5 or 1
            tx = min(max(i.x + dx / norm * 0.18, 0.05), 0.95)
            ty = min(max(i.y + dy / norm * 0.18, SAFE_TOP), SAFE_BOTTOM)
            best = (reach, (tx, ty))
    return (best[1] if best else None), face


def tag_clip(clip_path: Path, clip: dict) -> dict:
    with tempfile.TemporaryDirectory() as td:
        frames = extract_frames(clip_path, Path(td), start=float(clip.get("start") or 0), end=clip.get("end"))
        targets, faces = [], []
        for f in frames:
            try:
                t, face = detect_pointing(f)
            except Exception as e:  # model missing/broken -> face-only fallback
                print(f"[autotag] pose failed: {e}")
                t, face = None, None
            try:
                face = face or detect_face(f)
            except Exception as e:
                print(f"[autotag] face detect failed: {e}")
            if t:
                targets.append(t)
            if face:
                faces.append(face)
    target = (median(t[0] for t in targets), median(t[1] for t in targets)) if targets else None
    face = tuple(median(f[k] for f in faces) for k in range(4)) if faces else None
    zone = compute_zone(face, target)
    how = "pose" if targets else ("face" if faces else "default")
    return {"zone": zone, "auto": {"method": how, "face": [round(v, 3) for v in face] if face else None,
                                   "target": [round(v, 3) for v in target] if target else None}}


# ───────────────────────── contact sheets ─────────────────────────
def contact_sheet(entries: list[tuple[str, Path, dict]], out: Path, cols: int = 4) -> None:
    """entries: (clip_id, frame_path, zone). Draws each frame with its zone outlined."""
    tw, th = 270, 480
    rows = (len(entries) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * tw, rows * (th + 34)), "white")
    font = ImageFont.truetype(str(ensure_font(ROOT / "assets/fonts/Poppins-Bold.ttf")), 22)
    for n, (cid, frame, zone) in enumerate(entries):
        im = Image.open(frame).convert("RGB").resize((tw, th))
        d = ImageDraw.Draw(im, "RGBA")
        z = zone
        d.rectangle([z["x"] * tw, z["y"] * th, (z["x"] + z["w"]) * tw, (z["y"] + z["h"]) * th],
                    outline=(224, 69, 123, 255), width=4, fill=(224, 69, 123, 60))
        x, y = (n % cols) * tw, (n // cols) * (th + 34)
        sheet.paste(im, (x, y + 34))
        ImageDraw.Draw(sheet).text((x + 8, y + 2), cid, font=font, fill="black")
    sheet.save(out, quality=82)
