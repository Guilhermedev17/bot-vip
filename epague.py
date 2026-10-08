"""Cliente da API Epague (https://epague.net) — pronto pra produção.

Endpoints usados:
  POST /api/pix/create   → gera cobrança PIX (QR Code + copia e cola)
  GET  /api/pix/{id}     → consulta status (id = UUID, txid ou external_id)

Autenticação: header ``x-api-key`` em todas as chamadas.

Recursos implementados:
  - Idempotência via ``external_id`` (a API devolve a transação existente
    sem criar nova cobrança quando o external_id se repete).
  - Retry com backoff exponencial em 429/5xx (respeita Retry-After).
  - Erros mapeados por código HTTP com mensagens claras.
  - Validação HMAC-SHA256 dos webhooks (header x-webhook-signature).

Splits/subcontas: não usados neste bot (venda direta, carteira única).
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import time

import requests

log = logging.getLogger(__name__)

BASE_URL = "https://epague.net"
CREATE_PATH = "/api/pix/create"
STATUS_PATH = "/api/pix/{charge_id}"

# Status considerados "pagos" (docs: pending | paid | expired | cancelled)
PAID_STATUSES = {"paid"}

# Webhook
WEBHOOK_EVENT_PAID = "payment.confirmed"
SIGNATURE_HEADER = "x-webhook-signature"
# Janela aceita pro timestamp da assinatura (evita replay)
SIGNATURE_TOLERANCE_SECONDS = 300

# Política de retry
_MAX_RETRIES = 3
_BACKOFF_BASE = 1.0  # segundos; dobra a cada tentativa
_TIMEOUT = 15
_RETRYABLE = {429, 500, 502, 503, 504}


class EpagueError(Exception):
    """Erro da API Epague, com código HTTP e mensagem amigável."""

    def __init__(self, status_code: int | None, message: str):
        self.status_code = status_code
        super().__init__(f"[Epague {status_code}] {message}")


def _friendly_message(status_code: int, payload) -> str:
    detail = ""
    if isinstance(payload, dict):
        detail = str(payload.get("message") or payload.get("error") or "")
    hints = {
        400: "Requisição inválida (valor, split ou parâmetros).",
        401: "API key inválida ou ausente — confira EPAGUE_API_KEY.",
        404: "Transação não encontrada.",
        409: "Conflito de external_id.",
        429: "Muitas requisições (rate limit) — aguarde alguns segundos.",
        500: "Erro interno da Epague.",
        502: "Gateway da Epague indisponível.",
        503: "Serviço da Epague temporariamente indisponível.",
    }
    base = hints.get(status_code, "Erro na API da Epague.")
    return f"{base} {detail}".strip()


def verify_webhook_signature(
    raw_body: bytes, signature_header: str | None, secret: str
) -> bool:
    """Valida a assinatura HMAC-SHA256 do webhook.

    Header: ``x-webhook-signature: t=<unix>,v1=<hmac_hex>``
    Conteúdo assinado: ``"<unix>.<body_cru>"`` com o webhook_secret.
    """
    if not signature_header:
        return False
    try:
        fields = dict(part.split("=", 1) for part in signature_header.split(","))
        ts = fields.get("t", "").strip()
        sent = fields.get("v1", "").strip()
        if not ts or not sent:
            return False
        # Rejeita timestamps fora da janela (proteção contra replay)
        if abs(time.time() - int(ts)) > SIGNATURE_TOLERANCE_SECONDS:
            log.warning("Webhook com timestamp fora da janela: %s", ts)
            return False
        signed = ts.encode() + b"." + raw_body
        expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, sent)
    except Exception:
        log.exception("Falha ao validar assinatura do webhook")
        return False


class EpagueClient:
    def __init__(self, api_key: str, base_url: str = BASE_URL):
        if not api_key:
            raise EpagueError(None, "EPAGUE_API_KEY não configurada.")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")

    def _headers(self) -> dict:
        return {
            "x-api-key": self.api_key,
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, **kwargs) -> dict:
        """Executa a chamada com retry exponencial em 429/5xx.

        4xx (exceto 429) não são retentados: é erro do cliente.
        """
        url = self.base_url + path
        last_exc: Exception | None = None

        for attempt in range(_MAX_RETRIES + 1):
            try:
                resp = requests.request(
                    method, url, headers=self._headers(), timeout=_TIMEOUT, **kwargs
                )
            except requests.RequestException as exc:
                last_exc = exc
                log.warning("Falha de rede na Epague (tentativa %d): %s", attempt + 1, exc)
            else:
                if resp.status_code < 400:
                    try:
                        return resp.json()
                    except ValueError:
                        raise EpagueError(resp.status_code, "Resposta não-JSON da Epague.")
                if resp.status_code not in _RETRYABLE:
                    try:
                        payload = resp.json()
                    except ValueError:
                        payload = None
                    raise EpagueError(
                        resp.status_code, _friendly_message(resp.status_code, payload)
                    )
                last_exc = EpagueError(
                    resp.status_code, _friendly_message(resp.status_code, None)
                )
                log.warning(
                    "Epague retornou %d (tentativa %d)", resp.status_code, attempt + 1
                )
                # Respeita Retry-After quando a API mandar
                retry_after = resp.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    time.sleep(int(retry_after))
                    continue

            if attempt < _MAX_RETRIES:
                time.sleep(_BACKOFF_BASE * (2 ** attempt))

        raise last_exc if last_exc else EpagueError(None, "Falha desconhecida na Epague.")

    def create_charge(
        self,
        amount_reais: float,
        *,
        external_id: str,
        description: str = "",
        webhook_url: str | None = None,
        expiration: int = 1800,
    ) -> dict:
        """Cria uma cobrança PIX. Retorna o objeto ``transaction``.

        ``amount_reais`` em reais com 2 casas (ex: 49.90). Mínimo R$ 1,00.
        ``external_id`` funciona como chave de idempotência: repetir o
        mesmo valor devolve a transação existente (200) sem nova cobrança.
        """
        if amount_reais < 1:
            raise EpagueError(None, "Valor mínimo da cobrança: R$ 1,00.")

        body: dict = {
            "amount": round(amount_reais, 2),
            "external_id": external_id,
            "expiration": expiration,
        }
        if description:
            body["description"] = description
        if webhook_url:
            body["webhook_url"] = webhook_url

        try:
            data = self._request("POST", CREATE_PATH, json=body)
        except EpagueError as exc:
            # 409: external_id em conflito e não retornável — gera outro id
            if exc.status_code == 409:
                log.warning("Conflito de external_id, gerando novo: %s", external_id)
                body["external_id"] = f"{external_id}-{int(time.time())}"
                data = self._request("POST", CREATE_PATH, json=body)
            else:
                raise

        transaction = (data or {}).get("transaction") or {}
        if not transaction.get("id") or not transaction.get("pix_copia_cola"):
            raise EpagueError(None, f"Resposta inesperada da Epague: {data}")
        return transaction

    def get_status(self, charge_id: str) -> dict:
        """Consulta a cobrança (aceita UUID, txid ou external_id)."""
        data = self._request("GET", STATUS_PATH.format(charge_id=charge_id))
        return data or {}
