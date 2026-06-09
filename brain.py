"""
«Мозг» бота — разбор любого сообщения через Google Gemini.

Раньше задачу/дату/время вытаскивали самописные регулярки (extract_time,
extract_date, clean_task_text). Это было хрупко: лишние слова попадали в текст,
«завтра»/«в пятницу» ловились через раз. Теперь весь разбор делает Gemini —
он понимает естественную речь и возвращает СТРОГО JSON со списком действий.

Остальной бот зовёт только parse_message(text) и получает список словарей —
он не знает, какая модель внутри. Захочешь сменить провайдера (например, на
локальную модель для конфиденциальности клиентов) — меняется только этот файл.
"""

import json
import os
import time
from datetime import datetime

from dotenv import load_dotenv

# TZ — единый часовой пояс проекта (Asia/Makassar, UTC+8). Берём из scheduler,
# чтобы не плодить копии. scheduler ничего из brain не импортирует — цикла нет.
from scheduler import TZ

load_dotenv()

# Модель Gemini. flash — быстрая и бесплатная, её хватает для разбора фраз.
GEMINI_MODEL = "gemini-2.5-flash"
# Запасная модель: если основная перегружена (503), пробуем её — она стабильнее.
GEMINI_FALLBACK_MODEL = "gemini-2.0-flash"
_MODELS = [GEMINI_MODEL, GEMINI_FALLBACK_MODEL]
# Сколько попыток на каждую модель (с паузой между ними — спасает от 503-всплесков).
_ATTEMPTS_PER_MODEL = 3

# Клиент создаём один раз и кешируем (ленивая инициализация — как у STT).
_client = None


def _get_client():
    global _client
    if _client is None:
        from google import genai
        _client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
    return _client


# Дни недели по-русски — подставляем в промт, чтобы «в пятницу» считалось верно.
_RU_WEEKDAYS = [
    "понедельник", "вторник", "среда", "четверг",
    "пятница", "суббота", "воскресенье",
]


def _build_system_prompt(now: datetime) -> str:
    """
    Собирает системный промт с АКТУАЛЬНОЙ датой/временем (по Бали),
    чтобы «завтра», «через час», «в пятницу» Gemini считал правильно.
    """
    today_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H:%M")
    weekday = _RU_WEEKDAYS[now.weekday()]

    return f"""Ты — разборщик сообщений для личного планера-бота.
Пользователь пишет или диктует голосом задачи на русском. Твоя работа —
превратить сообщение в СПИСОК ДЕЙСТВИЙ и вернуть его СТРОГО как JSON-массив.

КОНТЕКСT ВРЕМЕНИ (часовой пояс Asia/Makassar, UTC+8, Бали):
- Сегодня: {today_str} ({weekday})
- Сейчас: {time_str}
Считай «сегодня», «завтра», «послезавтра», «в пятницу», «через 2 часа»,
«через 30 минут» относительно ЭТОГО момента и пояса.

ТИПЫ ДЕЙСТВИЙ (в одной фразе их может быть несколько):
1. {{"type": "task", "text": "...", "datetime": "YYYY-MM-DD HH:MM"}}
   — добавить разовую задачу.
2. {{"type": "birthday", "name": "...", "date": "MM-DD"}}
   — добавить день рождения (ежегодный, без года). name — чей это ДР.
3. {{"type": "delete", "text": "..."}}
   — удалить задачу. text — краткое описание, что удалить.
4. {{"type": "reschedule", "text": "...", "datetime": "YYYY-MM-DD HH:MM"}}
   — перенести существующую задачу на новое время.

ПРАВИЛА:
- В поле text — ТОЛЬКО чистая суть, с заглавной буквы. Вырезай мусорные слова:
  «запиши», «напомни», «у меня», «надо», «нужно», «короче», «потом», «ну»,
  «пожалуйста», «давай», «мне». Пример: «короче запиши мне завтра тренировку» → text="Тренировка".
- Если для task время НЕ указано — ставь 09:00 того же дня.
- Если дата не указана, а время есть — ставь сегодняшнюю дату. Если это время
  сегодня уже прошло — ставь завтрашнюю дату.
- birthday: год не указываем, только месяц-день в формате MM-DD.
- Если в сообщении нет ничего осмысленного для планера — верни пустой массив [].

ФОРМАТ ОТВЕТА:
- Только JSON-массив. Никакого markdown, никаких ```json, никакого текста до или после.
- datetime строго "YYYY-MM-DD HH:MM" (24 часа). date строго "MM-DD".
"""


# Разрешённые типы действий — всё лишнее от модели отфильтруем.
_VALID_TYPES = {"task", "birthday", "delete", "reschedule"}


def _strip_fences(raw: str) -> str:
    """На всякий случай срезаем ```json ... ``` если модель их всё же добавила."""
    s = raw.strip()
    if s.startswith("```"):
        s = s.lstrip("`")                 # убираем стартовые бэктики
        if s[:4].lower() == "json":
            s = s[4:]
        s = s.strip()
        if s.endswith("```"):
            s = s[:-3].strip()
    return s


def _call_gemini(text: str, now: datetime, model: str) -> str:
    """Один вызов Gemini указанной моделью, возвращает сырой текст ответа."""
    from google.genai import types

    cfg = dict(
        system_instruction=_build_system_prompt(now),
        response_mime_type="application/json",  # заставляем вернуть валидный JSON
        temperature=0,                          # детерминированный разбор
    )
    # «Размышления» есть только у моделей 2.5 — отключаем (для разбора не нужны,
    # на длинных фразах съедают лимит вывода). Для 2.0 параметра нет.
    if model.startswith("gemini-2.5"):
        cfg["thinking_config"] = types.ThinkingConfig(thinking_budget=0)

    client = _get_client()
    response = client.models.generate_content(
        model=model,
        contents=text,
        config=types.GenerateContentConfig(**cfg),
    )
    return response.text or ""


def parse_message(text: str) -> list:
    """
    Отправляет фразу в Gemini и возвращает список действий (list[dict]).
    Пустой список — нечего делать или не понял. При ошибке сети/разбора
    бросает исключение — бот ловит его и просит переформулировать.

    Устойчивость к 503 (перегрузка Gemini): несколько попыток на основную модель
    с нарастающей паузой, затем переключение на запасную модель.
    """
    now = datetime.now(TZ)

    # План попыток: (модель, номер попытки) — основная, потом запасная.
    plan = [(m, a) for m in _MODELS for a in range(_ATTEMPTS_PER_MODEL)]
    last_err = None

    for idx, (model, attempt) in enumerate(plan):
        try:
            raw = _strip_fences(_call_gemini(text, now, model))
            if not raw:
                raise ValueError("пустой ответ Gemini")
            data = json.loads(raw)                  # кривой JSON → бросит исключение
            if not isinstance(data, list):
                data = [data]                       # на случай одиночного объекта
            # Оставляем только словари с известным типом — мусор игнорируем.
            return [a for a in data if isinstance(a, dict) and a.get("type") in _VALID_TYPES]
        except Exception as e:
            last_err = e
            print(f"[brain] {model} попытка {attempt + 1}: {e!r}")
            if idx < len(plan) - 1:                 # перед следующей попыткой — пауза
                time.sleep(min(2 ** attempt, 4))    # 1, 2, 4 сек

    raise last_err
