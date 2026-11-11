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
# - O venv tem o homolog-guarda.pth (do root): todo processo dele nasce com a guarda; o pip do
#   root passa por pip_do_venv.

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

# O kernel precisa barrar hardlink/symlink de outro usuario em pasta compartilhada.
exigir_protecoes_kernel() {
	local chave
	for chave in fs.protected_hardlinks fs.protected_symlinks; do
		[ "$(sysctl -n "$chave" 2>/dev/null || true)" = 1 ] || erro "$chave precisa ser 1"
	done
}

# copiar_sem_link <origem> <destino> <dono[,dono...] aceito(s)> <pasta exigida>
# Arquivo entregue por outro usuario (www-data, homolog):
# - abre sem seguir link no ultimo nome e com O_NONBLOCK (FIFO plantado nao trava o root);
# - fstat no descritor: arquivo comum, 1 link so, um dos donos aceitos;
# - o caminho REAL do que foi aberto (/proc/self/fd/N) tem que estar dentro da pasta exigida:
#   uma pasta do meio trocada por link (corrida entre conferir e abrir) leva para fora e recusa;
# - COPIA o conteudo para um arquivo novo do root (600, O_EXCL).
# O root nunca da chown/mv no arquivo que outro usuario entregou.
copiar_sem_link() {
	"$PY_SISTEMA" -I - "$1" "$2" "$3" "$4" <<'PY'
import os
import pwd
import shutil
import stat
import sys

origem, destino, donos, pasta = sys.argv[1:]
pasta = os.path.realpath(pasta).rstrip("/") + "/"
uids = {pwd.getpwnam(d).pw_uid for d in donos.split(",")}
fd = os.open(origem, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
st = os.fstat(fd)
if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid not in uids:
    sys.exit("erro: %s nao e arquivo comum de %s com 1 link so" % (origem, donos))
real = os.readlink("/proc/self/fd/%d" % fd)
if not real.startswith(pasta):
    sys.exit("erro: %s nao esta dentro de %s" % (origem, pasta))
saida = os.open(destino, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
with os.fdopen(fd, "rb") as f, os.fdopen(saida, "wb") as g:
    shutil.copyfileobj(f, g, 1024 * 1024)
    g.flush()
    os.fsync(g.fileno())
PY
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

# pip do venv da homologacao. O homolog-guarda.pth poe a guarda em todo processo do venv, e o pip
# precisa do PyPI: HOMOLOG_PIP_SEM_GUARDA=1 so vale para o root (ver homologacao/pth_guarda.py).
pip_do_venv() {
	HOMOLOG_PIP_SEM_GUARDA=1 "$HML/venv/bin/pip" install --quiet --disable-pip-version-check -r "$HML/requirements.txt"
}

# Instala o infra/homolog/homolog-guarda.pth (do root, 644) no site-packages do venv: todo processo
# do venv nasce com a guarda, ate script rodado a mao sem os units (so -S pula; o nft segura).
# Prova como homolog, sem ambiente nenhum e com -I. Devolve 1 se algo falhar (quem chama decide a saida).
instalar_pth_guarda() {
	local sites
	sites=("$HML"/venv/lib/python3*/site-packages)
	if [ "${#sites[@]}" -ne 1 ] || [ ! -d "${sites[0]}" ] || [ -L "${sites[0]}" ]; then
		echo "erro: site-packages do venv nao achado (ou mais de um) em $HML/venv/lib" >&2
		return 1
	fi
	install -m 644 -o root -g root "$HML/infra/homolog/homolog-guarda.pth" "${sites[0]}/homolog-guarda.pth" || return 1
	if ! (cd / && sudo -u homolog env -i "$HML/venv/bin/python" -I -c 'import os, smtplib, sys
sys.exit(0 if smtplib.SMTP.__name__ == "SMTPFalso" and os.environ.get("FRESH_AMBIENTE") == "homolog" else 1)'); then
		echo "erro: venv/bin/python -I como homolog nao carregou a guarda pelo homolog-guarda.pth" >&2
		return 1
	fi
	echo "(homolog-guarda.pth em ${sites[0]}; guarda provada com -I)"
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
