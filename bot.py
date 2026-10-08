"""Bot de vendas no Telegram com pagamento Pix via Epague.

Fluxo:
  /start            -> pitch de vendas + botões de planos
  clica no plano   -> gera cobrança Pix -> envia QR + copia e cola +
                      instruções + botões (Verificar Status / Copiar Código / Ver QR Code)
  Verificar Status  -> consulta a Epague; se pago, libera o acesso
  /planos           -> lista os planos novamente

Roda com polling:  python bot.py
"""
import base64
import io
import logging
import os
import uuid

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

import config
import db
from epague import EpagueClient, EpagueError

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# TEXTOS — edite à vontade
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


def plans_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                f"{p['name']} — {format_price(p['price_cents'])}",
                callback_data=f"plan:{p['id']}",
            )
        ]
        for p in config.PLANS
    ]
    return InlineKeyboardMarkup(rows)


def payment_keyboard(charge_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Verificar Status", callback_data=f"verify:{charge_id}")],
            [InlineKeyboardButton("📋 Copiar Código", callback_data=f"code:{charge_id}")],
            [InlineKeyboardButton("📷 Ver QR Code", callback_data=f"qrcode:{charge_id}")],
        ]
    )


def qr_image_bytes(qr_code_base64: str) -> io.BytesIO:
    """Decodifica o QR em base64 devolvido pela Epague."""
    raw = qr_code_base64.split(",", 1)[-1]  # remove "data:image/png;base64," se houver
    photo = io.BytesIO(base64.b64decode(raw))
    photo.name = "qrcode.png"
    return photo


# ---------------------------------------------------------------------------
# HANDLERS
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    name = (user.first_name if user else None) or "visitante"

    # Vídeo de boas-vindas (opcional, configurado em WELCOME_VIDEO)
    if config.WELCOME_VIDEO:
        video = config.WELCOME_VIDEO
        if os.path.isfile(video):
            await update.message.reply_video(
                video=open(video, "rb"),
                caption=f"👋 Olá, {name}!",
            )
        else:
            # file_id do Telegram ou URL pública
            await update.message.reply_video(video, caption=f"👋 Olá, {name}!")

    await update.message.reply_text(
        PITCH.format(name=name),
        parse_mode=ParseMode.HTML,
        reply_markup=plans_keyboard(),
    )


async def planos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lines = []
    for p in config.PLANS:
        days = "acesso permanente" if not p.get("days") else f"{p['days']} dias de acesso"
        lines.append(
            f"💎 <b>{p['name']}</b> — {format_price(p['price_cents'])}\n"
            f"{p.get('description', '')}\n<i>{days}</i>"
        )
    await update.message.reply_text(
        "\n\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=plans_keyboard()
    )


async def on_plan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    plan_id = query.data.split(":", 1)[1]
    plan = config.PLAN_MAP.get(plan_id)
    if not plan:
        await query.message.reply_text("Plano não encontrado. Escolha novamente com /planos.")
        return

    chat_id = query.message.chat_id
    await query.message.reply_text("Gerando seu Pix, um instante... ⏳")

    try:
        # external_id único por cobrança; também é a chave de idempotência
        # da Epague (repetir o mesmo valor devolve a cobrança existente).
        external_id = f"vip2026-{chat_id}-{plan_id}-{uuid.uuid4().hex[:8]}"
        charge = EpagueClient(api_key=config.EPAGUE_API_KEY).create_charge(
            plan["price_cents"] / 100,
            external_id=external_id,
            description=f"VIP 2026 - {plan['name']}",
            webhook_url=config.WEBHOOK_URL or None,
        )
    except EpagueError:
        log.exception("Falha ao criar cobrança na Epague")
        await query.message.reply_text(
            "Deu erro ao gerar o Pix. Tenta de novo em instantes."
        )
        return

    charge_id = charge["id"]
    qr_code = charge["pix_copia_cola"]
    qr_b64 = charge.get("qr_code_base64", "")
    db.save_charge(
        charge_id, chat_id, plan_id, qr_code,
        qr_code_base64=qr_b64, status=charge.get("status", "created"),
    )

    # 1. Foto do QR Code
    if qr_b64:
        await query.message.reply_photo(
            qr_image_bytes(qr_b64), caption="Escaneie o QR Code para pagar 📱"
        )
    # 2. Instruções
    await query.message.reply_text(PAY_INSTRUCTIONS, parse_mode=ParseMode.HTML)
    # 3. Código copia e cola
    await query.message.reply_text(
        f"Copie o código abaixo:\n\n<code>{qr_code}</code>",
        parse_mode=ParseMode.HTML,
    )
    # 4. Botões de ação
    await query.message.reply_text(
        "Após efetuar o pagamento, clique no botão abaixo ⤵️",
        reply_markup=payment_keyboard(charge_id),
    )


async def on_verify(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer("Consultando pagamento...")
    charge_id = query.data.split(":", 1)[1]

    record = db.get_charge(charge_id)
    if not record:
        await query.message.reply_text(
            "Não achei essa cobrança. Gere um novo Pix com /planos."
        )
        return

    try:
        info = EpagueClient(api_key=config.EPAGUE_API_KEY).get_status(charge_id)
    except EpagueError:
        log.exception("Falha ao consultar status na Epague")
        await query.message.reply_text(
            "Não consegui consultar agora. Tenta de novo em instantes."
        )
        return

    status = str(info.get("status", "")).lower()
    if status in config.PAID_STATUSES:
        db.set_status(charge_id, status)
        plan = config.PLAN_MAP.get(record["plan_id"], {})
        await query.message.reply_text(
            "✅ <b>Pagamento confirmado!</b>\n\n"
            f"Seu acesso ao <b>{plan.get('name', 'plano')}</b> foi liberado. "
            "Bem-vindo(a)! 🎉\n\n"
            f"👉 Acesse aqui: {config.VIP_INVITE_LINK}",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    else:
        await query.message.reply_text(
            "Ainda não identificamos seu pagamento. Se você já pagou, aguarda "
            "uns instantes e clica de novo em <b>Verificar Status</b>. ⏳",
            parse_mode=ParseMode.HTML,
        )


async def on_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reenvia o código copia e cola (botão 'Copiar Código')."""
    query = update.callback_query
    await query.answer()
    charge_id = query.data.split(":", 1)[1]
    record = db.get_charge(charge_id)
    if not record:
        await query.message.reply_text(
            "Não achei essa cobrança. Gere um novo Pix com /planos."
        )
        return
    await query.message.reply_text(
        f"Copie o código abaixo:\n\n<code>{record['qr_code']}</code>",
        parse_mode=ParseMode.HTML,
    )


async def on_qrcode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reenvia a imagem do QR (botão 'Ver QR Code')."""
    query = update.callback_query
    await query.answer()
    charge_id = query.data.split(":", 1)[1]
    record = db.get_charge(charge_id)
    if not record or not record.get("qr_code_base64"):
        await query.message.reply_text(
            "Não achei o QR dessa cobrança. Gere um novo Pix com /planos."
        )
        return
    await query.message.reply_photo(
        qr_image_bytes(record["qr_code_base64"]),
        caption="Escaneie o QR Code para pagar 📱",
    )


def main() -> None:
    missing = config.validate()
    if missing:
        raise SystemExit(
            f"Faltando variáveis de ambiente: {', '.join(missing)}. "
            "Copie .env.example para .env e preencha os valores."
        )
    app = Application.builder().token(config.BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("planos", planos))
    app.add_handler(CallbackQueryHandler(on_plan, pattern=r"^plan:"))
    app.add_handler(CallbackQueryHandler(on_verify, pattern=r"^verify:"))
    app.add_handler(CallbackQueryHandler(on_code, pattern=r"^code:"))
    app.add_handler(CallbackQueryHandler(on_qrcode, pattern=r"^qrcode:"))
    log.info("Bot rodando (polling)...")
    app.run_polling()


if __name__ == "__main__":
    main()
