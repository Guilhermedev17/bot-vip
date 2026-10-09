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
from datetime import datetime, timedelta, timezone

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
    """Cria as tabelas se não existirem. Idempotente."""
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
    _one(
        """
        CREATE TABLE IF NOT EXISTS payments (
          id           INTEGER PRIMARY KEY AUTOINCREMENT,
          chat_id      INTEGER NOT NULL,
          plan_id      TEXT NOT NULL,
          amount_cents INTEGER NOT NULL DEFAULT 0,
          external_id  TEXT,
          paid_at      TEXT NOT NULL
        )
        """
    )
    _one("CREATE INDEX IF NOT EXISTS idx_payments_chat ON payments(chat_id)")
    _one("CREATE UNIQUE INDEX IF NOT EXISTS idx_payments_ext ON payments(external_id)")
    _one(
        """
        CREATE TABLE IF NOT EXISTS users (
          chat_id       INTEGER PRIMARY KEY,
          first_name    TEXT NOT NULL DEFAULT '',
          first_seen    TEXT NOT NULL,
          last_seen     TEXT NOT NULL,
          last_nudge_at TEXT,
          nudge_count   INTEGER NOT NULL DEFAULT 0,
          reachable     INTEGER NOT NULL DEFAULT 1
        )
        """
    )
    # migração idempotente: coluna de avisos de vencimento já enviados ("7,3,1")
    try:
        _one("ALTER TABLE subs ADD COLUMN warned_days TEXT NOT NULL DEFAULT ''")
    except Exception as exc:
        if "duplicate column" not in str(exc).lower():
            raise


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


def record_payment(chat_id: int, plan_id: str, amount_cents: int,
                   external_id: str | None) -> None:
    """Registra uma compra (usado no /status e na idempotência do webhook)."""
    _one(
        "INSERT INTO payments (chat_id, plan_id, amount_cents, external_id, paid_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (chat_id, plan_id, amount_cents, external_id, _now_iso()),
    )


def payment_exists(external_id: str) -> bool:
    """Diz se este external_id já foi processado (webhook duplicado)."""
    if not external_id:
        return False
    rows = _one("SELECT id FROM payments WHERE external_id = ? LIMIT 1", (external_id,))
    return bool(rows)


def get_payments(chat_id: int, limit: int = 5) -> list[dict]:
    """Últimas compras do usuário (mais recentes primeiro)."""
    rows = _one(
        "SELECT plan_id, amount_cents, paid_at FROM payments"
        " WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
        (chat_id, limit),
    )
    # nota: _pipeline já converte os valores; não converter de novo
    return [
        {"plan_id": r[0], "amount_cents": r[1] or 0, "paid_at": r[2]}
        for r in rows
    ]


def list_active_expiring() -> list[dict]:
    """Assinaturas ativas com prazo definido (base dos avisos de vencimento)."""
    rows = _one(
        "SELECT chat_id, plan_id, expires_at, warned_days FROM subs"
        " WHERE active = 1 AND expires_at IS NOT NULL AND expires_at > ?",
        (_now_iso(),),
    )
    # nota: _pipeline já converte os valores; não converter de novo
    return [
        {"chat_id": r[0], "plan_id": r[1],
         "expires_at": r[2], "warned_days": r[3] or ""}
        for r in rows
    ]


def mark_warned(chat_id: int, day: int) -> None:
    """Marca o aviso de N dias como enviado (não repete)."""
    rows = _one("SELECT warned_days FROM subs WHERE chat_id = ?", (chat_id,))
    current = rows[0][0] if rows else ""  # _pipeline já converte; sem conversão dupla
    warned = {w for w in str(current or "").split(",") if w}
    warned.add(str(day))
    _one("UPDATE subs SET warned_days = ? WHERE chat_id = ?",
         (",".join(sorted(warned)), chat_id))


def track_user(chat_id: int, first_name: str = "") -> None:
    """Registra/atualiza um visitante (base das campanhas de nudge). Idempotente."""
    _one(
        """INSERT INTO users (chat_id, first_name, first_seen, last_seen,
                              last_nudge_at, nudge_count, reachable)
           VALUES (?, ?, ?, ?, NULL, 0, 1)
           ON CONFLICT(chat_id) DO UPDATE SET
             first_name=excluded.first_name,
             last_seen=excluded.last_seen""",
        (chat_id, first_name or "", _now_iso(), _now_iso()),
    )


def nudge_candidates(limit: int = 50, min_interval_hours: int = 48) -> list[dict]:
    """Visitantes que NUNCA compraram, alcançáveis e sem nudge recente.

    Quem já pagou alguma vez sai da lista (entra no ciclo de retenção).
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=min_interval_hours)).isoformat()
    rows = _one(
        """SELECT u.chat_id, u.first_name, u.nudge_count FROM users u
           WHERE u.reachable = 1
             AND NOT EXISTS (SELECT 1 FROM payments p WHERE p.chat_id = u.chat_id)
             AND (u.last_nudge_at IS NULL OR u.last_nudge_at <= ?)
           ORDER BY u.last_nudge_at NULLS FIRST, u.first_seen
           LIMIT ?""",
        (cutoff, limit),
    )
    # nota: _pipeline já converte os valores; não converter de novo
    return [
        {"chat_id": r[0], "first_name": r[1] or "", "nudge_count": r[2] or 0}
        for r in rows
    ]


def mark_nudged(chat_id: int) -> None:
    _one("UPDATE users SET last_nudge_at = ?, nudge_count = nudge_count + 1"
         " WHERE chat_id = ?", (_now_iso(), chat_id))


def mark_unreachable(chat_id: int) -> None:
    """Bot bloqueado ou chat inválido: para de tentar."""
    _one("UPDATE users SET reachable = 0 WHERE chat_id = ?", (chat_id,))


def list_users(limit: int = 50) -> list[dict]:
    """Visitantes mais recentes (área admin)."""
    rows = _one(
        "SELECT chat_id, first_name, first_seen, last_seen, nudge_count, reachable"
        " FROM users ORDER BY last_seen DESC LIMIT ?",
        (limit,),
    )
    # nota: _pipeline já converte os valores; não converter de novo
    return [
        {"chat_id": r[0], "first_name": r[1] or "", "first_seen": r[2],
         "last_seen": r[3], "nudge_count": r[4] or 0, "reachable": r[5]}
        for r in rows
    ]
