#!/usr/bin/env python3
"""Factum daily post: pick unused place -> Gemini writes story -> photo via search+vision check -> publish to MAX."""
import base64
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "places.json"
HISTORY_PATH = ROOT / "history.json"
PROMPTS_DIR = ROOT / "prompts"

MAX_API = "https://platform-api2.max.ru"
MAX_MESSAGES = MAX_API + "/messages"
MAX_UPLOADS = MAX_API + "/uploads?type=image"
USER_AGENT = "factum-bot/1.0 (github.com/mrKamanov/ai-teachers-digest)"


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


def gemini_call(prompt, cfg, *, tools=None, temperature=0.9,
                max_tokens=16384, inline_image=None) -> str | None:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        log("GEMINI_API_KEY не задан")
        return None
    model = os.environ.get("GEMINI_MODEL") or cfg.get("gemini_model", "gemini-3.8-flash")
    parts = [{"text": prompt}]
    if inline_image:
        data, mime = inline_image
        parts.append({"inline_data": {"mime_type": mime, "data": base64.b64encode(data).decode()}})
    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": max_tokens,
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    if tools:
        payload["tools"] = tools
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/{model}"
           f":generateContent?key={urllib.parse.quote(key)}")
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    for attempt, delay in enumerate((0, 25, 60, 120)):
        if delay:
            log(f"gemini: жду {delay}s перед повтором")
            time.sleep(delay)
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            candidate = data["candidates"][0]
            text = candidate.get("content", {}).get("parts", [{}])[0].get("text", "")
            return text.strip()
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:200]
            except Exception:
                pass
            if (exc.code == 429 or exc.code >= 500) and attempt < 3:
                log(f"gemini {exc.code} — попытка {attempt + 2}/4 {detail}")
                continue
            log(f"gemini error: {exc} {detail}")
            return None
        except Exception as exc:
            log(f"gemini error: {exc}")
            return None
    return None


def ask_gemini_post(prompt: str, cfg) -> str | None:
    text = gemini_call(prompt, cfg, temperature=0.9)
    if not text:
        return None
    text = text.strip("`").strip()
    if len(text) < 1600:
        log(f"post too short ({len(text)} chars) — treat as failure")
        return None
    return text


def photo_search_terms(place_name: str, cfg) -> list[str]:
    prompt = (
        f"Чтобы найти фото места «{place_name}» на Wikimedia Commons, придумай 3 "
        "англоязычных поисковых запроса — точные английские названия объекта и места, "
        "короткие, без кавычек и пояснений.\n"
        'Верни строго JSON-массив строк. Пример: ["Skellig Michael island", "Skellig Rocks Ireland"]'
    )
    text = gemini_call(prompt, cfg, temperature=0.2, max_tokens=512)
    if not text:
        return []
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        return []
    try:
        terms = json.loads(m.group(0))
    except Exception:
        return []
    return [t.strip() for t in terms if isinstance(t, str) and t.strip()][:3]


def commons_search(terms: list[str]) -> list[str]:
    pages = []
    for term in terms:
        url = ("https://commons.wikimedia.org/w/api.php?action=query&list=search"
               "&srnamespace=6&format=json&srlimit=6&srsearch=" + urllib.parse.quote(term))
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            log(f"commons search error: {exc}")
            continue
        for item in data.get("query", {}).get("search", []):
            title = item.get("title", "")
            if not re.match(r"File:.*\.(jpg|jpeg|png)$", title, re.I):
                continue
            if re.search(r"(?i)[_\-\s](map|srtm|dem|diagram|chart|schema|sketch|"
                         r"plan|legend|labelled|labeled|relief|render|infographic)[_\-\s.]", title):
                continue
            page = "https://commons.wikimedia.org/wiki/" + title.replace(" ", "_")
            if page not in pages:
                pages.append(page)
    return pages


def pick_place_ai(history, cfg):
    """Gemini выбирает новое место; возвращает {name, hint, terms} или None."""
    used = "\n".join(f"- {e['place']}" for e in history.get("published", [])) or "- пока пусто"
    prompt = (
        "Ты — редактор канала с постами-историями о необычных местах планеты. "
        "Выбери одно НОВОЕ место для сегодняшнего поста.\n"
        "Уже опубликовано (не предлагай эти места или объекты из них, даже если "
        "по-другому):\n" + used + "\n"
        "Требования:\n"
        "- реально существующее место, о котором есть что рассказать: факты, история, тайны\n"
        "- необычное, удивляющее; НЕ мейнстрим-туризм (не Эйфелева башня, не Колизей, "
        "не Мачу-Пикчу, не Байкал)\n"
        "- разные регионы мира: не предлагай две подряд одной страны, если последний "
        "пост был из той же\n"
        '- "name" — кратко, в формате "Название, Страна", по-русски\n'
        '- "hint" — 4-8 слов по-русски, о чём это место\n'
        '- "terms" — 2-3 англоязычных запроса для поиска фото на Wikimedia Commons '
        "(точные английские названия объекта)\n"
        "Верни строго один JSON-объект без пояснений:\n"
        '{"name": "...", "hint": "...", "terms": ["...", "..."]}'
    )
    text = gemini_call(prompt, cfg, temperature=0.9, max_tokens=1024)
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        log(f"place picker: bad answer: {text[:200]}")
        return None
    try:
        sel = json.loads(m.group(0))
    except Exception:
        log(f"place picker: bad json: {text[:200]}")
        return None
    name = str(sel.get("name") or "").strip()
    hint = str(sel.get("hint") or "").strip()
    terms = [t.strip() for t in sel.get("terms") or [] if isinstance(t, str) and t.strip()]
    if not name or len(name) > 100:
        log(f"place picker: invalid name {name!r}")
        return None
    used_set = {e["place"].lower() for e in history.get("published", [])}
    if name.lower() in used_set:
        log(f"place picker: duplicate of history {name!r}")
        return None
    return {"name": name, "hint": hint or "необычное место", "terms": terms[:3]}


def find_photo_pages(place_name: str, cfg, terms=None) -> list[str]:
    photo_cfg = cfg.get("photo", {})
    if not photo_cfg.get("enabled", True):
        return []
    if not terms:
        terms = photo_search_terms(place_name, cfg)
    if not terms:
        log("photo: search terms not found")
        return []
    log(f"photo: terms {terms}")
    pages = commons_search(terms)[:5]
    if not pages:
        log("photo: no commons files found")
    return pages


def commons_image_url(page_url: str, width: int) -> str | None:
    m = re.match(r"https://commons\.wikimedia\.org/wiki/File:(.+)$", page_url)
    if not m:
        return None
    filename = urllib.parse.unquote(m.group(1)).replace(" ", "_")
    return ("https://commons.wikimedia.org/wiki/Special:FilePath/"
            + urllib.parse.quote(filename) + f"?width={width}")


def download_image(url: str) -> tuple[bytes, str] | None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = resp.read()
            mime = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    except Exception as exc:
        log(f"photo download error: {exc}")
        return None
    if mime not in ("image/jpeg", "image/png"):
        log(f"photo: unsupported mime {mime or 'unknown'}")
        return None
    if not (10_000 < len(data) < 20_000_000):
        log(f"photo: bad size {len(data)} bytes")
        return None
    return data, mime


def verify_image(data: bytes, mime: str, place_name: str, cfg) -> bool:
    prompt = (
        "Ты — строгий модератор. Тебе показали фото для поста про место: "
        f"{place_name}.\n"
        "Ответь СТРОГО одним JSON-объектом без пояснений:\n"
        "{\"match\": true|false, \"safe\": true|false, \"reason\": \"коротко\"}\n"
        "match=true ТОЛЬКО если на фото узнаваемо именно это место — допустимы разные "
        "ракурсы и расстояния, но объект должен быть главным на фото и его должно быть "
        "возможно опознать. Это настоящая фотография (не коллаж, не рисунок, "
        "не мем, не скриншот, не карта, не схема, не инфографика, не цифровая модель "
        "рельефа и не рендер.\n"
        "safe=false если есть: обнажённость, сексуальный контекст, насилие, оружие, "
        "дети в рискованных ситуациях, алкоголь/наркотики, религиозная/политическая "
        "символика-триггеры, чужие водяные знаки, реклама, расистская символика, "
        "аварии/трупы, что-либо шокирующее или запрещённое к публикации."
    )
    text = gemini_call(prompt, cfg, temperature=0.0, max_tokens=1024,
                       inline_image=(data, mime))
    if not text:
        return False
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        log(f"photo verdict unparseable: {text[:200]}")
        return False
    try:
        verdict = json.loads(m.group(0))
    except Exception:
        log(f"photo verdict bad json: {text[:200]}")
        return False
    ok = bool(verdict.get("match")) and bool(verdict.get("safe"))
    log(f"photo verdict: match={verdict.get('match')} safe={verdict.get('safe')} "
        f"({verdict.get('reason', '')})")
    return ok


def extract_media_token(node, depth=0):
    if depth > 6 or not isinstance(node, dict):
        return None
    token = node.get("token")
    if isinstance(token, str) and token:
        return token
    for value in node.values():
        found = extract_media_token(value, depth + 1)
        if found:
            return found
    return None


def get_photo_token(data: bytes, mime: str, token: str) -> str | None:
    req = urllib.request.Request(
        MAX_UPLOADS, method="POST",
        headers={"Authorization": token, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            upload_url = json.loads(resp.read().decode("utf-8"))["url"]
    except Exception as exc:
        log(f"MAX uploads error: {exc}")
        return None
    boundary = uuid.uuid4().hex
    filename = "photo.jpg" if mime == "image/jpeg" else "photo.png"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="data"; filename="{filename}"\r\n'
        f"Content-Type: {mime}\r\n\r\n"
    ).encode() + data + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        upload_url, data=body, method="POST",
        headers={"Authorization": token, "Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read().decode("utf-8")
        log(f"MAX upload response: {raw[:300]}")
        media_token = extract_media_token(json.loads(raw))
        if not media_token:
            log("MAX image upload: token not found in response")
            return None
        log("MAX image uploaded")
        return media_token
    except urllib.error.HTTPError as exc:
        log(f"MAX image upload error {exc.code}: {exc.read().decode('utf-8', 'replace')[:200]}")
        return None
    except Exception as exc:
        log(f"MAX image upload error: {exc}")
        return None


def publish_to_max(text: str, media_token: str | None, max_token: str, chat_id: str) -> bool:
    payload = {"text": text, "notify": True}
    if media_token:
        payload["attachments"] = [{"type": "image", "payload": {"token": media_token}}]
    body = json.dumps(payload).encode("utf-8")
    url = f"{MAX_MESSAGES}?chat_id={urllib.parse.quote(chat_id)}"
    for attempt, delay in enumerate((0, 8, 15)):
        if delay:
            time.sleep(delay)
        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": "application/json", "Authorization": max_token},
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                log(f"MAX publish: {resp.status}")
                return resp.status == 200
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            log(f"MAX publish error {exc.code}: {detail}")
            if "not.ready" in detail and attempt < 2:
                log("attachment not ready — retrying")
                continue
            return False
        except Exception as exc:
            log(f"MAX publish error: {exc}")
            return False
    return False


def extract_header_field(raw: str, field: str) -> str | None:
    m = re.search(rf"<!--(.*?)-->", raw)
    if not m:
        return None
    fm = re.search(rf"{field}:\s*([^|]+)", m.group(1))
    return fm.group(1).strip() if fm else None


def obtain_media_token(place_name, cfg, photo_page, verify=True):
    """Returns (media_token, photo_page, verified)."""
    photo_cfg = cfg.get("photo", {})
    width = int(photo_cfg.get("width", 1600))
    direct = commons_image_url(photo_page, width)
    if not direct:
        return None, photo_page, False
    dl = download_image(direct)
    if not dl:
        return None, photo_page, False
    data, mime = dl
    if verify and not verify_image(data, mime, place_name, cfg):
        log("photo rejected by vision check")
        return None, photo_page, False
    max_token = os.environ.get("MAX_BOT_TOKEN")
    if not max_token:
        return None, photo_page, True
    media_token = get_photo_token(data, mime, max_token)
    return media_token, photo_page, True


def main() -> int:
    cfg = load_json(CONFIG_PATH, None)
    if not cfg:
        log("config/places.json не найден")
        return 1
    history = load_json(HISTORY_PATH, {"published": []})
    photo_cfg = cfg.get("photo", {})

    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")

    for entry in history.get("published", []):
        if entry.get("date") == today and entry.get("published_to_max"):
            log(f"пост за {today} уже опубликован в канал — выходим")
            return 0

    existing = ROOT / cfg.get("posts_dir", "posts") / f"{today}.md"
    if existing.exists():
        log(f"пост за {today} уже сгенерирован — не перегенерирую")
        raw = existing.read_text(encoding="utf-8")
        text = "\n".join(
            line for line in raw.splitlines() if not line.startswith("<!--")
        ).strip()
        place_name = extract_header_field(raw, "place") or today
        photo_page = extract_header_field(raw, "photo")
        media_token = None
        if photo_page:
            log(f"photo: reusing {photo_page}")
            media_token, _, _ = obtain_media_token(place_name, cfg, photo_page, verify=False)
    else:
        selection = pick_place_ai(history, cfg)
        ai_terms = []
        if selection:
            place = {"name": selection["name"], "hint": selection["hint"]}
            ai_terms = selection["terms"]
            log(f"тема (AI): {place['name']}")
        else:
            log("place picker failed — пробую статичный список")
            place, used = pick_place(cfg, history, today)
            if not place:
                log("места закончились, а выбор через Gemini не работает — проверь лог выше")
                return 1
            log(f"тема: {place['name']}")

        prompt_tpl = (PROMPTS_DIR / Path(cfg["prompt_file"]).name).read_text(encoding="utf-8")
        style_example = (PROMPTS_DIR / Path(cfg["style_examples_file"]).name).read_text(encoding="utf-8")
        prompt = build_prompt(cfg, place, history, style_example, prompt_tpl)

        text = ask_gemini_post(prompt, cfg)
        if not text:
            log("генерация не удалась — выходим без изменений")
            return 1

        photo_page = None
        media_token = None
        if photo_cfg.get("enabled", True):
            log("photo: ищу фото на Викискладе")
            for candidate in find_photo_pages(place["name"], cfg, terms=ai_terms):
                log(f"photo: candidate {candidate}")
                token, _, verified = obtain_media_token(place["name"], cfg, candidate, verify=True)
                if token:
                    media_token, photo_page = token, candidate
                    break
                if verified:
                    log("photo verified but upload failed — publishing without image")
                    break

        posts_dir = ROOT / cfg.get("posts_dir", "posts")
        posts_dir.mkdir(exist_ok=True)
        photo_part = f" | photo: {photo_page}" if photo_page else ""
        header = f"<!-- place: {place['name']} | date: {today}{photo_part} -->\n\n"
        existing.write_text(header + text, encoding="utf-8")

        history.setdefault("published", []).append({
            "date": today,
            "place": place["name"],
            "title": place["name"].split(",")[0],
            **({"photo": photo_page} if photo_page else {}),
        })
        HISTORY_PATH.write_text(
            json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        log(f"пост сохранён: {existing.name} ({len(text)} знаков)")
        place_name = place["name"]

    max_token = os.environ.get("MAX_BOT_TOKEN")
    chat_id = os.environ.get("MAX_CHAT_ID")
    if not max_token or not chat_id:
        log("MAX_BOT_TOKEN/MAX_CHAT_ID не заданы — публикация пропущена")
        return 0
    ok = publish_to_max(text, media_token, max_token, chat_id)
    if ok:
        for entry in history.get("published", []):
            if entry.get("date") == today:
                entry["published_to_max"] = True
        HISTORY_PATH.write_text(
            json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        log("отметка: опубликовано сегодня")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
