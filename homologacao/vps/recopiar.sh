#!/usr/bin/env bash
# Homologacao do legado: recopia os dados da producao (spec D7). Roda como root na VPS.
# SO com pedido explicito do Hugo: exige --confirmo-recopia. Guarda o banco atual da
# homologacao em dados/dados.db.anterior (uma geracao so) e chama o copiar_dados.sh.
set -euo pipefail

HML=/opt/stokki-homolog
DB="$HML/dados/dados.db"

if [ "${1:-}" != "--confirmo-recopia" ]; then
	echo "recusado: a recopia sobrescreve o banco da homologacao; rode com --confirmo-recopia (so com pedido do Hugo)" >&2
	exit 2
fi
[ "$(id -u)" -eq 0 ] || { echo "erro: rode como root" >&2; exit 1; }

echo "== parar os servicos da homologacao"
systemctl stop homolog-painel homolog-portal

echo "== guardar o banco atual em dados.db.anterior"
if [ -f "$DB" ]; then
	# pela API de backup, como homolog (pega o que estiver no WAL; nao cria arquivo de root)
	(cd / && sudo -u homolog "$HML/venv/bin/python" -I - "$DB" "$DB.anterior.novo" <<'PY'
import sqlite3
import sys

origem = sqlite3.connect(sys.argv[1])
destino = sqlite3.connect(sys.argv[2])
origem.backup(destino)
destino.close()
origem.close()
PY
)
	mv -f "$DB.anterior.novo" "$DB.anterior"
else
	echo "(sem banco atual)"
fi

bash "$HML/homologacao/vps/copiar_dados.sh"
