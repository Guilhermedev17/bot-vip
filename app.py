"""App Flask em modo webhook — produção (Vercel / Render / qualquer host).

Arquitetura com banco de dados na nuvem (Turso): o mapeamento
cobrança -> usuário viaja dentro do próprio ``external_id`` da Epague
(``vip2026-{chat_id}-{plan_id}-{rand}``), que o webhook devolve, e a
assinatura é registrada na tabela `subs` (quem comprou, qual plano,
quando vence). Os botões consultam a Epague de novo pelo charge_id.

Endpoints:
  POST /telegram        <- updates do Telegram (configure via setWebhook)
  POST /webhook/epague  <- pagamento confirmado (payment.confirmed, HMAC)
  GET  /health

Roda local:   python app.py        (porta 5000)
Na Vercel:    api/index.py importa este app.
"""
from __future__ import annotations

import base64
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone

from flask import Flask, jsonify, request

import config
import db_cloud
import tg
from epague import (
    SIGNATURE_HEADER,
    WEBHOOK_EVENT_PAID,
    EpagueClient,
    EpagueError,
    verify_webhook_signature,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = Flask(__name__)

# vip2026-{chat_id}-{plan_id}-{rand}
_EXTERNAL_ID_RE = re.compile(r"^vip2026-(\d+)-([A-Za-z0-9_]+)-([0-9a-f]+)$")

# ---------------------------------------------------------------------------
# TEXTOS — edite à vontade (iguais aos do bot.py em modo polling)
# ---------------------------------------------------------------------------

PITCH = """\
👋 Olá, {name}!

🔥 <b>BEM-VINDO(A) À ÁREA VIP!</b> 🔥

Aqui você garante acesso ao nosso conteúdo exclusivo, atualizado com frequência.

✅ Acesso imediato após o pagamento
✅ Suporte direto no Telegram
✅ Conteúdo novo com frequência

👇 <b>Escolha seu plano abaixo:</b>\
"""

PAY_INSTRUCTIONS = """\
✅ <b>Como realizar o pagamento:</b>

1. Abra o aplicativo do seu banco.
2. Selecione a opção <b>"Pagar"</b> ou <b>"PIX"</b>.
3. Escolha <b>"PIX Copia e Cola"</b>.
4. Cole o código enviado acima e finalize o pagamento com segurança.\
"""


def format_price(cents: int) -> str:
    return f"R$ {cents / 100:.2f}".replace(".", ",")


def plans_keyboard() -> dict:
    return tg.inline_keyboard(
        [[(f"{p['name']} — {format_price(p['price_cents'])}", f"plan:{p['id']}")]
         for p in config.PLANS]
    )


def payment_keyboard(charge_id: str) -> dict:
    return tg.inline_keyboard(
        [
            [("✅ Verificar Status", f"v:{charge_id}")],
            [("📋 Copiar Código", f"c:{charge_id}")],
            [("📷 Ver QR Code", f"q:{charge_id}")],
        ]
    )


def plan_expires_at(plan_id: str) -> str | None:
    """Calcula o vencimento ISO 8601 UTC a partir dos dias do plano.

    Retorna None para plano vitalício (sem vencimento).
    """
    plan = config.PLAN_MAP.get(plan_id, {})
    days = plan.get("days", 0)
    if not days:
        return None
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(
        timespec="seconds"
    )


def release_access(chat_id: int, plan_id: str) -> None:
    """Libera o acesso após pagamento confirmado.

    1. Gera um convite INDIVIDUAL de uso único pro canal (se o bot for
       admin e VIP_CHANNEL_ID estiver configurado).
    2. Registra a assinatura no banco (pra re-liberação e expiração).
    3. Envia a mensagem de liberação com o convite.

    Se o banco ou a geração do convite falhar, a liberação continua com
    o link estático de fallback — vender nunca pode travar.
    """
    invite_link = None
    channel_id = (config.VIP_CHANNEL_ID or "").strip()
    if channel_id:
        try:
            invite_link = tg.create_chat_invite_link(
                int(channel_id),
                name=f"vip-{chat_id}-{plan_id}"[:32],
                member_limit=1,
                expire_in_seconds=24 * 3600,
            )
        except Exception:
            log.exception("Falha ao gerar convite individual; usando link estático")

    try:
        db_cloud.init_schema()
        db_cloud.save_sub(chat_id, plan_id, plan_expires_at(plan_id), invite_link)
    except Exception:
        log.exception("Falha ao registrar assinatura no banco (liberação continua)")

    plan = config.PLAN_MAP.get(plan_id, {})
    plan_name = plan.get("name", "plano")
    days = plan.get("days", 0)
    link = invite_link or config.VIP_INVITE_LINK
    text = (
        "✅ <b>Pagamento confirmado!</b>\n\n"
        f"Seu acesso ao <b>{plan_name}</b> foi liberado. "
        "Bem-vindo(a)! 🎉\n\n"
        f"👉 Acesse aqui: {link}"
    )
    if days:
        text += f"\n\n<i>⏳ Seu acesso vale por {days} dias.</i>"
    if invite_link:
        text += "\n<i>⚠️ Este convite é pessoal e intransferível.</i>"
    tg.send_message(chat_id, text)


def parse_external_id(external_id: str) -> tuple[int, str] | tuple[None, None]:
    m = _EXTERNAL_ID_RE.match(external_id or "")
    if not m:
        return None, None
    return int(m.group(1)), m.group(2)


def epague_client() -> EpagueClient:
    return EpagueClient(api_key=config.EPAGUE_API_KEY, base_url=config.EPAGUE_BASE_URL)


def webhook_url_for_charge() -> str | None:
    if config.WEBHOOK_URL:
        return config.WEBHOOK_URL
    if config.PUBLIC_URL:
        return config.PUBLIC_URL.rstrip("/") + "/webhook/epague"
    return None


# ---------------------------------------------------------------------------
# Telegram (updates via webhook)
# ---------------------------------------------------------------------------

def handle_start(chat_id: int, name: str) -> None:
    if config.WELCOME_VIDEO:
        try:
            tg.send_video(chat_id, config.WELCOME_VIDEO, caption=f"👋 Olá, {name}!")
        except Exception:
            log.exception("Falha ao enviar vídeo de boas-vindas")
    tg.send_message(chat_id, PITCH.format(name=name), reply_markup=plans_keyboard())


def handle_planos(chat_id: int) -> None:
    lines = []
    for p in config.PLANS:
        days = "acesso permanente" if not p.get("days") else f"{p['days']} dias de acesso"
        lines.append(
            f"💎 <b>{p['name']}</b> — {format_price(p['price_cents'])}\n"
            f"{p.get('description', '')}\n<i>{days}</i>"
        )
    tg.send_message(chat_id, "\n\n".join(lines), reply_markup=plans_keyboard())


def handle_plan(chat_id: int, plan_id: str, callback_id: str) -> None:
    tg.answer_callback(callback_id)
    plan = config.PLAN_MAP.get(plan_id)
    if not plan:
        tg.send_message(chat_id, "Plano não encontrado. Escolha novamente com /planos.")
        return

    tg.send_message(chat_id, "Gerando seu Pix, um instante... ⏳")
    try:
        external_id = f"vip2026-{chat_id}-{plan_id}-{uuid.uuid4().hex[:8]}"
        charge = epague_client().create_charge(
            plan["price_cents"] / 100,
            external_id=external_id,
            description=f"VIP 2026 - {plan['name']}",
            webhook_url=webhook_url_for_charge(),
        )
    except EpagueError:
        log.exception("Falha ao criar cobrança na Epague")
        tg.send_message(chat_id, "Deu erro ao gerar o Pix. Tenta de novo em instantes.")
        return

    charge_id = charge["id"]
    qr_code = charge["pix_copia_cola"]
    qr_b64 = charge.get("qr_code_base64", "")

    if qr_b64:
        try:
            raw = qr_b64.split(",", 1)[-1]  # remove "data:image/png;base64," se houver
            tg.send_photo(chat_id, base64.b64decode(raw), "Escaneie o QR Code para pagar 📱")
        except Exception:
            log.exception("Falha ao enviar QR Code")
    tg.send_message(chat_id, PAY_INSTRUCTIONS)
    tg.send_message(chat_id, f"Copie o código abaixo:\n\n<code>{qr_code}</code>")
    tg.send_message(
        chat_id,
        "Após efetuar o pagamento, clique no botão abaixo ⤵️",
        reply_markup=payment_keyboard(charge_id),
    )


def handle_verify(chat_id: int, charge_id: str, callback_id: str) -> None:
    tg.answer_callback(callback_id, "Consultando pagamento...")
    try:
        info = epague_client().get_status(charge_id)
    except EpagueError:
        log.exception("Falha ao consultar status na Epague")
        tg.send_message(chat_id, "Não consegui consultar agora. Tenta de novo em instantes.")
        return

    status = str(info.get("status", "")).lower()
    if status in config.PAID_STATUSES:
        _, plan_id = parse_external_id(info.get("external_id", ""))
        release_access(chat_id, plan_id or "")
    else:
        tg.send_message(
            chat_id,
            "Ainda não identificamos seu pagamento. Se você já pagou, aguarda "
            "uns instantes e clica de novo em <b>Verificar Status</b>. ⏳",
        )


def handle_code(chat_id: int, charge_id: str, callback_id: str) -> None:
    tg.answer_callback(callback_id)
    try:
        info = epague_client().get_status(charge_id)
        qr_code = info.get("pix_copia_cola", "")
    except EpagueError:
        log.exception("Falha ao buscar código copia e cola")
        qr_code = ""
    if not qr_code:
        tg.send_message(chat_id, "Não achei essa cobrança. Gere um novo Pix com /planos.")
        return
    tg.send_message(chat_id, f"Copie o código abaixo:\n\n<code>{qr_code}</code>")


def handle_qrcode(chat_id: int, charge_id: str, callback_id: str) -> None:
    tg.answer_callback(callback_id)
    try:
        info = epague_client().get_status(charge_id)
        qr_b64 = info.get("qr_code_base64", "")
    except EpagueError:
        log.exception("Falha ao buscar QR Code")
        qr_b64 = ""
    if not qr_b64:
        tg.send_message(chat_id, "Não achei o QR dessa cobrança. Gere um novo Pix com /planos.")
        return
    try:
        raw = qr_b64.split(",", 1)[-1]
        tg.send_photo(chat_id, base64.b64decode(raw), "Escaneie o QR Code para pagar 📱")
    except Exception:
        log.exception("Falha ao enviar QR Code")


def check_telegram_secret() -> bool:
    expected = config.TELEGRAM_WEBHOOK_SECRET
    if not expected:
        return True
    return request.headers.get("X-Telegram-Bot-Api-Secret-Token") == expected


@app.post("/telegram")
def telegram_webhook():
    if not check_telegram_secret():
        return jsonify({"ok": False}), 401

    update = request.get_json(force=True, silent=True) or {}

    if "callback_query" in update:
        cq = update["callback_query"]
        callback_id = cq["id"]
        chat_id = cq["message"]["chat"]["id"]
        data = cq.get("data", "")
        if data.startswith("plan:"):
            handle_plan(chat_id, data.split(":", 1)[1], callback_id)
        elif data.startswith("v:"):
            handle_verify(chat_id, data.split(":", 1)[1], callback_id)
        elif data.startswith("c:"):
            handle_code(chat_id, data.split(":", 1)[1], callback_id)
        elif data.startswith("q:"):
            handle_qrcode(chat_id, data.split(":", 1)[1], callback_id)
        else:
            tg.answer_callback(callback_id)
        return jsonify({"ok": True})

    msg = update.get("message") or {}
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    text = (msg.get("text") or "").strip()
    if not chat_id:
        return jsonify({"ok": True})

    name = ((msg.get("from") or {}).get("first_name")) or "visitante"
    if text in ("/start", "/start@vip2026oficialbot"):
        handle_start(chat_id, name)
    elif text.startswith("/planos"):
        handle_planos(chat_id)

    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Epague (webhook de pagamento)
# ---------------------------------------------------------------------------

def check_epague_signature(raw_body: bytes) -> bool:
    secret = config.EPAGUE_WEBHOOK_SECRET
    if not secret:
        log.warning("EPAGUE_WEBHOOK_SECRET vazio: aceitando webhook sem validar HMAC")
        return True
    return verify_webhook_signature(
        raw_body, request.headers.get(SIGNATURE_HEADER), secret
    )


@app.post("/webhook/epague")
def epague_webhook():
    raw_body = request.get_data()
    if not check_epague_signature(raw_body):
        return jsonify({"ok": False, "error": "invalid signature"}), 401

    payload = request.get_json(force=True, silent=True) or {}
    log.info("Webhook Epague: %s", {k: payload.get(k) for k in ("event", "status", "external_id")})

    if payload.get("event") != WEBHOOK_EVENT_PAID:
        return jsonify({"ok": True, "ignored": payload.get("event")})

    chat_id, plan_id = parse_external_id(payload.get("external_id", ""))
    status = str(payload.get("status", "")).lower()
    if not chat_id:
        return jsonify({"ok": False, "error": "external_id inválido"}), 400

    if status in config.PAID_STATUSES:
        try:
            release_access(chat_id, plan_id or "")
        except Exception:
            log.exception("Falha ao liberar acesso no Telegram")

    return jsonify({"ok": True})


@app.get("/health")
def health():
    return jsonify({"ok": True})


if __name__ == "__main__":
    import os

    missing = config.validate()
    if missing:
        raise SystemExit(f"Faltando variáveis de ambiente: {', '.join(missing)}.")
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
