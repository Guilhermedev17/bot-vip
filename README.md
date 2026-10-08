# Bot de Vendas no Telegram + Epague

Scaffold de bot de vendas com pagamento via **Pix (Epague)**, replicando o fluxo de bots como `@csvip2bot` / `@sharkspyvipbot`:

1. `/start` → pitch de vendas com botões de planos
2. Usuário escolhe o plano → bot gera cobrança Pix na Epague
3. Bot envia a **imagem do QR** + **código "copia e cola"** + instruções + botões **Verificar Status / Copiar Código / Ver QR Code**
4. Pagamento confirmado (pelo botão ou pelo webhook) → bot **libera o acesso** (link do grupo/canal VIP)

## Arquivos

| Arquivo | O que faz |
|---|---|
| `app.py` | **App principal (produção):** Flask em modo webhook — recebe updates do Telegram em `/telegram` e webhooks da Epague em `/webhook/epague`. 100% stateless (sem banco) |
| `tg.py` | Cliente mínimo da Bot API do Telegram (HTTP puro) usado pelo `app.py` |
| `api/index.py` | Entrypoint da Vercel (importa o app) |
| `vercel.json` | Config da Vercel (rewrites) |
| `bot.py` | Bot em modo polling — só pra testes locais (produção usa `app.py`) |
| `webhook.py` | Servidor Flask standalone do webhook da Epague (alternativa pra VPS) |
| `epague.py` | Cliente da API Epague (`create_charge` / `get_status`, retry exponencial, idempotência, HMAC) |
| `config.py` | Lê tudo das variáveis de ambiente (planos, tokens, links) |
| `db.py` | SQLite (legado, só usado pelo `bot.py` em polling) |
| `deploy/` | Deploy em VPS: `deploy.sh`, `nginx.conf`, units systemd |
| `.env.example` | Modelo de configuração (copie para `.env`) |

## Passo a passo

### 1. Criar o bot no Telegram

1. Abra conversa com o [@BotFather](https://t.me/BotFather)
2. Envie `/newbot`, escolha um nome e um username (tem que terminar em `bot`)
3. Ele devolve o **token** → vai em `BOT_TOKEN` no `.env`

### 2. Criar conta na Epague

1. Cadastre-se em `epague.net`
2. No painel (Dashboard / Integrações / API Keys), gere a **API key** **somente com a permissão "Cobranças"** → vai em `EPAGUE_API_KEY`
3. ⚠️ **Importante:** no cadastro, verifique se aceitam **pessoa física (CPF)** ou se exigem **CNPJ**. Alguns gateways liberam o sandbox com CPF mas pedem empresa para ativar o modo produção — confirme antes de anunciar.
3. Em Dashboard / Integrações / Webhooks, gere o **webhook secret** → vai em `EPAGUE_WEBHOOK_SECRET` (validação HMAC).

### 3. Configurar

```bash
cd bot-vendas-pushinpay
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edite o .env com seus dados
```

### 4. Webhook com URL pública (para teste local)

A Epague precisa de uma URL pública para avisar quando o Pix é pago. Para testar na sua máquina, use o ngrok:

```bash
ngrok http 5000
```

Coloque a URL no `.env`:

```
WEBHOOK_URL=https://abc123.ngrok.io/webhook/epague
```

> O `webhook.py` valida a assinatura HMAC (`x-webhook-signature`) quando `EPAGUE_WEBHOOK_SECRET` está preenchido — recomendado em produção.

### 5. Rodar local (teste)

Abra **dois terminais**:

```bash
# terminal 1 — o bot
python bot.py

# terminal 2 — o servidor do webhook
python webhook.py
```

Mande `/start` para o seu bot no Telegram e teste o fluxo inteiro no sandbox.

### 6. Produção numa VPS (24/7)

Para o bot ficar no ar direto, use uma VPS barata (Hostinger, Hetzner etc.). Exemplo com `systemd`:

```ini
# /etc/systemd/system/bot-vendas.service
[Unit]
Description=Bot de vendas Telegram
After=network.target

[Service]
WorkingDirectory=/opt/bot-vendas
ExecStart=/opt/bot-vendas/venv/bin/python /opt/bot-vendas/bot.py
Restart=always
EnvironmentFile=/opt/bot-vendas/.env

[Install]
WantedBy=multi-user.target
```

```ini
# /etc/systemd/system/bot-vendas-webhook.service
[Unit]
Description=Webhook Epague
After=network.target

[Service]
WorkingDirectory=/opt/bot-vendas
ExecStart=/opt/bot-vendas/venv/bin/gunicorn webhook:app -b 0.0.0.0:5000
Restart=always
EnvironmentFile=/opt/bot-vendas/.env

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now bot-vendas bot-vendas-webhook
```

Na VPS, `WEBHOOK_URL` deve ser o seu domínio/IP público (ex: `https://seudominio.com/webhook/epague`).

## Deploy grátis na Vercel (recomendado)

O app (`app.py`) é 100% stateless e roda em modo webhook — feito pra
hospedagem gratuita: sem banco de dados, sem disco persistente.

1. Crie conta grátis no [GitHub](https://github.com) e suba esta pasta
   num repositório (pode arrastar os arquivos pela interface web).
2. Crie conta grátis na [Vercel](https://vercel.com) (entre com o GitHub)
   e importe o repositório.
3. Em **Settings → Environment Variables**, cadastre:
   `BOT_TOKEN`, `EPAGUE_API_KEY`, `EPAGUE_WEBHOOK_SECRET`,
   `VIP_INVITE_LINK`, `PUBLIC_URL` (a URL `https://seuapp.vercel.app`
   que a Vercel gerar), `WELCOME_VIDEO` (opcional).
4. Deploy. Depois configure o webhook do Telegram (uma vez só):
   ```bash
   curl "https://api.telegram.org/botSEU_TOKEN/setWebhook" \
     -d "url=https://seuapp.vercel.app/telegram"
   ```
   O `WEBHOOK_URL` das cobranças é montado sozinho a partir de `PUBLIC_URL`.

## Deploy em VPS (alternativa)

Veja `deploy/`: `deploy.sh` instala tudo numa Ubuntu 24.04 zerada
(Python, nginx, HTTPS via Let's Encrypt, systemd). Útil se um dia ele
quiser sair do plano gratuito.

## Personalizar

- **Texto de vendas:** constante `PITCH` em `app.py` (produção) / `bot.py` (teste local)
- **Instruções de pagamento:** constante `PAY_INSTRUCTIONS` em `app.py` / `bot.py`
- **Planos:** edite `DEFAULT_PLANS` em `config.py` ou passe `PLANS_JSON` no `.env`
- **Link VIP:** `VIP_INVITE_LINK` no `.env` (use link de convite do grupo/canal)

## Avisos

- A integração segue a documentação oficial da Epague (epague.net/docs): `POST /api/pix/create`, `GET /api/pix/{id}` e webhook `payment.confirmed` com HMAC-SHA256.
- **Nunca** commite o `.env` com tokens reais.
- Taxa da Epague no Pix: **R$ 0,50 por Pix aprovado**, sem mensalidade obrigatória (site em 2026-10-08). API e dashboard inclusos; saques via Pix pelo painel.
- Tokens de API podem ter restrição de IP configurada no painel — ao gerar o token, confira a lista de IPs permitidos (ou deixe sem restrição) para o bot conseguir chamar a API da VPS.

## Toques aprendidos dos concorrentes (mapeamento de 2026-10-08)

- **Vídeo de boas-vindas** (o CS VIP 2 manda um vídeo de ~13s no `/start`): configure
  `WELCOME_VIDEO` no `.env` com o `file_id`, a URL ou o caminho local do vídeo.
- **Saudação com o nome** (o Shark Spy cumprimenta "Olá, {nome}!"): o `/start` já usa
  o primeiro nome do usuário automaticamente.
- **Promoções com desconto** (o CS VIP 2 já fez 50% OFF / 20% OFF): basta editar os
  planos em `PLANS_JSON` ou `config.py` — o bot gera os botões e preços sozinho.
