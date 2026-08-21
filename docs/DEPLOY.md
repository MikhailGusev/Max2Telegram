# Развёртывание на сервере

Инструкция под Ubuntu + systemd. Все команды на сервере выполняет владелец
сервера вручную — по установленному правилу работы Claude по SSH не ходит.

## 1. Код на сервер

```bash
mkdir -p /root/maxbridge && cd /root/maxbridge
git clone git@github.com:MikhailGusev/Max2Telegram.git .
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

## Российский сервер и Anthropic

Мост правильнее ставить в той же стране, где обычно используется аккаунт MAX:
сессия userbot приходит с IP сервера, и вход с зарубежного дата-центра — ровно
тот признак, на который срабатывают антифрод-проверки. Плюс переписка ложится
в локальную базу, а это уже вопрос 152-ФЗ, если продукт продаётся в России.

Проверить доступность хостов прямо с сервера:

```bash
for h in api.telegram.org ws-api.oneme.ru api.anthropic.com; do curl -sS -o /dev/null -m 10 -w "$h: %{http_code}\n" "https://$h"; done
```

`200` или `30x` — хост доступен. У `api.anthropic.com` ожидаем `401`
(нет ключа); **`403` означает отказ по региону** — Anthropic не обслуживает
эту страну, и AI-функции работать не будут.

### Прокси только для AI

Лечится выпуском наружу через зарубежный сервер — но **только для Anthropic**.
MAX и Telegram обязаны ходить напрямую, иначе теряется весь смысл локального
размещения.

На зарубежном сервере ставим прокси и пускаем в него **только** IP российского:

```bash
apt install -y tinyproxy
```

В `/etc/tinyproxy/tinyproxy.conf` оставить `Port 8888` и прописать
`Allow <IP-российского-сервера>`, убрав остальные `Allow`. Затем:

```bash
systemctl restart tinyproxy
```

> Не оставляй прокси открытым: без ограничения по IP его за сутки найдут
> и начнут через тебя ходить куда попало.

На российском сервере в `.env`:

```bash
AI_PROXY=http://IP-ЗАРУБЕЖНОГО-СЕРВЕРА:8888
```

Проверка, что подхватилось, — в выводе `python -m maxbridge check` строка
`прокси: ... (только для Anthropic)`.

Без прокси мост работает полностью, просто приоритеты считаются правилами,
а не моделью: пропадают AI-черновики и осмысленные сводки.

### WhatsApp с российского сервера

`graph.facebook.com` (официальный Cloud API) с российских адресов может быть
недоступен — проверь тем же curl. Если так, для WhatsApp остаётся вариант
`WHATSAPP_PROVIDER=waha` с self-hosted шлюзом.

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
