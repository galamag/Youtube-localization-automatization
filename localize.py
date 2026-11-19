#!/usr/bin/env python3
"""
localize.py — транспорт для локализации метаданных видео на YouTube.

Скрипт НЕ переводит и не вызывает никаких AI-сервисов. Он только:
  - audit: ищет непереведённые видео на канале (read-only) и может сразу выгрузить их оригиналы;
  - pull: читает оригинальные метаданные видео с YouTube и складывает в work/<дата>/pulled.json;
  - push: заливает готовые переводы (translated.json) обратно через videos.update.

Перевод делает интерактивный Claude Code в отдельных сессиях — этот файл лишь транспорт.

Использование:
    python localize.py audit --channel <alias> [--recent N] [--missing ja,ko] [--pull] ...
    python localize.py pull --channel <alias> --videos <id1,id2,id3>
    python localize.py push --channel <alias> --file <path/to/translated.json> [--dry-run]
"""

import argparse
import hashlib
import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path

# UTF-8 в консоль, чтобы русские сообщения не падали при перенаправлении вывода.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

try:
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
except ImportError as exc:
    sys.exit(
        "Не установлены зависимости Google API.\n"
        "Установи их: pip install -r requirements.txt\n"
        f"({exc})"
    )


# Scope для записи метаданных (videos.update). force-ssl нужен именно для записи.
SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]

# Стоимость вызовов в юнитах квоты (YouTube остаток не возвращает — считаем сами).
COST_LIST = 1
COST_UPDATE = 50
# Одно видео в push = свежий videos.list + videos.update.
COST_PUSH_VIDEO = COST_LIST + COST_UPDATE

# Суточный лимит принадлежит проекту Google Cloud, а не каналу: при работе с
# парком все каналы тратят из общего котла.
DAILY_QUOTA_LIMIT = 10000

# Локали, которые --drop-locales не удаляет никогда и ни на каком канале.
# uk долгое время не входил в target_languages, поэтому попадал в «лишние»,
# хотя это осмысленный перевод, а не региональный мусор.
PROTECTED_LOCALES = {"uk"}

# Локали, которые НЕЛЬЗЯ заливать. Решение владельца: индийский регион портит
# ранжирование и статистику канала. Пакистанских и бангладешских локалей YouTube
# не поддерживает вовсе, но шаблон покрывает и их на случай появления.
BANNED_LOCALES = re.compile(r"^[a-z]{2}-(IN|PK|BD|LK)$", re.I)

# videos.update отвергает эти региональные коды с HTTP 400 (проверено 2026-07-30).
# Держим списком, чтобы не ловить отказ всего батча из-за одной локали.
UNSUPPORTED_LOCALES = {"it-IT", "ja-JP", "ko-KR", "ru-RU", "tr-TR", "uk-UA"}

# Корни проекта относительно этого файла.
ROOT = Path(__file__).resolve().parent
CHANNELS_DIR = ROOT / "channels"
WORK_DIR = ROOT / "work"
QUOTA_FILE = ROOT / "quota.json"


class LocalizeError(Exception):
    """Ожидаемая ошибка с понятным для человека сообщением."""


class Quota:
    """
    Счётчик квоты: за текущий запуск и суммарно за сутки по ВСЕМУ парку.

    Лимит 10 000 юнитов/день принадлежит проекту Google Cloud, а не каналу, —
    все каналы едят из одного котла. Поэтому суточный итог копится в общем
    quota.json в корне проекта, а не в папке канала.
    """

    def __init__(self):
        self.units = 0

    def charge_list(self):
        self.units += COST_LIST

    def charge_update(self):
        self.units += COST_UPDATE

    def _accumulate_daily(self):
        """Прибавить расход к суточному итогу. Никогда не роняет команду."""
        today = date.today().isoformat()
        try:
            spent = {}
            if QUOTA_FILE.exists():
                with QUOTA_FILE.open(encoding="utf-8") as f:
                    spent = json.load(f)
                if not isinstance(spent, dict):
                    spent = {}
            spent[today] = spent.get(today, 0) + self.units
            # Держим только последние 30 дней, чтобы файл не разрастался.
            trimmed = dict(sorted(spent.items())[-30:])
            with QUOTA_FILE.open("w", encoding="utf-8") as f:
                json.dump(trimmed, f, ensure_ascii=False, indent=2, sort_keys=True)
            return trimmed[today]
        except Exception as exc:
            print(f"Предупреждение: не удалось обновить {QUOTA_FILE} ({exc}).")
            return None

    def report(self):
        print(
            f"\nПотрачено квоты за запуск: {self.units} юнитов "
            f"(list={COST_LIST}/вызов, update={COST_UPDATE}/вызов)."
        )
        daily = self._accumulate_daily()
        if daily is not None:
            left = DAILY_QUOTA_LIMIT - daily
            print(
                f"За сегодня по всем каналам: {daily} из {DAILY_QUOTA_LIMIT} "
                f"(осталось {left}, это ещё ~{max(0, left // COST_PUSH_VIDEO)} видео на push)."
            )


# --------------------------------------------------------------------------- #
# Каналы и конфиг
# --------------------------------------------------------------------------- #

def resolve_channel(alias):
    """Вернуть папку канала channels/<alias>/, проверив, что она существует."""
    if not alias or "/" in alias or "\\" in alias:
        raise LocalizeError(f"Некорректный alias канала: {alias!r}")
    channel_dir = CHANNELS_DIR / alias
    if not channel_dir.is_dir():
        available = (
            sorted(
                p.name
                for p in CHANNELS_DIR.iterdir()
                if p.is_dir() and p.name != "_example"
            )
            if CHANNELS_DIR.is_dir()
            else []
        )
        hint = f" Доступные каналы: {', '.join(available)}." if available else ""
        raise LocalizeError(
            f"Канал '{alias}' не найден: нет папки {channel_dir}.{hint}\n"
            f"Скопируй channels/_example/ в channels/{alias}/ и заполни config.json."
        )
    return channel_dir


def load_config(channel_dir):
    """Прочитать и минимально провалидировать config.json канала."""
    config_path = channel_dir / "config.json"
    if not config_path.exists():
        raise LocalizeError(f"Нет файла конфига: {config_path}")
    try:
        with config_path.open(encoding="utf-8") as f:
            config = json.load(f)
    except json.JSONDecodeError as exc:
        raise LocalizeError(f"Не удалось разобрать {config_path}: {exc}")
    if not config.get("default_language"):
        raise LocalizeError(
            f"В {config_path} не задан default_language — "
            f"без него локализации на YouTube не активируются."
        )
    return config


def describe_channel(config, channel_id=None):
    """Человекочитаемое «UCxxx (Название, @handle)» для сообщений предохранителя."""
    parts = [p for p in (config.get("channel_title"), config.get("channel_handle")) if p]
    suffix = f" ({', '.join(parts)})" if parts else ""
    return f"{channel_id or config.get('channel_id')}{suffix}"


def require_channel_binding(channel_dir, config):
    """
    Вернуть channel_id из конфига. Нет привязки — не работаем.

    channel_id вписывается только командой link: в 24-символьном ID человек
    ошибётся вернее, чем мышкой в списке аккаунтов.
    """
    channel_id = config.get("channel_id")
    if not channel_id:
        raise LocalizeError(
            f"Канал «{channel_dir.name}» не привязан: в {channel_dir / 'config.json'} "
            f"нет channel_id.\n"
            f"Выполни: .venv\\Scripts\\python.exe localize.py link --channel {channel_dir.name}"
        )
    return channel_id


def assert_token_matches_channel(alias, config, expected_id, actual_id, actual_title=None):
    """
    Предохранитель: токен должен вести ровно на тот канал, что записан в конфиге.

    Названия каналов в парке похожи до неразличимости, промах на один пункт в
    списке аккаунтов при OAuth = заливка переводов в чужой контент. Несовпадение —
    немедленный выход, никаких «предупредили и продолжаем».
    """
    if actual_id == expected_id:
        return
    got = f"{actual_id}" + (f" ({actual_title})" if actual_title else "")
    raise LocalizeError(
        f"ОСТАНОВЛЕНО: токен не соответствует каналу.\n\n"
        f"  Алиас:        {alias}\n"
        f"  Ожидался:     {describe_channel(config, expected_id)}\n"
        f"  Токен ведёт:  {got}\n\n"
        f"Токен авторизован не на тот канал. Удали channels/{alias}/token.json "
        f"и авторизуйся заново, выбрав правильный аккаунт."
    )


def assert_videos_belong_to_channel(alias, config, expected_id, items):
    """
    Тот же предохранитель для pull/push, но по данным, которые уже пришли.

    videos.list отдаёт snippet.channelId бесплатно, поэтому отдельный
    channels.list ради сверки не нужен — проверка не стоит ни одного юнита.
    Для push это даже точнее: ловит ровно тот случай, когда под алиасом одного
    канала правятся видео другого.
    """
    foreign = {}
    for item in items:
        owner = item.get("snippet", {}).get("channelId")
        if owner and owner != expected_id:
            foreign[item.get("id")] = owner
    if not foreign:
        return
    lines = "\n".join(f"    {vid} принадлежит каналу {owner}" for vid, owner in foreign.items())
    raise LocalizeError(
        f"ОСТАНОВЛЕНО: видео принадлежат не тому каналу.\n\n"
        f"  Алиас:        {alias}\n"
        f"  Ожидался:     {describe_channel(config, expected_id)}\n"
        f"  А эти видео ведут на другой канал:\n{lines}\n\n"
        f"Скорее всего, token.json алиаса «{alias}» авторизован на чужой канал "
        f"или перепутаны video ID. Ничего не изменено."
    )


def get_target_languages(channel_dir, config):
    """Достать target_languages из конфига канала (нужен только для audit)."""
    raw = config.get("target_languages")
    if raw is None:
        raise LocalizeError(
            f"В {channel_dir / 'config.json'} нет поля target_languages — "
            f"audit не знает, какие языки считать целевыми. "
            f'Добавь, например: "target_languages": ["de", "es", "pt"]'
        )
    if not isinstance(raw, list):
        raise LocalizeError(
            f"В {channel_dir / 'config.json'} target_languages должен быть списком "
            f'кодов языков, например ["de", "es", "pt"] (сейчас: {type(raw).__name__}).'
        )
    targets = [str(code).strip() for code in raw if str(code).strip()]
    if not targets:
        raise LocalizeError(
            f"В {channel_dir / 'config.json'} target_languages пуст — "
            f"нечего проверять. Укажи хотя бы один целевой язык."
        )
    return targets


# --------------------------------------------------------------------------- #
# OAuth
# --------------------------------------------------------------------------- #

def get_authenticated_service(channel_dir):
    """
    Авторизоваться и вернуть клиент YouTube Data API.

    Токен хранится на канал: channels/<alias>/token.json. Если он есть и валиден — используем;
    истёк — обновляем через refresh token; нет — запускаем OAuth-флоу в браузере и сохраняем.
    """
    client_secret = ROOT / "client_secret.json"   # общий OAuth-клиент в корне проекта
    token_path = channel_dir / "token.json"        # refresh token — свой на каждый канал

    if not client_secret.exists():
        raise LocalizeError(
            f"Нет OAuth-клиента: {client_secret}\n"
            f"Создай в Google Cloud OAuth client типа 'Desktop app', скачай JSON "
            f"и положи его сюда (см. README.md)."
        )

    creds = None
    if token_path.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        except Exception as exc:
            print(
                f"Предупреждение: не удалось прочитать {token_path} ({exc}). "
                f"Потребуется повторная авторизация."
            )
            creds = None

    if creds and creds.valid:
        pass  # токен на месте и действителен
    elif creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as exc:
            raise LocalizeError(
                f"Не удалось обновить токен ({exc}). "
                f"Удали {token_path} и авторизуйся заново."
            )
        _save_token(creds, token_path)
    else:
        flow = InstalledAppFlow.from_client_secrets_file(str(client_secret), SCOPES)
        print("Открываю браузер для авторизации в Google…")
        creds = flow.run_local_server(port=0)
        _save_token(creds, token_path)
        print(f"Токен сохранён: {token_path}")

    return build("youtube", "v3", credentials=creds)


def _save_token(creds, token_path):
    token_path.write_text(creds.to_json(), encoding="utf-8")


# --------------------------------------------------------------------------- #
# link — привязка алиаса к каналу (единственное место, где пишется channel_id)
# --------------------------------------------------------------------------- #

def ask(prompt):
    """Спросить у пользователя строку. В неинтерактивном запуске — понятная ошибка."""
    try:
        return input(prompt).strip()
    except EOFError:
        raise LocalizeError(
            "link требует интерактивного ввода (подтверждение и email аккаунта), "
            "а стандартный ввод закрыт. Запусти команду в обычном терминале."
        )


def cmd_link(args):
    channel_dir = resolve_channel(args.channel)
    config = load_config(channel_dir)

    # Токен мог существовать до нас — тогда при отказе его удалять нельзя.
    token_path = channel_dir / "token.json"
    token_existed = token_path.exists()

    youtube = get_authenticated_service(channel_dir)
    quota = Quota()

    try:
        response = youtube.channels().list(part="snippet,statistics", mine=True).execute()
    except HttpError as exc:
        raise LocalizeError(explain_http_error(exc, "чтении канала (channels.list)", during_link=True))
    quota.charge_list()

    items = response.get("items", [])
    if not items:
        raise LocalizeError(
            "channels.list вернул пустой список — у авторизованного аккаунта нет канала.\n"
            "Скорее всего, вход выполнен под аккаунтом, у которого доступ к каналу выдан "
            "только через channel permissions (Studio → Настройки → Разрешения). Такие "
            "аккаунты YouTube API не отдаёт. Авторизуйся под владельческим аккаунтом канала."
        )

    channel = items[0]
    snippet = channel.get("snippet", {})
    stats = channel.get("statistics", {})
    channel_id = channel.get("id")
    title = snippet.get("title", "—")
    # customUrl — единственное, что различается однозначно при похожих названиях.
    handle = snippet.get("customUrl", "—")

    def fmt(value):
        try:
            return f"{int(value):,}".replace(",", " ")
        except (TypeError, ValueError):
            return "—"

    print("\nТокен авторизован на канал:")
    print(f"  ID:          {channel_id}")
    print(f"  Название:    {title}")
    print(f"  Handle:      {handle}")
    print(f"  Подписчиков: {fmt(stats.get('subscriberCount'))}")
    print(f"  Видео:       {fmt(stats.get('videoCount'))}")

    # Наш scope (youtube.force-ssl) не даёт профиль аккаунта, поэтому email
    # спрашиваем текстом, а не добавляем лишнее разрешение ради одного поля.
    owner_email = ask("\n  Аккаунт (email), под которым пройден вход: ")

    current = config.get("channel_id")
    if current and current != channel_id:
        print(
            f"\n  ВНИМАНИЕ: в конфиге уже записан другой канал — "
            f"{describe_channel(config, current)}.\n"
            f"  Подтверждение перезапишет привязку."
        )

    answer = ask(f"\nЗаписать в config.json алиаса «{args.channel}»? [y/N]: ").lower()
    if answer != "y":
        print("Отменено, конфиг не тронут.")
        if not token_existed and token_path.exists():
            token_path.unlink()
            print(f"Удалил свежесозданный {token_path}.")
            print(
                "Авторизуйся заново, выбрав правильный аккаунт: "
                "открой браузер в режиме инкогнито, иначе OAuth молча подхватит "
                "текущую сессию Google."
            )
        else:
            print(f"{token_path} оставлен как был (существовал до запуска link).")
        quota.report()
        return

    config["channel_id"] = channel_id
    config["channel_title"] = title
    config["channel_handle"] = handle
    if owner_email:
        config["owner_email"] = owner_email
    config_path = channel_dir / "config.json"
    with config_path.open("w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    print(f"\nЗаписано в {config_path}: channel_id, channel_title, channel_handle, owner_email.")
    print(f"Предохранитель включён: audit/pull/push теперь проверяют, что токен ведёт на {channel_id}.")
    quota.report()


# --------------------------------------------------------------------------- #
# Формат pulled.json (общий для pull и audit --pull)
# --------------------------------------------------------------------------- #

def build_pulled_entry(vid, item):
    """
    Собрать одну запись pulled.json из элемента ответа videos.list.

    Единственное место, где формируется этот формат: и pull, и audit --pull зовут
    именно эту функцию, поэтому структура файла у них гарантированно совпадает.
    """
    snippet = item.get("snippet", {})
    return {
        "video_id": vid,
        "title": snippet.get("title", ""),
        "description": snippet.get("description", ""),
        "defaultLanguage": snippet.get("defaultLanguage"),
        "categoryId": snippet.get("categoryId"),
        "existing_localizations": item.get("localizations", {}),
    }


def write_pulled(entries):
    """Записать записи в work/<YYYY-MM-DD>/pulled.json и вернуть путь."""
    out_dir = WORK_DIR / date.today().isoformat()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "pulled.json"
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)
    return out_path


# --------------------------------------------------------------------------- #
# pull
# --------------------------------------------------------------------------- #

def cmd_pull(args):
    channel_dir = resolve_channel(args.channel)
    config = load_config(channel_dir)
    expected_id = require_channel_binding(channel_dir, config)

    video_ids = [v.strip() for v in args.videos.split(",") if v.strip()]
    if not video_ids:
        raise LocalizeError("Не передано ни одного video_id в --videos.")
    # YouTube отдаёт максимум 50 id за один videos.list. Для больших списков нужна разбивка.
    if len(video_ids) > 50:
        raise LocalizeError(
            f"Передано {len(video_ids)} id, а videos.list берёт максимум 50 за раз. "
            f"Разбей список на части."
        )

    youtube = get_authenticated_service(channel_dir)
    quota = Quota()

    # Один батч-вызов на весь список — 1 юнит квоты.
    try:
        response = youtube.videos().list(
            part="snippet,localizations",
            id=",".join(video_ids),
        ).execute()
    except HttpError as exc:
        raise LocalizeError(explain_http_error(exc, "чтении видео (videos.list)"))
    quota.charge_list()

    # Предохранитель: видео должны принадлежать каналу этого алиаса.
    assert_videos_belong_to_channel(args.channel, config, expected_id, response.get("items", []))

    items_by_id = {item["id"]: item for item in response.get("items", [])}

    results = []
    missing = []
    for vid in video_ids:
        item = items_by_id.get(vid)
        if item is None:
            missing.append(vid)
            continue
        results.append(build_pulled_entry(vid, item))

    if missing:
        print(f"Предупреждение: не найдены (или недоступны) видео: {', '.join(missing)}")

    out_path = write_pulled(results)
    print(f"Сохранено {len(results)} видео → {out_path}")
    quota.report()


# --------------------------------------------------------------------------- #
# audit — полностью read-only: только *.list, ни одного write-вызова
# --------------------------------------------------------------------------- #

# ISO 8601 длительность из contentDetails.duration (PT1M30S, P1DT2H3M4S).
_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+)D)?"
    r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?$"
)


def parse_duration_seconds(value):
    """
    ISO 8601 → секунды. Разбираем регуляркой, без внешних зависимостей.

    Возвращает None, если длительности нет или формат неожиданный (у трансляций
    бывает 'P0D' без секции времени) — такие видео не считаем Shorts.
    """
    if not value or "T" not in value:
        return None
    match = _DURATION_RE.match(value)
    if not match:
        return None
    parts = match.groupdict()
    return (
        int(parts["days"] or 0) * 86400
        + int(parts["hours"] or 0) * 3600
        + int(parts["minutes"] or 0) * 60
        + float(parts["seconds"] or 0)
    )


def parse_since(value):
    """
    --since today | YYYY-MM-DD | Nd → дата, начиная с которой берём видео.

    Фильтруем по дате публикации, а не по позиции в плейлисте: «новое видео» —
    понятие временное, и --recent 1 молча вернул бы не то, если индексация ещё
    не прошла.
    """
    raw = (value or "").strip().lower()
    if raw == "today":
        return date.today()
    match = re.fullmatch(r"(\d+)d", raw)
    if match:
        return date.today() - timedelta(days=int(match.group(1)))
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise LocalizeError(
            f"Не понимаю --since {value!r}. Ожидается 'today', 'YYYY-MM-DD' "
            f"или 'Nd' (например 7d)."
        )


def view_count(item):
    """statistics.viewCount может отсутствовать (статистика скрыта) — тогда 0."""
    try:
        return int((item.get("statistics") or {}).get("viewCount"))
    except (TypeError, ValueError):
        return 0


def classify_localizations(item, targets):
    """
    Разложить локализации видео относительно target_languages.

    Язык оригинала (snippet.defaultLanguage) не считается ни отсутствующим переводом,
    ни лишней локалью: это оригинал, а не перевод и не мусор.

    Локали вне target_languages делятся на две группы — обращаться с ними надо
    по-разному:

      - нормализуемые: региональный вариант ЦЕЛЕВОГО языка, у которого нет базового
        кода (de-AT при отсутствующем de). Для YouTube de-AT — не de, поэтому немец
        из Германии видит оригинал, хотя немецкий текст уже лежит рядом.
        Переименование ключа расширяет охват без единого нового перевода;

      - лишние: всё остальное — региональные варианты языка оригинала (en-GB, en-CA)
        и региональные варианты целевого языка, у которого базовый код уже есть.
        Это кандидаты на --drop-locales.

    Возвращает (есть, не хватает, лишние, нормализуемые).
    """
    snippet = item.get("snippet", {})
    locales = item.get("localizations") or {}
    default_lang = snippet.get("defaultLanguage")
    present = [code for code in targets if code in locales]
    missing = [code for code in targets if code not in locales and code != default_lang]

    extra, normalizable = [], []
    for code in locales:
        if code in targets or code == default_lang:
            continue
        base = code.split("-")[0]
        # Региональный вариант целевого языка спасаем только пока базового кода нет:
        # если base уже залит, региональная копия — мусор.
        if base != code and base in targets and base not in locales:
            normalizable.append(code)
        else:
            extra.append(code)
    return present, missing, sorted(extra), sorted(normalizable)


def fetch_uploads_playlist_id(youtube, quota, config, alias, expected_id):
    """
    Шаг 1 (1 юнит): ID uploads-плейлиста авторизованного канала.

    Здесь же срабатывает предохранитель привязки — сверка идёт по вызову, который
    и так делается, поэтому не стоит дополнительной квоты.
    """
    try:
        response = youtube.channels().list(part="snippet,contentDetails", mine=True).execute()
    except HttpError as exc:
        raise LocalizeError(explain_http_error(exc, "чтении канала (channels.list)"))
    quota.charge_list()

    items = response.get("items", [])
    if not items:
        raise LocalizeError(
            "channels.list вернул пустой список — у авторизованного аккаунта нет канала. "
            "Проверь, что token.json создан от аккаунта нужного канала."
        )
    channel = items[0]

    # Предохранитель — до любых дальнейших действий.
    assert_token_matches_channel(
        alias, config, expected_id, channel.get("id"),
        channel.get("snippet", {}).get("title"),
    )

    playlist_id = (
        channel.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
    )
    if not playlist_id:
        raise LocalizeError(
            "В ответе channels.list нет relatedPlaylists.uploads — "
            "не могу определить плейлист загрузок канала."
        )
    return playlist_id


def fetch_upload_video_ids(youtube, quota, playlist_id, recent, fetch_all=False):
    """
    Шаг 2 (1 юнит за страницу): video ID из uploads-плейлиста, от новых к старым.

    Пагинация уже написана и включается флагом fetch_all — сейчас audit её не
    использует (--all отключён). Чтобы охватить весь канал, достаточно передать
    fetch_all=True: переписывать функцию не потребуется.
    """
    ids = []
    page_token = None
    while True:
        remaining = None if fetch_all else max(0, recent - len(ids))
        if remaining == 0:
            break
        try:
            response = youtube.playlistItems().list(
                part="contentDetails",
                playlistId=playlist_id,
                maxResults=50 if fetch_all else min(50, remaining),
                pageToken=page_token,
            ).execute()
        except HttpError as exc:
            raise LocalizeError(
                explain_http_error(exc, "чтении списка загрузок (playlistItems.list)")
            )
        quota.charge_list()

        for entry in response.get("items", []):
            vid = entry.get("contentDetails", {}).get("videoId")
            if vid:
                ids.append(vid)

        page_token = response.get("nextPageToken")
        if not page_token or not fetch_all:
            break
    return ids


def fetch_video_details(youtube, quota, video_ids):
    """
    Шаг 3 (1 юнит на каждые 50 ID): все нужные данные одним запросом на батч.

    Квота считается по методу, а не по количеству частей, поэтому лишние части
    (statistics, contentDetails, status) бесплатны.
    """
    details = {}
    for start in range(0, len(video_ids), 50):
        batch = video_ids[start : start + 50]
        try:
            response = youtube.videos().list(
                part="snippet,localizations,statistics,contentDetails,status",
                id=",".join(batch),
            ).execute()
        except HttpError as exc:
            raise LocalizeError(explain_http_error(exc, "чтении видео (videos.list)"))
        quota.charge_list()
        for item in response.get("items", []):
            details[item["id"]] = item
    return details


def ellipsize(text, width):
    """Обрезать до width символов, чтобы таблица не разъезжалась."""
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width <= 3:
        return text[:width]
    return text[: width - 3] + "..."


def cmd_audit(args):
    if args.all:
        raise LocalizeError(
            "--all пока не реализован: audit смотрит только последние видео "
            "(--recent N, одна страница uploads-плейлиста). Пагинация в "
            "fetch_upload_video_ids уже готова и включается флагом fetch_all."
        )

    channel_dir = resolve_channel(args.channel)
    config = load_config(channel_dir)
    targets = get_target_languages(channel_dir, config)
    expected_id = require_channel_binding(channel_dir, config)

    # --missing без значения = «не хватает хотя бы одного целевого языка».
    missing_any = args.missing is not None and not args.missing.strip()
    missing_filter = [c.strip() for c in (args.missing or "").split(",") if c.strip()]
    since_date = parse_since(args.since) if args.since else None

    if args.recent > 50:
        print(
            f"Предупреждение: запрошено --recent {args.recent}, но без --all доступна "
            f"одна страница плейлиста (максимум 50). Проверю 50 последних видео."
        )

    youtube = get_authenticated_service(channel_dir)
    quota = Quota()

    playlist_id = fetch_uploads_playlist_id(youtube, quota, config, args.channel, expected_id)
    video_ids = fetch_upload_video_ids(youtube, quota, playlist_id, args.recent)
    if not video_ids:
        print(f"{args.channel} — в uploads-плейлисте нет видео.")
        quota.report()
        return
    details = fetch_video_details(youtube, quota, video_ids)

    # Видео может быть в плейлисте, но отсутствовать в videos.list (удалено, заблокировано).
    unavailable = [vid for vid in video_ids if vid not in details]
    if unavailable:
        print(
            f"Предупреждение: {len(unavailable)} видео из плейлиста недоступны "
            f"(удалены или заблокированы) — пропускаю: {', '.join(unavailable)}"
        )

    rows = []
    for vid in video_ids:
        item = details.get(vid)
        if item is None:
            continue
        snippet = item.get("snippet", {})
        present, missing, extra, normalizable = classify_localizations(item, targets)
        duration = parse_duration_seconds((item.get("contentDetails") or {}).get("duration"))
        rows.append(
            {
                "video_id": vid,
                "title": snippet.get("title", ""),
                "published_at": (snippet.get("publishedAt") or "")[:10],
                "views": view_count(item),
                "privacy_status": (item.get("status") or {}).get("privacyStatus"),
                "duration_seconds": duration,
                "is_short": duration is not None and duration <= 60,
                "present": present,
                "missing": missing,
                "extra": extra,
                "normalizable": normalizable,
                # Сырой item нужен только для --pull: повторных вызовов API не делаем.
                "_item": item,
            }
        )

    # Итоги считаем по всему проверенному набору, а не по отфильтрованному показу.
    checked = len(rows)
    fully = sum(1 for r in rows if not r["missing"])
    untouched = sum(1 for r in rows if r["missing"] and not r["present"])
    partial = checked - fully - untouched
    with_extra = sum(1 for r in rows if r["extra"])
    with_normalizable = sum(1 for r in rows if r["normalizable"])

    # Конвейер отбора: --since → --missing → --exclude-shorts → --sort → --limit.
    selected = rows
    if since_date:
        selected = [r for r in selected if r["published_at"] >= since_date.isoformat()]
    if missing_any:
        selected = [r for r in selected if r["missing"]]
    elif missing_filter:
        selected = [r for r in selected if any(c in r["missing"] for c in missing_filter)]
    if args.exclude_shorts:
        selected = [r for r in selected if not r["is_short"]]
    if args.sort == "views":
        selected = sorted(selected, key=lambda r: r["views"], reverse=True)
    else:
        selected = sorted(selected, key=lambda r: r["published_at"], reverse=True)
    if args.limit is not None:
        selected = selected[: args.limit]

    print(f"\n{args.channel} — проверено {checked} видео, потрачено {quota.units} юнита")
    print(f"Целевые языки ({len(targets)}): {', '.join(targets)}\n")

    if not selected:
        if since_date:
            # Свежезалитое видео появляется в uploads-плейлисте не мгновенно —
            # без этой подсказки пустой результат читается как «видео нет».
            print(
                f"За период с {since_date.isoformat()} видео не найдено.\n"
                f"Если ты только что загрузил видео — YouTube ещё не проиндексировал "
                f"его в uploads-плейлисте (задержка от секунд до нескольких минут). "
                f"Подожди пару минут и повтори команду."
            )
        else:
            print("Под фильтры не подошло ни одного видео.")
    else:
        name_width = 30
        print(
            f"{'ID':<11}  {'Дата':<10}  {'Просм.':>7}  {'Название':<{name_width}}  "
            f"{'Есть':<12}  {'Не хватает':<13}  {'Нормализ.':<12}  Лишние"
        )
        for r in selected:
            marks = "[S]" if r["is_short"] else ""
            status = r["privacy_status"]
            if status and status != "public":
                marks += f"[{status}]"
            if marks:
                marks += " "
            name = marks + ellipsize(r["title"], max(0, name_width - len(marks)))

            present_cell = ",".join(r["present"]) if r["present"] else "—"
            if not r["missing"]:
                missing_cell = "—"
            elif len(r["missing"]) == len(targets):
                missing_cell = f"все {len(targets)}"
            else:
                missing_cell = ",".join(r["missing"])
            extra_cell = ",".join(r["extra"]) if r["extra"] else "—"
            norm_cell = ",".join(r["normalizable"]) if r["normalizable"] else "—"

            print(
                f"{r['video_id']:<11}  {r['published_at']:<10}  {r['views']:>7}  "
                f"{name:<{name_width}}  {ellipsize(present_cell, 12):<12}  "
                f"{ellipsize(missing_cell, 13):<13}  {ellipsize(norm_cell, 12):<12}  "
                f"{ellipsize(extra_cell, 20)}"
            )

    print(
        f"\nИтого: {checked} видео | полностью переведено {fully} | "
        f"частично {partial} | не тронуто {untouched}"
    )
    print(
        f"Лишних локалей найдено на {with_extra} видео | "
        f"нормализуемых (региональный вариант целевого без базового) — на {with_normalizable}"
    )
    print(f"Проверено {checked} · показано {len(selected)} после фильтров")

    if args.pull:
        if not selected:
            # Пустой файл затёр бы pulled.json, выгруженный ранее в тот же день.
            print("\n--pull: выгружать нечего, pulled.json не трогаю.")
        else:
            entries = [build_pulled_entry(r["video_id"], r["_item"]) for r in selected]
            out_path = write_pulled(entries)
            print(f"\nВыгружено {len(entries)} видео → {out_path}")

    if args.json:
        report = {
            "channel": args.channel,
            "generated": date.today().isoformat(),
            "target_languages": targets,
            "checked": checked,
            "shown": len(selected),
            "quota_units": quota.units,
            "totals": {
                "fully_translated": fully,
                "partial": partial,
                "untouched": untouched,
                "with_extra_locales": with_extra,
            },
            "videos": [
                {k: v for k, v in r.items() if k != "_item"} for r in rows
            ],
        }
        json_path = Path(args.json)
        json_path.parent.mkdir(parents=True, exist_ok=True)
        with json_path.open("w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"Отчёт аудита → {json_path}")

    quota.report()


# --------------------------------------------------------------------------- #
# push (read-modify-write)
# --------------------------------------------------------------------------- #

def cmd_push(args):
    channel_dir = resolve_channel(args.channel)
    config = load_config(channel_dir)
    expected_id = require_channel_binding(channel_dir, config)
    default_language = config["default_language"]
    drop_locales = [c.strip() for c in args.drop_locales.split(",") if c.strip()]
    protected = sorted(set(drop_locales) & PROTECTED_LOCALES)
    if protected:
        print(
            f"Локали {', '.join(protected)} защищены от удаления "
            f"(осмысленные переводы, не региональный мусор) — исключаю из --drop-locales."
        )
        drop_locales = [c for c in drop_locales if c not in PROTECTED_LOCALES]

    translations = _load_translations(args.file)

    youtube = get_authenticated_service(channel_dir)
    quota = Quota()

    updated = 0
    for entry in translations:
        vid = entry.get("video_id")
        new_locs = entry.get("localizations") or {}
        if not vid:
            print("Предупреждение: запись без video_id пропущена.")
            continue
        # Пропускаем только когда делать нечего вообще: нет новых переводов И не задан
        # --drop-locales И не задан --set-default-language. Если применимо хоть одно
        # действие — видео обрабатывается (в т.ч. режим «только почистить/сменить default»).
        if not new_locs and not drop_locales and not args.set_default_language:
            print(f"[{vid}] ни переводов, ни --drop-locales, ни --set-default-language — пропускаю.")
            continue

        # 1. Свежий videos.list — не полагаемся на pulled.json, данные могли устареть.
        try:
            response = youtube.videos().list(
                part="snippet,localizations", id=vid
            ).execute()
        except HttpError as exc:
            raise LocalizeError(
                explain_http_error(exc, f"чтении видео {vid} (videos.list)")
            )
        quota.charge_list()

        items = response.get("items", [])
        if not items:
            print(f"[{vid}] видео не найдено или недоступно — пропускаю.")
            continue

        # Предохранитель: не писать в чужой канал. Проверяем до любых изменений.
        assert_videos_belong_to_channel(args.channel, config, expected_id, items)

        item = items[0]
        # 2. Полный существующий snippet. videos.update с part=snippet перезатирает snippet
        #    целиком, поэтому возвращаем его как есть (title, categoryId, description, tags,
        #    defaultLanguage, defaultAudioLanguage — всё, что есть). Read-only поля API игнорирует.
        snippet = item.get("snippet", {})
        if not snippet.get("title"):
            print(f"[{vid}] у видео пустой title — пропускаю, чтобы не сломать snippet.")
            continue
        # Копия для сравнения: нужна, чтобы понять, изменился ли snippet вообще.
        original_snippet = json.loads(json.dumps(snippet))

        # 2a. Необязательное обновление самого оригинала (snippet.title/description).
        #     Только по явному флагу: молча перезаписывать авторский текст нельзя.
        original = entry.get("snippet") or {}
        if original and not args.update_original:
            print(
                f"[{vid}] в файле есть блок snippet, но нет --update-original — "
                f"оригинал не тронут."
            )
        elif original:
            for field in ("title", "description"):
                new_value = original.get(field)
                if new_value is None or new_value == snippet.get(field):
                    continue
                old_len, new_len = len(snippet.get(field) or ""), len(new_value)
                snippet[field] = new_value
                print(f"[{vid}] оригинал: {field} обновляется ({old_len} → {new_len} симв).")

        # 3. defaultLanguage обязателен, иначе локализации не активируются. По умолчанию
        #    проставляем его только когда он пуст; с --set-default-language принудительно
        #    перезаписываем на default_language из config.json (даже если уже задан).
        current_default = snippet.get("defaultLanguage")
        if args.set_default_language:
            if current_default != default_language:
                print(f"[{vid}] defaultLanguage: {current_default or '—'} → {default_language}.")
                snippet["defaultLanguage"] = default_language
        elif not current_default:
            snippet["defaultLanguage"] = default_language
            print(f"[{vid}] defaultLanguage не был задан — проставляю '{default_language}'.")

        # Предохранители по кодам локалей: запрещённые владельцем и заведомо
        # отвергаемые API. Отсекаем до update, иначе одна локаль роняет весь батч.
        rejected = sorted(c for c in new_locs if BANNED_LOCALES.match(c))
        if rejected:
            print(f"[{vid}] запрещённые локали пропущены (индийский регион): {', '.join(rejected)}")
        bad = sorted(c for c in new_locs if c in UNSUPPORTED_LOCALES)
        if bad:
            print(f"[{vid}] локали не поддерживаются videos.update, пропущены: {', '.join(bad)}")
        if rejected or bad:
            new_locs = {k: v for k, v in new_locs.items()
                        if not BANNED_LOCALES.match(k) and k not in UNSUPPORTED_LOCALES}

        # 4. Read-modify-write объекта localizations: берём существующие → удаляем указанные
        #    в --drop-locales → мёржим новые переводы. Новые языки добавляются, совпадающие
        #    перезаписываются; существующие языки не из файла и не в drop остаются нетронутыми.
        existing_locs = item.get("localizations") or {}
        merged = dict(existing_locs)

        # Удаляем только реально присутствующие локали, которые не перекрываются новыми
        # переводами (итог тот же, что «удалить всё из drop, затем смёржить новые»).
        dropped = sorted(c for c in drop_locales if c in merged and c not in new_locs)
        for code in dropped:
            del merged[code]

        added, overwritten = [], []
        for lang, loc in new_locs.items():
            (overwritten if lang in merged else added).append(lang)
            merged[lang] = loc

        change = []
        if added:
            change.append(f"добавляются: {', '.join(sorted(added))}")
        if overwritten:
            change.append(f"перезаписываются: {', '.join(sorted(overwritten))}")
        if dropped:
            change.append(f"удаляются: {', '.join(dropped)}")
        keep = sorted(set(existing_locs) - set(new_locs) - set(dropped))
        if keep:
            change.append(f"сохраняются без изменений: {', '.join(keep)}")
        print(f"[{vid}] " + ("; ".join(change) if change else "изменений нет"))

        # Валидация длины перед отправкой: title ≤ 100, description ≤ 5000 символов (по len()).
        # Проверяем и оригинальный snippet, и каждый язык новых переводов. При превышении это
        # видео пропускаем — update не вызываем (в т.ч. при --dry-run).
        max_title, max_desc = 100, 5000
        to_check = [(
            f"snippet/{snippet.get('defaultLanguage', '—')}",
            snippet.get("title", ""),
            snippet.get("description", ""),
        )]
        for lang in sorted(new_locs):
            loc = new_locs[lang] or {}
            to_check.append((lang, loc.get("title", ""), loc.get("description", "")))

        violations = []
        for label, loc_title, loc_desc in to_check:
            if len(loc_title) > max_title:
                violations.append((label, "title", len(loc_title), max_title))
            if len(loc_desc) > max_desc:
                violations.append((label, "description", len(loc_desc), max_desc))

        if violations:
            print(f"[{vid}] превышены лимиты длины — видео пропущено, update не будет:")
            for label, field, actual, limit in violations:
                print(f"    {label}: {field} — {actual} символов (лимит {limit}).")
            continue

        # Если менять нечего — не тратим 50 юнитов на перезапись тем же содержимым.
        # Ловит случай, когда локаль из --drop-locales уже удалена руками в Studio.
        if not added and not overwritten and not dropped and snippet == original_snippet:
            print(f"[{vid}] изменений нет — update не нужен, пропускаю (экономия {COST_UPDATE} юнитов).")
            continue

        if args.dry_run:
            continue

        # 5. Обновляем видео целиком: полный snippet + смёрженные localizations — 50 юнитов.
        body = {"id": vid, "snippet": snippet, "localizations": merged}
        try:
            youtube.videos().update(part="snippet,localizations", body=body).execute()
        except HttpError as exc:
            raise LocalizeError(
                explain_http_error(exc, f"обновлении видео {vid} (videos.update)")
            )
        quota.charge_update()
        updated += 1
        print(f"[{vid}] обновлено.")
        record_push_state(channel_dir, vid, snippet, new_locs.keys())

    if args.dry_run:
        print("\n--dry-run: ничего не отправлено (update не вызывался).")
    else:
        print(f"\nОбновлено видео: {updated}.")
    quota.report()


def record_push_state(channel_dir, vid, snippet, locales):
    """
    Дописать факт заливки в channels/<alias>/state.json.

    Сейчас это состояние никто не читает — мы просто начинаем его накапливать, чтобы
    позже детектить протухшие переводы: YouTube не отдаёт timestamp локализаций, так
    что момент заливки и хэш оригинала иначе не восстановить.

    Никогда не роняет push: не смогли записать — предупредили и пошли дальше.
    """
    path = channel_dir / "state.json"
    try:
        state = {}
        if path.exists():
            with path.open(encoding="utf-8") as f:
                state = json.load(f)
            if not isinstance(state, dict):
                print(f"    Предупреждение: {path} не является объектом — перезаписываю с нуля.")
                state = {}

        source = (snippet.get("title") or "") + "\n" + (snippet.get("description") or "")
        source_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()

        # locales_pushed должен отвечать на вопрос «какие локали актуальны для ЭТОГО
        # оригинала». Пока оригинал тот же — накапливаем (заливка часто идёт в
        # несколько заходов). Оригинал изменился — прежние локали устарели, начинаем
        # список заново.
        previous = state.get(vid) or {}
        known = set(previous.get("locales_pushed") or []) \
            if previous.get("source_hash") == source_hash else set()

        state[vid] = {
            "last_push": date.today().isoformat(),
            "source_hash": source_hash,
            "locales_pushed": sorted(known | set(locales)),
        }
        with path.open("w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
    except Exception as exc:
        print(f"    Предупреждение: не удалось записать {path} ({exc}).")


def _load_translations(path):
    file_path = Path(path)
    if not file_path.exists():
        raise LocalizeError(f"Файл с переводами не найден: {file_path}")
    try:
        with file_path.open(encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as exc:
        raise LocalizeError(f"Не удалось разобрать {file_path}: {exc}")
    if not isinstance(data, list):
        raise LocalizeError(
            f"{file_path}: ожидался список записей вида "
            f"[{{\"video_id\": ..., \"localizations\": {{...}}}}, ...]."
        )
    return data


# --------------------------------------------------------------------------- #
# Ошибки API и точка входа
# --------------------------------------------------------------------------- #

def explain_http_error(exc, context, during_link=False):
    """Превратить HttpError в понятное сообщение (особенно про квоту и права доступа)."""
    status = getattr(exc.resp, "status", None)
    reason, message = "", ""
    try:
        data = json.loads(exc.content.decode("utf-8"))
        err = data.get("error", {})
        message = err.get("message", "")
        errors = err.get("errors", [])
        if errors:
            reason = errors[0].get("reason", "")
    except Exception:
        message = str(exc)

    base = f"Ошибка YouTube API при {context} (HTTP {status}, reason={reason or '—'}): {message}"

    if status == 403 and reason in ("quotaExceeded", "dailyLimitExceeded"):
        return (
            base + "\n"
            "Исчерпана дневная квота (по умолчанию 10 000 юнитов). Подожди сброса квоты "
            "(полночь по тихоокеанскому времени) или запроси увеличение в Google Cloud."
        )
    if status == 403 and during_link:
        return (
            base + "\n\n"
            "Похоже, у аккаунта нет доступа к каналу через YouTube API.\n\n"
            "Каналы, где доступ выдан только через channel permissions "
            "(Studio → Настройки → Разрешения), в API не видны вообще — Google не даёт "
            "таким пользователям доступ к YouTube Data API. В списке выбора аккаунта при "
            "OAuth появляются только каналы, доступные через владельческий аккаунт или "
            "старую систему Brand Account user access.\n\n"
            "Что делать: авторизоваться под ВЛАДЕЛЬЧЕСКИМ аккаунтом этого канала. "
            "Открой браузер в режиме инкогнито (иначе OAuth молча подхватит текущую "
            "сессию Google), удали channels/<alias>/token.json и повтори link."
        )
    if status == 403 and reason in ("forbidden", "insufficientPermissions", "authorizationError"):
        return (
            base + "\n"
            "Недостаточно прав на запись метаданных. Нужен OAuth от аккаунта, у которого есть "
            "право редактировать это видео (владелец канала или роль с доступом на изменение). "
            "Удали channels/<alias>/token.json и авторизуйся нужным аккаунтом."
        )
    if status == 401:
        return (
            base + "\n"
            "Токен недействителен. Удали channels/<alias>/token.json и авторизуйся заново."
        )
    if status == 404:
        return base + "\nВидео не найдено — проверь video_id."
    return base


def build_parser():
    parser = argparse.ArgumentParser(
        description="Транспорт локализации метаданных YouTube (pull/push). Сам не переводит."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_link = sub.add_parser(
        "link",
        help="Привязать алиас к каналу: показать, куда ведёт токен, и записать channel_id.",
    )
    p_link.add_argument("--channel", required=True, help="alias канала (папка в channels/).")
    p_link.set_defaults(func=cmd_link)

    p_audit = sub.add_parser(
        "audit",
        help="Найти непереведённые видео на канале (read-only, ~3 юнита).",
    )
    p_audit.add_argument("--channel", required=True, help="alias канала (папка в channels/).")
    p_audit.add_argument(
        "--recent", type=int, default=30, help="сколько последних видео проверять (по умолчанию 30)."
    )
    p_audit.add_argument(
        "--all",
        action="store_true",
        help="весь канал с пагинацией (пока не реализовано).",
    )
    p_audit.add_argument(
        "--since",
        metavar="today|YYYY-MM-DD|Nd",
        help="только видео, опубликованные начиная с этой даты (7d = за последние 7 дней).",
    )
    p_audit.add_argument(
        "--missing",
        nargs="?",
        const="",
        default=None,
        metavar="код1,код2",
        help="оставить видео, где нет этих языков. Без значения — где не хватает "
        "хотя бы одного языка из target_languages.",
    )
    p_audit.add_argument(
        "--sort",
        choices=("recent", "views"),
        default="recent",
        help="порядок сортировки отобранных (по умолчанию recent).",
    )
    p_audit.add_argument(
        "--exclude-shorts",
        action="store_true",
        help="отсечь видео длительностью 60 секунд и меньше.",
    )
    p_audit.add_argument("--limit", type=int, help="взять первые N после сортировки.")
    p_audit.add_argument(
        "--pull",
        action="store_true",
        help="выгрузить отобранные в work/<дата>/pulled.json (без доп. вызовов API).",
    )
    p_audit.add_argument("--json", metavar="PATH", help="сохранить полный отчёт аудита в JSON.")
    p_audit.set_defaults(func=cmd_audit)

    p_pull = sub.add_parser("pull", help="Скачать оригинальные метаданные видео.")
    p_pull.add_argument("--channel", required=True, help="alias канала (папка в channels/).")
    p_pull.add_argument("--videos", required=True, help="video ID через запятую.")
    p_pull.set_defaults(func=cmd_pull)

    p_push = sub.add_parser("push", help="Залить переводы из translated.json.")
    p_push.add_argument("--channel", required=True, help="alias канала (папка в channels/).")
    p_push.add_argument("--file", required=True, help="путь к translated.json.")
    p_push.add_argument(
        "--dry-run",
        action="store_true",
        help="показать, что будет отправлено, но не вызывать update.",
    )
    p_push.add_argument(
        "--update-original",
        action="store_true",
        help="применить блок snippet из файла к оригиналу (title/description). "
        "Без флага оригинал не меняется.",
    )
    p_push.add_argument(
        "--set-default-language",
        action="store_true",
        help="принудительно выставить snippet.defaultLanguage = default_language из config, "
        "даже если он уже задан.",
    )
    p_push.add_argument(
        "--drop-locales",
        default="",
        metavar="код1,код2,...",
        help="удалить эти локали из localizations перед update (напр. en-CA,en-GB,en-US).",
    )
    p_push.set_defaults(func=cmd_push)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
    except LocalizeError as exc:
        sys.exit(f"\nОшибка: {exc}")
    except KeyboardInterrupt:
        sys.exit("\nПрервано пользователем.")


if __name__ == "__main__":
    main()
