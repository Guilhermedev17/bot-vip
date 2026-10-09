"""Configuração central do bot.

Tudo vem de variáveis de ambiente (arquivo .env). Nada de segredo
hardcoded no código.
"""
import json
import os

from dotenv import load_dotenv

load_dotenv()

# --- Telegram ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
# (opcional) token secreto enviado no header X-Telegram-Bot-Api-Secret-Token.
# Configure o mesmo valor ao chamar setWebhook; o app rejeita updates sem ele.
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")

# --- URL pública do app (produção) ---
# Ex: https://vip2026bot.vercel.app — usada pra montar o webhook_url
# das cobranças quando WEBHOOK_URL não está definido.
PUBLIC_URL = os.getenv("PUBLIC_URL", "")

# --- Epague ---
EPAGUE_API_KEY = os.getenv("EPAGUE_API_KEY", "")
EPAGUE_BASE_URL = os.getenv("EPAGUE_BASE_URL", "https://epague.net")
# Segredo HMAC pra validar os webhooks (Dashboard / Integrações / Webhooks).
# Vazio = aceita sem validar (só pra teste).
EPAGUE_WEBHOOK_SECRET = os.getenv("EPAGUE_WEBHOOK_SECRET", "")
# URL pública que a Epague chama quando o pagamento muda de status.
# Ex: https://abc123.ngrok.io/webhook/epague
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")

# --- Banco de dados na nuvem (Turso) — caderneta de assinaturas ---
# Quem comprou, qual plano e quando vence. Sobrevive a recriações do bot/canal.
TURSO_URL = os.getenv("TURSO_URL", "")
TURSO_TOKEN = os.getenv("TURSO_TOKEN", "")

# --- Canal VIP ---
# ID numérico do canal (ex: -1003739284829). Usado pra gerar convites
# individuais e remover quem teve o plano vencido.
# O bot precisa ser ADMINISTRADOR do canal pra isso funcionar.
VIP_CHANNEL_ID = os.getenv("VIP_CHANNEL_ID", "")

# Segredo do cron diário de expiração (endpoint /cron/expire).
# A Vercel envia automaticamente "Authorization: Bearer <CRON_SECRET>"
# nas chamadas do cron. Vazio = endpoint recusado (fail-safe).
CRON_SECRET = os.getenv("CRON_SECRET", "")

# --- Acesso VIP ---
# Link de convite do grupo/canal VIP, enviado após o pagamento confirmado.
VIP_INVITE_LINK = os.getenv("VIP_INVITE_LINK", "")

# --- Boas-vindas ---
# Vídeo de apresentação enviado no /start antes do pitch (como o CS VIP 2 faz).
# Aceita file_id do Telegram, URL pública ou caminho de arquivo local.
# Vazio = não envia vídeo.
WELCOME_VIDEO = os.getenv("WELCOME_VIDEO", "")

# Status que consideramos "pago" (Epague: pending | paid | expired | cancelled).
# Tanto no botão Verificar Status quanto no webhook.
PAID_STATUSES = {"paid"}

# --- Planos ---
# Edite aqui ou sobrescreva com a variável PLANS_JSON no .env.
# price_cents: valor em centavos (4999 = R$ 49,99)
# days: dias de acesso (0 = permanente/vitalício)
DEFAULT_PLANS = [
    {
        "id": "semanal",
        "name": "Plano Semanal",
        "price_cents": 999,
        "description": "7 dias de acesso total ao conteúdo VIP",
        "days": 7,
    },
    {
        "id": "mensal",
        "name": "Plano Mensal",
        "price_cents": 2199,
        "description": "30 dias de acesso total ao conteúdo VIP",
        "days": 30,
    },
    {
        "id": "vitalicio",
        "name": "Plano Vitalício",
        "price_cents": 4999,
        "description": "Acesso permanente + todos os bônus",
        "days": 0,
    },
]


def _load_plans():
    raw = os.getenv("PLANS_JSON", "").strip()
    if raw:
        try:
            plans = json.loads(raw)
            assert isinstance(plans, list) and all(
                isinstance(p, dict) and {"id", "name", "price_cents"} <= set(p.keys())
                for p in plans
            )
            return plans
        except (json.JSONDecodeError, AssertionError):
            print("AVISO: PLANS_JSON inválido, usando planos padrão.")
    return DEFAULT_PLANS


PLANS = _load_plans()
PLAN_MAP = {p["id"]: p for p in PLANS}


def validate():
    """Retorna lista de variáveis obrigatórias que estão vazias."""
    missing = [
        name
        for name, value in {
            "BOT_TOKEN": BOT_TOKEN,
            "EPAGUE_API_KEY": EPAGUE_API_KEY,
            "VIP_INVITE_LINK": VIP_INVITE_LINK,
        }.items()
        if not value
    ]
    return missing
