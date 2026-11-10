#!/usr/bin/env bash
# Homologacao do legado: registra o OK do Hugo para o ramo que esta na bancada (spec D8).
# Roda como root na VPS. Acrescenta "OK <ramo> <commit da ponta> <quando>" no HOMOLOG_HISTORICO;
# o deploy-vps confere a ancestralidade dos commits contra essas linhas.
set -euo pipefail

HML=/opt/stokki-homolog
PY="$HML/venv/bin/python"
VERSAO="$HML/dados/HOMOLOG_VERSAO"
HISTORICO="$HML/dados/HOMOLOG_HISTORICO"

[ "$(id -u)" -eq 0 ] || { echo "erro: rode como root" >&2; exit 1; }
[ -s "$VERSAO" ] || { echo "erro: nenhum ramo subido na homologacao ($VERSAO vazio)" >&2; exit 1; }

echo "== ler a versao no ar"
read -r RAMO COMMIT < <("$PY" -I -c 'import json, sys; d = json.load(open(sys.argv[1], encoding="utf-8")); print(d["ramo"], d["commit"])' "$VERSAO")
[ -n "${RAMO:-}" ] && [ -n "${COMMIT:-}" ] || { echo "erro: $VERSAO sem ramo/commit" >&2; exit 1; }

echo "== registrar o OK"
QUANDO=$(date '+%d/%m/%Y %H:%M')
echo "OK $RAMO $COMMIT $QUANDO" >> "$HISTORICO"
chown homolog:homolog "$HISTORICO"
echo "OK $RAMO $COMMIT $QUANDO"
