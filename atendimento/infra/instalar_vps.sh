#!/usr/bin/env bash
# Instalacao do Chatwoot na VPS (Ubuntu 22.04/24.04) — FreshLog Fase 0
#
# Uso (como root, na VPS):
#   1. Copiar esta pasta para a VPS:  scp -r infra/ root@IP_DA_VPS:/opt/chatwoot
#   2. ssh root@IP_DA_VPS
#   3. cd /opt/chatwoot && bash instalar_vps.sh
#
# O script e idempotente: pode rodar de novo se algo falhar no meio.
# Obs.: se os arquivos vieram do Windows, o proprio script corrige CRLF no .env/Caddyfile.

set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

echo "== 1/6 Docker =="
if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sh
fi
docker compose version >/dev/null

echo "== 2/6 Firewall (ufw) =="
if command -v ufw >/dev/null 2>&1; then
  ufw allow 22/tcp >/dev/null
  ufw allow 80/tcp >/dev/null
  ufw allow 443/tcp >/dev/null
  ufw --force enable >/dev/null
  echo "ufw ativo: portas 22, 80 e 443 liberadas"
fi

echo "== 3/6 Arquivo .env =="
if [ ! -f .env ]; then
  cp env.exemplo .env
  # normaliza fins de linha caso os arquivos tenham vindo do Windows
  sed -i 's/\r$//' .env Caddyfile docker-compose.yml || true
  # gera segredos
  sed -i "s|^SECRET_KEY_BASE=.*|SECRET_KEY_BASE=$(openssl rand -hex 64)|" .env
  sed -i "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$(openssl rand -hex 24)|" .env
  sed -i "s|^REDIS_PASSWORD=.*|REDIS_PASSWORD=$(openssl rand -hex 24)|" .env
  echo ".env criado com segredos gerados. REVISE as linhas de SMTP antes de continuar!"
  echo "Edite com: nano $DIR/.env   — depois rode este script de novo."
  exit 0
else
  echo ".env ja existe, mantendo."
fi

echo "== 4/6 Baixando imagens =="
docker compose pull

echo "== 5/6 Preparando banco (primeira vez e a cada upgrade) =="
docker compose run --rm rails bundle exec rails db:chatwoot_prepare

echo "== 6/6 Subindo servicos =="
docker compose up -d

echo
echo "Pronto. Acompanhe com: docker compose logs -f rails caddy"
echo "Se o DNS ja aponta para esta VPS, acesse: https://atendimento.freshlogbr.com"
echo "Primeiro acesso: criar a conta 'FreshLog' (vira administrador)."
echo "Depois de criar os usuarios: trocar ENABLE_ACCOUNT_SIGNUP para false no .env e rodar 'docker compose up -d'."
