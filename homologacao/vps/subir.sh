#!/usr/bin/env bash
# Homologacao do legado: sobe um ramo para a bancada (spec D8). Roda como root na VPS.
# Uso: subir.sh <ramo> <quem> [--trocar]
#
# Saidas: 0 ok; 1 erro; 3 bancada ocupada por outro ramo (o vps_ops pergunta e chama de
# novo com --trocar); 4 conflito ao mesclar origin/master; 5 checkout sem a guarda;
# 6 servico nao subiu ou saude sem "guarda": "ativa" (VERSAO/historico NAO mudam).
# Nunca e pushado: o merge com origin/master existe so neste checkout.
set -euo pipefail
. "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/comum.sh"

RAMO=${1:-}
QUEM=${2:-}
TROCAR=${3:-}
if [ -z "$RAMO" ] || [ -z "$QUEM" ]; then
	echo "uso: subir.sh <ramo> <quem> [--trocar]" >&2
	exit 1
fi
[[ "$RAMO" =~ $RE_RAMO ]] || erro "nome de ramo invalido"
[[ "$QUEM" =~ ^[A-Za-z0-9][A-Za-z0-9._@-]*$ ]] || erro "<quem> so com letras, numeros, . _ @ -"
[ -z "$TROCAR" ] || [ "$TROCAR" = "--trocar" ] || erro "terceiro argumento so pode ser --trocar"
[ "$(id -u)" -eq 0 ] || erro "rode como root"
[ -d "$HML/.git" ] && [ -x "$HML/venv/bin/pip" ] || erro "bancada nao criada (rode o criar.sh)"
preparar_estado
VERSAO="$ESTADO/HOMOLOG_VERSAO"
REQ_SHA="$ESTADO/HOMOLOG_REQ_SHA"

echo "== bancada"
if [ -s "$VERSAO" ]; then
	exigir_de_root "$VERSAO"
	RAMO_ATUAL=$(campo_versao ramo)
	if [ "$RAMO_ATUAL" != "$RAMO" ] && [ "$TROCAR" != "--trocar" ]; then
		echo "bancada ocupada: ramo $RAMO_ATUAL desde $(campo_versao quando), subido por $(campo_versao quem)"
		exit 3
	fi
	echo "(antes: $RAMO_ATUAL)"
else
	echo "(vazia)"
fi

echo "== buscar origin"
"${G[@]}" fetch --quiet --prune origin
"${G[@]}" rev-parse --verify --quiet "origin/$RAMO^{commit}" >/dev/null \
	|| erro "origin/$RAMO nao existe (o ramo precisa estar pushado)"
COMMIT=$("${G[@]}" rev-parse "origin/$RAMO")
MASTER=$("${G[@]}" rev-parse origin/master)
[[ "$COMMIT" =~ $RE_COMMIT ]] && [[ "$MASTER" =~ $RE_COMMIT ]] || erro "rev-parse inesperado"

echo "== checkout de origin/$RAMO (${COMMIT:0:7})"
"${G[@]}" checkout --quiet --detach --force "origin/$RAMO"

echo "== mesclar origin/master (${MASTER:0:7}) so neste checkout"
if ! "${G[@]}" -c user.name=homolog -c user.email=homolog@localhost merge --quiet --no-edit origin/master; then
	"${G[@]}" merge --abort || true
	echo "erro: conflito com origin/master; atualize o ramo $RAMO e suba de novo" >&2
	exit 4
fi

echo "== donos: codigo e venv do root, pastas de dados do homolog"
normalizar_donos

echo "== conferir a guarda no checkout"
[ -f "$HML/homologacao/site/sitecustomize.py" ] \
	|| { echo "erro: checkout sem homologacao/site/sitecustomize.py; servicos nao reiniciados" >&2; exit 5; }

echo "== dependencias (venv do root)"
SHA_NOVO=$(sha256sum "$HML/requirements.txt" | cut -d' ' -f1)
if [ -f "$REQ_SHA" ] && [ "$(cat "$REQ_SHA")" = "$SHA_NOVO" ]; then
	echo "(requirements.txt igual, sem pip)"
else
	"$HML/venv/bin/pip" install --quiet --disable-pip-version-check -r "$HML/requirements.txt"
	find "$HML/venv" -xdev ! -type l -exec chmod go-w {} +
	gravar_estado HOMOLOG_REQ_SHA "$SHA_NOVO"
fi

echo "== esvaziar eventos pendentes do freshhub (como homolog)"
(cd / && sudo -u homolog truncate -s 0 -- "$HML/dados/eventos_freshhub_pendentes.jsonl")

echo "== reiniciar ${UNITS[*]}"
if ! systemctl restart "${UNITS[@]}"; then
	echo "erro: servico nao subiu (ExecStartPre da guarda?). Ver: journalctl -u homolog-painel -u homolog-portal -n 50" >&2
	exit 6
fi

echo "== provar a guarda (saude direto no waitress)"
for porta in 8171 8174; do
	RESPOSTA=""
	for _ in $(seq 1 30); do
		if RESPOSTA=$(curl -fsS --max-time 5 "http://127.0.0.1:$porta/homologacao/saude" 2>/dev/null); then
			break
		fi
		RESPOSTA=""
		sleep 2
	done
	if [ -z "$RESPOSTA" ]; then
		echo "erro: 127.0.0.1:$porta/homologacao/saude nao respondeu em 60 s" >&2
		exit 6
	fi
	echo "$porta: $RESPOSTA"
	if ! printf '%s' "$RESPOSTA" | "$PY_SISTEMA" -I -c 'import json, sys; sys.exit(0 if json.load(sys.stdin).get("guarda") == "ativa" else 1)'; then
		echo "erro: porta $porta sem \"guarda\": \"ativa\"" >&2
		exit 6
	fi
done

echo "== registrar versao e historico (so depois da saude ok)"
QUANDO=$(date '+%d/%m/%Y %H:%M')
JSON=$("$PY_SISTEMA" -I -c 'import json, sys
chaves = ("ramo", "commit", "master", "quando", "quem")
print(json.dumps(dict(zip(chaves, sys.argv[1:])), ensure_ascii=False))' "$RAMO" "$COMMIT" "$MASTER" "$QUANDO" "$QUEM")
gravar_estado HOMOLOG_VERSAO "$JSON"
acrescentar_estado HOMOLOG_HISTORICO "SUBIU $RAMO $COMMIT $QUANDO $QUEM"

echo "== endereco publico (espera 401: senha do Caddy)"
echo "HTTP $(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 https://homolog.freshhub.com.br/painel/ || true)"

echo "== no ar: $RAMO @ ${COMMIT:0:7} (+ master ${MASTER:0:7})"
echo "painel: https://homolog.freshhub.com.br/painel/"
echo "portal: https://homolog.freshhub.com.br/cliente/"
