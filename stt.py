"""
Распознавание речи (Speech-To-Text) со сменным движком.

Движок выбирается переменной окружения STT_PROVIDER:
  - "groq"    — облако Groq (по умолчанию). Ничего не грузит на хост,
                идеально для Railway и личного бота.
  - "whisper" — локальная модель Whisper. Аудио НЕ покидает сервер —
                для клиентов с требованием конфиденциальности.
                Нужен ffmpeg на хосте.

Весь остальной код зовёт только transcribe(path) и не знает, какой движок внутри —
поэтому сменить провайдера = поменять одну переменную, а не переписывать бота.
"""

import os

STT_PROVIDER = os.getenv("STT_PROVIDER", "groq")

# Модель Groq: turbo — самая быстрая и точная из Whisper-семейства.
GROQ_MODEL = "whisper-large-v3-turbo"
# Размер локальной модели Whisper (если STT_PROVIDER=whisper).
# "base" — лёгкая; на мощном сервере можно "small"/"medium".
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "base")

# Клиент/модель создаём один раз и кешируем (ленивая инициализация).
_groq_client = None
_whisper_model = None


def _transcribe_groq(audio_path: str) -> str:
    """Распознаёт через облако Groq. Принимает .ogg напрямую — конвертация не нужна."""
    global _groq_client
    if _groq_client is None:
        from groq import Groq
        _groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))

    with open(audio_path, "rb") as f:
        result = _groq_client.audio.transcriptions.create(
            file=(os.path.basename(audio_path), f.read()),
            model=GROQ_MODEL,
            language="ru",
        )
    return result.text.strip()


def _transcribe_whisper(audio_path: str) -> str:
    """Распознаёт локально через Whisper. Аудио не уходит наружу."""
    global _whisper_model
    if _whisper_model is None:
        import whisper
        _whisper_model = whisper.load_model(WHISPER_MODEL)

    result = _whisper_model.transcribe(audio_path, language="ru")
    return result["text"].strip()


def transcribe(audio_path: str) -> str:
    """
    Распознаёт речь из аудиофайла и возвращает текст.
    Движок берётся из STT_PROVIDER.
    """
    if STT_PROVIDER == "whisper":
        return _transcribe_whisper(audio_path)
    return _transcribe_groq(audio_path)
