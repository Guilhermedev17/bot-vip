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

🔥 <b>BEM-VINDO(A) AO VIP 2026</b> 🔥

Se você já pagou por algum VIP e recebeu menos do que prometeram, sabe bem como é: promessa demais, conteúdo de menos. 😤

Aqui a proposta é simples — entregar <b>mais</b> do que você espera:

💎 Acervo exclusivo, atualizado todo dia
⚡ Acesso liberado na hora após o pagamento
📲 Suporte direto aqui no Telegram

Sem enrolação: escolha o plano, pague o Pix e entre em segundos.

👇 <b>Garanta seu acesso agora:</b>\
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


def plans_keyboard(discount: float = 0.0, prefix: str = "plan") -> dict:
    def label(p):
        return f"{p['name']} — {format_price(plan_price_cents(p, discount))}"

    return tg.inline_keyboard(
        [[(label(p), f"{prefix}:{p['id']}")] for p in config.PLANS]
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

    1. Gera um convite com PEDIDO de entrada (join request) pro canal.
       A entrada só é aprovada se o usuário tiver assinatura ativa —
       link compartilhado não adianta: quem não pagou é recusado.
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
                creates_join_request=True,
            )
        except Exception:
            log.exception("Falha ao gerar convite; usando link estático")

    try:
        db_cloud.init_schema()
        db_cloud.save_sub(chat_id, plan_id, plan_expires_at(plan_id), invite_link)
    except Exception:
        log.exception("Falha ao registrar assinatura no banco (liberação continua)")

    plan = config.PLAN_MAP.get(plan_id, {})
    plan_name = plan.get("name", "plano")
    days = plan.get("days", 0)
    if invite_link:
        text = (
            "✅ <b>Pagamento confirmado!</b>\n\n"
            f"Seu acesso ao <b>{plan_name}</b> foi liberado. "
            "Bem-vindo(a)! 🎉\n\n"
            f"👉 Toque no link e confirme a solicitação de entrada: {invite_link}\n"
            "<i>Eu aprovo na hora, automaticamente. ✅</i>"
        )
    else:
        # fallback raríssimo: sem link gerado, o /start resolve via recuperação
        text = (
            "✅ <b>Pagamento confirmado!</b>\n\n"
            f"Seu acesso ao <b>{plan_name}</b> foi liberado. "
            "Bem-vindo(a)! 🎉\n\n"
            "Não consegui gerar seu link de acesso agora — "
            "mande /start aqui que eu gero na hora. 👍"
        )
    if days:
        text += f"\n\n<i>⏳ Seu acesso vale por {days} dias.</i>"
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
    # Ex-assinante (vencido) recebe oferta de renovação com 10% OFF.
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
        try:
            old_sub = db_cloud.get_sub(chat_id)
        except Exception:
            old_sub = None
            log.exception("Falha ao consultar histórico no /start")
        if old_sub:
            tg.send_message(
                chat_id,
                f"👋 Olá, {name}!\n\n"
                "Sua assinatura <b>venceu</b> — mas preparamos uma condição especial:\n"
                "🔄 Renove agora com <b>10% OFF</b> em qualquer plano 👇\n\n"
                "<i>Desconto de renovação já aplicado nos preços.</i>",
                reply_markup=plans_keyboard(discount=RENEW_DISCOUNT, prefix="renew"),
            )
            return
    if config.WELCOME_VIDEO:
        try:
            tg.send_video(chat_id, config.WELCOME_VIDEO, caption=f"👋 Olá, {name}!")
        except Exception:
            log.exception("Falha ao enviar vídeo de boas-vindas")
    tg.send_message(chat_id, promo_banner() + PITCH.format(name=name),
                    reply_markup=plans_keyboard(discount=promo_discount()))


def _send_recovery_invite(chat_id: int, name: str, sub: dict, channel_id: str) -> None:
    """Gera um convite novo para um assinante ativo (recuperação de acesso).

    O convite exige pedido de entrada: só quem tem assinatura ativa é
    aprovado — link compartilhado com não-pagante é recusado.
    """
    try:
        invite_link = tg.create_chat_invite_link(
            int(channel_id),
            name=f"vip-{chat_id}-rec"[:32],
            creates_join_request=True,
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
        f"👉 Toque no link e confirme a solicitação de entrada: {invite_link}\n"
        "<i>Eu aprovo na hora, automaticamente. ✅</i>",
    )


def handle_planos(chat_id: int) -> None:
    lines = []
    for p in config.PLANS:
        days = "acesso permanente" if not p.get("days") else f"{p['days']} dias de acesso"
        lines.append(
            f"💎 <b>{p['name']}</b> — {format_price(p['price_cents'])}\n"
            f"{p.get('description', '')}\n<i>{days}</i>"
        )
    tg.send_message(chat_id, promo_banner() + "\n\n".join(lines),
                    reply_markup=plans_keyboard(discount=promo_discount()))


def handle_status(chat_id: int, name: str) -> None:
    """/status — assinatura atual + histórico de compras + link do canal."""
    try:
        sub = db_cloud.is_active(chat_id)
    except Exception:
        sub = None
        log.exception("Falha ao consultar assinatura no /status")
    try:
        history = db_cloud.get_payments(chat_id, 5)
    except Exception:
        history = []
        log.exception("Falha ao consultar histórico no /status")

    if not sub:
        text = (
            "📊 <b>Minha assinatura</b>\n\n"
            "Você não tem uma assinatura ativa no momento.\n\n"
            "Use /start para ver os planos. 👋"
        )
    else:
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
        text = (
            "📊 <b>Minha assinatura</b>\n\n"
            f"💎 Plano: <b>{plan_name}</b>\n"
            f"{validity}"
        )
        if history:
            lines = []
            for h in history:
                pname = config.PLAN_MAP.get(h.get("plan_id") or "", {}).get("name", h.get("plan_id"))
                try:
                    dt = datetime.fromisoformat(h["paid_at"])
                    when = dt.strftime("%d/%m/%Y")
                except Exception:
                    when = ""
                amt = format_price(h.get("amount_cents") or 0)
                lines.append(f"• {pname} — {amt} ({when})".strip())
            text += "\n\n📜 <b>Últimas compras:</b>\n" + "\n".join(lines)
        text += "\n\nPerdeu o acesso ao canal? Toque abaixo que eu gero seu link. 👇"
        kb = tg.inline_keyboard([[("🎟️ Gerar meu link de acesso", "getlink")]])
    tg.send_message(chat_id, text, reply_markup=kb if sub else None)


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


def plan_price_cents(plan: dict, discount: float = 0.0) -> int:
    """Preço final em centavos aplicando desconto (0.0 a 1.0)."""
    return max(1, round(plan["price_cents"] * (1 - discount)))


# Desconto de renovação para ex-assinantes (igual ao Baby Shark: 10% OFF).
RENEW_DISCOUNT = 0.10


def promo_discount() -> float:
    """Desconto da promoção ativa (0.0 se não houver)."""
    return config.PROMO_DISCOUNT if config.PROMO_ACTIVE else 0.0


def promo_label() -> str:
    return config.PROMO_NAME if config.PROMO_ACTIVE and config.PROMO_DISCOUNT else ""


def promo_banner() -> str:
    if not config.PROMO_ACTIVE or not config.PROMO_DISCOUNT:
        return ""
    pct = int(round(config.PROMO_DISCOUNT * 100))
    return f"🔥 <b>{config.PROMO_NAME}</b> — {pct}% OFF em todos os planos! 🔥\n\n"


def handle_plan(chat_id: int, plan_id: str, callback_id: str, discount: float = 0.0,
                discount_label: str = "") -> None:
    tg.answer_callback(callback_id)
    plan = config.PLAN_MAP.get(plan_id)
    if not plan:
        tg.send_message(chat_id, "Plano não encontrado. Escolha novamente com /planos.")
        return

    tg.send_message(chat_id, "Gerando seu Pix, um instante... ⏳")
    try:
        external_id = f"vip2026-{chat_id}-{plan_id}-{uuid.uuid4().hex[:8]}"
        final_cents = plan_price_cents(plan, discount)
        desc = f"VIP 2026 - {plan['name']}"
        if discount_label:
            desc += f" ({discount_label})"
        charge = epague_client().create_charge(
            final_cents / 100,
            external_id=external_id,
            description=desc,
            webhook_url=webhook_url_for_charge(),
        )
    except EpagueError:
        log.exception("Falha ao criar cobrança na Epague")
        tg.send_message(chat_id, "Deu erro ao gerar o Pix. Tenta de novo em instantes.")
        return

    charge_id = charge["id"]
    qr_code = charge["pix_copia_cola"]
    qr_b64 = charge.get("qr_code_base64", "")

    summary = f"⭐ Você escolheu: <b>{plan['name']}</b> — {format_price(final_cents)}"
    if discount_label:
        summary += f"\n<i>🎁 {discount_label} aplicado!</i>"
    if qr_b64:
        try:
            raw = qr_b64.split(",", 1)[-1]  # remove "data:image/png;base64," se houver
            tg.send_photo(chat_id, base64.b64decode(raw), "Escaneie o QR Code para pagar 📱")
        except Exception:
            log.exception("Falha ao enviar QR Code")
    tg.send_message(chat_id, summary + "\n\n" + PAY_INSTRUCTIONS)
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

    # pedidos de entrada no canal (convites com join request)
    if "chat_join_request" in update:
        handle_join_request(update)
        return jsonify({"ok": True})

    if "callback_query" in update:
        cq = update["callback_query"]
        callback_id = cq["id"]
        chat_id = cq["message"]["chat"]["id"]
        data = cq.get("data", "")
        cb_name = ((cq.get("from") or {}).get("first_name")) or "você"
        _track_user(chat_id, cb_name)
        if data.startswith("plan:"):
            handle_plan(chat_id, data.split(":", 1)[1], callback_id,
                        discount=promo_discount(), discount_label=promo_label())
        elif data.startswith("renew:"):
            handle_plan(chat_id, data.split(":", 1)[1], callback_id,
                        discount=RENEW_DISCOUNT, discount_label="desconto de renovação 10% OFF")
        elif data.startswith("nudge:"):
            # "QUERO X% OFF" do nudge → mostra os planos com o desconto aplicado
            try:
                pct = int(data.split(":", 1)[1])
            except (ValueError, IndexError):
                pct = 10
            disc = min(max(pct, 1), 50) / 100
            tg.answer_callback(callback_id)
            tg.send_message(
                chat_id,
                f"🔥 <b>{pct}% OFF liberado!</b> Escolha seu plano com o desconto aplicado 👇",
                reply_markup=plans_keyboard(discount=disc, prefix=f"npbuy:{pct}"),
            )
        elif data.startswith("npbuy:"):
            # "npbuy:{pct}:{plan_id}" → gera a cobrança com o desconto do nudge
            parts = data.split(":")
            try:
                pct = int(parts[1])
                plan_id = parts[2]
            except (ValueError, IndexError):
                tg.answer_callback(callback_id)
                return jsonify({"ok": True})
            disc = min(max(pct, 1), 50) / 100
            handle_plan(chat_id, plan_id, callback_id,
                        discount=disc, discount_label=f"oferta {pct}% OFF")
        elif data == "getlink":
            # botão do /status: gera link de acesso (com verificação na entrada)
            tg.answer_callback(callback_id)
            try:
                db_cloud.init_schema()
                active_sub = db_cloud.is_active(chat_id)
            except Exception:
                active_sub = None
            channel_id = (config.VIP_CHANNEL_ID or "").strip()
            if active_sub and channel_id:
                _send_recovery_invite(chat_id, cb_name, active_sub, channel_id)
            else:
                tg.send_message(chat_id,
                                "Você não tem assinatura ativa no momento. "
                                "Use /start para ver os planos. 👋")
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
    _track_user(chat_id, name)
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
        external_id = payload.get("external_id", "") or ""
        try:
            db_cloud.init_schema()
        except Exception:
            log.exception("Falha ao inicializar schema (seguindo)")
        try:
            if external_id and db_cloud.payment_exists(external_id):
                log.info("Webhook duplicado ignorado: %s", external_id)
                return jsonify({"ok": True, "duplicate": True})
        except Exception:
            log.exception("Falha ao checar duplicidade (seguindo com liberação)")
        try:
            release_access(chat_id, plan_id or "")
        except Exception:
            log.exception("Falha ao liberar acesso no Telegram")
        # registra a compra de forma independente do envio da mensagem
        # (idempotência do webhook + histórico do /status)
        try:
            db_cloud.init_schema()
            amount = payload.get("amount") or payload.get("value") or 0
            try:
                amount_cents = int(round(float(amount) * 100))
            except (TypeError, ValueError):
                amount_cents = 0
            if not amount_cents:
                amount_cents = config.PLAN_MAP.get(plan_id or "", {}).get("price_cents", 0)
            db_cloud.record_payment(chat_id, plan_id or "", amount_cents, external_id or None)
        except Exception:
            log.exception("Falha ao registrar pagamento")

    return jsonify({"ok": True})


def send_expiry_warnings() -> dict:
    """Avisos de vencimento (7, 3 e 1 dia antes), como o Baby Shark faz.

    Roda no cron diário antes da faxina. Cada marco é avisado uma única vez
    (coluna warned_days); o texto já empurra pra renovação com 10% OFF.
    """
    import math

    try:
        subs = db_cloud.list_active_expiring()
    except Exception:
        log.exception("Falha ao buscar assinaturas a vencer")
        return {"warned": 0, "failed": ["db_error"]}

    now = datetime.now(timezone.utc)
    warned, failed = 0, []
    for sub in subs:
        try:
            exp = datetime.fromisoformat(sub["expires_at"])
        except Exception:
            continue
        days_left = math.ceil((exp - now).total_seconds() / 86400)
        if days_left not in (7, 3, 1):
            continue
        already = {w for w in str(sub.get("warned_days") or "").split(",") if w}
        if str(days_left) in already:
            continue
        when = "amanhã" if days_left == 1 else f"em {days_left} dias"
        try:
            tg.send_message(
                sub["chat_id"],
                "⏳ <b>Seu VIP 2026 vence " + when + "!</b>\n\n"
                "Não fique de fora — quando vencer, mande /start aqui "
                "e renove com <b>10% OFF</b>. 🔄",
            )
            db_cloud.mark_warned(sub["chat_id"], days_left)
            warned += 1
        except Exception:
            log.exception(f"Falha ao avisar vencimento de {sub['chat_id']}")
            failed.append(sub["chat_id"])
    return {"warned": warned, "failed": failed}


# ---------------------------------------------------------------------------
# Nudges: cutucadas promocionais pra quem nunca comprou (a cada ~2 dias,
# até assinar). Banco de copys agressivas com {name} e {discount}.
# ---------------------------------------------------------------------------

NUDGE_COPIES = [
    "😤 {name}, ainda tá de fora? Enquanto você pensa, o acervo do VIP 2026 só cresce... E hoje eu liberei {discount}% OFF só pra você. 👇",
    "🔥 {name}, vou ser direto: você já viu o que tá perdendo? Conteúdo novo todo dia + {discount}% OFF agora. Não deixa pra depois. 👇",
    "👀 Ei, {name}... quantos VIPs você já pagou e se arrependeu? Aqui é diferente — e com {discount}% OFF fica fácil tirar a prova. 👇",
    "⏳ {name}, essa condição de {discount}% OFF não dura pra sempre. O VIP 2026 tá te esperando. 👇",
    "💎 {name}, quem tá dentro não sai mais. Quem tá fora continua perdendo conteúdo novo todo dia. {discount}% OFF pra você entrar agora. 👇",
    "🚨 Última chamada, {name}: {discount}% OFF no VIP 2026. Amanhã pode ser tarde — e mais caro. 👇",
    "😏 {name}, o pessoal que entrou essa semana já tá aproveitando tudo. E você aí de fora, com {discount}% OFF na mão... 👇",
    "🔥 Chega de enrolar, {name}: {discount}% OFF + acesso imediato + acervo novo todo dia. É agora. 👇",
]

# Descontos rotacionados por nudge (10% → 15% → 12% → 15% ...).
NUDGE_DISCOUNTS = [10, 15, 12, 15]
NUDGE_INTERVAL_HOURS = 48
NUDGE_BATCH_LIMIT = 50


def send_nudges() -> dict:
    """Dispara ofertas pra visitantes que nunca compraram.

    Roda no cron diário. Cada um recebe no máximo 1 nudge a cada 48h,
    com copy e desconto rotacionados. Quem assinar sai da lista
    automaticamente (tem pagamento registrado). Quem bloquear o bot
    é marcado como inalcançável e não recebe mais.
    """
    try:
        db_cloud.init_schema()
        candidates = db_cloud.nudge_candidates(
            limit=NUDGE_BATCH_LIMIT, min_interval_hours=NUDGE_INTERVAL_HOURS)
    except Exception:
        log.exception("Falha ao buscar candidatos a nudge")
        return {"nudged": 0, "failed": ["db_error"]}

    nudged, failed = 0, []
    for u in candidates:
        n = u["nudge_count"]
        discount = NUDGE_DISCOUNTS[n % len(NUDGE_DISCOUNTS)]
        copy = NUDGE_COPIES[n % len(NUDGE_COPIES)]
        name = u["first_name"] or "você"
        kb = tg.inline_keyboard([[(f"🔥 QUERO {discount}% OFF", f"nudge:{discount}")]])
        try:
            tg.send_message(u["chat_id"], copy.format(name=name, discount=discount),
                            reply_markup=kb)
            db_cloud.mark_nudged(u["chat_id"])
            nudged += 1
        except Exception:
            log.exception(f"Falha no nudge pra {u['chat_id']}")
            try:
                db_cloud.mark_unreachable(u["chat_id"])
            except Exception:
                pass
            failed.append(u["chat_id"])
    return {"nudged": nudged, "failed": failed}


def _track_user(chat_id: int, name: str) -> None:
    """Registra o visitante (base das campanhas). Nunca quebra o fluxo."""
    try:
        db_cloud.init_schema()
        db_cloud.track_user(chat_id, name or "")
    except Exception:
        log.exception("Falha ao registrar visitante")


def handle_join_request(update: dict) -> None:
    """Aprova/recusa pedidos de entrada no canal VIP.

    Só aprova quem tem assinatura ativa no banco. Link compartilhado
    com quem não pagou é recusado automaticamente — fecha o furo do
    "manda o link pro amigo".
    """
    req = update.get("chat_join_request") or {}
    chat = req.get("chat") or {}
    user = req.get("from") or {}
    channel_id = chat.get("id")
    user_id = user.get("id")
    if not channel_id or not user_id:
        return
    # segurança: só processa pedidos pro nosso canal
    try:
        expected = int((config.VIP_CHANNEL_ID or "0").strip() or 0)
    except (ValueError, TypeError):
        return
    if int(channel_id) != expected:
        return
    try:
        db_cloud.init_schema()
        active = db_cloud.is_active(user_id)
    except Exception:
        log.exception("Falha ao verificar assinatura no join request")
        active = None
    try:
        if active:
            tg.approve_join_request(channel_id, user_id)
            log.info("Entrada aprovada no VIP: %s", user_id)
        else:
            tg.decline_join_request(channel_id, user_id)
            log.info("Entrada recusada (sem assinatura ativa): %s", user_id)
    except Exception:
        log.exception("Falha ao processar join request de %s", user_id)


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


def _check_admin() -> bool:
    expected = (config.CRON_SECRET or "").strip()
    return bool(expected) and request.headers.get("Authorization") == f"Bearer {expected}"


@app.get("/admin/users")
def admin_users():
    """Lista visitantes recentes (base das campanhas). Protegido por Bearer."""
    if not _check_admin():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    try:
        db_cloud.init_schema()
        users = db_cloud.list_users(limit=50)
    except Exception:
        log.exception("Falha ao listar usuários")
        return jsonify({"ok": False, "error": "db_error"}), 500
    return jsonify({"ok": True, "users": users})


@app.post("/admin/grant")
def admin_grant():
    """Concede VIP cortesia: {"chat_id": 123, "plan_id": "mensal"}.

    Útil pra testes e cortesias. Protegido por Bearer.
    """
    if not _check_admin():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    data = request.get_json(force=True, silent=True) or {}
    try:
        chat_id = int(data.get("chat_id") or 0)
    except (TypeError, ValueError):
        chat_id = 0
    plan_id = str(data.get("plan_id") or "mensal")
    plan = config.PLAN_MAP.get(plan_id)
    if not chat_id or not plan:
        return jsonify({"ok": False, "error": "chat_id e plan_id válidos são obrigatórios"}), 400
    try:
        db_cloud.init_schema()
        db_cloud.save_sub(chat_id, plan_id, plan_expires_at(plan_id), None)
        # registra como compra (cortesia) pra aparecer no /status
        ext = f"cortesia-{chat_id}-{plan_id}"
        if not db_cloud.payment_exists(ext):
            db_cloud.record_payment(chat_id, plan_id, plan["price_cents"], ext)
    except Exception:
        log.exception("Falha ao conceder cortesia")
        return jsonify({"ok": False, "error": "db_error"}), 500
    return jsonify({"ok": True, "chat_id": chat_id, "plan_id": plan_id})


@app.get("/cron/expire")
def cron_expire():
    """Endpoint do cron diário da Vercel. Protegido por CRON_SECRET
    (a Vercel envia Authorization: Bearer <CRON_SECRET> automaticamente)."""
    expected = (config.CRON_SECRET or "").strip()
    if not expected or request.headers.get("Authorization") != f"Bearer {expected}":
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    try:
        db_cloud.init_schema()
    except Exception:
        log.exception("Falha ao inicializar schema no cron")
    warnings = send_expiry_warnings()
    result = expire_subscriptions()
    nudges = send_nudges()
    return jsonify({"ok": True, "warnings": warnings, "nudges": nudges, **result})


if __name__ == "__main__":
    import os

    missing = config.validate()
    if missing:
        raise SystemExit(f"Faltando variáveis de ambiente: {', '.join(missing)}.")
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
