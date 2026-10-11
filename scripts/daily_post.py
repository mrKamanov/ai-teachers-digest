#!/usr/bin/env python3
"""Factum daily post: pick unused place -> Gemini writes story -> save + history -> publish to MAX."""
import json
import os
import random
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "places.json"
HISTORY_PATH = ROOT / "history.json"
PROMPTS_DIR = ROOT / "prompts"

MAX_API = "https://platform-api2.max.ru/messages"


def log(msg: str) -> None:
    print("[factum] " + msg, flush=True)


def load_json(path: Path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def pick_place(cfg, history, today_str):
    used = {p.get("place", "") for p in history.get("published", [])}
    available = [p for p in cfg["places"] if p["name"] not in used]
    if not available:
        return None, used
    rng = random.Random(today_str)
    return rng.choice(available), used


def build_prompt(cfg, place, history, style_example, prompt_tpl):
    hist_lines = "\n".join(
        f"- {p['date']}: {p['place']} ({p['title']})" for p in history.get("published", [])
    ) or "- пока ничего не опубликовано"
    return (
        prompt_tpl
        .replace("{place}", place["name"])
        .replace("{hint}", place.get("hint", ""))
        .replace("{length}", cfg.get("length", "2800-3600"))
        .replace("{history}", hist_lines)
        .replace("{style_example}", style_example)
    )


def ask_gemini(prompt: str, cfg) -> str | None:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        log("GEMINI_API_KEY не задан — пропускаю")
        return None
    model = os.environ.get("GEMINI_MODEL") or cfg.get("gemini_model", "gemini-3.8-flash")
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.9, "maxOutputTokens": 4096},
    }
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/{model}"
           f":generateContent?key={urllib.parse.quote(key)}")
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
        return text.strip("`").strip() or None
    except Exception as exc:
        log(f"gemini error: {exc}")
        return None


def publish_to_max(text: str, place_name: str) -> bool:
    token = os.environ.get("MAX_BOT_TOKEN")
    chat_id = os.environ.get("MAX_CHAT_ID")
    if not token or not chat_id:
        log("MAX_BOT_TOKEN/MAX_CHAT_ID не заданы — публикация пропущена")
        return False
    body = json.dumps({
        "chat_id": int(chat_id),
        "body": {"text": text},
        "notify": True,
    }).encode("utf-8")
    req = urllib.request.Request(
        MAX_API, data=body,
        headers={"Content-Type": "application/json", "Authorization": token},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        ok = resp.status == 200
    log(f"MAX publish: {resp.status}")
    return ok


def main() -> int:
    cfg = load_json(CONFIG_PATH, None)
    if not cfg:
        log("config/places.json не найден")
        return 1
    history = load_json(HISTORY_PATH, {"published": []})

    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")

    existing = ROOT / cfg.get("posts_dir", "posts") / f"{today}.md"
    if existing.exists():
        log(f"пост за {today} уже сгенерирован — не перегенерирую")
        raw = existing.read_text(encoding="utf-8")
        text = "\n".join(
            line for line in raw.splitlines() if not line.startswith("<!--")
        ).strip()
        place_name = today
    else:
        place, used = pick_place(cfg, history, today)
        if not place:
            log("все места из списка использованы — добавь новые в config/places.json")
            return 0
        log(f"тема: {place['name']}")

        prompt_tpl = (PROMPTS_DIR / Path(cfg["prompt_file"]).name).read_text(encoding="utf-8")
        style_example = (PROMPTS_DIR / Path(cfg["style_examples_file"]).name).read_text(encoding="utf-8")
        prompt = build_prompt(cfg, place, history, style_example, prompt_tpl)

        text = ask_gemini(prompt, cfg)
        if not text:
            log("генерация не удалась — выходим без изменений")
            return 1

        posts_dir = ROOT / cfg.get("posts_dir", "posts")
        posts_dir.mkdir(exist_ok=True)
        header = f"<!-- place: {place['name']} | date: {today} -->\n\n"
        existing.write_text(header + text, encoding="utf-8")

        history.setdefault("published", []).append({
            "date": today,
            "place": place["name"],
            "title": place["name"].split(",")[0],
        })
        HISTORY_PATH.write_text(
            json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        log(f"пост сохранён: {existing.name} ({len(text)} знаков)")
        place_name = place["name"]

    try:
        publish_to_max(text, place_name)
    except Exception as exc:
        log(f"MAX publish error: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())