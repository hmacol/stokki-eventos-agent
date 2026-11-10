# shellcheck shell=bash
# Homologacao do legado: definicoes comuns dos scripts de homologacao/vps (carregado com ".").
#
# Fronteira de privilegio: o root NUNCA executa nem grava atraves de algo que o usuario
# homolog possa mudar.
# - Codigo, venv e estes scripts: do root, sem escrita para grupo/outros. /opt/stokki-homolog
#   e root:homolog 750.
# - So as pastas de dados (DIRS_HOMOLOG), o config e a conta de servico sao do homolog. O
#   root nao grava dentro delas (a copia monta pasta nova do root e troca inteira).
# - Estado da bancada (VERSAO, HISTORICO, COPIA, REQ_SHA) em /var/lib/stokki-homolog, so do
#   root, gravado atomicamente (temporario na mesma pasta + mv).
# - Ajudantes do lado do root rodam com /usr/bin/python3 -I (sistema, isolado).

umask 022

HML=/opt/stokki-homolog
PROD=/opt/stokki-eventos
ESTADO=/var/lib/stokki-homolog
PY_SISTEMA=/usr/bin/python3
UNITS=(homolog-painel homolog-portal)
RE_RAMO='^[A-Za-z0-9][A-Za-z0-9._/-]*$'
RE_COMMIT='^[0-9a-f]{40}$'
# pastas gravaveis pelo homolog (as gitignored de dados que o painel/portal escrevem)
DIRS_HOMOLOG=(dados roteirizacao/dados painel_agentes/dados documentos_pedido/dados
	notificacao_entregas/dados notificacao_transportadoras/dados debug output)
ARQS_HOMOLOG=(config.yaml gcs-service-account.json)
G=(git -c safe.directory="$HML" -C "$HML")

erro() {
	echo "erro: $*" >&2
	exit 1
}

# Recusa se algum caminho for link simbolico, nao for do root ou for gravavel por grupo/outros.
exigir_de_root() {
	local alvo dono modo
	for alvo in "$@"; do
		[ -e "$alvo" ] || erro "$alvo nao existe"
		[ ! -L "$alvo" ] || erro "$alvo e link simbolico"
		read -r dono modo < <(stat -c '%u %a' -- "$alvo") || true
		[ "${dono:-}" = 0 ] || erro "$alvo nao e do root"
		(( (8#${modo:-777} & 8#022) == 0 )) || erro "$alvo e gravavel por grupo/outros"
	done
}

# Roda um script desta pasta so depois de conferir que ele e a pasta sao do root.
rodar_script() {
	local script="$HML/homologacao/vps/$1"
	shift
	exigir_de_root "$HML/homologacao/vps" "$script"
	bash "$script" "$@"
}

preparar_estado() {
	[ ! -L "$ESTADO" ] || erro "$ESTADO e link simbolico"
	mkdir -p "$ESTADO"
	chown root:root "$ESTADO"
	chmod 755 "$ESTADO"
}

# gravar_estado <nome> <texto>: troca o arquivo inteiro de uma vez
gravar_estado() {
	local tmp
	tmp=$(mktemp "$ESTADO/.$1.XXXXXX")
	printf '%s\n' "$2" > "$tmp"
	chmod 644 "$tmp"
	mv -f -- "$tmp" "$ESTADO/$1"
}

# acrescentar_estado <nome> <linha>: copia + linha nova + mv (nunca >> no arquivo vivo)
acrescentar_estado() {
	local tmp
	tmp=$(mktemp "$ESTADO/.$1.XXXXXX")
	if [ -f "$ESTADO/$1" ]; then
		cat -- "$ESTADO/$1" > "$tmp"
	fi
	printf '%s\n' "$2" >> "$tmp"
	chmod 644 "$tmp"
	mv -f -- "$tmp" "$ESTADO/$1"
}

# campo_versao <campo>: le do HOMOLOG_VERSAO (do root) com o python do sistema
campo_versao() {
	"$PY_SISTEMA" -I -c 'import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
print(d.get(sys.argv[2]) or "?")' "$ESTADO/HOMOLOG_VERSAO" "$1"
}

# Codigo e venv do root (sem escrita para grupo/outros); pastas de dados do homolog.
normalizar_donos() {
	local d p
	local poda=(-path "$HML/.git")
	for p in "${ARQS_HOMOLOG[@]}" "${DIRS_HOMOLOG[@]}"; do
		poda+=(-o -path "$HML/$p")
	done
	[ -d "$HML/.git" ] && [ ! -L "$HML" ] || erro "$HML nao e um checkout"
	chown -R root:root "$HML/.git"
	chmod 700 "$HML/.git"
	find "$HML" -xdev \( "${poda[@]}" \) -prune -o -exec chown -h root:root {} +
	find "$HML" -xdev \( "${poda[@]}" \) -prune -o ! -type l -exec chmod go-w {} +
	chown root:homolog "$HML"
	chmod 750 "$HML"
	for d in "${DIRS_HOMOLOG[@]}"; do
		p="$HML/$d"
		[ ! -L "$p" ] || erro "$p e link simbolico"
		mkdir -p "$p"
		chown homolog:homolog "$p"
		chmod 750 "$p"
	done
}

# Autoconferencia: esta pasta, este arquivo e o script que carregou (quando esta no checkout).
_COMUM=$(readlink -f "${BASH_SOURCE[0]}")
exigir_de_root "$(dirname "$_COMUM")" "$_COMUM"
_CHAMADOR=$(readlink -f "${BASH_SOURCE[1]:-}" 2>/dev/null || true)
case "$_CHAMADOR" in
	"$HML"/*) exigir_de_root "$_CHAMADOR" ;;
esac
unset _COMUM _CHAMADOR
