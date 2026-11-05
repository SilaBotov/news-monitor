#!/usr/bin/env python3
"""news-monitor: автопостинг в Telegram-канал по расписанию.

Рубрики (по слоту времени МСК):
  9:00, 15:00  -> service: полезный сервис/инструмент для работы (короткая выжимка от Groq)
  12:00, 18:00, 21:00 -> humor: мем с картинкой (или текстовая шутка), подпись со ссылкой на источник

Запуск:
  python news_monitor.py          # опубликовать один пост
  python news_monitor.py --check  # только проверить, какие ленты доступны

Секреты (переменные окружения):
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, GROQ_API_KEY_NEWS
Необязательно:
  GROQ_MODEL (по умолчанию llama-3.3-70b-versatile), RUBRIC (service|humor, принудительно)
"""
import html
import json
import os
import random
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import feedparser
import requests

# ---------------------------------------------------------------- настройки

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "state.json"
MAX_SEEN = 500          # сколько последних ссылок помним
MAX_ATTEMPTS = 5        # сколько записей пробуем за запуск
TIMEOUT = 20
USER_AGENT = "Mozilla/5.0 (compatible; news-monitor/1.0)"
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

# Слоты по московскому времени -> рубрика
SLOTS = {9: "service", 12: "humor", 15: "service", 18: "humor", 21: "humor"}

# ВАЖНО: адреса лент не проверены с серверов GitHub. Запусти режим check
# (см. workflow) и убери/замени те, что не отвечают.
SERVICE_FEEDS = [
    "https://habr.com/ru/rss/all/all/",
    "https://vc.ru/rss/all",
    "https://tproger.ru/feed",
]

HUMOR_FEEDS = [
    # kind="image": берём картинку из записи; kind="text": запасной вариант, текстовая шутка
    {"url": "https://pikabu.ru/xmlfeeds.php?cmd=popular", "kind": "image"},
    {"url": "https://www.anekdot.ru/rss/export_j.xml", "kind": "text"},
]

# Что считаем «полезным сервисом» (фильтр до модели, чтобы не тратить лимит Groq)
SERVICE_RE = re.compile(
    r"сервис|инструмент|приложени|нейросет|\bбот|автоматизац|расширени|плагин|"
    r"бесплатн|генератор|шаблон|продуктивн|календар|заметк|таблиц|конструктор",
    re.IGNORECASE,
)

IMG_RE = re.compile(r"""<img[^>]+src=["']([^"']+)["']""", re.IGNORECASE)
IMG_EXT_RE = re.compile(r"\.(jpe?g|png|webp)(\?|$)", re.IGNORECASE)


def log(msg):
    print(msg, flush=True)


# ---------------------------------------------------------------- состояние

def load_state():
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        seen = data.get("seen", [])
        return seen if isinstance(seen, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_state(seen):
    STATE_FILE.write_text(
        json.dumps({"seen": seen[-MAX_SEEN:]}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )


# ---------------------------------------------------------------- ленты

def fetch_feed(url):
    """Возвращает список записей или [] при любой ошибке."""
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
        r.raise_for_status()
        return feedparser.parse(r.content).entries
    except Exception as e:  # noqa: BLE001
        log(f"  лента недоступна: {url} ({type(e).__name__}: {e})")
        return []


def strip_html(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def entry_key(entry):
    return entry.get("link") or entry.get("id")


def _is_http(url):
    return isinstance(url, str) and url.startswith(("http://", "https://"))


def find_image(entry):
    """Ищет URL картинки в записи: media:content, thumbnail, enclosure, <img> в тексте."""
    for m in entry.get("media_content", []) or []:
        url = m.get("url")
        if _is_http(url) and (
            m.get("medium") == "image"
            or str(m.get("type", "")).startswith("image")
            or IMG_EXT_RE.search(url)
        ):
            return url
    for m in entry.get("media_thumbnail", []) or []:
        if _is_http(m.get("url")):
            return m["url"]
    for link in entry.get("links", []) or []:
        if link.get("rel") == "enclosure" and str(link.get("type", "")).startswith("image"):
            if _is_http(link.get("href")):
                return link["href"]
    blobs = [entry.get("summary", "")] + [
        c.get("value", "") for c in (entry.get("content", []) or [])
    ]
    for blob in blobs:
        m = IMG_RE.search(blob or "")
        if m:
            url = html.unescape(m.group(1))
            if _is_http(url):
                return url
    return None


# ---------------------------------------------------------------- Groq

def groq_blurb(title, text):
    key = os.getenv("GROQ_API_KEY_NEWS")
    if not key:
        return None
    prompt = (
        "Ты редактор Telegram-канала про IT, нейросети и автоматизацию работы. "
        "По заголовку и описанию напиши 1-2 коротких предложения по-русски: "
        "что это за сервис или материал и чем он полезен в работе. "
        "Используй только факты из текста, не выдумывай цифры, названия и функции. "
        "Без хэштегов, без ссылок, без восклицательных знаков.\n\n"
        f"Заголовок: {title}\nОписание: {text[:1500]}"
    )
    try:
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": GROQ_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
                "max_tokens": 200,
            },
            timeout=30,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:  # noqa: BLE001
        log(f"  Groq не ответил: {type(e).__name__}: {e}")
        return None


# ---------------------------------------------------------------- Telegram

def tg(method, payload):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    payload = {"chat_id": os.environ["TELEGRAM_CHAT_ID"], **payload}
    r = requests.post(
        f"https://api.telegram.org/bot{token}/{method}", json=payload, timeout=30
    )
    if not r.ok:
        raise RuntimeError(f"{method}: {r.status_code} {r.text[:200]}")


def send_text(text, preview=True):
    tg("sendMessage", {
        "text": text[:4096],
        "parse_mode": "HTML",
        "disable_web_page_preview": not preview,
    })


def send_photo(url, caption):
    tg("sendPhoto", {"photo": url, "caption": caption[:1024], "parse_mode": "HTML"})


def esc(s):
    return html.escape(s or "", quote=False)


# ---------------------------------------------------------------- рубрики

def run_service(seen):
    seen_set = set(seen)
    candidates = []
    for url in SERVICE_FEEDS:
        for e in fetch_feed(url):
            key = entry_key(e)
            if not key or key in seen_set:
                continue
            text = strip_html(f"{e.get('title', '')} {e.get('summary', '')}")
            if SERVICE_RE.search(text):
                candidates.append((key, e, text))
    log(f"  кандидатов: {len(candidates)}")

    for key, e, text in candidates[:MAX_ATTEMPTS]:
        title = strip_html(e.get("title", "")) or "Без названия"
        blurb = groq_blurb(title, text)
        if not blurb:
            # запасной вариант без модели: начало описания из ленты
            blurb = text[len(title):].strip()[:200] if text.startswith(title) else text[:200]
        post = f"<b>{esc(title)}</b>\n\n{esc(blurb)}\n\n<a href=\"{esc(key)}\">Читать</a>"
        try:
            send_text(post)
        except Exception as ex:  # noqa: BLE001
            log(f"  не отправилось: {ex}")
            continue
        seen.append(key)
        log(f"  опубликовано: {title}")
        return True
    return False


def run_humor(seen):
    seen_set = set(seen)
    image_feeds = [f for f in HUMOR_FEEDS if f["kind"] == "image"]
    text_feeds = [f for f in HUMOR_FEEDS if f["kind"] == "text"]
    random.shuffle(image_feeds)
    random.shuffle(text_feeds)
    attempts = 0

    for feed in image_feeds:
        for e in fetch_feed(feed["url"]):
            key = entry_key(e)
            if not key or key in seen_set:
                continue
            img = find_image(e)
            if not img:
                continue
            if attempts >= MAX_ATTEMPTS:
                break
            attempts += 1
            title = strip_html(e.get("title", ""))
            caption = f"<b>{esc(title)}</b>\n\n<a href=\"{esc(key)}\">Источник</a>"
            seen.append(key)  # помечаем сразу: битую картинку повторно не пробуем
            seen_set.add(key)
            try:
                send_photo(img, caption)
                log(f"  мем опубликован: {title}")
                return True
            except Exception as ex:  # noqa: BLE001
                log(f"  картинка не принялась: {ex}")

    for feed in text_feeds:
        for e in fetch_feed(feed["url"]):
            key = entry_key(e)
            if not key or key in seen_set:
                continue
            text = strip_html(e.get("summary", "")) or strip_html(e.get("title", ""))
            if not (20 <= len(text) <= 900):
                continue
            post = f"{esc(text)}\n\n<a href=\"{esc(key)}\">Источник</a>"
            try:
                send_text(post, preview=False)
            except Exception as ex:  # noqa: BLE001
                log(f"  не отправилось: {ex}")
                continue
            seen.append(key)
            log("  текстовая шутка опубликована")
            return True
    return False


def pick_rubric():
    forced = os.getenv("RUBRIC", "").strip()
    if forced in ("service", "humor"):
        return forced
    hour = datetime.now(ZoneInfo("Europe/Moscow")).hour
    # запуск может опоздать, поэтому берём ближайший слот
    return SLOTS[min(SLOTS, key=lambda h: abs(h - hour))]


# ---------------------------------------------------------------- режимы

def check_feeds():
    feeds = [("service", u) for u in SERVICE_FEEDS] + [
        (f"humor/{f['kind']}", f["url"]) for f in HUMOR_FEEDS
    ]
    for label, url in feeds:
        entries = fetch_feed(url)
        with_img = sum(1 for e in entries if find_image(e))
        first = strip_html(entries[0].get("title", ""))[:60] if entries else "-"
        log(f"[{label}] {url}\n    записей: {len(entries)}, с картинкой: {with_img}, первая: {first}")
    return 0


def main():
    if "--check" in sys.argv:
        return check_feeds()
    seen = load_state()
    rubric = pick_rubric()
    log(f"Рубрика: {rubric}")
    posted = run_service(seen) if rubric == "service" else run_humor(seen)
    save_state(seen)
    if not posted:
        log("::warning::Ничего не опубликовано: нет новых подходящих записей или ленты недоступны")
    return 0


if __name__ == "__main__":
    sys.exit(main())
