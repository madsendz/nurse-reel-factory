"""Loading settings, the topic bank, the clip library, and saved state."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yaml"
BANK_PATH = ROOT / "content" / "bank.yaml"
CLIPS_PATH = ROOT / "clips" / "clips.yaml"
POSTS_PATH = ROOT / "data" / "posts.json"
CLICKS_PATH = ROOT / "data" / "clicks.json"
CACHE_DIR = ROOT / ".cache"
OUT_DIR = ROOT / "out"


def load_yaml(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return default if data is None else data


def save_yaml(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True, width=100)


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    tmp.replace(path)


def load_config() -> dict:
    cfg = load_yaml(CONFIG_PATH, {})
    # Secrets / env overrides (GitHub Actions secrets)
    ig = cfg.setdefault("instagram", {})
    ig["ig_user_id"] = os.environ.get("IG_USER_ID") or ig.get("ig_user_id", "")
    drive = cfg.setdefault("drive", {})
    drive["folder_id"] = os.environ.get("DRIVE_FOLDER_ID") or drive.get("folder_id", "")
    return cfg


TOPIC_STATUS_PATH = ROOT / "data" / "topic_status.json"
TOPIC_TEXT_PATH = ROOT / "data" / "topic_text.json"  # Nick's text edits from the dashboard ("Edit text")


def load_bank() -> list[dict]:
    items = load_yaml(BANK_PATH, {"items": []}).get("items", [])
    over = load_json(TOPIC_STATUS_PATH, {})  # keep / toss decisions made on the dashboard
    text = load_json(TOPIC_TEXT_PATH, {})    # edited hook / fact lines: {item_id: {hook, beats, at}}
    for i in items:
        if i.get("id") in over:
            i["status"] = over[i["id"]]
        t = text.get(i.get("id"))
        if t:
            if t.get("hook"):
                i["hook"] = t["hook"]
            if isinstance(t.get("beats"), list):
                i["beats"] = t["beats"]
            i["text_edited_at"] = t.get("at")
    return items


def save_bank(items: list[dict]) -> None:
    header = load_yaml(BANK_PATH, {}) or {}
    header["items"] = items
    save_yaml(BANK_PATH, header)


def load_clips() -> list[dict]:
    return load_yaml(CLIPS_PATH, {"clips": []}).get("clips", [])


def save_clips(clips: list[dict]) -> None:
    save_yaml(CLIPS_PATH, {"clips": clips})


def load_posts() -> list[dict]:
    return load_json(POSTS_PATH, [])


def save_posts(posts: list[dict]) -> None:
    save_json(POSTS_PATH, posts)
