"""Cliente mínimo da Bot API do Telegram (HTTP puro).

Usado pelo app em modo webhook (produção). Sem polling, sem dependências
extras: só requests.
"""
from __future__ import annotations

import io
import logging

import requests

import config

log = logging.getLogger(__name__)
_TIMEOUT = 15


def _api_url() -> str:
    return f"https://api.telegram.org/bot{config.BOT_TOKEN}"


def _post(method: str, payload: dict, files: dict | None = None) -> dict:
    resp = requests.post(
        f"{_api_url()}/{method}",
        json=payload if files is None else None,
        data=payload if files is not None else None,
        files=files,
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API erro em {method}: {data}")
    return data["result"]


def send_message(chat_id: int, text: str, reply_markup: dict | None = None) -> dict:
    payload: dict = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return _post("sendMessage", payload)


def send_photo(chat_id: int, photo_bytes: bytes, caption: str = "") -> dict:
    bio = io.BytesIO(photo_bytes)
    bio.name = "qrcode.png"
    payload: dict = {"chat_id": str(chat_id), "caption": caption}
    return _post("sendPhoto", payload, files={"photo": bio})


def send_video(chat_id: int, video: str, caption: str = "") -> dict:
    """Envia vídeo por file_id do Telegram ou URL pública."""
    return _post("sendVideo", {"chat_id": chat_id, "video": video, "caption": caption})


def answer_callback(callback_query_id: str, text: str = "") -> dict:
    payload: dict = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    return _post("answerCallbackQuery", payload)


def inline_keyboard(buttons: list[list[tuple[str, str]]]) -> dict:
    """Monta reply_markup a partir de [[(texto, callback_data), ...], ...]."""
    return {
        "inline_keyboard": [
            [{"text": text, "callback_data": data} for text, data in row]
            for row in buttons
        ]
    }


def set_webhook(url: str, secret_token: str = "") -> dict:
    payload: dict = {"url": url}
    if secret_token:
        payload["secret_token"] = secret_token
    return _post("setWebhook", payload)


def create_chat_invite_link(
    chat_id: int | str,
    name: str | None = None,
    member_limit: int = 1,
    expire_in_seconds: int = 86400,
    creates_join_request: bool = False,
) -> str:
    """Cria um link de convite.

    Padrão: individual (1 uso, expira em 24h).
    Com creates_join_request=True: o link gera um PEDIDO de entrada em vez de
    liberar direto — o bot aprova/recusa via approve/decline_join_request.
    (Não combina member_limit com join request: a API do Telegram não permite.)
    Exige que o bot seja administrador do canal/grupo com permissão de convidar.
    """
    import time

    payload: dict = {"chat_id": chat_id}
    if creates_join_request:
        payload["creates_join_request"] = True
    else:
        payload["member_limit"] = member_limit
        payload["expire_date"] = int(time.time()) + expire_in_seconds
    if name:
        payload["name"] = name[:32]  # Telegram limita o nome do convite a 32 chars
    return _post("createChatInviteLink", payload)["invite_link"]


def approve_join_request(chat_id: int | str, user_id: int) -> bool:
    """Aprova um pedido de entrada no canal/grupo."""
    return _post("approveChatJoinRequest", {"chat_id": chat_id, "user_id": user_id})


def decline_join_request(chat_id: int | str, user_id: int) -> bool:
    """Recusa um pedido de entrada no canal/grupo."""
    return _post("declineChatJoinRequest", {"chat_id": chat_id, "user_id": user_id})


def revoke_chat_invite_link(chat_id: int | str, invite_link: str) -> dict:
    """Revoga (invalida) um link de convite."""
    return _post(
        "revokeChatInviteLink", {"chat_id": chat_id, "invite_link": invite_link}
    )


def ban_chat_member(chat_id: int | str, user_id: int) -> dict:
    """Bane um membro do canal/grupo."""
    return _post("banChatMember", {"chat_id": chat_id, "user_id": user_id})


def unban_chat_member(chat_id: int | str, user_id: int) -> dict:
    """Remove o ban (usado após banir pra 'expulsar' sem bloquear o retorno)."""
    return _post(
        "unbanChatMember",
        {"chat_id": chat_id, "user_id": user_id, "only_if_banned": True},
    )
