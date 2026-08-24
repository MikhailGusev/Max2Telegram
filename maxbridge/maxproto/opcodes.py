"""Коды операций внутреннего RPC MAX.

Протокол — JSON поверх WebSocket. Пакет: {ver, cmd, seq, opcode, payload}.
`cmd`: 0 — исходящий запрос, 1 — входящий. `seq` запроса и ответа совпадают.

Источник: публичная документация протокола проекта vkmax (MIT),
https://github.com/nsdkinx/vkmax/blob/main/docs/opcodes.md — плюс собственная
проверка. Опкоды могут поменяться без предупреждения: это НЕ официальный API.
"""

from __future__ import annotations

from enum import IntEnum


class Op(IntEnum):
    # --- служебные ---
    PING = 1
    HELLO = 6
    SETTINGS = 22

    # --- авторизация ---
    START_AUTH = 17          # запрос SMS-кода по номеру телефона
    CHECK_CODE = 18          # подтверждение кода -> выдаёт LOGIN-токен
    LOGIN_BY_TOKEN = 19      # вход по сохранённому токену + синхронизация чатов
    GET_QR = 288             # запрос QR: {} -> {qrLink, trackId, expiresAt, pollingInterval}
    GET_QR_STATUS = 289      # опрос:  {trackId} -> {status:{loginAvailable, expiresAt}}
    LOGIN_BY_QR = 291        # финал:  {trackId} -> {tokenAttrs:{LOGIN:{token}}}

    # --- пользователи и контакты ---
    RESOLVE_USERS = 32
    CONTACT_ACTION = 34

    # --- чаты ---
    CHAT_HISTORY = 49        # получить сообщения чата
    CHAT_MARK = 50           # READ_MESSAGE / SET_AS_UNREAD
    PIN_MESSAGE = 55
    JOIN_BY_LINK = 57
    GET_MEMBERS = 59         # список участников: {type:MEMBER, chatId, marker, count}
    LEAVE_CHAT = 75
    CHAT_MEMBERS = 77

    # --- сообщения ---
    SEND_MESSAGE = 64
    UPLOAD_DONE = 65
    DELETE_MESSAGE = 66
    EDIT_MESSAGE = 67
    REACTION = 178

    # --- файлы ---
    UPLOAD_PHOTO = 80
    UPLOAD_VIDEO = 82
    DOWNLOAD_VIDEO = 83
    UPLOAD_FILE = 87
    DOWNLOAD_FILE = 88

    # --- входящие события (cmd = 1) ---
    EVT_NEW_MESSAGE = 128
    EVT_UPLOAD_PROGRESS = 136


#: опкоды, которые сервер присылает сам (без нашего запроса)
SERVER_EVENTS = {Op.EVT_NEW_MESSAGE, Op.EVT_UPLOAD_PROGRESS}
