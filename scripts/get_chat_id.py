#!/usr/bin/env python3
"""Разовый утилита: получить chat_id канала, в который добавили бота.

Порядок:
1. Запусти скрипт: python scripts/get_chat_id.py
2. Пока он работает — добавь бота в канал (и назначь админом с правом write)
3. Скрипт поймает событие bot_added и напечатает chat_id

Требуется env MAX_BOT_TOKEN.
"""
import json
import os
import sys
import time
import urllib.request

API = "https://platform-api2.max.ru/updates"


def find_chat_id(obj, depth=0):
    if depth > 6:
        return None
    if isinstance(obj, dict):
        if "chat_id" in obj and obj["chat_id"]:
            return obj["chat_id"]
        chat = obj.get("chat")
        if isinstance(chat, dict) and chat.get("id"):
            return chat["id"]
        for v in obj.values():
            found = find_chat_id(v, depth + 1)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = find_chat_id(v, depth + 1)
            if found:
                return found
    return None


def main():
    token = os.environ.get("MAX_BOT_TOKEN")
    if not token:
        print("Set env MAX_BOT_TOKEN")
        return 1
    print("Waiting for updates (add the bot to the channel now)...", flush=True)
    deadline = time.time() + 600
    while time.time() < deadline:
        req = urllib.request.Request(
            API, headers={"Authorization": token, "Accept": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            print(f"poll error: {exc}", flush=True)
            time.sleep(3)
            continue
        updates = data if isinstance(data, list) else data.get("updates", [data])
        for upd in updates:
            print("update:", json.dumps(upd, ensure_ascii=False)[:500], flush=True)
            utype = str(upd.get("update_type") or upd.get("type") or "")
            chat_id = find_chat_id(upd)
            if chat_id and ("bot_added" in utype or "bot_started" in utype or utype):
                print(f"\nCHAT_ID: {chat_id}", flush=True)
                return 0
    print("Timeout: no events in 10 minutes")
    return 1


if __name__ == "__main__":
    sys.exit(main())