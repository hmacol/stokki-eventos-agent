#!/usr/bin/env bash
# Homologacao do legado: senha nova do Caddy (usuario homolog) em homolog.freshhub.com.br.
# Roda como root na VPS. A senha vai SO para /root/homolog-senha-caddy.txt (600);
# este script nunca a imprime. No .caddy fica so o hash bcrypt.
set -euo pipefail

CADDYFILE=/etc/caddy/Caddyfile
ARQ_CADDY=/etc/caddy/legado-homolog.caddy
ARQ_SENHA=/root/homolog-senha-caddy.txt

[ "$(id -u)" -eq 0 ] || { echo "erro: rode como root" >&2; exit 1; }
[ -f "$ARQ_CADDY" ] || { echo "erro: $ARQ_CADDY nao existe (rode o criar.sh)" >&2; exit 1; }

echo "== gerar senha nova"
SENHA=$(openssl rand -base64 15)
[ "${#SENHA}" -eq 20 ] || { echo "erro: senha gerada com tamanho inesperado" >&2; exit 1; }

echo "== hash bcrypt (caddy hash-password, senha pelo stdin)"
HASH=$(printf '%s\n' "$SENHA" | caddy hash-password)
case "$HASH" in
	'$2'*) ;;
	*) echo "erro: caddy hash-password nao devolveu um hash bcrypt" >&2; exit 1 ;;
esac

echo "== gravar o hash em $ARQ_CADDY e validar o Caddyfile"
VOLTA=$(mktemp)
trap 'rm -f "$VOLTA"' EXIT
cp -p "$ARQ_CADDY" "$VOLTA"
sed -i "s|^\([[:space:]]*homolog[[:space:]]\{1,\}\).*\$|\1$HASH|" "$ARQ_CADDY"
if ! grep -qF "homolog $HASH" "$ARQ_CADDY" || ! caddy validate --config "$CADDYFILE"; then
	cp -p "$VOLTA" "$ARQ_CADDY"
	echo "erro: hash nao entrou ou Caddyfile invalido; $ARQ_CADDY restaurado, senha antiga segue valendo" >&2
	exit 1
fi

echo "== gravar a senha em $ARQ_SENHA (600)"
(umask 077 && printf '%s\n' "$SENHA" > "$ARQ_SENHA")
chmod 600 "$ARQ_SENHA"

echo "== recarregar o Caddy"
if ! systemctl reload caddy; then
	echo "erro: reload do Caddy falhou; a senha nova ja esta em $ARQ_SENHA. Rode de novo." >&2
	exit 1
fi

echo "== senha trocada; esta em $ARQ_SENHA (ler por SSH, nunca colar no chat)"
