"""Низкоуровневый клиент внутреннего WebSocket-протокола MAX (OneMe)."""

from .opcodes import Op
from .ws import MaxWSClient, MaxAuthError, MaxProtocolError

__all__ = ["Op", "MaxWSClient", "MaxAuthError", "MaxProtocolError"]
