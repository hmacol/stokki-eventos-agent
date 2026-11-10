#!/usr/bin/env bash
# Homologacao do legado: registra o OK do Hugo para o ramo que esta na bancada (spec D8).
# Roda como root na VPS. Acrescenta "OK <ramo> <commit da ponta> <quando>" no HOMOLOG_HISTORICO
# (estado do root, /var/lib/stokki-homolog); o deploy-vps confere a ancestralidade dos commits
# contra essas linhas.
set -euo pipefail
. "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/comum.sh"

[ "$(id -u)" -eq 0 ] || erro "rode como root"
VERSAO="$ESTADO/HOMOLOG_VERSAO"
HISTORICO="$ESTADO/HOMOLOG_HISTORICO"
exigir_de_root "$ESTADO"
[ -s "$VERSAO" ] || erro "nenhum ramo subido na homologacao ($VERSAO vazio)"
exigir_de_root "$VERSAO" "$HISTORICO"
# subida pela metade (saida 4/5/6 ou erro): checkout/servicos podem nao ser o que a VERSAO diz
[ ! -e "$ESTADO/EM_TROCA" ] && [ ! -L "$ESTADO/EM_TROCA" ] \
	|| erro "subida em andamento ou que falhou ($ESTADO/EM_TROCA); suba de novo antes do OK"

echo "== ler a versao no ar"
RAMO=$(campo_versao ramo)
COMMIT=$(campo_versao commit)
CHECKOUT=$(campo_versao checkout)
[[ "$RAMO" =~ $RE_RAMO ]] || erro "ramo invalido em $VERSAO"
[[ "$COMMIT" =~ $RE_COMMIT ]] || erro "commit invalido em $VERSAO"
[[ "$CHECKOUT" =~ $RE_COMMIT ]] || erro "checkout invalido em $VERSAO (subida antiga? suba de novo)"
"${G[@]}" cat-file -e "$COMMIT^{commit}" 2>/dev/null || erro "commit $COMMIT nao existe no checkout"

# O subir.sh grava em "checkout" o HEAD depois da mescla com origin/master (merge, fast-forward
# ou ja atualizado). O checkout tem que estar exatamente nele e conter a ponta do ramo.
echo "== conferir o checkout"
[ "$("${G[@]}" rev-parse HEAD)" = "$CHECKOUT" ] || erro "HEAD do checkout nao e o da VERSAO; suba de novo"
"${G[@]}" merge-base --is-ancestor "$COMMIT" HEAD || erro "checkout nao contem $RAMO ${COMMIT:0:7}"

echo "== conferir o ultimo SUBIU"
ULTIMO=$(grep '^SUBIU ' "$HISTORICO" | tail -n 1 | awk '{print $2, $3}' || true)
[ "$ULTIMO" = "$RAMO $COMMIT" ] || erro "ultimo SUBIU do historico nao e a versao no ar ($RAMO ${COMMIT:0:7})"

echo "== registrar o OK"
LINHA="OK $RAMO $COMMIT $(date '+%d/%m/%Y %H:%M')"
acrescentar_estado HOMOLOG_HISTORICO "$LINHA"
echo "$LINHA"
