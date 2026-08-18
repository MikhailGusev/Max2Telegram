# Развёртывание на сервере

Инструкция под Ubuntu + systemd. Все команды на сервере выполняет владелец
сервера вручную — по установленному правилу работы Claude по SSH не ходит.

## 1. Код на сервер

```bash
mkdir -p /root/maxbridge && cd /root/maxbridge
git clone git@github.com:<owner>/maxbridge.git .
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Если репозиторий приватный — заведи deploy-key по рецепту из внутреннего
`SERVER_HANDOFF.md` (ключ на репозиторий, `Allow write access` не включать).

## 2. Конфигурация

```bash
cp .env.example .env
nano .env          # токены, id владельца, режим MAX
chmod 600 .env
```

## 3. Вход в MAX

Логин интерактивный — нужен SMS-код, поэтому выполняется руками один раз:

```bash
cd /root/maxbridge && .venv/bin/python -m maxbridge login
```

Создастся `data/max_session.json`. Телефон при этом продолжает работать:
это параллельная WEB-сессия, как открытый web.max.ru.

## 4. Проверка перед запуском

```bash
.venv/bin/python -m maxbridge check
```

Покажет, что настроено, есть ли сессия, включён ли AI, какие каналы эскалации
готовы и что говорит лицензия.

## 5. systemd

```bash
cp deploy/maxbridge.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now maxbridge
systemctl status maxbridge
```

Логи:

```bash
journalctl -u maxbridge -f          # живой поток
journalctl -u maxbridge --since today
```

Файловый лог дублируется в `data/maxbridge.log`. Он растёт — заведи logrotate
или подрезай вручную: `: > /root/maxbridge/data/maxbridge.log`.

## 6. Обновление

```bash
cd /root/maxbridge
git pull origin main
.venv/bin/pip install -r requirements.txt   # если менялись зависимости
systemctl restart maxbridge
```

## Egress

Мосту нужен доступ к:

| Хост | Зачем |
|---|---|
| `ws-api.oneme.ru` | внутренний протокол MAX (режим userbot) |
| `platform-api.max.ru` | Bot API MAX (режим botapi) |
| `api.telegram.org` | Telegram |
| `api.anthropic.com` | AI-функции |
| `sms.ru` / `smsc.ru` / `api.twilio.com` | SMS-эскалация |
| `graph.facebook.com` | WhatsApp Cloud API |

Если на сервере включён белый список исходящих соединений, добавь нужные хосты,
иначе соответствующие функции молча перестанут работать.

## Что проверить после запуска

1. В Telegram-группе выполни `/bind`, затем `/status` — транспорт должен быть
   `userbot` с охватом «все сообщения аккаунта».
2. Напиши себе в MAX с другого аккаунта — в группе появится новая тема.
3. Ответь в теме — сообщение должно прийти в MAX от твоего имени.
4. `/find` по слову из этого сообщения — проверка индексации.

## Диагностика

| Симптом | Причина |
|---|---|
| «нет сохранённой сессии MAX» | не выполнен `python -m maxbridge login` |
| темы не создаются | бот не админ группы или выключены Темы (Topics) |
| «thread not found» | тему удалили руками — мост пересоздаст её сам |
| сообщения не приходят в режиме botapi | так и задумано: бот видит только адресованное ему |
| эскалация молчит | `ESCALATE_AFTER_MINUTES=0`, канал не настроен или нужна лицензия Pro |
| после логина мост отвалился | в приложении MAX завершили сессию «MaxBridge» |
