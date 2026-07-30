"""Usuários e sessões do aplicativo (usuário + senha), só com a stdlib.

- Senhas nunca são guardadas: fica o hash scrypt + salt por usuário, em
  .users.json (permissão 0600, fora do git).
- Sessões vivem na memória do processo (reiniciou o app, todo mundo faz
  login de novo) e expiram sozinhas.
- Aviso honesto: o app fala HTTP puro; em rede interna tudo bem, mas para
  acesso pela internet coloque um proxy HTTPS na frente (ex.: Caddy).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

DEFAULT_USERS_FILE = ".users.json"
SESSION_TTL_SECONDS = 12 * 3600
SESSION_COOKIE = "sjo_session"


def _hash_password(password: str, salt_hex: str) -> str:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1
    ).hex()


def load_users(path: str | Path) -> dict[str, dict]:
    path = Path(path)
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_users(path: str | Path, users: dict[str, dict]) -> None:
    path = Path(path)
    path.write_text(json.dumps(users, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def add_user(path: str | Path, email: str, password: str, admin: bool = False) -> None:
    email = email.strip().lower()
    if not email or "@" not in email:
        raise ValueError("e-mail inválido")
    if len(password) < 8:
        raise ValueError("senha muito curta (mínimo 8 caracteres)")
    users = load_users(path)
    salt = secrets.token_hex(16)
    users[email] = {"salt": salt, "hash": _hash_password(password, salt), "admin": bool(admin)}
    save_users(path, users)


def remove_user(path: str | Path, email: str) -> None:
    email = email.strip().lower()
    users = load_users(path)
    record = users.get(email)
    if record is None:
        raise ValueError("usuário não encontrado")
    admins = [e for e, u in users.items() if u.get("admin")]
    if record.get("admin") and admins == [email]:
        raise ValueError("não é possível remover o único administrador")
    del users[email]
    save_users(path, users)


def verify_user(path: str | Path, email: str, password: str) -> dict | None:
    """Devolve o registro do usuário se e-mail+senha conferem, senão None."""
    users = load_users(path)
    record = users.get(email.strip().lower())
    if not record:
        return None
    expected = record.get("hash", "")
    candidate = _hash_password(password, record.get("salt", "00"))
    return record if hmac.compare_digest(candidate, expected) else None


class SessionStore:
    """Sessões em memória: token aleatório → (e-mail, validade)."""

    def __init__(self, ttl: int = SESSION_TTL_SECONDS):
        self._ttl = ttl
        self._sessions: dict[str, tuple[str, float]] = {}

    def create(self, email: str) -> str:
        token = secrets.token_urlsafe(32)
        self._sessions[token] = (email, time.time() + self._ttl)
        return token

    def get(self, token: str | None) -> str | None:
        if not token:
            return None
        entry = self._sessions.get(token)
        if entry is None:
            return None
        email, expires = entry
        if time.time() > expires:
            self._sessions.pop(token, None)
            return None
        return email

    def drop(self, token: str | None) -> None:
        if token:
            self._sessions.pop(token, None)
