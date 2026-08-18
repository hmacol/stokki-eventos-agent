#!/usr/bin/env bash
# Instalacao do app de resposta ao insucesso (Flask) na VPS (Ubuntu 24.04) -- FreshLog
#
# Uso (como root, na VPS):
#   1. Copiar esta pasta para a VPS:  scp -r insucesso_resposta/ root@IP_DA_VPS:/opt/insucesso-resposta
#   2. ssh root@IP_DA_VPS
#   3. cd /opt/insucesso-resposta/infra && bash instalar_vps.sh
#
# O script e idempotente: pode rodar de novo se algo falhar no meio.
# Obs.: se os arquivos vieram do Windows, o proprio script corrige CRLF no .env/Caddyfile.
# Mesmo molde de confirmacao_motoristas/infra/instalar_vps.sh -- se essa
# VPS ja roda o app de confirmacao de motoristas, o Caddy e o firewall
# ja estao instalados e os passos 2/6 e 3/6 so confirmam o estado atual.

set -euo pipefail

DIR="$(cd "$(dirname "$0")/.." && pwd)"  # raiz do projeto (insucesso_resposta/)
cd "$DIR"

echo "== 1/6 Pacotes do sistema =="
apt-get update -qq
apt-get install -y -qq python3-venv python3-pip curl gnupg debian-keyring debian-archive-keyring apt-transport-https

echo "== 2/6 Firewall (ufw) =="
if command -v ufw >/dev/null 2>&1; then
  ufw allow 22/tcp >/dev/null
  ufw allow 80/tcp >/dev/null
  ufw allow 443/tcp >/dev/null
  ufw --force enable >/dev/null
  echo "ufw ativo: portas 22, 80 e 443 liberadas"
fi

echo "== 3/6 Caddy (proxy HTTPS automatico) =="
if ! command -v caddy >/dev/null 2>&1; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
  apt-get update -qq
  apt-get install -y -qq caddy
fi
sed -i 's/\r$//' infra/Caddyfile || true
# Acrescenta este site ao Caddyfile em vez de sobrescrever -- outra
# VPS pode ja estar servindo confirmacao_motoristas no mesmo Caddy.
if ! grep -q "insucesso.freshhub.com.br" /etc/caddy/Caddyfile 2>/dev/null; then
  cat infra/Caddyfile >> /etc/caddy/Caddyfile
fi
systemctl reload caddy 2>/dev/null || systemctl restart caddy

echo "== 4/6 Arquivo .env =="
if [ ! -f infra/.env ]; then
  cp infra/env.exemplo infra/.env
  sed -i 's/\r$//' infra/.env || true
  echo "infra/.env criado -- PREENCHA TOKEN_SECRET e SYNC_SECRET com os MESMOS valores"
  echo "de resposta_insucesso.token_secret / .sync_secret no config.yaml da maquina local."
  echo "Edite com: nano $DIR/infra/.env   -- depois rode este script de novo."
  exit 0
else
  echo "infra/.env ja existe, mantendo."
fi

echo "== 5/6 Ambiente Python =="
python3 -m venv venv
venv/bin/pip install -q --upgrade pip
venv/bin/pip install -q -r requirements.txt

echo "== 6/6 Servico systemd =="
sed -i 's/\r$//' infra/insucesso-resposta.service || true
cp -f infra/insucesso-resposta.service /etc/systemd/system/insucesso-resposta.service
systemctl daemon-reload
systemctl enable insucesso-resposta >/dev/null
systemctl restart insucesso-resposta

echo
echo "Pronto. Acompanhe com: journalctl -u insucesso-resposta -f"
echo "Se o DNS ja aponta para esta VPS, teste: https://insucesso.freshhub.com.br/r/teste"
echo "(deve dar 'link nao e valido' -- esperado, sem um token real gerado pela maquina local)."
