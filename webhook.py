"""Servidor Flask que recebe os webhooks da Epague.

Quando um Pix é pago, a Epague faz POST em /webhook/epague com o evento
``payment.confirmed``. Validamos a assinatura HMAC-SHA256
(header x-webhook-signature), identificamos a cobrança no SQLite,
atualizamos o status e mandamos a mensagem de liberação no Telegram
via Bot API (HTTP puro).

Reentregas são idempotentes: se a cobrança já estiver paga no banco,
não reenviamos a mensagem.

Roda local:   python webhook.py        (porta 5000)
Roda em prod: gunicorn webhook:app -b 0.0.0.0:5000
"""
import logging

import requests
from flask import Flask, jsonify, request

import config
import db
from epague import (
    SIGNATURE_HEADER,
    WEBHOOK_EVENT_PAID,
    verify_webhook_signature,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = Flask(__name__)


def send_telegram_message(chat_id: int, text: str) -> None:
    url = f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage"
    resp = requests.post(
        url,
        json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=15,
    )
    resp.raise_for_status()


def check_signature(raw_body: bytes) -> bool:
    """Valida o HMAC do webhook, se o segredo estiver configurado."""
    secret = config.EPAGUE_WEBHOOK_SECRET
    if not secret:
        log.warning("EPAGUE_WEBHOOK_SECRET vazio: aceitando webhook sem validar HMAC")
        return True
    signature = request.headers.get(SIGNATURE_HEADER)
    return verify_webhook_signature(raw_body, signature, secret)


@app.post("/webhook/epague")
def epague_webhook():
    raw_body = request.get_data()
    if not check_signature(raw_body):
        return jsonify({"ok": False, "error": "invalid signature"}), 401

    payload = request.get_json(force=True, silent=True) or {}
    log.info("Webhook recebido: %s", payload)

    event = payload.get("event")
    if event != WEBHOOK_EVENT_PAID:
        # Outros eventos (cashout.*, refund.*) não dizem respeito ao bot.
        return jsonify({"ok": True, "ignored": event})

    charge_id = payload.get("transaction_id") or payload.get("external_id")
    status = str(payload.get("status", "")).lower()
    if not charge_id:
        return jsonify({"ok": False, "error": "transaction_id ausente"}), 400

    record = db.get_charge(charge_id)
    if not record:
        log.warning("Webhook para cobrança desconhecida: %s", charge_id)
        return jsonify({"ok": False, "error": "cobrança não encontrada"}), 400

    db.set_status(charge_id, status or "unknown")

    # Idempotência: reentrega de webhook não reenvia a liberação.
    if status in config.PAID_STATUSES and record.get("status") not in config.PAID_STATUSES:
        plan = config.PLAN_MAP.get(record["plan_id"], {})
        text = (
            "✅ <b>Pagamento confirmado!</b>\n\n"
            f"Seu acesso ao <b>{plan.get('name', 'plano')}</b> foi liberado. "
            "Bem-vindo(a)! 🎉\n\n"
            f"👉 Acesse aqui: {config.VIP_INVITE_LINK}"
        )
        try:
            send_telegram_message(record["chat_id"], text)
        except Exception:
            log.exception("Falha ao enviar mensagem de liberação no Telegram")

    return jsonify({"ok": True})


@app.get("/health")
def health():
    return jsonify({"ok": True})


if __name__ == "__main__":
    import os

    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
