"""Cliente minimalista da API PushinPay.

Endpoints verificados contra a documentação oficial (https://doc.pushinpay.com.br,
2026-10-08):
  Produção: https://api.pushinpay.com.br/api
  Sandbox:   https://api-sandbox.pushinpay.com.br/api
  Auth:      header Authorization: Bearer <API_KEY>
  Criar QR:  POST /pix/cashIn  {"value": centavos:int, "webhook_url"?: str}
  Status:    GET  /transactions/{ID}
  Webhook:   {"id": ..., "value": ..., "status": "paid"}
"""
import requests

import config

PROD_BASE = "https://api.pushinpay.com.br/api"
SANDBOX_BASE = "https://api-sandbox.pushinpay.com.br/api"


class PushinPayError(Exception):
    pass


class PushinPayClient:
    def __init__(self, api_key=None, sandbox=None):
        self.api_key = api_key or config.PUSHINPAY_API_KEY
        use_sandbox = config.PUSHINPAY_SANDBOX if sandbox is None else sandbox
        self.base = SANDBOX_BASE if use_sandbox else PROD_BASE
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }
        )

    def _request(self, method, path, **kwargs):
        resp = self.session.request(method, self.base + path, timeout=30, **kwargs)
        if resp.status_code >= 400:
            raise PushinPayError(f"PushinPay {resp.status_code}: {resp.text}")
        return resp.json()

    def create_charge(self, value_cents, webhook_url=None):
        """Cria uma cobrança Pix.

        Retorna dict com: id, qr_code (copia e cola), qr_code_base64,
        status, value.
        """
        payload = {"value": int(value_cents)}
        if webhook_url:
            payload["webhook_url"] = webhook_url
        return self._request("POST", "/pix/cashIn", json=payload)

    def get_status(self, charge_id):
        """Consulta o status de uma cobrança pelo id.

        Endpoint oficial (doc.pushinpay.com.br): GET /api/transactions/{ID}.
        """
        return self._request("GET", f"/transactions/{charge_id}")
