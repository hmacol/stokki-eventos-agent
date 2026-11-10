#!/usr/bin/env bash
# Homologacao do legado: cria a bancada na VPS (1 vez; idempotente, pode rodar de novo).
# Roda como root, a partir de uma copia scp deste arquivo (o checkout ainda nao existe).
# Uso: criar.sh [ramo]    (padrao: master; o ramo precisa ter homologacao/ e infra/homolog/)
#
# Usuario homolog, clone em /opt/stokki-homolog, firewall do usuario, venv proprio,
# config sem segredos, copia dos dados, units, Caddy com senha. Nao sobe ramo: depois
# rode homologacao/vps/subir.sh <ramo> <quem>. Nunca imprime segredo.
set -euo pipefail

PROD=/opt/stokki-eventos
HML=/opt/stokki-homolog
HOST=homolog.freshhub.com.br
IP_VPS=187.127.52.197
CADDYFILE=/etc/caddy/Caddyfile
ARQ_CADDY=/etc/caddy/legado-homolog.caddy
ARQ_SENHA=/root/homolog-senha-caddy.txt
IMPORT="import $ARQ_CADDY"
RAMO=${1:-master}
G=(git -c safe.directory="$HML" -C "$HML")

[ "$(id -u)" -eq 0 ] || { echo "erro: rode como root" >&2; exit 1; }
[[ "$RAMO" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ ]] || { echo "erro: nome de ramo invalido" >&2; exit 1; }

echo "== conferir o DNS de $HOST"
IPS=$(getent ahostsv4 "$HOST" | awk '{print $1}' | sort -u || true)
if [ "$IPS" != "$IP_VPS" ]; then
	echo "erro: $HOST nao resolve so para $IP_VPS (resolveu: ${IPS:-nada}). Crie o registro A antes." >&2
	exit 1
fi

echo "== conferir ferramentas"
for cmd in git rsync caddy nft openssl curl sudo sha256sum getent; do
	command -v "$cmd" >/dev/null || { echo "erro: falta o comando $cmd" >&2; exit 1; }
done
[ -x /usr/sbin/nft ] || { echo "erro: /usr/sbin/nft nao existe (o homolog-nft.service usa esse caminho)" >&2; exit 1; }

echo "== python 3.11+ para o venv"
if [ -x /usr/bin/python3.11 ]; then
	PYBASE=/usr/bin/python3.11
else
	PYBASE=$(command -v python3 || true)
fi
[ -n "$PYBASE" ] || { echo "erro: nem python3.11 nem python3 encontrados" >&2; exit 1; }
"$PYBASE" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
	|| { echo "erro: $PYBASE e anterior ao 3.11" >&2; exit 1; }
echo "base: $PYBASE ($("$PYBASE" -c 'import platform; print(platform.python_version())')); producao: $("$PROD/venv/bin/python" -c 'import platform; print(platform.python_version())')"

echo "== usuario homolog"
if id -u homolog >/dev/null 2>&1; then
	echo "(ja existe)"
else
	useradd --system --user-group --home "$HML" --shell /usr/sbin/nologin homolog
fi

echo "== clone do repositorio (mesma origem da producao)"
if [ -d "$HML/.git" ]; then
	echo "(ja clonado)"
else
	# a URL pode ter credencial: nunca imprimir
	ORIGEM=$(git -c safe.directory="$PROD" -C "$PROD" remote get-url origin)
	git clone --quiet --no-checkout "$ORIGEM" "$HML"
	unset ORIGEM
fi
"${G[@]}" fetch --quiet --prune origin
"${G[@]}" rev-parse --verify --quiet "origin/$RAMO^{commit}" >/dev/null \
	|| { echo "erro: origin/$RAMO nao existe" >&2; exit 1; }
"${G[@]}" checkout --quiet --detach --force "origin/$RAMO"
for arq in homologacao/site/sitecustomize.py homologacao/gerar_config.py homologacao/vps/copiar_dados.sh \
		infra/homolog/homolog.nft infra/homolog/homolog-nft.service infra/homolog/homolog-painel.service \
		infra/homolog/homolog-portal.service infra/homolog/legado-homolog.caddy; do
	[ -f "$HML/$arq" ] || { echo "erro: origin/$RAMO nao tem $arq" >&2; exit 1; }
done

echo "== firewall do usuario homolog (tabela inet homolog)"
mkdir -p /etc/nftables.d
install -m 644 "$HML/infra/homolog/homolog.nft" /etc/nftables.d/homolog.nft
install -m 644 "$HML/infra/homolog/homolog-nft.service" /etc/systemd/system/homolog-nft.service
systemctl daemon-reload
systemctl enable --quiet homolog-nft.service
systemctl restart homolog-nft.service
nft list table inet homolog >/dev/null

echo "== venv proprio e dependencias (sem playwright install: nenhum browser na homologacao)"
[ -x "$HML/venv/bin/python" ] || "$PYBASE" -m venv "$HML/venv"
"$HML/venv/bin/pip" install --quiet --disable-pip-version-check -r "$HML/requirements.txt"
mkdir -p "$HML/dados"
sha256sum "$HML/requirements.txt" | cut -d' ' -f1 > "$HML/dados/HOMOLOG_REQ_SHA"

echo "== config da homologacao (sem segredos; imprime so nomes de chaves)"
# caminho da conta de servico do GCS na producao (nao e impresso)
GCS_PROD=$("$PROD/venv/bin/python" -I - "$PROD/config.yaml" <<'PY'
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
	[ -f "$GCS_PROD" ] || { echo "erro: arquivo da conta de servico do GCS nao encontrado" >&2; exit 1; }
	install -m 600 -o homolog -g homolog "$GCS_PROD" "$GCS_HML"
else
	echo "(producao sem gcs.credenciais_json)"
fi
unset GCS_PROD
(cd "$HML" && "$HML/venv/bin/python" -E -s -m homologacao.gerar_config \
	--origem "$PROD/config.yaml" --destino "$HML/config.yaml" --caminho-gcs "$GCS_HML")
chown homolog:homolog "$HML/config.yaml"
chmod 600 "$HML/config.yaml"

echo "== dono homolog (750); .git fica so do root (pode ter credencial)"
chown -R homolog:homolog "$HML"
chmod 750 "$HML"
chown -R root:root "$HML/.git"
chmod 700 "$HML/.git"

echo "== copia dos dados"
if [ -f "$HML/dados/dados.db" ]; then
	# rodar o criar de novo nao recopia: recopia so pelo recopiar.sh, com pedido do Hugo
	echo "(dados ja copiados em $(cat "$HML/dados/HOMOLOG_COPIA" 2>/dev/null || echo '?'); sem recopia)"
else
	bash "$HML/homologacao/vps/copiar_dados.sh"
fi

echo "== units homolog-painel e homolog-portal"
install -m 644 "$HML/infra/homolog/homolog-painel.service" /etc/systemd/system/homolog-painel.service
install -m 644 "$HML/infra/homolog/homolog-portal.service" /etc/systemd/system/homolog-portal.service
systemctl daemon-reload
systemctl enable --quiet homolog-painel homolog-portal
systemctl restart homolog-painel homolog-portal

echo "== Caddy: backup do Caddyfile"
BACKUP="$CADDYFILE.bak-$(date +%d-%m)-antes-homolog"
if [ -e "$BACKUP" ]; then
	echo "(backup de hoje ja existe: $BACKUP)"
else
	cp -p "$CADDYFILE" "$BACKUP"
fi
VOLTA_CF=$(mktemp)
VOLTA_LH=$(mktemp)
trap 'rm -f "$VOLTA_CF" "$VOLTA_LH"' EXIT
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
trap desfazer_caddy ERR

echo "== Caddy: $ARQ_CADDY e import no fim do Caddyfile"
install -m 644 "$HML/infra/homolog/legado-homolog.caddy" "$ARQ_CADDY"
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
	bash "$HML/homologacao/vps/trocar_senha.sh"
fi
trap - ERR

echo "== pronto"
echo "senha do Caddy (usuario homolog): em $ARQ_SENHA, ler por SSH, nunca colar no chat"
echo "proximo passo: bash $HML/homologacao/vps/subir.sh <ramo> <quem>"
