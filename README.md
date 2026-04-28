# AFK Discord Bot (approve/reject)

Бот создает AFK-заявки через slash-команду `/afk` и отправляет их в канал модерации, где модераторы могут нажать:
- `Принять`
- `Отклонить`

После решения бот обновляет статус заявки и отправляет пользователю уведомление в ЛС.

## 1) Установка

```bash
python -m venv .venv
```

Windows PowerShell:

```bash
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## 2) Настройка переменных

Скопируй `.env.example` в `.env` и заполни:

- `515c0ecd51119677565f34485c652d7e9f93acde7df870908e4089f192d518cb` - токен бота из Discord Developer Portal
- `1458244914398887939` - ID твоего сервера
- `1458249019301302402` - ID канала, куда летят заявки
- `MODERATOR_ROLE_ID` - ID роли, которая может принимать/отклонять
  - Если не хочешь ограничивать по роли, укажи `0`
- `DATABASE_PATH` - путь к SQLite (по умолчанию `afk_reports.db`)

## 3) Настройка Discord-приложения

1. Открой <https://discord.com/developers/applications>
2. `New Application` -> `Bot` -> скопируй токен
3. В `OAuth2 -> URL Generator`:
   - Scopes: `bot`, `applications.commands`
   - Bot Permissions:
     - Send Messages
     - Embed Links
     - Read Message History
     - Use Slash Commands
4. Пригласи бота на сервер по сгенерированной ссылке

## 4) Запуск

```bash
python bot.py
```

После запуска команда `/afk` появится на указанном сервере.

---

## Как пользоваться

Пользователь пишет:

`/afk reason:болею until_text:01.05 18:00 comment:если срочно, пинг в лс`

Бот отправляет заявку в канал модерации с кнопками `Принять` и `Отклонить`.

---

## Бесплатный хостинг: вариант 1 (самый простой) — Railway

### Шаги

1. Создай аккаунт: <https://railway.app>
2. Создай новый проект `New Project -> Deploy from GitHub repo`
3. Залей этот проект в GitHub
4. В Railway открой `Variables` и добавь:
   - `DISCORD_TOKEN`
   - `GUILD_ID`
   - `AFK_REVIEW_CHANNEL_ID`
   - `MODERATOR_ROLE_ID`
   - `DATABASE_PATH=afk_reports.db`
5. В `Settings` проверь стартовую команду:
   - `python bot.py`
6. Нажми Deploy

Важно: на бесплатных тарифах могут быть лимиты/засыпание.

---

## Бесплатный хостинг: вариант 2 (более стабильный 24/7) — Oracle Cloud Free VM

### Шаги

1. Зарегистрируйся в Oracle Cloud (Free Tier)
2. Создай Always Free VM (Ubuntu)
3. Подключись по SSH
4. Установи Python и git:

```bash
sudo apt update && sudo apt install -y python3 python3-venv python3-pip git
```

5. Клонируй репозиторий с ботом
6. Создай `.env` на сервере и заполни переменные
7. Установи зависимости:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

8. Запусти через `systemd`, чтобы бот стартовал автоматически

Создай файл `/etc/systemd/system/afkbot.service`:

```ini
[Unit]
Description=Discord AFK Bot
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/afk-discord-bot
EnvironmentFile=/home/ubuntu/afk-discord-bot/.env
ExecStart=/home/ubuntu/afk-discord-bot/.venv/bin/python bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Активируй:

```bash
sudo systemctl daemon-reload
sudo systemctl enable afkbot
sudo systemctl start afkbot
sudo systemctl status afkbot
```

Проверка логов:

```bash
journalctl -u afkbot -f
```

---

## Что можно улучшить дальше

- Добавить причину отклонения через модальное окно
- Логи в отдельный канал (`#afk-logs`)
- Авто-роль AFK пользователю после одобрения
- Веб-панель для списка заявок
