# -*- coding: utf-8 -*-
"""
portal_cliente/link_ciencia.py

Link publico da pagina de ciencia do dedicado fora do dia fixo, sem login
(spec docs/superpowers/specs/2026-11-17-ciencia-dedicado-dia-fixo-design.md,
secao 4). Mesmo padrao de link_canhoto.py: itsdangerous, segredo do portal
com salt proprio. O token e por embarcador (carrega so o sender_id) e vale
30 dias; a pagina lista os pedidos PENDENTE daquele sender_id.

Pagina (registrar, chamado em app.py): GET /ciencia/<token> lista os PENDENTE
e os respondidos dos ultimos 30 dias; POST .../ciente e .../reagendar (so a
data, pelo alterar_entrega, pra um dos proximos 4 dias de visita da regiao).
Nao depende de portal_cliente.alteracao_entrega. No piloto (decisao do e-mail
na tela de Notificacoes = PILOTO) valida tudo e simula: nao grava, nao chama a
Vuupt, nao tira o dedicado. O cliente teste do portal sempre age de verdade.
"""
from datetime import date, datetime

from itsdangerous import BadData, URLSafeTimedSerializer

_SALT = "ciencia-dedicado"
VALIDADE_DIAS = 30


def gerar(sender_id: int, segredo: str) -> str:
    return URLSafeTimedSerializer(segredo).dumps({"r": int(sender_id)}, salt=_SALT)


def ler(token: str, segredo: str, validade_dias: int = VALIDADE_DIAS) -> int | None:
    """sender_id, ou None se adulterado, vencido ou de outra finalidade (outro salt)."""
    try:
        dados = URLSafeTimedSerializer(segredo).loads(token, salt=_SALT, max_age=validade_dias * 86400)
        return int(dados["r"])
    except (BadData, KeyError, TypeError, ValueError):
        return None


def url(sender_id: int, segredo: str, url_base: str) -> str:
    return f"{url_base.rstrip('/')}/ciencia/{gerar(sender_id, segredo)}"


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
              avaliar_servico=None, chat_mensagem=None):
    """Rotas /ciencia/<token>. Em producao os falsos ficam None e valem os reais:
    deps_fabrica(config, por) = alterar_entrega.dependencias_reais; buscar_servico(id)
    = GET da Vuupt; avaliar_servico(conn, servico) = mesma avaliacao da rota logada
    de alteracao (app._avaliar_alteracao); chat_mensagem(chamado_id, texto) = chat
    do portal. Sem sessao: o token da URL e a credencial (como /c/<token>)."""
    import logging
    from contextlib import closing

    from flask import redirect, render_template, request, url_for

    import alterar_entrega as ae
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
    if chat_mensagem is None:
        chat_mensagem = ncd.canais_reais(config, db_path=db_path).chat_mensagem

    def _avaliar_real(conn, servico):
        # mesmo que app._avaliar_alteracao (mora no app.py, que nao pode ser importado daqui)
        import cancelamento
        codigos = pedidos_dedicados.codigos_do_servico(servico)
        servicos, rotas = cancelamento.servicos_do_pedido(conn, dados.codigo_base(codigos[0])) if codigos else ([], {})
        status_nucleo = next((s.get("status") for s in servicos if s.get("vuupt_service_id") == servico.get("id")), None)
        lalamove = int((config.get("lalamove") or {}).get("agent_id_vuupt") or 0)
        return ae.avaliar(servico, rotas, status_nucleo, lalamove)

    avaliar_servico = avaliar_servico or _avaliar_real

    def _mapa_horarios() -> dict:
        # mesmo que app._mapa_horarios: horario de recebimento da planilha BD_CLIENTES
        try:
            from regras.complexidade_entrega import carregar_horarios
            caminho = (config.get("complexidade_entrega") or {}).get("planilha", "")
            return carregar_horarios(caminho) if caminho else {}
        except Exception as e:  # noqa: BLE001 -- sem a planilha vale o ajuste manual / padrao
            logger.warning(f"ciencia: horarios da planilha indisponiveis: {e}")
            return {}

    def _piloto(cnpj) -> bool:
        if ae._digitos(cnpj) == CNPJ_CLIENTE_TESTE:
            return False
        return ncd.decisao_email(cnpj, config, db_path=db_path).acao == "PILOTO"

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
        elif r["situacao"] == cd.REAGENDADO:
            rotulo = f"Reagendado para {_br(r['data_nova'])}"
        else:
            rotulo = f"Encerrado pelo sistema ({r.get('motivo_encerramento') or 'sem motivo'})"
        quando = ""
        if r.get("respondido_em"):
            quando = datetime.strptime(r["respondido_em"], "%Y-%m-%d %H:%M:%S").strftime("%d/%m/%Y %H:%M")
        return dict(r, rotulo=rotulo, quando=quando,
                    quem=r.get("respondido_por") if r["situacao"] != cd.ENCERRADO else "")

    def _erro():
        return render_template("ciencia_erro.html"), 404

    def _pagina(token, sender_id, *, aviso=None, erro=None, abrir=None, nome="", status=200):
        with closing(cd.conectar(db)) as conn:
            pendentes = cd.pendentes_do_sender(conn, sender_id)
            respondidos = [r for r in cd.do_sender(conn, sender_id) if r["situacao"] != cd.PENDENTE]
        linhas = pendentes + respondidos
        cnpj = next((r["cnpj"] for r in linhas if r.get("cnpj")), None)
        return render_template("ciencia.html", token=token, pendentes=[_vista_pendente(p) for p in pendentes],
                               respondidos=[_vista_respondido(r) for r in respondidos],
                               piloto=bool(linhas) and _piloto(cnpj), aviso=aviso, erro=erro, abrir=abrir,
                               nome=nome), status

    def _ir(token, resultado, ciencia_id=None, data=None):
        return redirect(url_for("ciencia_pagina", token=token, r=resultado, c=ciencia_id, d=data), code=303)

    def _aviso(sender_id, resultado, ciencia_id, data_txt):
        """Aviso depois do POST, so de chaves fixas: nada da query string vira texto."""
        if resultado == "ja_respondido":
            return "Este pedido já foi respondido."
        if resultado not in ("ciente", "reagendado"):
            return None
        with closing(cd.conectar(db)) as conn:
            ciencia = cd.buscar(conn, ciencia_id) if ciencia_id else None
        if not ciencia or ciencia["sender_id"] != sender_id:
            return "Resposta recebida."
        codigo = ciencia["codigo"]
        if resultado == "ciente":
            if ciencia["situacao"] == cd.CIENTE:
                return f"Pedido {codigo}: ciência registrada em nome de {ciencia['respondido_por']}. Obrigado!"
            return f"Pedido {codigo}: ciência simulada."
        if ciencia["situacao"] == cd.REAGENDADO:
            return f"Pedido {codigo}: entrega reagendada para {_br(ciencia['data_nova'])}. Obrigado!"
        try:
            dia = date.fromisoformat(data_txt or "").strftime("%d/%m/%Y")
        except ValueError:
            dia = "a data escolhida"
        return f"Pedido {codigo}: reagendamento simulado para {dia}."

    def _pendente_do_form(conn, sender_id):
        try:
            ciencia = cd.buscar(conn, int(request.form.get("ciencia_id") or ""))
        except ValueError:
            return None
        if not ciencia or ciencia["sender_id"] != sender_id or ciencia["situacao"] != cd.PENDENTE:
            return None
        return ciencia

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
                        "config": config, "mapa_horarios": _mapa_horarios(), "empresa_nome": ""}
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
        sender_id = ler(token, secret)
        if sender_id is None:
            return _erro()
        aviso = _aviso(sender_id, request.args.get("r"), request.args.get("c", type=int), request.args.get("d"))
        return _pagina(token, sender_id, aviso=aviso)

    @app.route("/ciencia/<token>/ciente", methods=["POST"])
    def ciencia_ciente(token):
        sender_id = ler(token, secret)
        if sender_id is None:
            return _erro()
        with closing(cd.conectar(db)) as conn:
            ciencia = _pendente_do_form(conn, sender_id)
            if not ciencia:
                return _ir(token, "ja_respondido")
            nome = _nome_do_form()
            if not nome:
                return _pagina(token, sender_id, erro=_ERRO_NOME, status=400)
            if _piloto(ciencia["cnpj"]):
                return _ir(token, "ciente", ciencia["id"])
            agora = datetime.now()
            if not cd.responder(conn, ciencia["id"], cd.CIENTE, nome, request.remote_addr, agora):
                return _ir(token, "ja_respondido")
            logger.info(f"ciencia {ciencia['codigo']}: CIENTE por {nome}")
            _postar_no_chat(conn, ciencia, f"{nome} confirmou ciência da entrega dedicada do pedido "
                                           f"{ciencia['codigo']} em {agora:%d/%m %H:%M}.")
        return _ir(token, "ciente", ciencia["id"])

    @app.route("/ciencia/<token>/reagendar", methods=["POST"])
    def ciencia_reagendar(token):
        sender_id = ler(token, secret)
        if sender_id is None:
            return _erro()
        with closing(cd.conectar(db)) as conn:
            ciencia = _pendente_do_form(conn, sender_id)
            if not ciencia:
                return _ir(token, "ja_respondido")
            nome = _nome_do_form()
            if not nome:
                return _pagina(token, sender_id, erro=_ERRO_NOME, abrir=ciencia["id"], status=400)
            datas = _datas(ciencia)
            if not datas:
                return _pagina(token, sender_id, erro=f"Este pedido não pode ser reagendado por aqui. {ae.CHAT}",
                               nome=nome, status=409)
            try:
                data = date.fromisoformat(request.form.get("data") or "")
            except ValueError:
                data = None
            if data not in datas:
                return _pagina(token, sender_id, erro=_ERRO_DATA, abrir=ciencia["id"], nome=nome, status=400)
            if _piloto(ciencia["cnpj"]):
                return _ir(token, "reagendado", ciencia["id"], data.isoformat())
            recusa = _reagendar(conn, ciencia, data, nome)
            if recusa:
                logger.info(f"ciencia {ciencia['codigo']}: reagendamento recusado ({recusa})")
                return _pagina(token, sender_id, erro=recusa, abrir=ciencia["id"], nome=nome, status=409)
            agora = datetime.now()
            try:
                pedidos_dedicados.remover(conn, codigo_pedido=ciencia["codigo"], service_id=ciencia["service_id"],
                                          por=pedidos_dedicados.POR_CIENCIA_REAGENDADO)
                cd.responder(conn, ciencia["id"], cd.REAGENDADO, nome, request.remote_addr, agora, data_nova=data)
            except Exception:  # noqa: BLE001 -- a data ja mudou na Vuupt; a rotina do timer encerra a ciencia
                logger.exception(f"ciencia {ciencia['codigo']}: reagendado na Vuupt, mas o registro falhou")
            logger.info(f"ciencia {ciencia['codigo']}: REAGENDADO para {data.isoformat()} por {nome}")
            _postar_no_chat(conn, ciencia, f"{nome} reagendou o pedido {ciencia['codigo']} para "
                                           f"{data:%d/%m/%Y} (dia de visita da região) em {agora:%d/%m %H:%M}.")
        return _ir(token, "reagendado", ciencia["id"])
