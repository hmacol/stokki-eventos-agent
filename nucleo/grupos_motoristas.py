# -*- coding: utf-8 -*-
"""
grupos_motoristas.py

Grupos do WhatsApp dos motoristas (gateway OpenWA): sugere qual grupo e de qual
motorista (telefone entre os participantes = forte; nome no titulo = fraca),
guarda o vinculo CONFIRMADO pela equipe em motoristas_grupos_whatsapp e renomeia
o grupo para "FRESHLOG + NOME COMPLETO - veiculo". Nada grava sem confirmacao;
renomear de verdade so no modo 'real' (modo_renomear: renomear_grupos ligado, canal
ligado e envio real); piloto so mostra a previa.
"""
import logging
import re
import sqlite3
import time
import unicodedata
from datetime import date, datetime, timedelta

import integracao_openwa
import notificar_whatsapp
from nucleo import atualizacao_motorista as atu, banco
from regras.preferencias_motoristas import _parse_bool

logger = logging.getLogger("nucleo.grupos_motoristas")

PREFIXOS = ("FRESHLOG +", "FRESH +")
_MAX_NOME = 100
_PAUSA_RENOMEAR = 3


def _limpo(nome) -> str:
    """Tira espacos e caracteres invisiveis (zero-width etc.) da esquerda."""
    texto = str(nome or "")
    i = 0
    while i < len(texto) and (texto[i].isspace() or unicodedata.category(texto[i]) in ("Cf", "Zs", "Zl", "Zp")):
        i += 1
    return texto[i:].strip()


def _prefixo(nome_limpo: str) -> str | None:
    maiusculo = nome_limpo.upper()
    return next((p for p in PREFIXOS if maiusculo.startswith(p)), None)


def _sem_acento(texto: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", texto) if not unicodedata.combining(c)).upper()


def _palavras(texto: str) -> set[str]:
    return {p for p in re.split(r"[^A-Z0-9]+", _sem_acento(texto)) if p}


def _trecho_nome(nome_limpo: str) -> str:
    """Parte do titulo entre o prefixo e o primeiro ' - '."""
    resto = nome_limpo[len(_prefixo(nome_limpo)):]
    return resto.split(" - ", 1)[0]


def _chaves_telefone(pessoa: dict) -> set[str]:
    chaves = set()
    for linha in pessoa["linhas"]:
        d = atu._digitos(linha.get("TELEFONE_MOTORISTA"))
        if len(d) >= 11:
            chaves.add(d[-11:])
        elif len(d) == 10:
            chaves.add(d)
    return chaves


def _vinculos(conn: sqlite3.Connection) -> dict[int, dict]:
    """principal_id da pessoa -> vinculo. O vinculo pode estar guardado em qualquer linha da pessoa
    (a principal muda quando uma linha entra ou sai); vale o da principal, senao o de menor id.
    `bd_motorista_id` do vinculo e a linha onde ele esta gravado. Vinculo de linha que nao existe
    mais fica com o proprio id."""
    principal = {i: p["principal_id"] for p in atu.pessoas(conn) for i in p["ids"]}
    resultado: dict[int, dict] = {}
    for r in conn.execute("SELECT * FROM motoristas_grupos_whatsapp ORDER BY bd_motorista_id"):
        pid = principal.get(r["bd_motorista_id"], r["bd_motorista_id"])
        if pid not in resultado or r["bd_motorista_id"] == pid:
            resultado[pid] = dict(r)
    return resultado


def sugerir(conn: sqlite3.Connection, cfg_openwa: dict, *, grupo=integracao_openwa.grupo,
            listar=integracao_openwa.listar_grupos) -> dict:
    """Le os grupos do padrao e sugere um por motorista. Nao grava nada."""
    lista = listar(cfg_openwa)
    if lista is None:
        return {"erro": "Gateway do WhatsApp indisponivel: nao foi possivel ler os grupos.",
                "pessoas": [], "grupos": []}
    grupos = []
    for g in lista:
        if not isinstance(g, dict):
            continue
        nome = _limpo(g.get("name"))
        if g.get("id") and _prefixo(nome):
            grupos.append({"grupo_id": g["id"], "grupo_nome": g.get("name") or "", "_trecho": _palavras(_trecho_nome(nome))})
    grupos.sort(key=lambda g: _sem_acento(_limpo(g["grupo_nome"])))

    numeros, sem_leitura = {}, 0
    for g in grupos:
        dados = grupo(cfg_openwa, g["grupo_id"])
        if not dados:
            sem_leitura += 1
            dados = {}
        numeros[g["grupo_id"]] = [atu._digitos(p.get("number")) for p in dados.get("participants") or []]

    vinculos = _vinculos(conn)
    pessoas = []
    for pessoa in atu.pessoas(conn):
        chaves = _chaves_telefone(pessoa)
        primeiro = next(iter(_sem_acento(pessoa["nome"]).split()), "")
        por_telefone, por_nome = [], []
        for g in grupos:
            item = {"grupo_id": g["grupo_id"], "grupo_nome": g["grupo_nome"]}
            if chaves and any(n.endswith(c) for n in numeros[g["grupo_id"]] for c in chaves):
                por_telefone.append(dict(item, como="telefone"))
            elif primeiro and primeiro in g["_trecho"]:
                por_nome.append(dict(item, como="nome"))
        v = vinculos.get(pessoa["principal_id"])
        pessoas.append({
            "principal_id": pessoa["principal_id"],
            "nome": pessoa["nome"],
            "confirmado": ({k: v[k] for k in ("grupo_id", "grupo_nome", "como", "nome_anterior", "renomeado_em")}
                           if v else None),
            "sugestoes": por_telefone + por_nome,
        })
    return {"erro": None, "grupos_sem_leitura": sem_leitura, "pessoas": pessoas,
            "grupos": [{"grupo_id": g["grupo_id"], "grupo_nome": g["grupo_nome"]} for g in grupos]}


def confirmar(conn: sqlite3.Connection, principal_id: int, grupo_id: str, grupo_nome: str, como: str,
              usuario: str) -> None:
    """Grava (ou troca) o grupo confirmado do motorista. grupo_id vazio remove o vinculo."""
    pessoa = atu._pessoa_de(conn, principal_id)
    outros = [i for i in (pessoa["ids"] if pessoa else []) if i != principal_id]
    if outros:
        # vinculo guardado numa linha que deixou de ser a principal passa pra ela; um vinculo por pessoa
        atual = _vinculos(conn).get(principal_id)
        if atual and atual["bd_motorista_id"] != principal_id:
            conn.execute("UPDATE motoristas_grupos_whatsapp SET bd_motorista_id = ? WHERE bd_motorista_id = ?",
                         (principal_id, atual["bd_motorista_id"]))
        conn.execute(f"DELETE FROM motoristas_grupos_whatsapp WHERE bd_motorista_id IN ({','.join('?' * len(outros))})",
                     outros)
    if not grupo_id:
        conn.execute("DELETE FROM motoristas_grupos_whatsapp WHERE bd_motorista_id = ?", (principal_id,))
    else:
        conn.execute(
            "INSERT INTO motoristas_grupos_whatsapp (bd_motorista_id, grupo_id, grupo_nome, como, confirmado_por, "
            "confirmado_em) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(bd_motorista_id) DO UPDATE SET grupo_id = excluded.grupo_id, "
            "grupo_nome = excluded.grupo_nome, como = excluded.como, confirmado_por = excluded.confirmado_por, "
            "confirmado_em = excluded.confirmado_em, "
            "nome_anterior = CASE WHEN grupo_id = excluded.grupo_id THEN nome_anterior END, "
            "renomeado_em = CASE WHEN grupo_id = excluded.grupo_id THEN renomeado_em END",
            (principal_id, grupo_id, grupo_nome, como, usuario, banco.agora()))
    conn.commit()


def nome_novo(nome_atual: str, nome_completo: str) -> str:
    """'FRESHLOG + NOME COMPLETO' + o sufixo (veiculo) do nome atual, se houver.
    Maximo de 100 caracteres: o sufixo e cortado, nunca o nome."""
    base = "FRESHLOG + " + " ".join(str(nome_completo or "").split()).upper()
    atual = _limpo(nome_atual)
    prefixo = _prefixo(atual)
    resto = atual[len(prefixo):] if prefixo else atual
    sufixo = resto.split(" - ", 1)[1].strip() if " - " in resto else ""
    if len(base) >= _MAX_NOME:
        return base[:_MAX_NOME].rstrip()
    if not sufixo:
        return base
    sobra = _MAX_NOME - len(base) - 3
    if sobra <= 0:
        return base
    sufixo = sufixo[:sobra].rstrip()
    return f"{base} - {sufixo}" if sufixo else base


def previa_renomear(conn: sqlite3.Connection, cfg_openwa: dict, numero_proprio: str | None = None, *,
                    grupo=integracao_openwa.grupo) -> list[dict]:
    """Por vinculo confirmado: nome atual -> novo e se da para renomear. Grupo trancado (so admin
    edita): com `numero_proprio`, sem permissao quando ele nao e admin; sem ele nao da pra saber,
    entao vai como 'renomear' com a observacao (o gateway responde 403 se nao for admin)."""
    nomes = {p["principal_id"]: p["nome"] for p in atu.pessoas(conn)}
    vinculos = _vinculos(conn)
    contagem: dict[str, int] = {}
    for v in vinculos.values():
        contagem[v["grupo_id"]] = contagem.get(v["grupo_id"], 0) + 1
    proprio = atu._digitos(numero_proprio)[-11:]
    previa = []
    for pid in sorted(vinculos):
        v = vinculos[pid]
        dados = grupo(cfg_openwa, v["grupo_id"]) or {}
        atual = dados.get("name") or v["grupo_nome"] or ""
        novo = nome_novo(atual, nomes.get(pid, ""))
        observacao = None
        if contagem[v["grupo_id"]] > 1:
            situacao = "dupla"
        elif not dados.get("name") or not (nomes.get(pid) or "").strip():
            situacao = "erro"
        elif _limpo(atual) == novo:
            situacao = "ja_no_padrao"
        elif dados.get("locked") and proprio and not any(
                p.get("isAdmin") and atu._digitos(p.get("number")).endswith(proprio)
                for p in dados.get("participants") or []):
            situacao = "sem_permissao"
        else:
            situacao = "renomear"
            if dados.get("locked") and not proprio:
                observacao = OBS_TRANCADO
        previa.append({"principal_id": pid, "grupo_id": v["grupo_id"], "atual": atual, "novo": novo,
                       "situacao": situacao, "observacao": observacao})
    return previa


OBS_TRANCADO = "grupo trancado: só funciona se o número conectado for administrador"


def modo_renomear(config: dict) -> str:
    """'real' so com atualizacao_motoristas.renomear_grupos ligado, o canal do WhatsApp ligado
    (gateway_clientes_ok) e o envio real (forcar_destino_whatsapp presente e vazio). Com numero de
    teste = 'piloto' (so a previa); o resto = 'desligado'."""
    if not _parse_bool(((config or {}).get("atualizacao_motoristas") or {}).get("renomear_grupos")):
        return "desligado"
    destino = notificar_whatsapp.destino_link_atualizacao(config)
    if destino is None or not notificar_whatsapp.gateway_clientes_ok(config):
        return "desligado"
    return "piloto" if destino else "real"


def renomear(conn: sqlite3.Connection, config: dict, usuario: str, principal_ids: list[int], *,
             modo_teste: bool = False, dormir=time.sleep, numero_proprio: str | None = None,
             grupo=integracao_openwa.grupo, renomear_grupo=integracao_openwa.renomear_grupo) -> list[dict]:
    """Renomeia os grupos confirmados dos motoristas pedidos (so os de situacao
    'renomear'). Cada renomeado e gravado na hora: falha no meio mantem os feitos.
    Fora do modo 'real' (modo_renomear) nada e chamado no gateway: todos saem
    'desligado' ou 'piloto'."""
    modo = modo_renomear(config)
    if modo != "real":
        return [{"principal_id": pid, "situacao": modo, "resultado": modo} for pid in principal_ids]
    cfg_openwa = (config or {}).get("whatsapp_notificacoes") or {}
    pedidos = set(principal_ids)
    previa = [p for p in previa_renomear(conn, cfg_openwa, numero_proprio, grupo=grupo) if p["principal_id"] in pedidos]
    previa += [{"principal_id": pid, "grupo_id": None, "atual": None, "novo": None, "situacao": "sem_grupo"}
               for pid in principal_ids if pid not in {p["principal_id"] for p in previa}]
    if modo_teste:
        return previa
    resultado, chamou = [], False
    for item in previa:
        if item["situacao"] != "renomear":
            resultado.append(dict(item, resultado=item["situacao"]))
            continue
        if chamou:
            dormir(_PAUSA_RENOMEAR)
        chamou = True
        ok, erro = renomear_grupo(cfg_openwa, item["grupo_id"], item["novo"])
        if not ok and erro == "incerto":
            # 503: o WhatsApp nao confirmou; confere o nome antes de decidir
            ok = (grupo(cfg_openwa, item["grupo_id"]) or {}).get("name") == item["novo"]
            if not ok:
                logger.warning("Renomear grupo %s incerto: nome nao mudou", item["grupo_id"])
                resultado.append(dict(item, situacao="incerto", resultado="incerto", erro=erro))
                continue
        if not ok:
            logger.warning("Renomear grupo %s falhou: %s", item["grupo_id"], erro)
            situacao = "sem_permissao" if erro == "sem_permissao" else item["situacao"]
            resultado.append(dict(item, situacao=situacao, resultado="sem_permissao" if erro == "sem_permissao" else "falhou",
                                  erro=erro))
            continue
        # nome_anterior guarda o de antes da 1a renomeacao (pra poder voltar); o vinculo pode estar
        # gravado numa linha da pessoa que nao e a principal
        linha_vinculo = (_vinculos(conn).get(item["principal_id"]) or {}).get("bd_motorista_id", item["principal_id"])
        conn.execute("UPDATE motoristas_grupos_whatsapp SET nome_anterior = COALESCE(nome_anterior, ?), grupo_nome = ?, "
                     "renomeado_em = ? WHERE bd_motorista_id = ?",
                     (item["atual"], item["novo"], banco.agora(), linha_vinculo))
        conn.commit()
        logger.info("Grupo %s renomeado por %s: %r -> %r", item["grupo_id"], usuario, item["atual"], item["novo"])
        resultado.append(dict(item, situacao="renomeado", resultado="renomeado"))
    return resultado


# ── Envio do link de atualizacao ──────────────────────────────────────────────

_INTERROMPEM_LOTE = ("teto", "canal_em_pausa")  # situacoes que interrompem o lote


def rotas_30d(conn: sqlite3.Connection, hoje: date | None = None) -> dict[int, int]:
    """agent_id -> rotas em nucleo_rotas com data_rota nos ultimos 30 dias (hoje incluido)."""
    hoje = hoje or date.today()
    desde = (hoje - timedelta(days=29)).isoformat()
    return {r[0]: r[1] for r in conn.execute(
        "SELECT agent_id, COUNT(*) FROM nucleo_rotas WHERE agent_id IS NOT NULL AND data_rota >= ? "
        "AND data_rota <= ? GROUP BY agent_id", (desde, hoje.isoformat()))}


def candidatos_envio(conn: sqlite3.Connection, hoje: date | None = None) -> list[dict]:
    """Quem pode receber o link: ativo, com pendencia (documentos ou campos), com CPF, com grupo
    confirmado e sem link real em aberto (link de piloto nao conta). Mais rotas primeiro."""
    rotas = rotas_30d(conn, hoje)
    vinculos = _vinculos(conn)
    candidatos = []
    for pessoa in atu.pessoas(conn):
        vinculo = vinculos.get(pessoa["principal_id"])
        if not pessoa["ativo"] or not pessoa["cpf"] or not vinculo:
            continue
        pend = atu.pendencias(conn, pessoa)
        if not (pend["documentos"] or pend["campos"]) or atu.link_em_aberto(conn, pessoa["principal_id"]):
            continue
        candidatos.append({"principal_id": pessoa["principal_id"], "nome": pessoa["nome"],
                           "rotas_30d": sum(rotas.get(a, 0) for a in pessoa["agent_ids"]),
                           "grupo_nome": vinculo["grupo_nome"], "pendencias": pend})
    candidatos.sort(key=lambda c: (-c["rotas_30d"], _sem_acento(c["nome"])))
    return candidatos


def _cancelar_link(conn: sqlite3.Connection, link_id: int) -> None:
    conn.execute("UPDATE motoristas_links_atualizacao SET cancelado_em = ? WHERE id = ? AND cancelado_em IS NULL",
                 (banco.agora(), link_id))
    conn.commit()


def enviar_lote(conn: sqlite3.Connection, config: dict, principal_ids: list[int], usuario: str, url_base: str, *,
                modo_teste: bool = False, avisar=notificar_whatsapp.avisar_motorista_link_atualizacao) -> list[dict]:
    """Um link por motorista, na ordem pedida, cada um no grupo dele. Envio desligado: todos
    'desligado', nenhum link gerado (gerar link cancela os abertos). Elegibilidade conferida de novo
    antes de cada link ('nao_elegivel' sem gerar nada). Erro no meio de uma pessoa cancela o link
    dela, sai 'falhou' e o lote segue. So 'enviado' deixa o link vivo e grava enviado_em; em modo
    teste o link nasce piloto, registra 'modo_teste' e e cancelado. Teto/pausa do canal interrompem
    o lote (os que sobram saem 'nao_tentado')."""
    destino = notificar_whatsapp.destino_link_atualizacao(config)
    if destino is None or not notificar_whatsapp.gateway_clientes_ok(config):
        return [{"principal_id": pid, "situacao": "desligado"} for pid in principal_ids]
    cfg_atu = (config or {}).get("atualizacao_motoristas") or {}
    validade = int(cfg_atu.get("validade_dias") or atu.VALIDADE_DIAS)
    piloto = bool(destino)
    candidatos = {c["principal_id"]: c for c in candidatos_envio(conn)}
    vinculos = _vinculos(conn)
    resultado, parado = [], False
    for pid in principal_ids:
        if pid not in candidatos or atu.link_em_aberto(conn, pid):
            resultado.append({"principal_id": pid, "situacao": "nao_elegivel"})
            continue
        if parado:
            resultado.append({"principal_id": pid, "situacao": "nao_tentado"})
            continue
        link_id = None
        try:
            link = atu.gerar_link(conn, pid, usuario, validade_dias=validade, piloto=piloto or modo_teste)
            link_id = link["id"]
            primeiro = next(iter(candidatos[pid]["nome"].split()), "").capitalize()
            texto = notificar_whatsapp.texto_link_atualizacao(
                primeiro, f"{url_base.rstrip('/')}/atualizar/{link['token']}",
                datetime.strptime(link["expira_em"], "%Y-%m-%d %H:%M:%S").date())
            situacao, _ = avisar(link_id, vinculos[pid]["grupo_id"], vinculos[pid]["grupo_nome"], texto, config,
                                 modo_teste=modo_teste)
            agora = banco.agora()
            enviado = situacao == "enviado"
            conn.execute("UPDATE motoristas_links_atualizacao SET envio_destino = ?, envio_situacao = ?, "
                         "enviado_em = ?, cancelado_em = ? WHERE id = ?",
                         (f"{destino}@c.us" if destino else vinculos[pid]["grupo_id"], situacao,
                          agora if enviado else None, None if enviado else agora, link_id))
            conn.commit()
        except Exception:
            # se a mensagem saiu e a gravacao seguinte falhou, o link e cancelado mesmo entregue (raro; lado seguro)
            logger.exception("Envio do link de atualizacao do motorista %s falhou", pid)
            situacao = "falhou"
            if link_id is not None:
                try:
                    _cancelar_link(conn, link_id)
                except Exception:
                    logger.exception("Nao foi possivel cancelar o link %s", link_id)
        resultado.append({"principal_id": pid, "situacao": situacao})
        parado = situacao in _INTERROMPEM_LOTE
    return resultado
