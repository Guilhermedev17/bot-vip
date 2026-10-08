"""Persistência simples em SQLite (só stdlib).

Mapeia cada cobrança (charge_id) ao usuário (chat_id), para que o webhook
da PushinPay consiga encontrar quem pagou e liberar o acesso.
"""
import sqlite3
import time
from pathlib import Path

import config

DB_PATH = Path(__file__).parent / "bot.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS charges (
    charge_id      TEXT PRIMARY KEY,
    chat_id        INTEGER NOT NULL,
    plan_id        TEXT NOT NULL,
    qr_code        TEXT NOT NULL,
    qr_code_base64 TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL DEFAULT 'created',
    created_at     INTEGER NOT NULL
);
"""


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(_SCHEMA)
    return conn


def save_charge(charge_id, chat_id, plan_id, qr_code, qr_code_base64="", status="created"):
    with _connect() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO charges
               (charge_id, chat_id, plan_id, qr_code, qr_code_base64, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (charge_id, chat_id, plan_id, qr_code, qr_code_base64, status, int(time.time())),
        )


def get_charge(charge_id):
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM charges WHERE charge_id = ?", (charge_id,)
        ).fetchone()
        return dict(row) if row else None


def set_status(charge_id, status):
    with _connect() as conn:
        conn.execute(
            "UPDATE charges SET status = ? WHERE charge_id = ?", (status, charge_id)
        )


def latest_pending(chat_id):
    """Última cobrança ainda não paga de um usuário (se houver)."""
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        placeholders = ",".join("?" for _ in config.PAID_STATUSES)
        row = conn.execute(
            f"""SELECT * FROM charges
                WHERE chat_id = ? AND status NOT IN ({placeholders})
                ORDER BY created_at DESC LIMIT 1""",
            (chat_id, *config.PAID_STATUSES),
        ).fetchone()
        return dict(row) if row else None
