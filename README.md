# Factum Daily Post

Ежедневный пост-историю о необычных местах планеты для канала [max.ru/channel_factum](https://max.ru/channel_factum).

**Как работает:**
1. Каждый день в 14:00 МСК Actions выбирает **неиспользованное** место из `config/places.json` (список уже использованных — в `history.json`)
2. Gemini пишет пост в стиле автора канала (эталон — `prompts/style_example.txt`, промпт — `prompts/story.txt`), история всех прошлых постов скармливается в промпт для исключения повторов
3. Пост сохраняется в `posts/YYYY-MM-DD.md`, история пополняется
4. Пост публикуется в канал через MAX Bot API
5. Всё коммитится — активность в репозитории каждый день

**Секреты (Settings → Secrets → Actions):**
- `GEMINI_API_KEY` — ключ Google AI Studio
- `MAX_BOT_TOKEN` — токен бота из «MAX для бизнеса»
- `MAX_CHAT_ID` — ID канала (без секрета публикация пропускается, пост всё равно генерируется и коммитится)

**Добавление новых мест:** `config/places.json` → массив `places`, поля `name` и `hint`.