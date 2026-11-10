#!/usr/bin/env bash
# Homologacao do legado: copia os dados da producao para /opt/stokki-homolog (spec D7).
# Roda como root na VPS. Chamado pelo criar.sh e pelo recopiar.sh; nunca sozinho sem
# pedido do Hugo (sobrescreve o banco da homologacao).
#
# - dados.db pela API de backup do SQLite, como www-data, para um temporario 600
#   (consistente com o WAL, sem cp de arquivo vivo, sem travar a producao);
# - rsync do resto de dados/ e roteirizacao/dados/ SEM banco, logs, sessoes da Stokki,
#   contas de servico, backups e eventos pendentes do freshhub.
set -euo pipefail

PROD=/opt/stokki-eventos
HML=/opt/stokki-homolog
UNITS=(homolog-painel homolog-portal)

[ "$(id -u)" -eq 0 ] || { echo "erro: rode como root" >&2; exit 1; }
id -u homolog >/dev/null 2>&1 || { echo "erro: usuario homolog nao existe (rode o criar.sh)" >&2; exit 1; }
[ -d "$HML" ] || { echo "erro: $HML nao existe (rode o criar.sh)" >&2; exit 1; }

units_instalados() {
	[ -f /etc/systemd/system/homolog-painel.service ] && [ -f /etc/systemd/system/homolog-portal.service ]
}

echo "== parar os servicos da homologacao"
if units_instalados; then
	systemctl stop "${UNITS[@]}"
else
	echo "(units ainda nao instalados)"
fi

echo "== backup do banco da producao (API do SQLite, como www-data)"
TMP=$(sudo -u www-data mktemp /var/tmp/homolog-dados-db.XXXXXX)
trap 'rm -f "$TMP" "$TMP-journal" "$TMP-wal" "$TMP-shm"' EXIT
(cd / && sudo -u www-data "$PROD/venv/bin/python" -I - "$PROD/dados/dados.db" "$TMP" <<'PY'
import sqlite3
import sys

origem = sqlite3.connect("file:%s?mode=ro" % sys.argv[1], uri=True)
destino = sqlite3.connect(sys.argv[2])
origem.backup(destino)
origem.close()
ok = destino.execute("pragma quick_check").fetchone()[0] == "ok"
destino.close()
if not ok:
    print("erro: quick_check da copia falhou", file=sys.stderr)
sys.exit(0 if ok else 1)
PY
)

echo "== copiar dados/ e roteirizacao/dados/ (sem banco, logs, sessoes, contas de servico, backups)"
# Excluir tambem protege do --delete o que e so da homologacao (banco, .anterior, HOMOLOG_*).
EXCLUI=(
	--exclude 'dados.db*'
	--exclude '*.log'
	--exclude 'logs/'
	--exclude 'sessao_*.json'
	--exclude '*service*account*'
	--exclude '*.bak*'
	--exclude 'eventos_freshhub_pendentes.jsonl'
	--exclude 'HOMOLOG_*'
)
mkdir -p "$HML/dados" "$HML/roteirizacao/dados"
rsync -a --delete "${EXCLUI[@]}" "$PROD/dados/" "$HML/dados/"
if [ -d "$PROD/roteirizacao/dados" ]; then
	rsync -a --delete "${EXCLUI[@]}" "$PROD/roteirizacao/dados/" "$HML/roteirizacao/dados/"
else
	echo "(producao sem roteirizacao/dados)"
fi

echo "== instalar o banco copiado"
# WAL/SHM da copia anterior aplicados no banco novo o corromperiam
rm -f "$HML/dados/dados.db-wal" "$HML/dados/dados.db-shm" "$HML/dados/dados.db-journal"
mv "$TMP" "$HML/dados/dados.db"

echo "== data da copia e dono homolog"
date '+%d/%m/%Y %H:%M' > "$HML/dados/HOMOLOG_COPIA"
chown -R homolog:homolog "$HML/dados" "$HML/roteirizacao/dados"
chmod 750 "$HML/dados"
chmod 600 "$HML/dados/dados.db"

echo "== subir os servicos da homologacao"
if units_instalados; then
	systemctl start "${UNITS[@]}"
else
	echo "(units ainda nao instalados; o criar.sh sobe depois)"
fi

echo "== copia concluida: $(cat "$HML/dados/HOMOLOG_COPIA")"
