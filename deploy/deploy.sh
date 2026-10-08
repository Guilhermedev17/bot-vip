#!/usr/bin/env bash
# Deploy do bot VIP + webhook Epague numa VPS Ubuntu 24.04 zerada.
set -euo pipefail
DOMAIN="${1:?Informe o domínio: sudo bash deploy.sh seubot.duckdns.org}"
SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DEST=/opt/bot-vendas
echo "==> Instalando pacotes..."
apt-get update -qq
apt-get install -y -qq python3 python3-venv nginx certbot python3-certbot-nginx
echo "==> Copiando projeto para $DEST..."
mkdir -p "$DEST"
cp "$SRC_DIR"/bot.py "$SRC_DIR"/config.py "$SRC_DIR"/db.py \
   "$SRC_DIR"/epague.py "$SRC_DIR"/webhook.py \
   "$SRC_DIR"/requirements.txt "$SRC_DIR"/.env "$DEST"/
chmod 600 "$DEST/.env"
echo "==> Criando venv e instalando dependências..."
python3 -m venv "$DEST/venv"
"$DEST/venv/bin/pip" install -q --upgrade pip
"$DEST/venv/bin/pip" install -q -r "$DEST/requirements.txt"
echo "==> Instalando serviços systemd..."
cp "$SRC_DIR/deploy/bot-vendas.service" /etc/systemd/system/
cp "$SRC_DIR/deploy/bot-vendas-webhook.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now bot-vendas.service
systemctl enable --now bot-vendas-webhook.service
echo "==> Configurando nginx..."
sed "s/{{DOMAIN}}/$DOMAIN/g" "$SRC_DIR/deploy/nginx.conf" > /etc/nginx/sites-available/bot-vip
ln -sf /etc/nginx/sites-available/bot-vip /etc/nginx/sites-enabled/bot-vip
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx
echo "==> Emitindo certificado HTTPS (Let's Encrypt)..."
certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos \
    --register-unsafely-without-email --redirect
echo "==> Ajustando WEBHOOK_URL no .env..."
sed -i "s|^WEBHOOK_URL=.*|WEBHOOK_URL=https://$DOMAIN/webhook/epague|" "$DEST/.env"
systemctl restart bot-vendas-webhook.service
echo "DEPLOY CONCLUÍDO"
