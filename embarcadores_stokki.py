# -*- coding: utf-8 -*-
"""
embarcadores_stokki.py

Embarcador novo na Stokki vira pre-cadastro em `interno` e recebe convite
pra completar o cadastro no portal (pedido do Hugo, 07/10/2026; spec em
docs/superpowers/specs/2026-10-08-embarcadores-novos-stokki-design.md).

Roda de hora em hora, 07h-19h (infra/stokki-embarcadores-novos.timer):
1. le o select de clientes da Stokki e compara com interno.stkkc_id;
2. novo: linha em interno (sem habilidade) + cadastro_embarcador;
3. ativo (pedido nos ultimos 30 dias): sender da Vuupt (o do pedido mais
   antigo no nucleo, ou cria o customer) e convite: WhatsApp do celular da
   Stokki, senao e-mail, senao so aviso no grupo;
4. convite sem PIN depois de 3 dias: reenvia uma vez.

Tela de Notificacoes (controle_notificacoes.py, tipo "convite_embarcador",
e-mail e WhatsApp decididos separadamente): vale por cima do config. Convite e
ESTADO, nao evento: NAO pela tela (ou PILOTO) nao marca CONVIDADO, o
pre-cadastro segue pendente e o convite sai quando a tela ligar. PILOTO manda
a copia so ao piloto, uma por dia por canal.

Envio ao cliente preso por padrao: WhatsApp em
cadastro_embarcador.forcar_destino_whatsapp (ausente = vale
whatsapp_notificacoes.clientes.forcar_destino); e-mail em
cadastro_embarcador.forcar_destino_email (ausente = hugo@). Nos dois, ''
explicito = envio real.

Uso:
    python embarcadores_stokki.py [--modo-teste]
"""
import argparse
import html as html_lib
import logging
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

_RAIZ = Path(__file__).resolve().parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(_RAIZ / "portal_cliente"))

import auth_cliente as auth  # noqa: E402  (portal_cliente/auth_cliente.py)
import cadastro_embarcador as cad  # noqa: E402
import notificar_whatsapp as nw  # noqa: E402
import limite_piloto  # noqa: E402
import preferencias_notificacao as pn  # noqa: E402
from controle_notificacoes import acao_do_config, decidir  # noqa: E402

logger = logging.getLogger("embarcadores_stokki")

_RE_ROTULO = re.compile(r"^(?P<apelido>.*?)\s*\((?P<razao>.*)\s+-\s+(?P<cnpj>[\d./-]{14,18})\)\s*$")


def interpretar_rotulo(select_label: str, name: str) -> tuple[str | None, str, str]:
    """'APELIDO (RAZAO - 00.000.000/0000-00)' -> (apelido, razao, cnpj).
    Apelido igual a razao vira None. Fora do padrao: (None, name, '')."""
    m = _RE_ROTULO.match(str(select_label or "").strip())
    if not m:
        return None, str(name or "").strip(), ""
    razao = m.group("razao").strip()
    apelido = m.group("apelido").strip()
    cnpj = re.sub(r"\D", "", m.group("cnpj"))
    if len(cnpj) != 14:
        return None, razao or str(name or "").strip(), ""
    if not apelido or apelido.upper() == razao.upper():
        apelido = None
    return apelido, razao, cnpj


def clientes_do_select(bruto) -> list[dict]:
    """Resposta de stokki.pedidos.listar_embarcadores -> lista de clientes."""
    itens = bruto.get("clients", []) if isinstance(bruto, dict) else (bruto or [])
    clientes = []
    for c in itens:
        apelido, razao, cnpj = interpretar_rotulo(c.get("select_label", ""), c.get("name", ""))
        clientes.append({"stkkc_id": int(c["id"]), "apelido": apelido, "razao": razao, "cnpj": cnpj})
    return clientes


def _valor_input(html: str, nome: str) -> str:
    m = re.search(r'<input[^>]*\bname="%s"[^>]*>' % re.escape(nome), html or "")
    if not m:
        return ""
    v = re.search(r'\bvalue="([^"]*)"', m.group(0))
    return html_lib.unescape(v.group(1)).strip() if v else ""


def extrair_contato(html: str) -> dict:
    """Tela /administrator/client/edit/{id} da Stokki -> contato. Pessoa
    primeiro, empresa como reserva."""
    return {
        "celular": _valor_input(html, "cellphone") or _valor_input(html, "company_cellphone"),
        "telefone": _valor_input(html, "phone") or _valor_input(html, "company_phone"),
        "email": (_valor_input(html, "email") or _valor_input(html, "company_email[]")).lower(),
        "cidade": _valor_input(html, "city"),
    }


def celular_whatsapp(valor) -> str:
    """Numero que vale tentar no WhatsApp, ou ''. Recusa invalido e numero de
    mentira com o mesmo digito repetido, ex. (11) 999 999 999."""
    try:
        numero = pn.normalizar_whatsapp(valor)
    except ValueError:
        return ""
    if not numero or len(set(numero[4:])) == 1:
        return ""
    return numero


DIAS_ATIVO = 30
_SQL_CNPJ_REMETENTE = "REPLACE(REPLACE(REPLACE(remetente_codigo, '.', ''), '/', ''), '-', '')"


def _fmt(agora: datetime) -> str:
    return agora.strftime("%Y-%m-%d %H:%M:%S")


def _tabela_existe(conn, nome: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (nome,)).fetchone() is not None


def tem_pedido_recente(conn, cnpj: str, agora: datetime, dias: int = DIAS_ATIVO) -> bool:
    if not _tabela_existe(conn, "nucleo_pedidos"):
        return False
    limite = _fmt(agora - timedelta(days=dias))
    return conn.execute(f"SELECT 1 FROM nucleo_pedidos WHERE {_SQL_CNPJ_REMETENTE} = ? AND criado_em >= ? LIMIT 1",
                        (cnpj, limite)).fetchone() is not None


def sender_do_nucleo(conn, cnpj: str) -> int | None:
    """Sender do pedido mais antigo desse remetente: e o que a Vuupt criou
    primeiro; os pedidos seguintes passam a apontar pra ele via interno."""
    if not _tabela_existe(conn, "nucleo_pedidos"):
        return None
    row = conn.execute(f"SELECT sender_id FROM nucleo_pedidos WHERE {_SQL_CNPJ_REMETENTE} = ? AND sender_id IS NOT NULL "
                       "ORDER BY criado_em, codigo LIMIT 1", (cnpj,)).fetchone()
    return int(row[0]) if row else None


def resolver_sender(conn, cnpj: str, razao: str, vuupt) -> int | None:
    sender = sender_do_nucleo(conn, cnpj)
    if sender or vuupt is None:
        return sender
    try:
        resposta = vuupt.criar_customer({"name": razao, "code": auth.formatar_cnpj(cnpj)})
    except Exception as exc:
        logger.warning(f"Falha ao criar o remetente de {cnpj} na Vuupt: {exc}")
        return None
    dados = resposta.get("customer", resposta) if isinstance(resposta, dict) else {}
    return int(dados["id"]) if isinstance(dados, dict) and dados.get("id") else None


def separar(clientes: list[dict], conn) -> dict:
    """novos: CNPJ fora de interno; sem_stkkc: CNPJ em interno sem stkkc_id
    (cadastro manual antigo); conflitos: CNPJ em interno com outro stkkc_id;
    ignorados: rotulo sem CNPJ."""
    por_stkkc, por_cnpj = set(), {}
    for cnpj, stkkc in conn.execute("SELECT cnpj_embarcador, stkkc_id FROM interno").fetchall():
        if stkkc is not None:
            por_stkkc.add(int(stkkc))
        por_cnpj[re.sub(r"\D", "", str(cnpj or ""))] = stkkc
    g = {"novos": [], "sem_stkkc": [], "conflitos": [], "ignorados": []}
    for cli in clientes:
        if cli["stkkc_id"] in por_stkkc:
            continue
        if not cli["cnpj"]:
            g["ignorados"].append(cli)
        elif cli["cnpj"] not in por_cnpj:
            g["novos"].append(cli)
        elif por_cnpj[cli["cnpj"]] is None:
            g["sem_stkkc"].append(cli)
        else:
            g["conflitos"].append((cli, int(por_cnpj[cli["cnpj"]])))
    return g


def pre_cadastrar(conn, cli: dict, contato: dict, ativo: bool, agora: datetime) -> None:
    momento = _fmt(agora)
    obs = f"Pré-cadastro automático {agora:%d/%m/%Y} (Stokki #stkkc-{cli['stkkc_id']})"
    if not ativo:
        obs += ", inativo: sem pedido nos últimos 30 dias"
    conn.execute(
        "INSERT INTO interno (cnpj_embarcador, stkkc_id, nome_remetente, apelido, email, fator_ponderado, habilidade, "
        "notificar_email, observacoes, criado_em, atualizado_em) VALUES (?, ?, ?, ?, ?, 1.0, NULL, ?, ?, ?, ?)",
        (cli["cnpj"], cli["stkkc_id"], cli["razao"], cli["apelido"], contato["email"] or None,
         1 if ativo else 0, obs, momento, momento))
    # sem commit aqui: cad.registrar grava os dois INSERTs juntos. interno.email
    # nasce com o e-mail da Stokki (Hugo, 17/10) e vira o do cliente quando ele
    # completa o cadastro (cad.salvar_preenchimento).
    cad.registrar(conn, cli["cnpj"], cli["stkkc_id"], cad.PENDENTE if ativo else cad.INATIVO,
                  celular=contato["celular"], email=contato["email"], agora=agora)


DIAS_REENVIO = 3
ORIGEM_GRUPO = "embarcadores_stokki"
FORCAR_EMAIL_PADRAO = "hugo@freshlogbr.com"
# retornos de convidar() que nao marcam o convite (segue pendente)
SEM_MARCAR = ("adiado", "bloqueado", "piloto")
_DESFECHOS = {"whatsapp": "Convite por WhatsApp.", "email": "Sem WhatsApp válido, convite por e-mail.",
              "nenhum": "Sem contato na Stokki, cadastrar à mão."}


def link_convite(conn, config: dict, cnpj: str) -> str:
    portal = config.get("portal_cliente") or {}
    if not portal.get("secret_key"):
        raise RuntimeError("portal_cliente.secret_key ausente no config.yaml")
    url_base = (portal.get("url_base") or "https://app.freshhub.com.br/cliente").rstrip("/")
    return f"{url_base}/definir-pin/{auth.gerar_token_definir_pin(portal['secret_key'], conn, cnpj)}"


def texto_convite(link: str) -> str:
    return f"Fresh Log: complete o cadastro da sua empresa: {link}"


def texto_grupo_novo(nome: str, razao: str, canal: str) -> str:
    return f"Embarcador novo na Stokki: {nome} ({razao[:40]}). {_DESFECHOS[canal]}"


def texto_reenvio(nome: str) -> str:
    return f"Convite reenviado a {nome}: ainda sem cadastro depois de {DIAS_REENVIO} dias."


def texto_conflito(cli: dict, outro: int) -> str:
    return (f"Stokki #{cli['stkkc_id']} ({cli['apelido'] or cli['razao'][:30]}): CNPJ "
            f"{auth.formatar_cnpj(cli['cnpj'])} já está em interno com stkkc_id {outro}. Conferir.")


def texto_sem_sender(nome: str) -> str:
    return f"Embarcador novo na Stokki: {nome}. Não consegui o remetente na Vuupt; tento de novo na próxima hora."


def texto_inativos(n: int) -> str:
    return f"{n} embarcador(es) da Stokki sem pedido nos últimos {DIAS_ATIVO} dias entraram como inativos (sem convite)."


def _forcar_email(config: dict) -> str:
    """forcar_destino_email do config: ausente = Hugo; '' explicito = envio real."""
    return str((config.get("cadastro_embarcador") or {}).get("forcar_destino_email", FORCAR_EMAIL_PADRAO) or "").strip()


def _forcar_whatsapp(config: dict) -> str:
    """forcar_destino_whatsapp do config; ausente vale whatsapp_notificacoes.clientes.forcar_destino."""
    cfg_cad = config.get("cadastro_embarcador") or {}
    if "forcar_destino_whatsapp" in cfg_cad:
        return str(cfg_cad["forcar_destino_whatsapp"] or "").strip()
    return str(nw._cfg_clientes(config).get("forcar_destino") or "").strip()


def destino_email(config: dict, email: str, destino_forcado: str | None = None) -> tuple[list[str], bool]:
    """Ausente = Hugo; envio real so com forcar_destino_email: '' explicito.
    `destino_forcado` (tela de Notificacoes) troca isso so nesta chamada:
    e-mail = copia de piloto, '' = envio real; None = vale o config."""
    forcar = _forcar_email(config) if destino_forcado is None else str(destino_forcado or "").strip()
    return ([forcar], True) if forcar else ([email], False)


def enviar_convite_email(config: dict, nome: str, cnpj: str, email: str, link: str, enviar=None,
                         destino_forcado: str | None = None) -> bool:
    from email_utils import envelope_html
    if enviar is None:
        from email_utils import enviar_email as enviar
    destinos, teste = destino_email(config, email, destino_forcado)
    nome_html, cnpj_fmt = html_lib.escape(nome), auth.formatar_cnpj(cnpj)
    aviso = (f"<p style='background:#FEF3C7;border:1px solid #FCD34D;border-radius:7px;padding:10px 12px;font-size:13px'>"
             f"<b>Convite pra repassar.</b> Pré-cadastro automático de <b>{nome_html}</b> (CNPJ {cnpj_fmt}). "
             f"E-mail da Stokki: {html_lib.escape(email)}.</p>") if teste else ""
    corpo = envelope_html(
        aviso +
        f"<p>Olá, <strong>{nome_html}</strong>.</p>"
        f"<p>A Fresh Log abriu o cadastro da sua empresa (CNPJ {cnpj_fmt}). Falta pouco: defina seu PIN e informe "
        f"o tipo de carga e os contatos para os avisos de entrega.</p>"
        f"<p style='margin:24px 0'><a href='{link}' style='background:#0EA575;color:#fff;padding:12px 22px;"
        f"border-radius:7px;text-decoration:none;font-weight:700'>Completar cadastro</a></p>"
        f"<p style='color:#6B7280;font-size:13px'>O link vale por 24 horas e só funciona uma vez.</p>",
        rodape="Fresh Log · Portal de acompanhamento de entregas")
    assunto = (f"[Convite pra repassar] {nome} · pré-cadastro na Fresh Log" if teste
               else "Fresh Log · Complete o cadastro da sua empresa")
    return bool(enviar(destinos, assunto, corpo, config.get("email", {})))


def _ja_avisado(conn, assinatura: str) -> bool:
    conn.execute(nw._SCHEMA)
    return conn.execute("SELECT 1 FROM notificacoes_whatsapp WHERE origem = ? AND assinatura = ? "
                        "AND situacao = 'enviado' LIMIT 1", (ORIGEM_GRUPO, assinatura)).fetchone() is not None


def avisar_grupo(conn, config: dict, texto: str, assinatura: str, modo_teste: bool = False, **kw) -> str:
    """Grupo ALERTAS, uma vez por evento (a janela de 2 h do despachar nao basta)."""
    if _ja_avisado(conn, assinatura):
        return "repetido"
    return nw.despachar(config, ORIGEM_GRUPO, "cadastro", texto[:nw.MAX_MENSAGEM], assinatura,
                        modo_teste=modo_teste, conn=conn, **kw)


def _destino_forcado(d) -> tuple[str | None, bool]:
    """(destino_forcado, piloto) da decisao da tela. Decisao do config (ou
    modo teste, d None): None, vale o forcar_destino do config."""
    if d is None or d.origem != "tela":
        return None, False
    piloto = d.acao == "PILOTO"
    return (d.destino_piloto if piloto else ""), piloto


def convidar(conn, config: dict, cnpj: str, agora: datetime, modo_teste: bool = False, enviar_email=None,
             reenvio: bool = False, db_path=None, **kw) -> str:
    """Convite pelo melhor canal: whatsapp | email | nenhum | adiado | bloqueado
    | piloto. Adiado (teto, gateway ou SMTP fora) nao grava nada: tenta na
    proxima rodada. Bloqueado (tela de Notificacoes barrou os canais) e piloto
    (so a copia ao piloto saiu) tambem nao gravam: o convite segue pendente.
    `db_path`: banco do controle da tela de Notificacoes (so nos testes)."""
    c = cad.buscar(conn, cnpj)
    emb = conn.execute("SELECT apelido, nome_remetente, email FROM interno WHERE cnpj_embarcador = ?",
                       (c["cnpj_embarcador"],)).fetchone()
    nome = emb[0] or emb[1] or c["cnpj_embarcador"]
    email = (c["email_stokki"] or emb[2] or "").split(",")[0].strip()
    link = link_convite(conn, config, c["cnpj_embarcador"])
    numero = celular_whatsapp(c["celular_stokki"])
    cnpj_emb = c["cnpj_embarcador"]
    d_wpp = d_email = None   # modo teste ignora a tela
    if not modo_teste:
        d_wpp = decidir("convite_embarcador", cnpj_emb, canal="whatsapp", db_path=db_path,
                        fallback_acao=acao_do_config(nw.clientes_ligado(config), _forcar_whatsapp(config)))
        forcar_email = _forcar_email(config)
        d_email = decidir("convite_embarcador", cnpj_emb, canal="email", db_path=db_path,
                          fallback_acao=acao_do_config(True, forcar_email), fallback_piloto=forcar_email)
    wpp_barrado = d_wpp is not None and d_wpp.origem == "tela" and d_wpp.acao == "NAO"
    email_barrado = d_email is not None and d_email.origem == "tela" and d_email.acao == "NAO"
    forcado_wpp, wpp_piloto = _destino_forcado(d_wpp)
    forcado_email, email_piloto = _destino_forcado(d_email)
    canal = None
    if numero and not wpp_barrado:
        if wpp_piloto and limite_piloto.ja_enviado_hoje("piloto_convite_whatsapp", cnpj_emb, db_path):
            return "piloto"   # a copia de hoje ja saiu (e o convite iria por aqui)
        assinatura = f"cadastro:{c['cnpj_embarcador']}:{'reenvio' if reenvio else 'convite'}"
        situacao = nw.avisar_cliente_cadastro(c["cnpj_embarcador"], numero, texto_convite(link), assinatura, config,
                                              modo_teste=modo_teste, conn=conn, agora=agora,
                                              destino_forcado=forcado_wpp, **kw)
        if situacao == "enviado" and wpp_piloto:
            limite_piloto.registrar_enviado("piloto_convite_whatsapp", cnpj_emb, db_path)
            logger.info(f"Convite de {nome}: copia de piloto -> {forcado_wpp}; embarcador nao convidado.")
            return "piloto"
        if situacao in ("enviado", "modo_teste"):
            canal = "whatsapp"
            if situacao == "enviado":
                try:
                    prefs = pn.ler(conn, c["cnpj_embarcador"])
                    if not prefs["whatsapp"]:
                        pn.salvar(conn, c["cnpj_embarcador"], prefs["emails"], {}, "pre-cadastro", whatsapp=numero)
                except Exception as exc:
                    logger.warning(f"WhatsApp de {nome} nao guardado nas preferencias ({exc}): convite segue registrado.")
        elif situacao in ("nao_enviado", "indeterminado", "falhou"):
            logger.info(f"Convite de {nome} adiado (WhatsApp: {situacao}).")
            return "adiado"
        elif situacao == "desligado":
            logger.info(f"WhatsApp de clientes desligado: convite de {nome} vai por e-mail.")
    if canal is None and ((email and email_barrado) or (not email and wpp_barrado)):
        logger.info(f"Convite de {nome} barrado pela tela de Notificacoes; segue pendente.")
        return "bloqueado"
    if canal is None and email:
        if modo_teste:
            logger.info(f"[MODO TESTE] convite por e-mail pra {destino_email(config, email)[0]} ({nome}).")
            canal = "email"
        else:
            if email_piloto and limite_piloto.ja_enviado_hoje("piloto_convite_email", cnpj_emb, db_path):
                return "piloto"
            if not enviar_convite_email(config, nome, c["cnpj_embarcador"], email, link, enviar_email,
                                        destino_forcado=forcado_email):
                logger.info(f"Convite de {nome} adiado (e-mail falhou).")
                return "adiado"
            if email_piloto:
                limite_piloto.registrar_enviado("piloto_convite_email", cnpj_emb, db_path)
                logger.info(f"Convite de {nome}: copia de piloto -> {forcado_email}; embarcador nao convidado.")
                return "piloto"
            canal = "email"
    canal = canal or "nenhum"
    if not modo_teste:
        if reenvio:
            cad.atualizar(conn, cnpj, agora, situacao=cad.REENVIADO, reenvio_em=_fmt(agora), canal_convite=canal)
        else:
            cad.atualizar(conn, cnpj, agora, situacao=cad.CONVIDADO if canal != "nenhum" else cad.SEM_CONTATO,
                          canal_convite=canal, convite_enviado_em=_fmt(agora))
    return canal


def reenviar_vencidos(conn, config: dict, agora: datetime, modo_teste: bool = False, enviar_email=None, **kw) -> int:
    """CONVIDADO ha mais de DIAS_REENVIO dias sem PIN definido: reenvia uma vez."""
    limite = _fmt(agora - timedelta(days=DIAS_REENVIO))
    rows = conn.execute("""
        SELECT c.cnpj_embarcador, COALESCE(i.apelido, i.nome_remetente, c.cnpj_embarcador)
        FROM cadastro_embarcador c
        LEFT JOIN interno i ON i.cnpj_embarcador = c.cnpj_embarcador
        LEFT JOIN clientes_portal p ON p.cnpj = c.cnpj_embarcador
        WHERE c.situacao = ? AND c.convite_enviado_em <= ? AND p.pin_hash IS NULL
    """, (cad.CONVIDADO, limite)).fetchall()
    enviados = 0
    for cnpj, nome in rows:
        try:
            canal = convidar(conn, config, cnpj, agora, modo_teste, enviar_email, reenvio=True, **kw)
        except Exception as exc:
            logger.warning(f"Reenvio de {nome} falhou (segue pro proximo): {exc}")
            continue
        if canal in SEM_MARCAR or canal == "nenhum":
            continue
        enviados += 1
        avisar_grupo(conn, config, texto_reenvio(nome), f"reenvio:{cnpj}", modo_teste, **kw)
    return enviados


def executar(config: dict, conn, clientes: list[dict], ler_contato, vuupt, agora: datetime | None = None,
             modo_teste: bool = False, enviar_email=None, **kw) -> dict:
    """Uma rodada. `clientes` vem de clientes_do_select; `ler_contato(id)`
    devolve o HTML da tela de edicao do cliente na Stokki. `kw` (ex.: dormir)
    so nos testes."""
    agora = agora or datetime.now()
    cad.garantir_tabela(conn)
    resumo = {"novos": 0, "ativos": 0, "inativos": 0, "convites": {}, "reenvios": 0, "conflitos": 0,
              "sem_sender": 0, "stkkc_preenchido": 0}
    grupos = separar(clientes, conn)

    for cli in grupos["ignorados"]:
        logger.warning(f"Stokki #{cli['stkkc_id']} ({cli['razao']}): rotulo sem CNPJ, ignorado.")
    for cli in grupos["sem_stkkc"]:
        resumo["stkkc_preenchido"] += 1
        logger.info(f"Stokki #{cli['stkkc_id']}: CNPJ {cli['cnpj']} ja em interno sem stkkc_id, preenchendo.")
        if not modo_teste:
            conn.execute("UPDATE interno SET stkkc_id = ?, atualizado_em = ? WHERE cnpj_embarcador = ?",
                         (cli["stkkc_id"], _fmt(agora), cli["cnpj"]))
            conn.commit()
    for cli, outro in grupos["conflitos"]:
        resumo["conflitos"] += 1
        avisar_grupo(conn, config, texto_conflito(cli, outro), f"conflito:{cli['stkkc_id']}", modo_teste, **kw)

    novos_inativos = 0
    for cli in grupos["novos"]:
        resumo["novos"] += 1
        try:
            try:
                contato = extrair_contato(ler_contato(cli["stkkc_id"]) or "")
            except Exception as exc:
                logger.warning(f"Contato do cliente #{cli['stkkc_id']} nao lido na Stokki ({exc}): segue sem contato.")
                contato = extrair_contato("")
            ativo = tem_pedido_recente(conn, cli["cnpj"], agora)
            if modo_teste:
                resumo["ativos" if ativo else "inativos"] += 1
                logger.info(f"[MODO TESTE] novo #{cli['stkkc_id']} {cli['apelido'] or cli['razao']} ({cli['cnpj']}): "
                            f"{'ativo' if ativo else 'inativo'}, sender do nucleo {sender_do_nucleo(conn, cli['cnpj'])}, "
                            f"celular {'valido' if celular_whatsapp(contato['celular']) else 'invalido'}, "
                            f"e-mail {contato['email'] or '-'}")
                continue
            pre_cadastrar(conn, cli, contato, ativo, agora)
            if not ativo:
                novos_inativos += 1
        except Exception as exc:
            conn.rollback()
            logger.warning(f"Pre-cadastro de {cli['cnpj']} falhou nesta rodada ({exc}): tenta na proxima.")
            continue
    if novos_inativos:
        avisar_grupo(conn, config, texto_inativos(novos_inativos), f"inativos:{_fmt(agora)[:13]}", modo_teste, **kw)

    pendentes = conn.execute("SELECT cnpj_embarcador, situacao FROM cadastro_embarcador WHERE situacao IN (?, ?) "
                             "ORDER BY criado_em, cnpj_embarcador", (cad.PENDENTE, cad.INATIVO)).fetchall()
    for cnpj, situacao in pendentes:
        try:
            if situacao == cad.INATIVO and not tem_pedido_recente(conn, cnpj, agora):
                if not modo_teste:
                    resumo["inativos"] += 1
                continue
            emb = conn.execute("SELECT apelido, nome_remetente, sender_id FROM interno WHERE cnpj_embarcador = ?",
                               (cnpj,)).fetchone()
            if emb is None:
                continue
            resumo["ativos"] += 1
            nome = emb[0] or emb[1] or cnpj
            sender = emb[2] or resolver_sender(conn, cnpj, emb[1] or nome, None if modo_teste else vuupt)
            if not sender:
                resumo["sem_sender"] += 1
                avisar_grupo(conn, config, texto_sem_sender(nome), f"sem_sender:{cnpj}", modo_teste, **kw)
                continue
            if not modo_teste:
                if not emb[2]:
                    conn.execute("UPDATE interno SET sender_id = ?, notificar_email = 1, atualizado_em = ? "
                                 "WHERE cnpj_embarcador = ?", (sender, _fmt(agora), cnpj))
                    conn.commit()
                if situacao == cad.INATIVO:
                    cad.atualizar(conn, cnpj, agora, situacao=cad.PENDENTE)
            canal = convidar(conn, config, cnpj, agora, modo_teste, enviar_email, **kw)
            if canal in SEM_MARCAR:
                continue
            resumo["convites"][cnpj] = canal
            avisar_grupo(conn, config, texto_grupo_novo(nome, emb[1] or "", canal), f"novo:{cnpj}", modo_teste, **kw)
        except Exception as exc:
            logger.warning(f"Pre-cadastro de {cnpj} falhou nesta rodada ({exc}): tenta na proxima.")
            continue

    resumo["reenvios"] = reenviar_vencidos(conn, config, agora, modo_teste, enviar_email, **kw)
    return resumo


def _carregar_config() -> dict:
    import yaml
    with open(_RAIZ / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Pre-cadastro de embarcador novo da Stokki.")
    parser.add_argument("--modo-teste", action="store_true", help="so mostra o que faria, sem gravar nem enviar")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = _carregar_config()
    try:
        from stokki.auth import StokkiSession
        from stokki.pedidos import BASE_URL, listar_embarcadores
        sessao = StokkiSession(config)
        clientes = clientes_do_select(listar_embarcadores(sessao))
    except Exception as exc:
        logger.warning(f"Stokki indisponivel ({exc}): tenta na proxima rodada.")
        return 0

    def ler_contato(stkkc_id: int) -> str:
        resp = sessao.get(f"{BASE_URL}/pt-br/administrator/client/edit/{stkkc_id}")
        resp.raise_for_status()
        return resp.text

    from vuupt_client import VuuptClient
    vuupt = VuuptClient((config.get("vuupt_api") or {}).get("token", ""))
    conn = auth.conectar()
    try:
        resumo = executar(config, conn, clientes, ler_contato, vuupt, modo_teste=args.modo_teste)
    finally:
        conn.close()
    logger.info(f"Resumo: {resumo}")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
