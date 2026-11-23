# -*- coding: utf-8 -*-
"""
portal_cliente/link_liberacao.py

Link publico (sem login) da liberacao de recebimento: quando a temperatura medida na
chegada do caminhao sai da faixa, a carga fica parada e o escritorio manda este link ao
cliente; por ele o cliente ve a medicao e as fotos e autoriza ou nao o recebimento.
Mesmo padrao de link_ciencia.py: itsdangerous, segredo do portal com salt proprio. O
token carrega so o id da chegada; a resposta e gravada por wms_chegada.responder_cliente
(que e quem garante que so a primeira resposta vale e que a carga ainda aguarda decisao).
"""
from itsdangerous import BadData, URLSafeTimedSerializer

_SALT = "liberacao-recebimento"
VALIDADE_DIAS = 7

_ERRO_NOME = "Informe o seu nome (de 3 a 80 letras) para registrar a resposta."
_ROTULO_FOTO = {"CAMINHAO_ABERTO": "Caminhão aberto", "PLACA": "Placa", "NOTA_FISCAL": "Nota fiscal",
                "TERMOMETRO": "Termômetro", "CARGA": "Carga", "AVARIA": "Avaria"}


def gerar(chegada_id: int, segredo: str) -> str:
    return URLSafeTimedSerializer(segredo).dumps({"c": int(chegada_id)}, salt=_SALT)


def ler(token: str, segredo: str, validade_dias: int = VALIDADE_DIAS) -> int | None:
    """Id da chegada, ou None se adulterado, vencido ou de outra finalidade (outro salt)."""
    try:
        dados = URLSafeTimedSerializer(segredo).loads(token, salt=_SALT, max_age=validade_dias * 86400)
        return int(dados["c"])
    except (BadData, KeyError, TypeError, ValueError):
        return None


def url(chegada_id: int, segredo: str, url_base: str) -> str:
    return f"{url_base.rstrip('/')}/liberacao/{gerar(chegada_id, segredo)}"


def _graus(valor) -> str:
    return f"{float(valor):g}".replace(".", ",") + " °C"


def _quando(texto) -> str:
    from datetime import datetime
    try:
        return datetime.strptime(str(texto), "%Y-%m-%d %H:%M:%S").strftime("%d/%m/%Y %H:%M")
    except ValueError:
        return str(texto or "")


def _registro(d: dict) -> list:
    """O que ficou registrado na chegada (modo leitura), em frases."""
    frases = []
    if d["resposta_cliente"] == "AUTORIZADO":
        frases.append(f"Recebimento autorizado por {d['resposta_cliente_nome']} em {_quando(d['resposta_cliente_em'])}.")
    elif d["resposta_cliente"] == "NAO_AUTORIZADO":
        frases.append(f"Recebimento não autorizado por {d['resposta_cliente_nome']} em "
                      f"{_quando(d['resposta_cliente_em'])}.")
    if d["estado"] == "NAO_RECEBIDA":
        frases.append("A Fresh Log decidiu não receber esta carga.")
    elif d["estado"] == "LIBERADA" and d["liberacao_via"] != "CLIENTE":
        frases.append("Liberação registrada pela Fresh Log.")
    return frases or ["Esta carga não está mais aguardando decisão."]


def registrar(app, *, config: dict, secret: str, db_path=None, enviar=None):
    """Rotas /liberacao/<token>. `enviar` (falso nos testes) tem a assinatura de
    email_utils.enviar_email, que vale quando None. Sem sessao: o token da URL e a credencial."""
    import html
    import logging
    from contextlib import closing
    from pathlib import Path

    from flask import abort, redirect, render_template, request, send_file, url_for

    import auth_cliente
    from painel_agentes import wms_chegada

    logger = logging.getLogger("portal_cliente.link_liberacao")
    db = Path(db_path or auth_cliente.DB_PATH)
    pasta_fotos = db.parent / "chegadas"

    def _erro():
        return render_template("liberacao_erro.html"), 404

    def _codigos(d: dict) -> str:
        return ", ".join(r["codigo"] or f"#{r['id']}" for r in d["recebimentos"]) or f"chegada {d['id']}"

    def _pagina(token, d, *, erro=None, nome="", status=200):
        aguardando = d["estado"] == "AGUARDANDO_ESCRITORIO" and not d["resposta_cliente"]
        temperaturas = [dict(t, medida=_graus(t["medida_c"]), faixa=f"{_graus(t['faixa_min'])} a {_graus(t['faixa_max'])}",
                             classe_txt=t["classe"].capitalize()) for t in d["temperaturas"]]
        fotos = [dict(f, rotulo=_ROTULO_FOTO.get(f["tipo"], f["tipo"]),
                      url=url_for("liberacao_foto", token=token, foto_id=f["id"])) for f in d["fotos"]]
        return render_template("liberacao.html", token=token, codigos=_codigos(d), chegada_em=_quando(d["chegada_em"]),
                               temperaturas=temperaturas, fotos=fotos, aguardando=aguardando,
                               registro=[] if aguardando else _registro(d), erro=erro, nome=nome), status

    def _chegada(conn, chegada_id):
        try:
            return wms_chegada.detalhe(conn, chegada_id)
        except wms_chegada.ErroChegada:
            return None

    def _avisar_escritorio(conn, d: dict):
        """Aviso interno da resposta. Falha aqui nunca desfaz nem esconde a resposta gravada: nao
        registra o envio e a varredura de pendentes do painel cobre."""
        try:
            autorizou = d["resposta_cliente"] == "AUTORIZADO"
            codigos = _codigos(d)
            assunto = f"Cliente {'autorizou' if autorizou else 'não autorizou'} o recebimento: {codigos}"
            corpo = (f"<p>{html.escape(d['resposta_cliente_nome'])} respondeu em "
                     f"{html.escape(_quando(d['resposta_cliente_em']))}: "
                     f"<strong>{'autorizou' if autorizou else 'não autorizou'}</strong> o recebimento de "
                     f"{html.escape(codigos)}.</p>"
                     "<p>Entre no <strong>/wms</strong> do painel para decidir o que fazer com a carga.</p>")
            destino = (config.get("notificacao_chegada_recebimento") or {}).get("email_escritorio") \
                or "entregas@freshlogbr.com"
            if enviar is None:
                import email_utils
                enviar_ = email_utils.enviar_email
            else:
                enviar_ = enviar
            if enviar_([destino], assunto, corpo, config.get("email", {})):
                wms_chegada.registrar_envio(conn, f"escritorio-RESPOSTA_CLIENTE-{d['id']}")
        except Exception:  # noqa: BLE001
            logger.exception(f"liberacao {d['id']}: resposta gravada, mas o aviso ao escritorio falhou")

    @app.route("/liberacao/<token>")
    def liberacao_pagina(token):
        chegada_id = ler(token, secret)
        if chegada_id is None:
            return _erro()
        with closing(wms_chegada.conectar(db)) as conn:
            d = _chegada(conn, chegada_id)
        return _erro() if d is None else _pagina(token, d)

    @app.route("/liberacao/<token>/responder", methods=["POST"])
    def liberacao_responder(token):
        chegada_id = ler(token, secret)
        if chegada_id is None:
            return _erro()
        resposta = request.form.get("resposta")
        nome = " ".join((request.form.get("nome") or "").split())
        with closing(wms_chegada.conectar(db)) as conn:
            d = _chegada(conn, chegada_id)
            if d is None:
                return _erro()
            if resposta not in ("autorizo", "nao_autorizo") or len(nome) > 80:
                return _pagina(token, d, erro=_ERRO_NOME if resposta in ("autorizo", "nao_autorizo")
                               else "Escolha uma das duas respostas.", nome=nome[:80], status=400)
            try:
                d = wms_chegada.responder_cliente(conn, chegada_id, nome, resposta == "autorizo")
            except wms_chegada.ErroChegada as e:
                atual = _chegada(conn, chegada_id)
                if atual and atual["estado"] == "AGUARDANDO_ESCRITORIO" and not atual["resposta_cliente"]:
                    return _pagina(token, atual, erro=str(e), nome=nome, status=400)   # nome curto
                return redirect(url_for("liberacao_pagina", token=token), code=303)    # ja decidido
            logger.info(f"liberacao {chegada_id}: {d['resposta_cliente']} por {nome}")
            _avisar_escritorio(conn, d)
        return redirect(url_for("liberacao_pagina", token=token), code=303)

    @app.route("/liberacao/<token>/foto/<int:foto_id>")
    def liberacao_foto(token, foto_id):
        chegada_id = ler(token, secret)
        if chegada_id is None:
            abort(404)
        try:
            with closing(wms_chegada.conectar(db)) as conn:
                caminho = wms_chegada.caminho_foto(conn, chegada_id, foto_id, pasta_fotos)
            resposta = send_file(caminho, mimetype="image/jpeg", max_age=0)
        except (wms_chegada.ErroChegada, OSError):
            abort(404)
        resposta.headers["Cache-Control"] = "private, no-store"
        return resposta
