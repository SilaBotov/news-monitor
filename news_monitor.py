#!/usr/bin/env python3
"""news-monitor: автопостинг в Telegram-канал по расписанию.

Рубрики (по слоту времени МСК):
  9:00 -> service: полезный сервис/инструмент для работы (готовый пост от Groq)
  12:00, 15:00, 18:00, 21:00 -> humor: мем с картинкой (или текстовая шутка)

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
MAX_ATTEMPTS = 5        # сколько записей пробуем за запуск (юмор)
MAX_SERVICE_TRIES = 12  # сколько кандидатов может отклонить модель за запуск (сервисы)
TIMEOUT = 20
USER_AGENT = "Mozilla/5.0 (compatible; news-monitor/1.0)"
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")
ARTICLE_CHARS = 4000    # сколько текста статьи отдаём модели

# Добавлять ли в пост строку «Источник» со ссылкой
SOURCE_LINK_SERVICE = False   # рубрика «сервисы»: False = готовый пост без ссылки
SOURCE_LINK_HUMOR = False     # рубрика «юмор»: True = подпись «Источник» под мемом

SKIP = "SKIP"           # модель решила, что материал не подходит

# Слоты по московскому времени -> рубрика
SLOTS = {9: "service", 12: "humor", 15: "humor", 18: "humor", 21: "humor"}

# ВАЖНО: адреса лент не проверены с серверов GitHub. Запусти режим check
# (см. workflow) и убери/замени те, что не отвечают.
SERVICE_FEEDS = [
    "https://habr.com/ru/rss/all/all/",
    "https://vc.ru/rss/all",
    "https://tproger.ru/feed",
]

# kind="image": берём картинку из записи; kind="text": запасной вариант, текстовая шутка.
# Pikabu «горячее» убран 09.11.2026: в популярное попал комикс с оскорблениями по
# национальному признаку (pikabu.ru/story/vnimanie_14506494), фильтры его не отсекли.
# Источников с картинками сейчас нет — рубрика юмора публикует текстовые шутки.
HUMOR_FEEDS = [
    {"url": "https://www.anekdot.ru/rss/export_j.xml", "kind": "text"},
]

# Что считаем «полезным сервисом» (фильтр до модели, чтобы не тратить лимит Groq)
SERVICE_RE = re.compile(
    r"сервис|инструмент|приложени|нейросет|\bбот|автоматизац|расширени|плагин|"
    r"генератор|шаблон|продуктивн|календар|заметк|конструктор|\bии\b|\bai\b",
    re.IGNORECASE,
)
# Явно «не то»: отсекаем до модели, чтобы не тратить лимит Groq
EXCLUDE_RE = re.compile(
    r"\bpython|\bjava\b|javascript|\bgo\b|rust|c\+\+|\bsql\b|postgres|kubernetes|docker|"
    r"devops|linux|\bapi\b|фреймворк|библиотек|компилятор|ядр[оа]|микроконтроллер|"
    r"\bплк\b|асу тп|\bscada\b|\bfpga\b|уязвимост|\bcve\b|пентест|эксплойт|шифрован|"
    r"вакансии|зарплат|санкци|законопроект|минцифр|роскомнадзор|биржа|акци[ийя]|"
    r"квартальн|выручк|инвестиц|ipo",
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

def article_text(entry, fallback):
    """Текст материала: из ленты, если он там полный, иначе со страницы статьи."""
    for c in entry.get("content", []) or []:
        t = strip_html(c.get("value", ""))
        if len(t) > 800:
            return t[:ARTICLE_CHARS]
    link = entry_key(entry)
    try:
        r = requests.get(link, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
        r.raise_for_status()
        page = re.sub(r"(?is)<(script|style|nav|header|footer|aside)[^>]*>.*?</\1>", " ", r.text)
        m = re.search(r"(?is)<article[^>]*>(.*?)</article>", page)
        t = strip_html(m.group(1) if m else page)
        if len(t) > len(fallback):
            return t[:ARTICLE_CHARS]
    except Exception as e:  # noqa: BLE001
        log(f"  страницу статьи не загрузить: {type(e).__name__}: {e}")
    return fallback[:ARTICLE_CHARS]


OG_IMG_RE = re.compile(
    r"""<meta[^>]+(?:property|name)=["']og:image["'][^>]+content=["']([^"']+)["']"""
    r"""|<meta[^>]+content=["']([^"']+)["'][^>]+(?:property|name)=["']og:image["']""",
    re.IGNORECASE,
)


def page_image(link):
    """Картинка статьи из og:image на её странице (запасной вариант)."""
    try:
        r = requests.get(link, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
        r.raise_for_status()
        m = OG_IMG_RE.search(r.text)
        if m:
            url = html.unescape(m.group(1) or m.group(2))
            if _is_http(url):
                return url
    except Exception as e:  # noqa: BLE001
        log(f"  og:image не получить: {type(e).__name__}: {e}")
    return None


def parse_post(content):
    """Ответ модели -> (заголовок, текст) или SKIP или None."""
    content = (content or "").strip()
    if content.upper().startswith(SKIP) and len(content) < 20:
        return SKIP
    content = content.replace("*", "").replace("#", "")
    lines = [ln.strip() for ln in content.splitlines()]
    lines = [ln for ln in lines if ln]
    if len(lines) < 2:
        return None
    headline = re.sub(r"^(заголовок|название)\s*:\s*", "", lines[0], flags=re.IGNORECASE)
    headline = headline.strip(" «»\"'")
    body = "\n\n".join(lines[1:])
    body = split_paragraphs(body)
    return (headline, body) if headline and body else None


def split_paragraphs(body):
    """Если модель дала сплошной текст, разбиваем его на абзацы по 1-2 предложения."""
    if "\n\n" in body:
        return body
    sentences = re.split(r"(?<=[.!?…])\s+", body.strip())
    if len(sentences) < 3:
        return body
    size = 2 if len(sentences) <= 6 else 3
    chunks = [" ".join(sentences[i:i + size]) for i in range(0, len(sentences), size)]
    return "\n\n".join(chunks)


def text_with_breaks(s):
    """Как strip_html, но сохраняет переносы строк (для шуток-диалогов)."""
    s = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", s or "")
    s = html.unescape(re.sub(r"<[^>]+>", "", s))
    lines = [re.sub(r"[ \t\xa0]+", " ", ln).strip() for ln in s.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def groq_post(title, text):
    """Готовый пост от модели: (заголовок, текст), SKIP (не подходит) или None (ошибка)."""
    key = os.getenv("GROQ_API_KEY_NEWS")
    if not key:
        log("  нет ключа GROQ_API_KEY_NEWS")
        return None
    prompt = (
        "Ты редактор Telegram-канала команды разработчиков «Сила ботов»: Telegram-боты, "
        "нейросети, IT и автоматизация бизнеса. Канал читают предприниматели и офисные "
        "сотрудники. Тебе дан материал.\n\n"
        "Подходит ТОЛЬКО материал, где назван конкретный готовый сервис, приложение, "
        "нейросеть, Telegram-бот или инструмент, которым обычный человек может "
        "воспользоваться в работе уже сейчас, либо понятный приём автоматизации "
        "рутинных задач (отчёты, переписка, документы, расписание, учёт, продажи, "
        "поддержка клиентов) без программирования.\n"
        "Во всех остальных случаях ответь одним словом: SKIP. Всегда отвечай SKIP, если "
        "материал про: программирование и код, разработку и тестирование ПО, DevOps, "
        "базы данных, архитектуру, информационную безопасность, железо и электронику, "
        "промышленную автоматику, политику, законы и регулирование, новости компаний "
        "и рынка, инвестиции, реклама и вакансии, история и мнения, обзор нескольких "
        "тем сразу, либо если непонятно, какой именно сервис или инструмент имеется в виду.\n\n"
        "Иначе напиши готовый пост на русском языке, без ссылок и без фраз вроде "
        "«читайте далее»:\n"
        "- первая строка: короткий заголовок до 70 символов, без кавычек и разметки;\n"
        "- пустая строка;\n"
        "- основной текст 400-700 символов из ТРЁХ коротких абзацев, между абзацами пустая "
        "строка: 1) что это за сервис или приём; 2) что он умеет, 2-3 главные возможности; "
        "3) кому и чем полезен в работе;\n"
        "- используй только факты из текста, не выдумывай цифры, цены, названия и функции;\n"
        "- без хэштегов, без эмодзи, без разметки Markdown.\n\n"
        f"Материал:\n{title}\n{text}"
    )
    body = {
        "model": GROQ_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": 600,
    }
    if "gpt-oss" in GROQ_MODEL:
        # модель с «рассуждением»: короткое рассуждение и запас токенов, иначе ответ может обрезаться
        body["reasoning_effort"] = "low"
        body["max_tokens"] = 1500
    try:
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json=body,
            timeout=45,
        )
        if not r.ok:  # показываем ответ Groq целиком, чтобы была видна причина
            raise RuntimeError(f"{r.status_code} {r.text[:300]}")
        return parse_post(r.json()["choices"][0]["message"]["content"])
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
            title_only = strip_html(e.get("title", ""))
            if SERVICE_RE.search(text) and not EXCLUDE_RE.search(title_only):
                candidates.append((key, e, text))
    log(f"  кандидатов: {len(candidates)}")

    for key, e, text in candidates[:MAX_SERVICE_TRIES]:
        title = strip_html(e.get("title", "")) or "Без названия"
        result = groq_post(title, article_text(e, text))
        if result is None:
            # Groq недоступен или ответил непонятно: без модели готовый пост не сделать
            log("::warning::Groq не дал пост, рубрика сервисов пропущена")
            return False
        seen.append(key)  # запоминаем и отклонённые, чтобы не гонять модель повторно
        if result == SKIP:
            log(f"  не подходит: {title}")
            continue
        headline, body = result
        post = f"<b>{esc(headline)}</b>\n\n{esc(body)}"
        if SOURCE_LINK_SERVICE:
            post += f"\n\n<a href=\"{esc(key)}\">Источник</a>"
        img = find_image(e) or page_image(key)
        try:
            send_service_post(post, img)
        except Exception as ex:  # noqa: BLE001
            log(f"  не отправилось: {ex}")
            seen.pop()
            continue
        log(f"  опубликовано: {headline} (картинка: {'да' if img else 'нет'})")
        return True
    return False


def send_service_post(post, img):
    """Пост с картинкой-превью; если картинка не принялась или текст длинный, подстраиваемся."""
    if img:
        try:
            if len(post) <= 1024:
                send_photo(img, post)          # фото и текст одним сообщением
            else:
                send_photo(img, "")            # длинный текст: фото, затем текст
                send_text(post, preview=False)
            return
        except Exception as ex:  # noqa: BLE001
            log(f"  картинка не принялась ({ex}), отправляю без неё")
    send_text(post, preview=False)


# Темы, которым не место в юмористической рубрике (проверяется по заголовку, описанию и тегам)
BLOCK_RE = re.compile(
    r"суицид|самоубий|покончил|погиб|умер|умерл|скончал|смерт|убий|убит|убил|труп|"
    r"трагед|следстви|\bск\b|полиц|задержан|арест|\bсуд\b|приговор|уголовн|преступлен|"
    r"теракт|взрыв|обстрел|войн|\bсво\b|украин|мобилиз|фронт|насили|изнасил|педофил|"
    r"школьниц|\bдтп\b|аварi|авари|катастроф|пожар|жертв|траур|похорон|"
    r"политик|путин|депутат|выборы|санкци|новости|происшеств|криминал|скандал|"
    r"заявлени|онколог|диагноз|больниц|трамп|зеленск|мигрант",
    re.IGNORECASE,
)


def is_blocked(entry, *texts):
    tags = " ".join(t.get("term", "") for t in (entry.get("tags") or []) if isinstance(t, dict))
    return bool(BLOCK_RE.search(" ".join([*texts, tags])))


def groq_humor_ok(title, text):
    """Вторая проверка моделью: безобидный ли это юмор. Если Groq недоступен, пропускаем (True)."""
    key = os.getenv("GROQ_API_KEY_NEWS")
    if not key:
        return True
    prompt = (
        "Канал про IT и офисную жизнь для предпринимателей и сотрудников. Нужен лёгкий "
        "безобидный юмор: шутки, мемы, забавные истории.\n"
        "Ответь одним словом: SKIP, если материал про смерть, самоубийство, насилие, "
        "преступления, аварии, болезни, войну, политику, скандалы с реальными людьми, "
        "новости, либо это не шутка, либо неприлично. Иначе ответь одним словом: OK.\n\n"
        f"Заголовок: {title}\nТекст: {text[:800]}"
    )
    body = {"model": GROQ_MODEL, "messages": [{"role": "user", "content": prompt}],
            "temperature": 0, "max_tokens": 300}
    if "gpt-oss" in GROQ_MODEL:
        body["reasoning_effort"] = "low"
        body["max_tokens"] = 1000
    try:
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"}, json=body, timeout=30,
        )
        if not r.ok:
            raise RuntimeError(f"{r.status_code} {r.text[:200]}")
        answer = (r.json()["choices"][0]["message"]["content"] or "").strip().upper()
        return not answer.startswith("SKIP")
    except Exception as e:  # noqa: BLE001
        log(f"  проверка юмора моделью не удалась: {type(e).__name__}: {e}")
        return True


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
            title = strip_html(e.get("title", ""))
            summary = strip_html(e.get("summary", ""))
            if is_blocked(e, title, summary):
                log(f"  отклонено фильтром тем: {title}")
                continue
            if attempts >= MAX_ATTEMPTS:
                break
            attempts += 1
            if not groq_humor_ok(title, summary):
                log(f"  отклонено моделью: {title}")
                seen.append(key)
                seen_set.add(key)
                continue
            caption = f"<b>{esc(title)}</b>"
            if SOURCE_LINK_HUMOR:
                caption += f"\n\n<a href=\"{esc(key)}\">Источник</a>"
            seen.append(key)  # помечаем сразу: битую картинку повторно не пробуем
            seen_set.add(key)
            try:
                send_photo(img, caption)
                log(f"  мем опубликован: {title} | {key}")
                return True
            except Exception as ex:  # noqa: BLE001
                log(f"  картинка не принялась: {ex}")

    for feed in text_feeds:
        for e in fetch_feed(feed["url"]):
            key = entry_key(e)
            if not key or key in seen_set:
                continue
            text = text_with_breaks(e.get("summary", "")) or strip_html(e.get("title", ""))
            if not (20 <= len(text) <= 900):
                continue
            if is_blocked(e, text) or not groq_humor_ok("Шутка", text):
                log("  шутка отклонена фильтром")
                continue
            post = esc(text)
            if SOURCE_LINK_HUMOR:
                post += f"\n\n<a href=\"{esc(key)}\">Источник</a>"
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
    if not posted and rubric == "service":
        log("  сервисов нет, публикуем юмор, чтобы слот не пропал")
        posted = run_humor(seen)
    save_state(seen)
    if not posted:
        log("::warning::Ничего не опубликовано: нет новых подходящих записей или ленты недоступны")
    return 0


if __name__ == "__main__":
    sys.exit(main())
