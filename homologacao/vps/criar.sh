#!/usr/bin/env bash
# Homologacao do legado: cria a bancada na VPS (1 vez; idempotente, pode rodar de novo).
# Roda como root, a partir de uma copia scp deste arquivo (o checkout ainda nao existe).
# Uso: criar.sh [ramo]    (padrao: master; o ramo precisa ter homologacao/ e infra/homolog/)
#
# Usuario homolog, clone em /opt/stokki-homolog, firewall do usuario, venv proprio,
# config sem segredos, copia dos dados, units, Caddy com senha. Nao sobe ramo: depois
# rode homologacao/vps/subir.sh <ramo> <quem>. Nunca imprime segredo.
# Fronteira de privilegio (ver comum.sh): codigo, venv e scripts do root; so as pastas de
# dados, o config e a conta de servico sao do homolog; estado em /var/lib/stokki-homolog.
set -euo pipefail
umask 022

PROD=/opt/stokki-eventos
HML=/opt/stokki-homolog
HOST=homolog.freshhub.com.br
IP_VPS=187.127.52.197
CADDYFILE=/etc/caddy/Caddyfile
ARQ_CADDY=/etc/caddy/legado-homolog.caddy
ARQ_SENHA=/root/homolog-senha-caddy.txt
IMPORT="import $ARQ_CADDY"
RAMO=${1:-master}

falha() {
	echo "erro: $*" >&2
	exit 1
}

[ "$(id -u)" -eq 0 ] || falha "rode como root"
[[ "$RAMO" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ ]] || falha "nome de ramo invalido"

echo "== conferir o DNS de $HOST"
IPS=$(getent ahostsv4 "$HOST" | awk '{print $1}' | sort -u || true)
[ "$IPS" = "$IP_VPS" ] || falha "$HOST nao resolve so para $IP_VPS (resolveu: ${IPS:-nada}). Crie o registro A antes."

echo "== conferir ferramentas"
for cmd in git rsync caddy nft openssl curl sudo sha256sum getent stat find install truncate; do
	command -v "$cmd" >/dev/null || falha "falta o comando $cmd"
done
[ -x /usr/sbin/nft ] || falha "/usr/sbin/nft nao existe (o homolog-nft.service usa esse caminho)"
[ -x /usr/bin/python3 ] || falha "/usr/bin/python3 nao existe (ajudantes do root usam o python do sistema)"

echo "== conferir protecoes do kernel (hardlink/symlink de outro usuario)"
for chave in fs.protected_hardlinks fs.protected_symlinks; do
	[ "$(sysctl -n "$chave" 2>/dev/null || true)" = 1 ] || falha "$chave precisa ser 1"
done

echo "== python 3.11+ para o venv"
if [ -x /usr/bin/python3.11 ]; then
	PYBASE=/usr/bin/python3.11
else
	PYBASE=/usr/bin/python3
fi
"$PYBASE" -I -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
	|| falha "$PYBASE e anterior ao 3.11"
echo "base: $PYBASE ($("$PYBASE" -I -c 'import platform; print(platform.python_version())'))"

echo "== usuario homolog"
if id -u homolog >/dev/null 2>&1; then
	echo "(ja existe)"
else
	useradd --system --user-group --home "$HML" --shell /usr/sbin/nologin homolog
fi

echo "== clone do repositorio (mesma origem da producao; tudo do root)"
G=(git -c safe.directory="$HML" -C "$HML")
if [ -d "$HML/.git" ]; then
	echo "(ja clonado)"
else
	[ ! -e "$HML" ] || falha "$HML existe e nao e um clone; confira a mao"
	# a URL pode ter credencial: nunca imprimir
	ORIGEM=$(git -c safe.directory="$PROD" -C "$PROD" remote get-url origin)
	git clone --quiet --no-checkout "$ORIGEM" "$HML"
	unset ORIGEM
fi
[ "$(stat -c %u "$HML")" = 0 ] && [ "$(stat -c %u "$HML/.git")" = 0 ] \
	|| falha "$HML ou $HML/.git nao e do root (layout antigo?); confira a mao"
"${G[@]}" fetch --quiet --prune origin
"${G[@]}" rev-parse --verify --quiet "origin/$RAMO^{commit}" >/dev/null || falha "origin/$RAMO nao existe"
"${G[@]}" checkout --quiet --detach --force "origin/$RAMO"
for arq in homologacao/vps/comum.sh homologacao/site/sitecustomize.py homologacao/gerar_config.py \
		homologacao/vps/copiar_dados.sh homologacao/vps/trocar_senha.sh infra/homolog/homolog.nft \
		infra/homolog/homolog-nft.service infra/homolog/homolog-painel.service \
		infra/homolog/homolog-portal.service infra/homolog/legado-homolog.caddy; do
	[ -f "$HML/$arq" ] || falha "origin/$RAMO nao tem $arq"
done

echo "== donos: codigo do root, pastas de dados do homolog; estado em /var/lib/stokki-homolog"
# antes de carregar o comum.sh: ele e as pastas acima dele sao do root e sem escrita para grupo/outros
for alvo in "$HML" "$HML/homologacao" "$HML/homologacao/vps" "$HML/homologacao/vps/comum.sh"; do
	[ ! -L "$alvo" ] || falha "$alvo e link simbolico"
	read -r dono modo < <(stat -c '%u %a' -- "$alvo") || true
	[ "${dono:-}" = 0 ] && (( (8#${modo:-777} & 8#022) == 0 )) || falha "$alvo nao e do root ou e gravavel por grupo/outros"
done
. "$HML/homologacao/vps/comum.sh"
normalizar_donos
preparar_estado

echo "== firewall do usuario homolog (tabela inet homolog)"
mkdir -p /etc/nftables.d
install -m 644 -o root -g root "$HML/infra/homolog/homolog.nft" /etc/nftables.d/homolog.nft
install -m 644 -o root -g root "$HML/infra/homolog/homolog-nft.service" /etc/systemd/system/homolog-nft.service
systemctl daemon-reload
systemctl enable --quiet homolog-nft.service
systemctl restart homolog-nft.service
nft list table inet homolog >/dev/null

echo "== venv proprio, do root (sem playwright install: nenhum browser na homologacao)"
[ -x "$HML/venv/bin/python" ] || "$PYBASE" -m venv "$HML/venv"
"$HML/venv/bin/pip" install --quiet --disable-pip-version-check -r "$HML/requirements.txt"
normalizar_donos
gravar_estado HOMOLOG_REQ_SHA "$(sha256sum "$HML/requirements.txt" | cut -d' ' -f1)"

echo "== config da homologacao (sem segredos; imprime so nomes de chaves)"
# python do venv de PRODUCAO, isolado (-I -B), rodando o arquivo do checkout do root.
# O config e a conta de servico da producao (pasta do www-data) sao COPIADOS sem seguir link
# para uma pasta temporaria do root; ninguem le direto o caminho que o www-data controla.
exigir_de_root "$HML/homologacao" "$HML/homologacao/gerar_config.py"
TMPD=$(mktemp -d)
trap 'rm -rf "$TMPD"' EXIT
copiar_sem_link "$PROD/config.yaml" "$TMPD/prod.yaml" www-data "$PROD"
GCS_PROD=$(cd / && "$PROD/venv/bin/python" -I -B - "$TMPD/prod.yaml" <<'PY'
import sys
import yaml

with open(sys.argv[1], encoding="utf-8") as f:
    cfg = yaml.safe_load(f) or {}
gcs = cfg.get("gcs") if isinstance(cfg.get("gcs"), dict) else {}
print(gcs.get("credenciais_json") or "")
PY
)
GCS_HML="$HML/gcs-service-account.json"
if [ -n "$GCS_PROD" ]; then
	case "$GCS_PROD" in /*) ;; *) GCS_PROD="$PROD/$GCS_PROD" ;; esac
	GCS_REAL=$(readlink -f -- "$GCS_PROD" || true)
	case "$GCS_REAL" in
		"$PROD"/*) ;;
		*) falha "conta de servico do GCS fora de $PROD ou inexistente" ;;
	esac
	# readlink -f acima so da erro amigavel; quem garante e a conferencia do descritor aberto
	copiar_sem_link "$GCS_REAL" "$TMPD/gcs.json" www-data "$PROD"
	install -m 600 -o homolog -g homolog "$TMPD/gcs.json" "$GCS_HML"
	unset GCS_REAL
else
	echo "(producao sem gcs.credenciais_json)"
fi
unset GCS_PROD
(cd / && "$PROD/venv/bin/python" -I -B "$HML/homologacao/gerar_config.py" \
	--origem "$TMPD/prod.yaml" --destino "$TMPD/homolog.yaml" --caminho-gcs "$GCS_HML")
install -m 600 -o homolog -g homolog "$TMPD/homolog.yaml" "$HML/config.yaml"
rm -rf "$TMPD"

echo "== copia dos dados"
if [ -f "$HML/dados/dados.db" ]; then
	# rodar o criar de novo nao recopia: recopia so pelo recopiar.sh, com pedido do Hugo
	echo "(dados ja copiados em $(cat "$ESTADO/HOMOLOG_COPIA" 2>/dev/null || echo '?'); sem recopia)"
else
	rodar_script copiar_dados.sh
fi

echo "== units homolog-painel e homolog-portal"
install -m 644 -o root -g root "$HML/infra/homolog/homolog-painel.service" /etc/systemd/system/homolog-painel.service
install -m 644 -o root -g root "$HML/infra/homolog/homolog-portal.service" /etc/systemd/system/homolog-portal.service
systemctl daemon-reload
systemctl enable --quiet "${UNITS[@]}"
systemctl restart "${UNITS[@]}"

echo "== Caddy: backup do Caddyfile"
BACKUP="$CADDYFILE.bak-$(date +%d-%m)-antes-homolog"
if [ -e "$BACKUP" ]; then
	echo "(backup de hoje ja existe: $BACKUP)"
else
	cp -p "$CADDYFILE" "$BACKUP"
fi
VOLTA_CF=$(mktemp)
VOLTA_LH=$(mktemp)
cp -p "$CADDYFILE" "$VOLTA_CF"
TINHA_LH=0
HASH_ATUAL=""
if [ -f "$ARQ_CADDY" ]; then
	TINHA_LH=1
	cp -p "$ARQ_CADDY" "$VOLTA_LH"
	HASH_ATUAL=$(sed -n 's/^[[:space:]]*homolog[[:space:]]\{1,\}\(\$2[^[:space:]]*\).*$/\1/p' "$ARQ_CADDY" | head -n 1)
fi
desfazer_caddy() {
	cp -p "$VOLTA_CF" "$CADDYFILE"
	if [ "$TINHA_LH" -eq 1 ]; then cp -p "$VOLTA_LH" "$ARQ_CADDY"; else rm -f "$ARQ_CADDY"; fi
	echo "erro: passo do Caddy falhou; Caddyfile e $ARQ_CADDY restaurados (Caddy nao recarregado)" >&2
}
# qualquer saida antes do fim do passo (erro, exit de funcao, sinal) desfaz o Caddy
CADDY_OK=0
trap 'if [ "$CADDY_OK" = 0 ]; then desfazer_caddy; fi; rm -f "$VOLTA_CF" "$VOLTA_LH"' EXIT

echo "== Caddy: $ARQ_CADDY e import no fim do Caddyfile"
install -m 644 -o root -g root "$HML/infra/homolog/legado-homolog.caddy" "$ARQ_CADDY"
if grep -qE "^[[:space:]]*import[[:space:]]+$ARQ_CADDY[[:space:]]*$" "$CADDYFILE"; then
	echo "(import ja existe)"
else
	printf '\n%s\n' "$IMPORT" >> "$CADDYFILE"
fi

if [ -n "$HASH_ATUAL" ] && [ -s "$ARQ_SENHA" ]; then
	echo "== Caddy: mantem a senha atual ($ARQ_SENHA)"
	sed -i "s|^\([[:space:]]*homolog[[:space:]]\{1,\}\).*\$|\1$HASH_ATUAL|" "$ARQ_CADDY"
	grep -qF "homolog $HASH_ATUAL" "$ARQ_CADDY"
	caddy validate --config "$CADDYFILE"
	systemctl reload caddy
else
	echo "== Caddy: senha nova (trocar_senha.sh)"
	rodar_script trocar_senha.sh
fi
CADDY_OK=1

echo "== pronto"
echo "senha do Caddy (usuario homolog): em $ARQ_SENHA, ler por SSH, nunca colar no chat"
echo "proximo passo: bash $HML/homologacao/vps/subir.sh <ramo> <quem>"
