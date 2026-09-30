#!/usr/bin/env bash
# Instalacao da Evolution API na VPS (Ubuntu 22.04/24.04) — FreshLog
# (substitui o Chatwoot, 26/08)
#
# Essa VPS ja roda Caddy NATIVO (systemd) na frente de outros servicos —
# este script sobe so os containers da Evolution API (evolution/postgres/
# redis), publicando a API em 127.0.0.1:8080. Diferente do Chatwoot, essa
# porta NUNCA e exposta pelo Caddy -- so o app Python novo de atendimento
# (infra/atendimento-central.service, na raiz do repo) fala com ela, por
# loopback. O bloco de Caddy (Caddyfile-atendimento) aponta pro app Python,
# nao pra Evolution API.
#
# Uso (como root, na VPS):
#   1. Copiar esta pasta para a VPS:  scp -r infra/ root@IP_DA_VPS:/opt/stokki-eventos/atendimento/infra
#   2. ssh root@IP_DA_VPS
#   3. cd /opt/stokki-eventos/atendimento/infra && bash instalar_vps.sh
#
# O script e idempotente: pode rodar de novo se algo falhar no meio.
# Obs.: se os arquivos vieram do Windows, o proprio script corrige CRLF no .env.

set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

echo "== 1/5 Docker =="
if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sh
fi
docker compose version >/dev/null

echo "== 2/5 Firewall (ufw) =="
if command -v ufw >/dev/null 2>&1; then
  ufw allow 22/tcp >/dev/null
  ufw allow 80/tcp >/dev/null
  ufw allow 443/tcp >/dev/null
  ufw --force enable >/dev/null
  echo "ufw ativo: portas 22, 80 e 443 liberadas"
fi

echo "== 3/5 Arquivo .env =="
if [ ! -f .env ]; then
  cp env.exemplo .env
  # normaliza fins de linha caso os arquivos tenham vindo do Windows
  sed -i 's/\r$//' .env docker-compose.yml || true
  # gera segredos
  sed -i "s|^AUTHENTICATION_API_KEY=.*|AUTHENTICATION_API_KEY=$(openssl rand -hex 32)|" .env
  senha_postgres="$(openssl rand -hex 24)"
  sed -i "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=${senha_postgres}|" .env
  sed -i "s|GERAR_SENHA_FORTE@postgres|${senha_postgres}@postgres|" .env
  senha_redis="$(openssl rand -hex 24)"
  sed -i "s|^REDIS_PASSWORD=.*|REDIS_PASSWORD=${senha_redis}|" .env
  sed -i "s|GERAR_SENHA_FORTE@redis|${senha_redis}@redis|" .env
  segredo_webhook="$(openssl rand -hex 24)"
  sed -i "s|GERAR_SEGREDO_ALEATORIO|${segredo_webhook}|" .env
  echo ".env criado com segredos gerados."
  echo "IMPORTANTE: copie AUTHENTICATION_API_KEY e o segredo do WEBHOOK_GLOBAL_URL"
  echo "deste .env para config.yaml (evolution_api.api_key e evolution_api.webhook_secret)."
  echo "Edite/confira com: nano $DIR/.env   — depois rode este script de novo."
  exit 0
else
  echo ".env ja existe, mantendo."
fi

echo "== 4/5 Baixando imagens =="
docker compose pull

echo "== 5/5 Subindo servicos =="
docker compose up -d

echo
echo "Pronto. Acompanhe com: docker compose logs -f evolution"
echo "Confira se o bloco de Caddyfile-atendimento no /etc/caddy/Caddyfile nativo"
echo "da VPS ja aponta pro app Python novo (porta do config.yaml -> atendimento.porta),"
echo "nao mais pro Chatwoot."
echo "Depois de subir o app Python (ver ../../infra/atendimento-central.service),"
echo "acesse https://atendimento.freshhub.com.br -> WhatsApp -> Gerar (QR ou codigo)"
echo "para parear o numero."
