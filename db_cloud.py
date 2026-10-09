"""Banco de dados na nuvem (Turso) — a "caderneta" de assinaturas.

Por que existe: o bot é stateless (sem disco na Vercel). Sem um banco
externo, ele "esquece" quem pagou. Aqui fica registrado quem comprou,
qual plano e quando vence — e esses dados sobrevivem mesmo se o bot,
o canal ou o projeto na Vercel forem recriados do zero.

Tabela `subs`:
  chat_id      INTEGER PRIMARY KEY  -- id do usuário no Telegram (nunca muda,
                                      mesmo falando com um bot novo)
  plan_id      TEXT NOT NULL        -- semanal | mensal | vitalicio
  purchased_at TEXT NOT NULL        -- ISO 8601 UTC da compra
  expires_at   TEXT                 -- ISO 8601 UTC do vencimento (NULL = vitalício)
  invite_link  TEXT                 -- último convite individual gerado
  active       INTEGER NOT NULL DEFAULT 1

Acesso via protocolo Hrana (HTTP) direto com urllib — sem dependências
externas. Config via env: TURSO_URL, TURSO_TOKEN.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

_COLUMNS = ("chat_id", "plan_id", "purchased_at", "expires_at", "invite_link", "active")
_TIMEOUT = 25
_MAX_RETRIES = 3


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _config():
    url = os.getenv("TURSO_URL", "").strip()
    token = os.getenv("TURSO_TOKEN", "").strip()
    if not url:
        raise RuntimeError("TURSO_URL não configurado")
    host = url.replace("libsql://", "").replace("https://", "").rstrip("/")
    return f"https://{host}/v2/pipeline", token


def _to_hrana_arg(value):
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "integer", "value": str(int(value))}
    if isinstance(value, int):
        return {"type": "integer", "value": str(value)}
    if isinstance(value, float):
        return {"type": "float", "value": value}
    return {"type": "text", "value": str(value)}


def _from_hrana_val(cell):
    t = cell.get("type")
    v = cell.get("value")
    if t == "null":
        return None
    if t == "integer":
        return int(v)
    if t == "float":
        return float(v)
    if t == "blob":
        import base64
        return base64.b64decode(v)
    return v


def _pipeline(statements: list[tuple[str, tuple]]) -> list:
    """Executa statements num único pipeline (autocommit). Retorna lista de rows."""
    endpoint, token = _config()
    payload = {
        "requests": [
            {"type": "execute", "stmt": {"sql": sql, "args": [_to_hrana_arg(a) for a in args]}}
            for sql, args in statements
        ]
    }
    data = json.dumps(payload).encode()
    last_err = None
    for attempt in range(_MAX_RETRIES):
        req = urllib.request.Request(
            endpoint,
            data=data,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                body = json.load(resp)
            results = []
            for res in body.get("results", []):
                if res.get("type") == "error":
                    raise RuntimeError(f"Turso: {res['error'].get('message')}")
                rows = res["response"]["result"].get("rows", [])
                results.append([[_from_hrana_val(c) for c in row] for row in rows])
            return results
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last_err = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Turso indisponível após {_MAX_RETRIES} tentativas: {last_err}")


def _one(sql: str, args: tuple = ()) -> list:
    return _pipeline([(sql, args)])[0]


def _row_to_dict(row) -> dict | None:
    if not row:
        return None
    return dict(zip(_COLUMNS, row))


def init_schema() -> None:
    """Cria a tabela se não existir. Idempotente."""
    _one(
        """
        CREATE TABLE IF NOT EXISTS subs (
          chat_id      INTEGER PRIMARY KEY,
          plan_id      TEXT NOT NULL,
          purchased_at TEXT NOT NULL,
          expires_at   TEXT,
          invite_link  TEXT,
          active       INTEGER NOT NULL DEFAULT 1
        )
        """
    )


def save_sub(chat_id: int, plan_id: str, expires_at: str | None,
             invite_link: str | None = None) -> None:
    """Registra (ou atualiza) a assinatura de um usuário."""
    _one(
        """
        INSERT INTO subs (chat_id, plan_id, purchased_at, expires_at, invite_link, active)
        VALUES (?, ?, ?, ?, ?, 1)
        ON CONFLICT(chat_id) DO UPDATE SET
          plan_id=excluded.plan_id,
          purchased_at=excluded.purchased_at,
          expires_at=excluded.expires_at,
          invite_link=excluded.invite_link,
          active=1
        """,
        (chat_id, plan_id, _now_iso(), expires_at, invite_link),
    )


def get_sub(chat_id: int) -> dict | None:
    rows = _one(
        "SELECT chat_id, plan_id, purchased_at, expires_at, invite_link, active"
        " FROM subs WHERE chat_id = ?",
        (chat_id,),
    )
    return _row_to_dict(rows[0] if rows else None)


def is_active(chat_id: int) -> dict | None:
    """Devolve a assinatura se estiver ativa e dentro do prazo; senão None."""
    sub = get_sub(chat_id)
    if not sub or not sub["active"]:
        return None
    exp = sub["expires_at"]
    if exp and exp <= _now_iso():
        return None
    return sub


def list_expired() -> list[dict]:
    """Assinaturas marcadas como ativas mas com prazo vencido (faxina diária)."""
    rows = _one(
        "SELECT chat_id, plan_id, purchased_at, expires_at, invite_link, active"
        " FROM subs WHERE active = 1 AND expires_at IS NOT NULL AND expires_at <= ?",
        (_now_iso(),),
    )
    return [_row_to_dict(r) for r in rows]


def deactivate(chat_id: int) -> None:
    _one("UPDATE subs SET active = 0 WHERE chat_id = ?", (chat_id,))
