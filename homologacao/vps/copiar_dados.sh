#!/usr/bin/env bash
# Homologacao do legado: copia os dados da producao para /opt/stokki-homolog (spec D7).
# Roda como root na VPS. Chamado pelo criar.sh e pelo recopiar.sh; nunca sozinho sem
# pedido do Hugo (substitui o banco da homologacao).
#
# - dados.db pela API de backup do SQLite, como www-data, para um temporario 600
#   (consistente com o WAL, sem cp de arquivo vivo, sem travar a producao);
# - rsync do resto de dados/ e roteirizacao/dados/ SEM banco, logs, sessoes da Stokki,
#   contas de servico, backups e eventos pendentes do freshhub;
# - tudo montado numa pasta NOVA do root e trocado inteiro: o root nunca grava dentro de
#   uma pasta do homolog (links simbolicos plantados la nao levam a lugar nenhum).
set -euo pipefail
. "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/comum.sh"

[ "$(id -u)" -eq 0 ] || erro "rode como root"
id -u homolog >/dev/null 2>&1 || erro "usuario homolog nao existe (rode o criar.sh)"
[ -d "$HML/.git" ] || erro "$HML nao existe (rode o criar.sh)"
exigir_protecoes_kernel
preparar_estado

units_instalados() {
	[ -f /etc/systemd/system/homolog-painel.service ] && [ -f /etc/systemd/system/homolog-portal.service ]
}

NOVO_D=""
NOVO_R=""
TMP=""
limpar() {
	[ -z "$TMP" ] || rm -f -- "$TMP" "$TMP-journal" "$TMP-wal" "$TMP-shm"
	[ -z "$NOVO_D" ] || rm -rf -- "$NOVO_D"
	[ -z "$NOVO_R" ] || rm -rf -- "$NOVO_R"
}
trap limpar EXIT

echo "== parar os servicos da homologacao"
if units_instalados; then
	systemctl stop "${UNITS[@]}"
else
	echo "(units ainda nao instalados)"
fi

echo "== backup do banco da producao (API do SQLite, como www-data)"
TMP=$(sudo -u www-data mktemp /var/tmp/homolog-dados-db.XXXXXX)
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

echo "== montar dados/ e roteirizacao/dados/ novos (pasta do root; sem banco, logs, sessoes, contas de servico, backups)"
EXCLUI=(
	--exclude 'dados.db*'
	--exclude '*.log'
	--exclude 'logs/'
	--exclude 'sessao_*.json'
	--exclude '*service*account*'
	--exclude '*.bak*'
	--exclude 'eventos_freshhub_pendentes.jsonl'
)
NOVO_D=$(mktemp -d "$HML/.dados.novo.XXXXXX")
NOVO_R=$(mktemp -d "$HML/roteirizacao/.dados.novo.XXXXXX")
rsync -a "${EXCLUI[@]}" "$PROD/dados/" "$NOVO_D/"
if [ -d "$PROD/roteirizacao/dados" ]; then
	rsync -a "${EXCLUI[@]}" "$PROD/roteirizacao/dados/" "$NOVO_R/"
else
	echo "(producao sem roteirizacao/dados)"
fi
# banco entregue pelo www-data e .anterior do homolog: COPIA para arquivo novo do root (sem
# seguir link, arquivo comum, 1 link so, dono certo). Na pasta nova so ha arquivo criado pelo
# root, entao o chown -R abaixo nunca entrega um hardlink plantado por outro usuario.
copiar_sem_link "$TMP" "$NOVO_D/dados.db" www-data /var/tmp
rm -f -- "$TMP"
TMP=""
if [ -e "$HML/dados/dados.db.anterior" ] || [ -L "$HML/dados/dados.db.anterior" ]; then
	copiar_sem_link "$HML/dados/dados.db.anterior" "$NOVO_D/dados.db.anterior" homolog "$HML/dados"
fi
chown -R -h homolog:homolog "$NOVO_D" "$NOVO_R"
chmod 750 "$NOVO_D" "$NOVO_R"

echo "== trocar as pastas"
trocar_pasta() {
	local alvo=$1 novo=$2 velho="$1.velho.$$"
	[ ! -e "$velho" ] && [ ! -L "$velho" ] || erro "$velho ja existe"
	if [ -e "$alvo" ] || [ -L "$alvo" ]; then
		mv -T -- "$alvo" "$velho"
	fi
	mv -T -- "$novo" "$alvo"
	rm -rf -- "$velho"
}
trocar_pasta "$HML/dados" "$NOVO_D"
NOVO_D=""
trocar_pasta "$HML/roteirizacao/dados" "$NOVO_R"
NOVO_R=""

echo "== data da copia (estado do root)"
gravar_estado HOMOLOG_COPIA "$(date '+%d/%m/%Y %H:%M')"

echo "== subir os servicos da homologacao"
if units_instalados; then
	systemctl start "${UNITS[@]}"
else
	echo "(units ainda nao instalados; o criar.sh sobe depois)"
fi

echo "== copia concluida: $(cat "$ESTADO/HOMOLOG_COPIA")"
