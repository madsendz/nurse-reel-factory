"""Optional: ask Claude for new topic ideas modeled on what's winning.

New ideas are saved with status: review, so nothing unchecked ever posts.
Flip an idea to status: active in content/bank.yaml once you've checked the facts.
"""
from __future__ import annotations

import os
import re

import requests
import yaml

API = "https://api.anthropic.com/v1/messages"


def generate(bank: list[dict], est: dict, cfg: dict, n: int | None = None) -> list[dict]:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    n = n or cfg["ideas"].get("per_run", 8)
    by_id = {i["id"]: i for i in bank}
    ranked = sorted(est.get("item_id", {}).items(), key=lambda kv: kv[1]["est"], reverse=True)
    winners = [by_id[k] for k, _ in ranked[:6] if k in by_id]
    losers = [by_id[k] for k, _ in ranked[-3:] if k in by_id and len(ranked) > 8]
    styles = sorted(est.get("style", {}).items(), key=lambda kv: kv[1]["est"], reverse=True)
    existing = ", ".join(sorted(by_id))

    def fmt(items):
        return yaml.safe_dump([{k: i[k] for k in ("category", "style", "hook", "beats")} for i in items],
                              sort_keys=False, allow_unicode=True) if items else "(none yet)"

    prompt = f"""You write on-screen text for short educational reels by "Nurse Lexi", a nurse persona
who teaches men's health, sexual wellness, sex ed and rare anatomy facts. Reels are a clip of her
pointing at the text. Tone: confident, friendly, a little cheeky, never explicit. Facts MUST be
medically accurate and match mainstream sources (CDC, WHO, Mayo Clinic, peer-reviewed studies).
Avoid anything graphic; this must be OK for Instagram's educational content rules.

Best performing reels (by link clicks):
{fmt(winners)}
Weakest reels:
{fmt(losers)}
Hook styles ranked best→worst: {", ".join(k for k, _ in styles) or "no data yet"}

Write {n} NEW reel ideas that lean into what's working. Do not repeat these existing ids or topics: {existing}

Return ONLY a YAML list. Each item has: id (short kebab-case starting with "nw-", e.g. nw-morning-wood),
category (mens_health|sexual_wellness|sex_ed|anatomy), style (did_you_know|myth_vs_fact|warning_sign|
stat_reveal|is_it_normal|how_to|tip|rare_variation), hook (max 45 chars), beats (exactly 3 lines, max 55
chars each), caption (1-2 sentences, cite the source type briefly), source (where the fact comes from)."""

    r = requests.post(API, timeout=120, headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                                                  "content-type": "application/json"},
                      json={"model": cfg["ideas"].get("model", "claude-sonnet-4-5"), "max_tokens": 4000,
                            "messages": [{"role": "user", "content": prompt}]})
    r.raise_for_status()
    text = "".join(b.get("text", "") for b in r.json()["content"])
    text = re.sub(r"^```(?:yaml)?|```$", "", text.strip(), flags=re.MULTILINE)
    new = yaml.safe_load(text) or []
    out = []
    for it in new:
        if not isinstance(it, dict) or not it.get("id") or it["id"] in by_id:
            continue
        if not it.get("hook") or not it.get("beats"):
            continue
        if not str(it["id"]).startswith("nw-"):  # the dashboard's review list only shows nw- ids
            it["id"] = "nw-" + re.sub(r"^(mh|sw|se|an)-", "", str(it["id"]))
        if it["id"] in by_id:
            continue
        it["status"] = "review"
        out.append(it)
    return out
