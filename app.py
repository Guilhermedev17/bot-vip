"""App Flask em modo webhook — produção (Vercel / Render / qualquer host).

Arquitetura 100% stateless (sem banco de dados): o mapeamento
cobrança -> usuário viaja dentro do próprio ``external_id`` da Epague
(``vip2026-{chat_id}-{plan_id}-{rand}``), que o webhook devolve.
Os botões consultam a Epague de novo pelo charge_id, então nada
precisa ser persistido em disco.

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
    # /start inteligente: assinante ativo recupera o acesso sem pagar de novo.
    # Se o banco estiver fora do ar, cai no fluxo normal de planos (vender nunca trava).
    channel_id = (config.VIP_CHANNEL_ID or "").strip()
    if channel_id:
        try:
            active_sub = db_cloud.is_active(chat_id)
        except Exception:
            active_sub = None
            log.exception("Falha ao consultar assinatura no /start")
        if active_sub:
            _send_recovery_invite(chat_id, name, active_sub, channel_id)
            return
    if config.WELCOME_VIDEO:
        try:
            tg.send_video(chat_id, config.WELCOME_VIDEO, caption=f"👋 Olá, {name}!")
        except Exception:
            log.exception("Falha ao enviar vídeo de boas-vindas")
    tg.send_message(chat_id, PITCH.format(name=name), reply_markup=plans_keyboard())


def _send_recovery_invite(chat_id: int, name: str, sub: dict, channel_id: str) -> None:
    """Gera um convite individual novo para um assinante ativo (recuperação de acesso)."""
    try:
        invite_link = tg.create_chat_invite_link(
            int(channel_id),
            name=f"vip-{chat_id}-rec"[:32],
            member_limit=1,
            expire_in_seconds=24 * 3600,
        )
    except Exception:
        log.exception("Falha ao gerar convite de recuperação no /start")
        invite_link = None

    if not invite_link:
        tg.send_message(
            chat_id,
            f"👋 Olá, {name}!\n\n"
            "Sua assinatura está ativa, mas não consegui gerar seu link agora. "
            "Aguarde um instante e mande /start de novo.",
        )
        return

    try:
        db_cloud.save_sub(chat_id, sub.get("plan_id") or "",
                          sub.get("expires_at"), invite_link)
    except Exception:
        log.exception("Falha ao salvar convite de recuperação (acesso continua)")

    plan = config.PLAN_MAP.get(sub.get("plan_id") or "", {})
    plan_name = plan.get("name", "sua assinatura")
    tg.send_message(
        chat_id,
        f"👋 Olá, {name}!\n\n"
        f"✅ <b>{plan_name}</b> ativa — bom te ver de volta! 🎉\n\n"
        f"👉 Seu novo link de acesso: {invite_link}\n\n"
        "<i>⚠️ Este convite é pessoal, de uso único e expira em 24h.</i>",
    )


def handle_planos(chat_id: int) -> None:
    lines = []
    for p in config.PLANS:
        days = "acesso permanente" if not p.get("days") else f"{p['days']} dias de acesso"
        lines.append(
            f"💎 <b>{p['name']}</b> — {format_price(p['price_cents'])}\n"
            f"{p.get('description', '')}\n<i>{days}</i>"
        )
    tg.send_message(chat_id, "\n\n".join(lines), reply_markup=plans_keyboard())


def handle_status(chat_id: int, name: str) -> None:
    """/status — mostra a assinatura atual do usuário (plano e vencimento)."""
    try:
        sub = db_cloud.is_active(chat_id)
    except Exception:
        sub = None
        log.exception("Falha ao consultar assinatura no /status")
    if not sub:
        tg.send_message(
            chat_id,
            "📊 <b>Minha assinatura</b>\n\n"
            "Você não tem uma assinatura ativa no momento.\n\n"
            "Use /start para ver os planos. 👋",
        )
        return
    plan = config.PLAN_MAP.get(sub.get("plan_id") or "", {})
    plan_name = plan.get("name", "VIP")
    exp = sub.get("expires_at")
    if exp:
        try:
            dt = datetime.fromisoformat(exp)
            days_left = max(0, (dt - datetime.now(timezone.utc)).days)
            validity = f"⏳ Vence em <b>{days_left} dia(s)</b>."
        except Exception:
            validity = "⏳ Assinatura por tempo limitado."
    else:
        validity = "♾️ Acesso <b>vitalício</b>."
    tg.send_message(
        chat_id,
        "📊 <b>Minha assinatura</b>\n\n"
        f"💎 Plano: <b>{plan_name}</b>\n"
        f"{validity}\n\n"
        "Perdeu o acesso ao canal? Mande /start que eu gero um link novo. 👍",
    )


def handle_suporte(chat_id: int, name: str) -> None:
    """/suporte — direciona para o atendimento (SUPPORT_CONTACT)."""
    contact = (config.SUPPORT_CONTACT or "").strip()
    if contact:
        tg.send_message(
            chat_id,
            "💬 <b>Suporte</b>\n\n"
            f"Fale com a gente: {contact}\n\n"
            "Descreva seu problema que respondemos o quanto antes. 👍",
        )
    else:
        tg.send_message(
            chat_id,
            "💬 <b>Suporte</b>\n\n"
            "Nosso atendimento está sendo configurado.\n"
            "Tente novamente em breve. 🙏",
        )


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
    elif text.startswith("/status"):
        handle_status(chat_id, name)
    elif text.startswith("/suporte"):
        handle_suporte(chat_id, name)

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


def expire_subscriptions() -> dict:
    """Rotina diária (Parte 4): remove do canal quem teve o plano vencido.

    Para cada assinatura ativa com prazo estourado:
    1. Expulsa do canal (ban + unban imediato = sai sem ficar bloqueado,
       pode comprar de novo quando quiser).
    2. Marca como inativa no banco.

    Nunca trava por causa de um usuário: falha individual é registrada
    e a faxina continua pros demais.
    """
    try:
        db_cloud.init_schema()
        expired = db_cloud.list_expired()
    except Exception:
        log.exception("Falha ao buscar assinaturas vencidas")
        return {"checked": 0, "removed": 0, "failed": ["db_error"]}

    channel_id = (config.VIP_CHANNEL_ID or "").strip()
    removed, failed = 0, []
    for sub in expired:
        user_id = sub["chat_id"]
        ok = True
        if channel_id:
            try:
                tg.ban_chat_member(int(channel_id), user_id)
            except Exception:
                log.exception(f"Falha ao banir {user_id} do canal")
                ok = False
            try:
                tg.unban_chat_member(int(channel_id), user_id)
            except Exception:
                log.exception(f"Falha ao desbanir {user_id} do canal")
                ok = False
        try:
            db_cloud.deactivate(user_id)
        except Exception:
            log.exception(f"Falha ao desativar {user_id} no banco")
            ok = False
        if ok:
            removed += 1
        else:
            failed.append(user_id)
        log.info(f"Assinatura vencida removida: chat_id={user_id} plano={sub.get('plan_id')}")
    return {"checked": len(expired), "removed": removed, "failed": failed}


@app.get("/health")
def health():
    return jsonify({"ok": True})


@app.get("/cron/expire")
def cron_expire():
    """Endpoint do cron diário da Vercel. Protegido por CRON_SECRET
    (a Vercel envia Authorization: Bearer <CRON_SECRET> automaticamente)."""
    expected = (config.CRON_SECRET or "").strip()
    if not expected or request.headers.get("Authorization") != f"Bearer {expected}":
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    result = expire_subscriptions()
    return jsonify({"ok": True, **result})


if __name__ == "__main__":
    import os

    missing = config.validate()
    if missing:
        raise SystemExit(f"Faltando variáveis de ambiente: {', '.join(missing)}.")
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
