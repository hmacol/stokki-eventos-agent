#!/usr/bin/env bash
# Homologacao do legado: recopia os dados da producao (spec D7). Roda como root na VPS.
# SO com pedido explicito do Hugo: exige --confirmo-recopia. Guarda o banco atual da
# homologacao em dados/dados.db.anterior (uma geracao so) e chama o copiar_dados.sh.
set -euo pipefail

if [ "${1:-}" != "--confirmo-recopia" ]; then
	echo "recusado: a recopia substitui o banco da homologacao; rode com --confirmo-recopia (so com pedido do Hugo)" >&2
	exit 2
fi
. "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/comum.sh"
[ "$(id -u)" -eq 0 ] || erro "rode como root"

echo "== parar os servicos da homologacao"
systemctl stop "${UNITS[@]}"

echo "== guardar o banco atual em dados.db.anterior (como homolog, dentro da pasta dele)"
if [ -f "$HML/dados/dados.db" ]; then
	# API de backup pega o que estiver no WAL; roda como homolog: o root nao grava em dados/
	(cd / && sudo -u homolog "$PY_SISTEMA" -I - "$HML/dados/dados.db" "$HML/dados/dados.db.anterior.novo" <<'PY'
import sqlite3
import sys

origem = sqlite3.connect(sys.argv[1])
destino = sqlite3.connect(sys.argv[2])
origem.backup(destino)
destino.close()
origem.close()
PY
)
	(cd / && sudo -u homolog mv -f -T -- "$HML/dados/dados.db.anterior.novo" "$HML/dados/dados.db.anterior")
else
	echo "(sem banco atual)"
fi

rodar_script copiar_dados.sh
