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
