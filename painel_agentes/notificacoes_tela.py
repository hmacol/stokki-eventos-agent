# -*- coding: utf-8 -*-
"""
painel_agentes/notificacoes_tela.py

Dados da tela /notificacoes (24/10): lista de tipos, painel de um tipo e
contagem de quem receberia. Quem decide e grava e controle_notificacoes;
a preferencia do cliente vem de preferencias_notificacao. Sem Flask.
"""
import logging
import os
import re
import sqlite3
import subprocess
import sys
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

import controle_notificacoes as cn
import preferencias_notificacao as pn
from regras.transportadoras import CatalogoTransportadoras

logger = logging.getLogger(__name__)

_RAIZ = Path(__file__).resolve().parent.parent

# Coluna de preferencias_notificacao de cada tipo da tela (a do portal): o
# mesmo nome, menos o chamado sem resposta, que la se chama diferente.
_COLUNA_PREF = {t: t for t in cn.TIPOS if t in pn.TIPOS}
_COLUNA_PREF["whatsapp_sem_resposta"] = "chamado_sem_resposta"

_ALVO_EMAILS = "_emails"   # linha de historico da edicao de e-mail/WhatsApp (so registro, sem desfazer)
_ROTULO_ALVO = {"_geral": "Geral", "_piloto": "Destino do piloto", _ALVO_EMAILS: "E-mails do embarcador"}


def _db_path(conn: sqlite3.Connection) -> Path:
    return Path(conn.execute("PRAGMA database_list").fetchone()[2])


def conectar_preferencias() -> sqlite3.Connection:
    """Mesmo arquivo do controle, pra ler/gravar a lista de e-mails do portal."""
    return sqlite3.connect(cn.DB_PATH, timeout=10)


def _plural(n: int, singular: str, plural: str) -> str:
    return f"{n} {singular if n == 1 else plural}"


def _resumo_excecoes(excecoes: dict) -> str:
    bloqueados = sum(1 for r in excecoes.values() if r == "BLOQUEADO")
    liberados = sum(1 for r in excecoes.values() if r == "LIBERADO")
    partes = []
    if bloqueados:
        partes.append(_plural(bloqueados, "bloqueado", "bloqueados"))
    if liberados:
        partes.append(_plural(liberados, "liberado", "liberados"))
    return " · ".join(partes) or "—"


def _ultimo_envio(conn: sqlite3.Connection, tipo: str) -> str:
    try:
        row = conn.execute("SELECT MAX(enviado_em) FROM notificacoes_enviadas WHERE tipo = ?", (tipo,)).fetchone()
    except sqlite3.OperationalError:    # a tabela so nasce quando a primeira rotina envia
        return "—"
    return row[0] if row and row[0] else "—"


def montar_lista(conn: sqlite3.Connection) -> list[dict]:
    """Uma linha por tipo, na ordem de cn.TIPOS. estado "config" = sem linha
    na tela (vale o config de hoje)."""
    linhas = []
    for tipo, info in cn.TIPOS.items():
        linhas.append({
            "tipo": tipo,
            "rotulo": info["rotulo"],
            "estado": cn.estado_tipo(conn, tipo) or "config",
            "resumo_excecoes": _resumo_excecoes(cn.excecoes(conn, tipo)),
            "quando": info["quando"],
            "ultimo_envio": _ultimo_envio(conn, tipo),
        })
    return linhas


def _embarcadores(conn: sqlite3.Connection, tipo: str) -> list[dict]:
    """Embarcadores do cadastro (um por CNPJ) com a preferencia do cliente
    para o tipo (None quando o tipo nao tem escolha no portal)."""
    coluna = _COLUNA_PREF.get(tipo)
    # carregar_embarcadores so conhece os tipos do portal; pros outros qualquer
    # um serve (so o nome, os e-mails e o CNPJ interessam).
    todos = pn.carregar_embarcadores(coluna or "nfs_em_rota", db_path=_db_path(conn))
    vistos = {}
    for emb in todos.values():
        cnpj = emb["cnpj"]
        if cnpj and cnpj not in vistos:
            vistos[cnpj] = {"chave": cnpj, "nome": emb["nome"] or cnpj, "emails": emb["emails"],
                            "preferencia_cliente": (not emb["desligado"]) if coluna else None}
    return sorted(vistos.values(), key=lambda e: e["nome"].lower())


def _pontos(conn: sqlite3.Connection) -> list:
    try:
        return CatalogoTransportadoras.carregar_do_banco(_db_path(conn)).pontos_redespacho()
    except FileNotFoundError:   # sem cadastro ativo (ou sem a tabela): nada a listar
        return []


def detalhe_tipo(conn: sqlite3.Connection, tipo: str) -> dict:
    """Painel lateral de um tipo: estado e um destino por embarcador (ou ponto
    de transportadora). nao_enviar so existe pra transportadoras e e somente
    leitura aqui (quem edita e o /transportadoras)."""
    info = cn.TIPOS[tipo]
    estado = cn.estado_tipo(conn, tipo)
    excecoes = cn.excecoes(conn, tipo)
    if info["destino"] == "transportadora":
        destinos = [{"chave": p.nome_normalizado.strip().lower(), "nome": p.nome, "excecao": None,
                     "preferencia_cliente": None, "nao_enviar": bool(p.nao_enviar)} for p in _pontos(conn)]
    else:
        destinos = [{"chave": e["chave"], "nome": e["nome"], "excecao": None,
                     "preferencia_cliente": e["preferencia_cliente"]} for e in _embarcadores(conn, tipo)]
    conhecidos = set()
    for d in destinos:
        d["excecao"] = excecoes.get(d["chave"])
        conhecidos.add(d["chave"])
    for chave, regra in excecoes.items():   # excecao de destino que saiu do cadastro continua visivel
        if chave not in conhecidos:
            extra = {"chave": chave, "nome": f"{chave} (fora do cadastro)", "excecao": regra,
                     "preferencia_cliente": None}
            if info["destino"] == "transportadora":
                extra["nao_enviar"] = False
            destinos.append(extra)
    return {"tipo": tipo, "rotulo": info["rotulo"], "destino": info["destino"], "canais": list(info["canais"]),
            "quando": info["quando"], "estado": estado or "config", "origem": "tela" if estado else "config",
            "destinos": destinos}


def contar_destinatarios(conn: sqlite3.Connection, tipo: str) -> int:
    """Quantos destinatarios receberiam de verdade se o tipo fosse LIGADO:
    embarcadores com contato no canal e preferencia ligada, menos os
    BLOQUEADOS; transportadoras = pontos sem NAO ENVIAR."""
    info = cn.TIPOS[tipo]
    if info["destino"] == "transportadora":
        return sum(1 for p in _pontos(conn) if not p.nao_enviar)
    bloqueados = {d for d, r in cn.excecoes(conn, tipo).items() if r == "BLOQUEADO"}
    so_whatsapp = tuple(info["canais"]) == ("whatsapp",)
    total = 0
    for emb in _embarcadores(conn, tipo):
        if emb["chave"] in bloqueados or emb["preferencia_cliente"] is False:
            continue
        if so_whatsapp:
            # numero valido e preferencia ligada (a regra do proprio aviso)
            if not pn.whatsapp_do_embarcador(conn, emb["chave"]):
                continue
        elif not emb["emails"]:
            continue
        total += 1
    return total


def historico_legivel(conn: sqlite3.Connection, limite: int = 50) -> list[dict]:
    """cn.historico com rotulo do alvo e nome do destino pra tela."""
    nomes = {e["chave"]: e["nome"] for e in _embarcadores(conn, "nfs_em_rota")}
    saida = []
    for h in cn.historico(conn, limite):
        h["alvo_rotulo"] = _ROTULO_ALVO.get(h["alvo"]) or cn.TIPOS.get(h["alvo"], {}).get("rotulo", h["alvo"])
        h["destino_nome"] = nomes.get(h["destino"]) if h["destino"] else None
        h["desfazivel"] = h["alvo"] != _ALVO_EMAILS
        saida.append(h)
    return saida


def lista_embarcadores(conn: sqlite3.Connection) -> list[dict]:
    """Pra caixa de busca do topo da tela: [{chave (CNPJ), nome}]."""
    return [{"chave": e["chave"], "nome": e["nome"]} for e in _embarcadores(conn, "nfs_em_rota")]


def situacao_embarcador(conn: sqlite3.Connection, cnpj) -> dict:
    """Uma linha por tipo de destino embarcador: estado do tipo, a excecao
    DESTE embarcador e a preferencia dele no portal (None = tipo sem escolha
    no portal). Somente leitura. ValueError se o CNPJ nao esta no cadastro."""
    chave = re.sub(r"\D", "", str(cnpj or ""))
    emb = next((e for e in _embarcadores(conn, "nfs_em_rota") if e["chave"] == chave), None)
    if emb is None:
        raise ValueError("Embarcador não encontrado no cadastro.")
    prefs = pn.ler(conn, chave)["tipos"]
    linhas = []
    for tipo, info in cn.TIPOS.items():
        if info["destino"] != "embarcador":
            continue
        coluna = _COLUNA_PREF.get(tipo)
        linhas.append({"tipo": tipo, "rotulo": info["rotulo"], "estado": cn.estado_tipo(conn, tipo) or "config",
                       "excecao": cn.excecoes(conn, tipo).get(chave),
                       "preferencia_cliente": bool(prefs[coluna]) if coluna else None})
    return {"cnpj": chave, "nome": emb["nome"], "situacao": linhas}


def confirmacao_do_desfazer(conn: sqlite3.Connection, historico_id: int) -> dict | None:
    """Desfazer que RELIGA pede a mesma confirmacao de ligar: devolve o corpo do
    409 (com a contagem) ou None se pode ir direto. Religa = volta para LIGADO,
    ou volta para "sem linha" (de None) saindo de um estado que cala: o geral
    sai de DESLIGADO (tudo volta a enviar) e o tipo sai de DESLIGADO/PILOTO
    (volta a seguir o config, que pode enviar de verdade; `volta_ao_config`).
    ValueError se o item nao existe ou nao tem desfazer."""
    row = conn.execute("SELECT alvo, destino, de FROM notificacoes_controle_historico WHERE id = ?",
                       (historico_id,)).fetchone()
    if not row:
        raise ValueError(f"Item de histórico inexistente: {historico_id}")
    alvo, destino, de = row
    if alvo == _ALVO_EMAILS:
        raise ValueError("Edição de e-mails fica só no registro: não tem desfazer.")
    if destino is not None or de not in ("LIGADO", None):
        return None
    if alvo == "_geral":
        atual = cn.estado_geral(conn)
        if atual == "LIGADO" or (de is None and atual != "DESLIGADO"):
            return None
        return {"geral": True, "volta_ao_config": de is None,
                "tipos_ligados": sum(1 for t in cn.TIPOS if cn.estado_tipo(conn, t) == "LIGADO")}
    if alvo not in cn.TIPOS:
        return None
    atual = cn.estado_tipo(conn, alvo)
    if de == "LIGADO" and atual != "LIGADO" or de is None and atual in ("DESLIGADO", "PILOTO"):
        return {"tipo": alvo, "volta_ao_config": de is None, "destinatarios": contar_destinatarios(conn, alvo)}
    return None


def separar_emails(valor) -> list[str]:
    if isinstance(valor, str):
        valor = re.split(r"[\n,;]+", valor)
    return [str(e).strip() for e in (valor or []) if str(e).strip()]


def _texto_contato(pref: dict) -> str:
    return f"e-mails: {', '.join(pref['emails']) or '—'}; WhatsApp: {pref['whatsapp'] or '—'}"


# ----------------------------------------------------------- disparar agora
# Tipos que tem rotina propria de lote. "teste" = --modo-teste (ignora a tela e
# manda so pro Hugo); "todos" = a rotina do jeito que o timer roda (quem recebe
# de verdade ainda e decidido pelo estado do tipo); "um" = um embarcador (so as
# rotinas que aceitam --sender-id). consulta_transportadora e os tipos que saem
# dentro de outra rotina ou no evento nao entram. Os de timer frequente
# (SO_PELO_TIMER) so tem "teste": "todos" correria junto com o timer e mandaria
# em dobro aos clientes.
DISPARAVEIS = {
    "nfs_em_rota": {"script": "notificar_nfs_em_rota.py", "alvos": ("teste", "todos", "um")},
    "resumo_diario": {"script": "resumo_diario_embarcador.py", "alvos": ("teste", "todos", "um")},
    "entrega_concluida": {"script": "notificacao_entregas/notificar_entrega_concluida.py", "alvos": ("teste",)},
    "pedidos_em_espera": {"script": "notificar_pedidos_em_espera.py", "alvos": ("teste", "todos")},
    "transportadoras": {"script": "notificacao_transportadoras/notificar_transportadoras.py", "alvos": ("teste", "todos")},
    "whatsapp_sem_resposta": {"script": "avisar_cliente_sem_resposta.py", "alvos": ("teste",)},
}

# Nota do painel dos tipos que o timer ja roda o tempo todo (infra/*.timer).
SO_PELO_TIMER = {
    "entrega_concluida": "Sai sozinho a cada 5 min pelo timer (06h às 23h); aqui só o teste.",
    "whatsapp_sem_resposta": "Sai sozinho a cada 2 min pelo timer (no horário de atendimento); aqui só a simulação.",
}

# Texto do alvo "teste" quando o --modo-teste do tipo NAO manda pro Hugo.
NOTA_TESTE = {
    "whatsapp_sem_resposta": "Só simula (mostra o que enviaria, não manda nada).",
}

# Texto do painel dos tipos sem botao.
SEM_DISPARO = {
    "insucesso": "Sem disparo manual aqui: use o botão Notificar da Torre.",
    "agendamento": "Sai dentro das sequências da roteirização (notificar_agendamento_pendente e notificar_agendamento_dia_fixo).",
    "redespacho": "Sai dentro do pipeline (redespacho_confirmacao).",
    "fora_dia_fixo": "Sai dentro das sequências da roteirização (notificar_fora_dia_fixo).",
    "faltas_recebimento": "Sai ao encerrar o recebimento no WMS (wms_faltas_recebimento).",
    "chamados": "Sai na hora do evento, dentro dos chamados do portal.",
    "area_nao_atendida": "Sai na hora do envio do XML, dentro do portal do cliente.",
    "convite_embarcador": "Sai dentro do timer de convites das 07h10 (embarcadores_stokki).",
    "consulta_transportadora": "Sai dentro do pipeline e do lembrete (consulta_transportadora); não tem disparo manual.",
    "rota_cancelada": "Sai no botão Cancelar e notificar do card da rota no Planejamento (mensagem no chat do portal).",
}

_TIPOS_COM_JA_RECEBEU = ("nfs_em_rota", "resumo_diario")   # chave "<sender_id>|<data>" em notificacoes_enviadas
_DISPARO_TIMEOUT_S = 1800
_DISPARO_TRAVA_MIN = 30
_DISPARO_LINHAS = 40


def montar_comando(tipo: str, alvo: str, sender_id: int | None, reenviar: bool) -> list[str]:
    """[python, script, flags...]. ValueError se o tipo nao e disparavel, o alvo
    nao vale pro tipo, ou "um" vem sem sender_id."""
    info = DISPARAVEIS.get(tipo)
    if not info:
        raise ValueError(f"Este tipo não tem disparo manual: {tipo}")
    if alvo not in info["alvos"]:
        raise ValueError(f"Alvo inválido para {cn.TIPOS[tipo]['rotulo']}: {alvo}")
    cmd = [sys.executable, str(_RAIZ / info["script"])]
    if alvo == "teste":
        cmd.append("--modo-teste")
    elif alvo == "um":
        if not sender_id:
            raise ValueError("Escolha o embarcador.")
        cmd += ["--sender-id", str(int(sender_id))]
        if reenviar:
            cmd.append("--reenviar")
    return cmd


def ja_recebeu_hoje(conn: sqlite3.Connection, tipo: str) -> dict[int, str]:
    """sender_id -> "HH:MM" de quem recebeu de verdade hoje. A copia de piloto
    (chave com |redirecionado) nao conta."""
    if tipo not in _TIPOS_COM_JA_RECEBEU:
        return {}
    hoje = date.today().isoformat()
    try:
        rows = conn.execute("SELECT chave, enviado_em FROM notificacoes_enviadas WHERE tipo = ? AND chave LIKE ?",
                            (tipo, f"%|{hoje}")).fetchall()
    except sqlite3.OperationalError:    # a tabela so nasce quando a primeira rotina envia
        return {}
    saida = {}
    for chave, enviado_em in rows:
        sender, _, data = chave.partition("|")
        if data != hoje or not sender.isdigit():
            continue
        saida[int(sender)] = (enviado_em or "")[11:16]
    return saida


def embarcadores_disparo(conn: sqlite3.Connection) -> list[dict]:
    """[{sender_id, nome}] pro alvo "um" (o sender_id da Stokki e o que as rotinas recebem)."""
    todos = pn.carregar_embarcadores("nfs_em_rota", db_path=_db_path(conn))
    itens = []
    for sender, emb in todos.items():
        try:
            itens.append({"sender_id": int(sender), "nome": emb["nome"] or emb["cnpj"] or str(sender)})
        except (TypeError, ValueError):
            continue
    return sorted(itens, key=lambda e: e["nome"].lower())


def _garantir_disparos(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS notificacoes_disparos ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, tipo TEXT, alvo TEXT, destino TEXT, por TEXT, "
        "iniciado_em TEXT, terminado_em TEXT, codigo_saida INTEGER, saida TEXT)")


def _fechar_orfaos(conn: sqlite3.Connection) -> None:
    """Disparo sem terminado_em iniciado ha mais de 30 min perdeu a thread (o
    painel reiniciou): fecha com codigo -3 pra tela nao ficar presa em "rodando"."""
    _garantir_disparos(conn)
    if conn.in_transaction:
        conn.commit()
    limite = (datetime.now() - timedelta(minutes=_DISPARO_TRAVA_MIN)).isoformat(timespec="seconds")
    conn.execute("UPDATE notificacoes_disparos SET terminado_em = ?, codigo_saida = -3, saida = ? "
                 "WHERE terminado_em IS NULL AND iniciado_em <= ?",
                 (cn._agora(), "interrompido (painel reiniciou ou passou de 30 min)", limite))
    conn.commit()


def ultimos_disparos(conn: sqlite3.Connection, tipo: str, limite: int = 5) -> list[dict]:
    _fechar_orfaos(conn)
    rows = conn.execute(
        "SELECT id, alvo, destino, por, iniciado_em, terminado_em, codigo_saida, saida "
        "FROM notificacoes_disparos WHERE tipo = ? ORDER BY id DESC LIMIT ?", (tipo, limite)).fetchall()
    return [dict(zip(("id", "alvo", "destino", "por", "iniciado_em", "terminado_em", "codigo_saida", "saida"), r))
            for r in rows]


def _texto(valor) -> str:
    if isinstance(valor, bytes):
        valor = valor.decode("utf-8", errors="replace")
    return valor or ""


def _executar_disparo(db_path: Path, disparo_id: int, cmd: list[str]) -> None:
    """Roda o subprocesso (thread de fundo) e grava o resultado. Nunca levanta."""
    try:
        r = subprocess.run(cmd, cwd=_RAIZ, timeout=_DISPARO_TIMEOUT_S, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        codigo = r.returncode
        texto = _texto(r.stdout) + ("\n" if r.stdout and r.stderr else "") + _texto(r.stderr)
    except subprocess.TimeoutExpired:
        codigo, texto = -1, f"Tempo esgotado ({_DISPARO_TIMEOUT_S // 60} min): a rotina foi interrompida."
    except Exception as e:     # nao conseguiu nem iniciar: o resultado fica na tela
        logger.exception("Disparo %s falhou ao iniciar", disparo_id)
        codigo, texto = -2, f"Falha ao iniciar a rotina: {e}"
    saida = "\n".join(texto.strip().splitlines()[-_DISPARO_LINHAS:])
    try:
        conn = sqlite3.connect(db_path, timeout=10)
        try:
            conn.execute("UPDATE notificacoes_disparos SET terminado_em = ?, codigo_saida = ?, saida = ? WHERE id = ?",
                         (cn._agora(), codigo, saida, disparo_id))
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        logger.exception("Disparo %s: nao gravou o resultado", disparo_id)


def iniciar_disparo(conn: sqlite3.Connection, tipo: str, alvo: str, sender_id: int | None,
                    reenviar: bool, por: str) -> int:
    """Grava a linha e roda a rotina numa thread. RuntimeError se ja ha disparo
    do mesmo tipo rodando (iniciado ha menos de 30 min e sem terminado_em);
    ValueError se o pedido nao vale."""
    cmd = montar_comando(tipo, alvo, sender_id, reenviar)
    destino = None
    if alvo == "um":
        nome = next((e["nome"] for e in embarcadores_disparo(conn) if e["sender_id"] == int(sender_id)), None)
        destino = f"{nome} (sender {int(sender_id)})" if nome else f"sender {int(sender_id)}"
        if reenviar:
            destino += " · reenviar"
    _fechar_orfaos(conn)
    conn.execute("BEGIN IMMEDIATE")     # checar e gravar sem outro disparo passar no meio
    try:
        limite = (datetime.now() - timedelta(minutes=_DISPARO_TRAVA_MIN)).isoformat(timespec="seconds")
        if conn.execute("SELECT 1 FROM notificacoes_disparos WHERE tipo = ? AND terminado_em IS NULL "
                        "AND iniciado_em > ?", (tipo, limite)).fetchone():
            raise RuntimeError(f"Já existe um disparo de \"{cn.TIPOS[tipo]['rotulo']}\" em andamento.")
        disparo_id = conn.execute(
            "INSERT INTO notificacoes_disparos (tipo, alvo, destino, por, iniciado_em) VALUES (?,?,?,?,?)",
            (tipo, alvo, destino, por, cn._agora())).lastrowid
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    try:
        threading.Thread(target=_executar_disparo, args=(_db_path(conn), disparo_id, cmd), daemon=True,
                         name=f"disparo-{disparo_id}").start()
    except BaseException as e:      # sem thread nao ha quem feche a linha: fecha aqui
        conn.execute("UPDATE notificacoes_disparos SET terminado_em = ?, codigo_saida = -2, saida = ? WHERE id = ?",
                     (cn._agora(), f"Falha ao iniciar a rotina: {e}", disparo_id))
        conn.commit()
        raise
    return disparo_id


def salvar_contato(cnpj, corpo: dict, por: str) -> dict:
    """Grava e-mails e/ou WhatsApp do embarcador na lista do portal (chave
    ausente no corpo = nao mexe) e registra a edicao no historico do controle
    (alvo _emails, de/para = valores antigo e novo; so registro). ValueError
    com mensagem pronta pra tela; com erro nada e gravado nem registrado."""
    conn = conectar_preferencias()
    try:
        antes = pn.ler(conn, cnpj)
        emails = separar_emails(corpo["emails"]) if "emails" in corpo else antes["emails"]
        depois = pn.salvar(conn, cnpj, emails, {}, por,
                           whatsapp=str(corpo["whatsapp"] or "") if "whatsapp" in corpo else None)
    finally:
        conn.close()
    de, para = _texto_contato(antes), _texto_contato(depois)
    if de != para:
        ctrl = cn.conectar()
        try:
            cn._registrar(ctrl, por, _ALVO_EMAILS, depois["cnpj"], de, para)
            ctrl.commit()
        finally:
            ctrl.close()
    return depois
