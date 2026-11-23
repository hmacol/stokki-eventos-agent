# -*- coding: utf-8 -*-
"""
portal_cliente/link_ciencia.py

Link publico da pagina de ciencia do dedicado fora do dia fixo, sem login
(spec docs/superpowers/specs/2026-11-17-ciencia-dedicado-dia-fixo-design.md,
secao 4). Mesmo padrao de link_canhoto.py: itsdangerous, segredo do portal
com salt proprio. O token vale 30 dias e carrega o sender_id, as ciencias que
cobre (as do aviso em que saiu: pedido aberto depois ganha link novo) e a origem
(canal do aviso, alerta da equipe ou painel:<usuario>), gravada com a resposta.
A pagina so mostra e so aceita resposta das ciencias do token (revisao de
seguranca 19/11: link vazado nao alcanca pedido novo).

Pagina (registrar, chamado em app.py): GET /ciencia/<token> lista os PENDENTE
e os respondidos (CIENTE/REAGENDADO; encerrado pelo sistema nao aparece) dos
ultimos 30 dias; POST .../ciente e .../reagendar (so a
data, pelo alterar_entrega, pra um dos proximos 4 dias de visita da regiao).
Nao depende de portal_cliente.alteracao_entrega. No piloto (decisao do e-mail
na tela de Notificacoes = PILOTO) valida tudo e simula: nao grava, nao chama a
Vuupt, nao tira o dedicado. O cliente teste do portal sempre age de verdade.
"""
import re
from datetime import date, datetime
from typing import NamedTuple

from itsdangerous import BadData, URLSafeTimedSerializer

_SALT = "ciencia-dedicado"
VALIDADE_DIAS = 30


class Link(NamedTuple):
    sender_id: int
    ids: frozenset
    origem: str


def gerar(sender_id: int, segredo: str, ids, origem: str) -> str:
    return URLSafeTimedSerializer(segredo).dumps(
        {"r": int(sender_id), "c": sorted({int(i) for i in ids}), "o": str(origem)[:80]}, salt=_SALT)


def ler(token: str, segredo: str, validade_dias: int = VALIDADE_DIAS) -> Link | None:
    """Link, ou None se adulterado, vencido, de outra finalidade (outro salt) ou sem ciencias."""
    try:
        dados = URLSafeTimedSerializer(segredo).loads(token, salt=_SALT, max_age=validade_dias * 86400)
        if not isinstance(dados["c"], list) or not dados["c"]:
            return None
        return Link(int(dados["r"]), frozenset(int(i) for i in dados["c"]), str(dados["o"]))
    except (BadData, KeyError, TypeError, ValueError):
        return None


def url(sender_id: int, segredo: str, url_base: str, ids, origem: str) -> str:
    return f"{url_base.rstrip('/')}/ciencia/{gerar(sender_id, segredo, ids, origem)}"


# ── Pagina de resposta (spec, secao 4) ─────────────────────────────────────────

# cliente teste do portal: age de verdade mesmo no piloto, pra provar o caminho inteiro
CNPJ_CLIENTE_TESTE = "00000000000191"
_DIAS_SEMANA = ("seg", "ter", "qua", "qui", "sex", "sáb", "dom")
_ERRO_NOME = "Informe seu nome (de 2 a 80 letras) para registrar a resposta."
_ERRO_DATA = "Escolha uma das datas de visita da lista."


def datas_de_visita(regra: dict, hoje: date, n: int = 4) -> list[date]:
    """Proximos `n` dias de visita da regiao a partir de amanha (feriado tratado por
    regioes_dia_fixo: a visita passa pro dia util seguinte)."""
    import ciencia_dedicado  # noqa: F401  (poe roteirizacao/ no sys.path)
    import regioes_dia_fixo
    datas, dia = [], hoje
    for _ in range(n):
        dia = regioes_dia_fixo.proxima_data_valida(regra, dia)
        datas.append(dia)
    return datas


def registrar(app, *, config: dict, secret: str, db_path=None, deps_fabrica=None, buscar_servico=None,
              avaliar_servico=None, chat_mensagem=None, alerta_equipe=None):
    """Rotas /ciencia/<token>. Em producao os falsos ficam None e valem os reais:
    deps_fabrica(config, por) = alterar_entrega.dependencias_reais; buscar_servico(id)
    = GET da Vuupt; avaliar_servico(conn, servico) = mesma avaliacao da rota logada
    de alteracao (alterar_entrega.avaliar_no_nucleo); chat_mensagem(chamado_id, texto)
    = chat do portal; alerta_equipe(texto) = grupo da equipe (o mesmo da rotina do
    timer). Sem sessao: o token da URL e a credencial (como /c/<token>)."""
    import logging
    from contextlib import closing

    from flask import redirect, render_template, request, url_for

    import alterar_entrega as ae
    import auth_cliente
    import ciencia_dedicado as cd
    import dados_cliente as dados
    import notificar_ciencia_dedicado as ncd
    import pedidos_dedicados
    import registro_dia_fixo  # roteirizacao/ (ciencia_dedicado poe no sys.path)

    logger = logging.getLogger("portal_cliente.link_ciencia")
    db = db_path or cd.DB_PATH
    token_vuupt = (config.get("vuupt_api") or {}).get("token", "")
    deps_fabrica = deps_fabrica or ae.dependencias_reais
    buscar_servico = buscar_servico or (
        lambda sid: dados.buscar_servico(token_vuupt, sid, incluir="checklistAnswers,customer"))
    reais = ncd.canais_reais(config, db_path=db_path)
    chat_mensagem = chat_mensagem or reais.chat_mensagem
    alerta_equipe = alerta_equipe or reais.alerta_equipe
    avaliar_servico = avaliar_servico or (lambda conn, servico: ae.avaliar_no_nucleo(conn, servico, config))

    def _piloto(cnpj, link) -> bool:
        """Simula salvo decisao ENVIAR da tela (cn.ACOES) ou cliente teste. NAO (geral ou
        tipo desligado, embarcador bloqueado, piloto sem e-mail), PILOTO e erro: simula.
        Link de copia de piloto (origem '*-piloto', a que vai pro Hugo) simula sempre, mesmo
        depois de o embarcador virar ENVIAR: so o cliente teste age por ele."""
        if ae._digitos(cnpj) == CNPJ_CLIENTE_TESTE:
            return False
        if link.origem.endswith("-piloto"):
            return True
        try:
            return ncd.decisao_email(cnpj, config, db_path=db_path).acao != "ENVIAR"
        except Exception as e:  # noqa: BLE001
            logger.warning(f"ciencia: nao leu a decisao da tela para {cnpj} ({e}); simulando")
            return True

    def _id(valor) -> int | None:
        """Inteiro positivo simples abaixo de 2**31; qualquer outra coisa e None (nao vai ao banco)."""
        texto = str(valor or "")
        if not re.fullmatch(r"[1-9][0-9]{0,9}", texto) or int(texto) >= 2 ** 31:
            return None
        return int(texto)

    def _nome_embarcador(conn, cnpj) -> str:
        try:
            return (auth_cliente.buscar_embarcador(conn, cnpj) or {}).get("nome") or ""
        except Exception:  # noqa: BLE001 -- so enfeita o aviso da equipe
            return ""

    def _br(valor) -> str:
        try:
            return date.fromisoformat(str(valor)[:10]).strftime("%d/%m/%Y")
        except ValueError:
            return str(valor or "")

    def _datas(ciencia) -> list[date]:
        regra = ncd._regra_por_nome(ciencia.get("regra_nome"))
        if not regra:
            return []
        try:
            return datas_de_visita(regra, date.today())
        except ValueError:   # regiao sem dia de visita em 4 semanas
            return []

    def _vista_pendente(p: dict) -> dict:
        valor = ("valor a confirmar pela Fresh Log" if p.get("valor_pendente") or p.get("valor") is None
                 else ncd._brl(p["valor"]))
        return dict(p, data_br=_br(p["data_entrega"]), valor_txt=valor,
                    datas=[{"iso": d.isoformat(), "rotulo": f"{_DIAS_SEMANA[d.weekday()]}, {d:%d/%m/%Y}"}
                           for d in _datas(p)])

    def _vista_respondido(r: dict) -> dict:
        if r["situacao"] == cd.CIENTE:
            rotulo = "Ciente do carro dedicado"
        else:
            rotulo = f"Reagendado para {_br(r['data_nova'])}"
        quando = ""
        if r.get("respondido_em"):
            quando = datetime.strptime(r["respondido_em"], "%Y-%m-%d %H:%M:%S").strftime("%d/%m/%Y %H:%M")
        return dict(r, rotulo=rotulo, quando=quando, quem=r.get("respondido_por"))

    def _erro():
        return render_template("ciencia_erro.html"), 404

    def _pagina(token, link, *, aviso=None, erro=None, abrir=None, nome="", status=200):
        with closing(cd.conectar(db)) as conn:
            pendentes = [p for p in cd.pendentes_do_sender(conn, link.sender_id) if p["id"] in link.ids]
            # encerrado pelo sistema nao aparece: o motivo e interno
            respondidos = [r for r in cd.do_sender(conn, link.sender_id)
                           if r["id"] in link.ids and r["situacao"] in (cd.CIENTE, cd.REAGENDADO)]
        linhas = pendentes + respondidos
        cnpj = next((r["cnpj"] for r in linhas if r.get("cnpj")), None)
        return render_template("ciencia.html", token=token, pendentes=[_vista_pendente(p) for p in pendentes],
                               respondidos=[_vista_respondido(r) for r in respondidos],
                               piloto=bool(linhas) and _piloto(cnpj, link), aviso=aviso, erro=erro, abrir=abrir,
                               nome=nome), status

    def _ir(token, resultado, ciencia_id=None, data=None):
        return redirect(url_for("ciencia_pagina", token=token, r=resultado, c=ciencia_id, d=data), code=303)

    def _aviso(link, resultado, ciencia_id, data_txt):
        """Aviso depois do POST, so de chaves fixas: nada da query string vira texto."""
        if resultado == "ja_respondido":
            return "Este pedido já foi respondido."
        if resultado == "em_andamento":
            return "Outra resposta para este pedido está sendo registrada agora. Atualize a página em instantes."
        if resultado == "ajuste_pendente":
            return "A data foi alterada. Nossa equipe vai concluir o ajuste do pedido."
        if resultado not in ("ciente", "reagendado"):
            return None
        with closing(cd.conectar(db)) as conn:
            ciencia = cd.buscar(conn, ciencia_id) if ciencia_id in link.ids else None
        if not ciencia or ciencia["sender_id"] != link.sender_id:
            return "Resposta recebida."
        codigo = ciencia["codigo"]
        if resultado == "ciente" and ciencia["situacao"] == cd.CIENTE:
            return f"Pedido {codigo}: ciência registrada em nome de {ciencia['respondido_por']}. Obrigado!"
        if resultado == "reagendado" and ciencia["situacao"] == cd.REAGENDADO:
            return f"Pedido {codigo}: entrega reagendada para {_br(ciencia['data_nova'])}. Obrigado!"
        if not _piloto(ciencia["cnpj"], link):
            return f"Pedido {codigo}: resposta recebida."
        if resultado == "ciente":
            return f"Pedido {codigo}: ciência simulada."
        try:
            dia = date.fromisoformat(data_txt or "").strftime("%d/%m/%Y")
        except ValueError:
            dia = "a data escolhida"
        return f"Pedido {codigo}: reagendamento simulado para {dia}."

    def _pendente_do_form(conn, link):
        ciencia_id = _id(request.form.get("ciencia_id"))
        ciencia = cd.buscar(conn, ciencia_id) if ciencia_id in link.ids else None
        if not ciencia or ciencia["sender_id"] != link.sender_id or ciencia["situacao"] != cd.PENDENTE:
            return None
        return ciencia

    def _navegador() -> str:
        return (request.headers.get("User-Agent") or "")[:300]

    def _nome_do_form() -> str | None:
        nome = " ".join((request.form.get("nome") or "").split())
        return nome if 2 <= len(nome) <= 80 else None

    def _postar_no_chat(conn, ciencia, texto):
        """Mensagem do sistema na conversa em que o pedido foi avisado; sem conversa,
        nada. Falha aqui nunca desfaz nem esconde a resposta ja gravada."""
        try:
            chamado = ncd._conversa_anterior(conn, [ciencia["id"]])
            if chamado:
                chat_mensagem(chamado, texto)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"ciencia {ciencia['codigo']}: resposta gravada, mas o chat falhou ({e})")

    def _reagendar(conn, ciencia, data, nome) -> str | None:
        """Aplica so a data pelo alterar_entrega. None se aplicou; senao a mensagem pro cliente."""
        hoje = date.today()
        por = f"{nome} (ciência do dedicado)"
        conn_reg = registro_dia_fixo.conectar(db)
        servico = None
        try:
            servico = buscar_servico(ciencia["service_id"]) if ciencia["service_id"] else None
            if not servico or servico.get("sender_id") != ciencia["sender_id"]:
                return f"Não foi possível consultar o pedido agora. {ae.CHAT}"
            avaliacao = avaliar_servico(conn, servico)
            if not avaliacao.pode:
                return avaliacao.motivo
            validado = ae.validar({"data": data.isoformat()}, hoje)
            plano = ae.planejar(servico, validado, avaliacao, conn_reg, hoje)
            if any(a["codigo"] == "dedicado" for a in plano.avisos):
                # a regiao do endereco atual nao bate com a da ciencia: nao tira o dedicado as cegas
                return f"Essa data não é dia de visita da região do pedido. {ae.CHAT}"
            cli = dados._desembrulhar(servico, "customer")
            contexto = {"cnpj_embarcador": ciencia["cnpj"] or "", "sender_id": ciencia["sender_id"], "por": por,
                        "destinatario_doc": ae._digitos(cli.get("code")), "destinatario_nome": cli.get("name") or "",
                        "config": config, "mapa_horarios": ae.mapa_horarios(config),
                        "empresa_nome": _nome_embarcador(conn, ciencia["cnpj"])}
            resultado = ae.aplicar(conn, servico, validado, plano, contexto, deps_fabrica(config, por))
        except ae.ErroAlteracao as e:
            return str(e)
        except Exception:  # noqa: BLE001
            logger.exception(f"ciencia {ciencia['codigo']}: reagendamento falhou")
            return f"Não foi possível reagendar agora. {ae.CHAT}"
        finally:
            conn_reg.close()
            if servico is not None:   # a Vuupt pode ter mudado mesmo se o aplicar estourou
                dados.invalidar_cache(ciencia["sender_id"])
        return None if resultado["ok"] else resultado["mensagem"]

    @app.route("/ciencia/<token>")
    def ciencia_pagina(token):
        link = ler(token, secret)
        if link is None:
            return _erro()
        aviso = _aviso(link, request.args.get("r"), _id(request.args.get("c")), request.args.get("d"))
        return _pagina(token, link, aviso=aviso)

    @app.route("/ciencia/<token>/ciente", methods=["POST"])
    def ciencia_ciente(token):
        link = ler(token, secret)
        if link is None:
            return _erro()
        with closing(cd.conectar(db)) as conn:
            ciencia = _pendente_do_form(conn, link)
            if not ciencia:
                return _ir(token, "ja_respondido")
            nome = _nome_do_form()
            if not nome:
                return _pagina(token, link, erro=_ERRO_NOME, status=400)
            if _piloto(ciencia["cnpj"], link):
                return _ir(token, "ciente", ciencia["id"])
            agora = datetime.now()
            # um reagendar em andamento (esperando a Vuupt) ganha: o ciente nao grava por cima
            if not cd.reivindicar_resposta(conn, ciencia["id"], agora):
                return _ir(token, "em_andamento")
            if not cd.responder(conn, ciencia["id"], cd.CIENTE, nome, request.remote_addr, agora,
                                origem=link.origem, navegador=_navegador()):
                return _ir(token, "ja_respondido")
            logger.info(f"ciencia {ciencia['codigo']}: CIENTE por {nome}")
            _postar_no_chat(conn, ciencia, f"{nome} confirmou ciência da entrega dedicada do pedido "
                                           f"{ciencia['codigo']} em {agora:%d/%m %H:%M}.")
        return _ir(token, "ciente", ciencia["id"])

    @app.route("/ciencia/<token>/reagendar", methods=["POST"])
    def ciencia_reagendar(token):
        link = ler(token, secret)
        if link is None:
            return _erro()
        with closing(cd.conectar(db)) as conn:
            ciencia = _pendente_do_form(conn, link)
            if not ciencia:
                return _ir(token, "ja_respondido")
            nome = _nome_do_form()
            if not nome:
                return _pagina(token, link, erro=_ERRO_NOME, abrir=ciencia["id"], status=400)
            datas = _datas(ciencia)
            if not datas:
                return _pagina(token, link, erro=f"Este pedido não pode ser reagendado por aqui. {ae.CHAT}",
                               nome=nome, status=409)
            try:
                data = date.fromisoformat(request.form.get("data") or "")
            except ValueError:
                data = None
            if data not in datas:
                return _pagina(token, link, erro=_ERRO_DATA, abrir=ciencia["id"], nome=nome, status=400)
            if _piloto(ciencia["cnpj"], link):
                return _ir(token, "reagendado", ciencia["id"], data.isoformat())
            # trava antes da Vuupt: ciente ou outra aba no meio nao gravam por cima do que a Vuupt fizer
            if not cd.reivindicar_resposta(conn, ciencia["id"], datetime.now()):
                return _ir(token, "em_andamento")
            recusa = _reagendar(conn, ciencia, data, nome)
            if recusa:
                cd.liberar_resposta(conn, ciencia["id"])
                logger.info(f"ciencia {ciencia['codigo']}: reagendamento recusado ({recusa})")
                return _pagina(token, link, erro=recusa, abrir=ciencia["id"], nome=nome, status=409)
            agora = datetime.now()
            # a data ja mudou na Vuupt: cada registro tenta por si; o que falhar vira alerta da equipe
            falhas = []
            try:
                if not cd.responder(conn, ciencia["id"], cd.REAGENDADO, nome, request.remote_addr, agora,
                                    data_nova=data, origem=link.origem, navegador=_navegador()):
                    logger.error(f"ciencia {ciencia['codigo']}: reagendado na Vuupt, mas a ciencia ja nao "
                                 f"estava PENDENTE (encerrada no meio)")
                    falhas.append("a resposta não foi gravada (a ciência saiu de pendente no meio)")
            except Exception:  # noqa: BLE001
                logger.exception(f"ciencia {ciencia['codigo']}: reagendado na Vuupt, mas a resposta nao foi gravada")
                falhas.append("a resposta não foi gravada")
            try:
                pedidos_dedicados.remover(conn, codigo_pedido=ciencia["codigo"], service_id=ciencia["service_id"],
                                          por=pedidos_dedicados.POR_CIENCIA_REAGENDADO)
            except Exception:  # noqa: BLE001
                logger.exception(f"ciencia {ciencia['codigo']}: reagendado na Vuupt, mas o dedicado nao foi removido")
                falhas.append("o dedicado não foi removido")
            logger.info(f"ciencia {ciencia['codigo']}: REAGENDADO para {data.isoformat()} por {nome}")
            _postar_no_chat(conn, ciencia, f"{nome} reagendou o pedido {ciencia['codigo']} para "
                                           f"{data:%d/%m/%Y} (dia de visita da região) em {agora:%d/%m %H:%M}.")
            if falhas:
                embarcador = " ".join(p for p in (_nome_embarcador(conn, ciencia["cnpj"]),
                                                  f"(CNPJ {ciencia['cnpj'] or '?'})") if p)
                texto = (f"Ciência do dedicado: pedido {ciencia['codigo']} de {embarcador} reagendado pelo "
                         f"embarcador para {data:%d/%m/%Y}: data alterada, mas {' e '.join(falhas)}. "
                         f"Conferir à mão.")
                try:
                    saiu, motivo = alerta_equipe(texto), "nao saiu"
                except Exception as e:  # noqa: BLE001
                    saiu, motivo = False, f"falhou ({e})"
                if not saiu:   # ultimo rastro de um cliente que ficou cobrado: vai pro log de erro
                    logger.error(f"ciencia {ciencia['codigo']}: alerta da equipe {motivo}; reagendado para "
                                 f"{data:%d/%m/%Y}, mas {' e '.join(falhas)}. Conferir a mao: {texto}")
                return _ir(token, "ajuste_pendente", ciencia["id"])
        return _ir(token, "reagendado", ciencia["id"])
