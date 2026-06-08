# Planner Bot

Личный Telegram-бот для планирования задач с напоминаниями.

## Возможности

- Свободный ввод: просто напиши "встреча в 15:00" или "позвонить маме через 2 часа"
- Голосовые сообщения — бот распознаёт речь локально через Whisper (без API-ключа) и создаёт задачу
- Двойное напоминание: за 10 минут и в точное время
- Команды: `/add`, `/list`, `/done`

## Установка

### 1. Клонируй репозиторий

```bash
git clone https://github.com/Samyra1XZ/planner-bot.git
cd planner-bot
```

### 2. Установи зависимости

```bash
pip install -r requirements.txt
```

### 3. Установи ffmpeg (нужен для голосовых сообщений)

**Windows:**
```bash
winget install ffmpeg
```
или скачай с https://ffmpeg.org/download.html и добавь в PATH.

**macOS:**
```bash
brew install ffmpeg
```

**Linux (Ubuntu/Debian):**
```bash
sudo apt install ffmpeg
```

**Railway / деплой:**
Создай файл `nixpacks.toml` в корне:
```toml
[phases.setup]
nixPkgs = ["ffmpeg"]
```

### 4. Создай файл `.env`

```bash
cp .env.example .env
```

Заполни значения:
```
BOT_TOKEN=токен_от_BotFather
MY_CHAT_ID=твой_chat_id
```

Узнать свой chat_id: напиши боту `@userinfobot` в Telegram.

### 5. Запусти бота

```bash
python bot.py
```

> При первом запуске бот скачает модель Whisper (`base`, ~140 МБ) — это происходит один раз.
> Размер модели задаётся константой `WHISPER_MODEL` в начале `bot.py`. На слабом железе
> (Railway, ноутбук) оставь `base`; на мощном VPS можно поднять до `small`/`medium` ради точности.

## Использование

| Способ | Пример |
|--------|--------|
| Свободный текст | `встреча с клиентом в 15:00` |
| Через час | `позвонить маме через 2 часа` |
| Голосовое | отправь голосовое сообщение |
| Команда | `/add 15:00 встреча с клиентом` |
| Список | `/list` |
| Выполнить | `/done 3` |
