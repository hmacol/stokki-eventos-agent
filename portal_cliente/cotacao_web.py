# -*- coding: utf-8 -*-
"""
portal_cliente/cotacao_web.py

Rotas HTTP da calculadora de frete dedicado (pedido do Hugo, 11/09).
Registradas em app.py por `registrar(app, ...)`, mesmo padrão do
chamados_web.py. Regras, banco, e-mail e PDF em cotacao.py.

    GET  /cotacao                              tela da calculadora + histórico
    GET  /api/cotacao/cep/<cep>                preenche o endereço pelo CEP
    POST /api/cotacao/lista                    upload .xlsx/.csv -> paradas
    GET  /api/cotacao/modelo-lista             modelo .xlsx
    POST /api/cotacao/calcular                 calcula e registra
    GET  /api/cotacao/historico                cotações do cliente
    GET  /api/cotacao/<id>                     uma cotação
    POST /api/cotacao/<id>/proposta            e-mail com PDF + botão de aceite
    POST /api/cotacao/<id>/aceitar             aceite logado
    GET  /cotacao/<id>/pdf                     PDF da proposta
    GET|POST /cotacao/aceite/<token>           aceite pelo link do e-mail (PÚBLICO)

Oculta do cliente (Hugo, 17/09): com portal_cliente.cotacao.visivel_cliente
desligado (padrão), só a equipe Fresh Log operando em nome do cliente vê a
tela; pro cliente o link some e as rotas logadas dão 404. O aceite pelo link
do e-mail continua valendo (proposta enviada pela equipe).
"""
import logging
from functools import wraps

from flask import abort, g, jsonify, redirect, render_template, request, send_file, url_for

import auth_cliente as auth
import cotacao as ct

logger = logging.getLogger("portal_cliente.cotacao_web")

_NIVEIS_EQUIPE_ENVIA = ("total", "operador")


def registrar(app, *, requer_cliente, exige_mesma_origem, config: dict, secret: str, url_base: str):

    def _regras() -> dict:
        return ct.regras_de(config)

    def _quem() -> str:
        if g.get("equipe"):
            return f"equipe:{g.equipe.get('usuario', '?')}"
        return "cliente"

    def _pode_agir() -> bool:
        return not (g.get("equipe") and g.equipe.get("nivel") not in _NIVEIS_EQUIPE_ENVIA)

    def _exige_pode_agir():
        if not _pode_agir():
            abort(403)

    def _erro(e, status=400):
        return jsonify({"erro": str(e)}), status

    def _emails_cliente(conn) -> list[str]:
        emb = auth.buscar_embarcador(conn, g.cliente["cnpj"])
        return list(emb["emails"]) if emb else []

    def _url_aceite(cot: dict) -> str:
        return f"{url_base}/cotacao/aceite/{ct.gerar_token_aceite(secret, cot)}"

    def _url_portal(cot: dict) -> str:
        return f"{url_base}/cotacao?ver={cot['id']}"

    def _visivel() -> bool:
        return bool(g.get("equipe")) or bool(_regras().get("visivel_cliente"))

    def exige_visivel(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if not _visivel():
                abort(404)
            return f(*args, **kwargs)
        return wrapper

    @app.context_processor
    def _cotacao_globais():
        return {"cotacao_visivel": _visivel()}

    # ── Tela ────────────────────────────────────────────────────────────────

    @app.route("/cotacao")
    @requer_cliente
    @exige_visivel
    def cotacao_pagina():
        conn = ct.conectar()
        try:
            emails = _emails_cliente(conn)
        finally:
            conn.close()
        return render_template("cotacao.html", regras=ct.regras_publicas(_regras()), emails_cliente=emails,
                               pode_agir=_pode_agir(), ver_inicial=request.args.get("ver", type=int))

    # ── API ─────────────────────────────────────────────────────────────────

    @app.route("/api/cotacao/cep/<cep>")
    @requer_cliente
    @exige_visivel
    def cotacao_cep(cep):
        d = ct.normalizar_cep(cep)
        if not d:
            return _erro("CEP inválido: use 8 dígitos.")
        achado = ct.consultar_cep(d)
        if not achado:
            return _erro("CEP não encontrado. Preencha o endereço manualmente.", 404)
        return jsonify(achado)

    @app.route("/api/cotacao/modelo-lista")
    @requer_cliente
    @exige_visivel
    def cotacao_modelo_lista():
        import io
        return send_file(io.BytesIO(ct.modelo_lista()), as_attachment=True, download_name="modelo_entregas_cotacao.xlsx",
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    @app.route("/api/cotacao/lista", methods=["POST"])
    @requer_cliente
    @exige_visivel
    @exige_mesma_origem
    def cotacao_lista():
        arq = request.files.get("arquivo")
        if not arq or not arq.filename:
            return _erro("Envie um arquivo .xlsx ou .csv.")
        try:
            paradas = ct.ler_lista(arq.read(), arq.filename)
            regras = _regras()
            if len(paradas) > int(regras["max_paradas"]):
                return _erro(f"A lista tem {len(paradas)} entregas; o máximo é {regras['max_paradas']}.")
            # completa pelo CEP o que faltar (sem exigir número ainda: a tela mostra pra conferir)
            for p in paradas:
                cep = ct.normalizar_cep(p.get("cep"))
                p["cep"] = ct.formatar_cep(cep) if cep else p.get("cep", "")
                if cep and not p.get("cidade"):
                    achado = ct.consultar_cep(cep)
                    if achado:
                        for k in ("logradouro", "bairro", "cidade", "uf"):
                            p.setdefault(k, achado[k])
            return jsonify({"paradas": paradas})
        except ct.ErroCotacao as e:
            return _erro(e)

    @app.route("/api/cotacao/calcular", methods=["POST"])
    @requer_cliente
    @exige_visivel
    @exige_mesma_origem
    def cotacao_calcular():
        dados = request.get_json(silent=True) or {}
        conn = ct.conectar()
        try:
            cot = ct.cotar(conn, g.cliente, dados, config, _quem())
            return jsonify({"cotacao": cot, "emails_cliente": _emails_cliente(conn), "url_pdf": url_for("cotacao_pdf", cot_id=cot["id"])})
        except ct.ForaDaTabela as e:
            return jsonify({"erro": str(e), "fora_da_tabela": True}), 422
        except ct.ErroCotacao as e:
            return _erro(e)
        except Exception as e:
            logger.exception("cotacao_calcular falhou")
            return _erro(f"Não foi possível calcular agora ({type(e).__name__}). Tente de novo.", 502)
        finally:
            conn.close()

    @app.route("/api/cotacao/historico")
    @requer_cliente
    @exige_visivel
    def cotacao_historico():
        conn = ct.conectar()
        try:
            itens = ct.listar(conn, g.cliente["cnpj"])
        finally:
            conn.close()
        for c in itens:
            c["url_pdf"] = url_for("cotacao_pdf", cot_id=c["id"]) if c.get("resultado") else None
        return jsonify({"cotacoes": itens})

    @app.route("/api/cotacao/<int:cot_id>")
    @requer_cliente
    @exige_visivel
    def cotacao_uma(cot_id):
        conn = ct.conectar()
        try:
            cot = ct.buscar(conn, cot_id, g.cliente["cnpj"])
            if not cot:
                return _erro("Cotação não encontrada.", 404)
            return jsonify({"cotacao": cot, "emails_cliente": _emails_cliente(conn),
                            "url_pdf": url_for("cotacao_pdf", cot_id=cot["id"]) if cot.get("resultado") else None})
        finally:
            conn.close()

    @app.route("/api/cotacao/<int:cot_id>/proposta", methods=["POST"])
    @requer_cliente
    @exige_visivel
    @exige_mesma_origem
    def cotacao_proposta(cot_id):
        _exige_pode_agir()
        dados = request.get_json(silent=True) or {}
        emails = [str(e).strip() for e in (dados.get("emails") or []) if str(e).strip()]
        conn = ct.conectar()
        try:
            cot = ct.buscar(conn, cot_id, g.cliente["cnpj"])
            if not cot:
                return _erro("Cotação não encontrada.", 404)
            cot = ct.enviar_proposta(conn, cot, emails, config, _url_aceite(cot), _url_portal(cot))
            return jsonify({"cotacao": cot, "enviado_para": emails,
                            "redirecionado": bool((_regras().get("forcar_destino") or "").strip())})
        except ct.ErroCotacao as e:
            return _erro(e)
        finally:
            conn.close()

    @app.route("/api/cotacao/<int:cot_id>/aceitar", methods=["POST"])
    @requer_cliente
    @exige_visivel
    @exige_mesma_origem
    def cotacao_aceitar(cot_id):
        _exige_pode_agir()
        conn = ct.conectar()
        try:
            cot = ct.buscar(conn, cot_id, g.cliente["cnpj"])
            if not cot:
                return _erro("Cotação não encontrada.", 404)
            por = g.cliente["nome"] if not g.get("equipe") else f"equipe {g.equipe.get('usuario', '?')} em nome de {g.cliente['nome']}"
            cot = ct.aceitar(conn, cot, por, "portal", config, _emails_cliente(conn))
            return jsonify({"cotacao": cot})
        except ct.ErroCotacao as e:
            return _erro(e)
        finally:
            conn.close()

    @app.route("/cotacao/<int:cot_id>/pdf")
    @requer_cliente
    @exige_visivel
    def cotacao_pdf(cot_id):
        conn = ct.conectar()
        try:
            cot = ct.buscar(conn, cot_id, g.cliente["cnpj"])
            if not cot or not cot.get("resultado"):
                abort(404)
            caminho = ct.gerar_pdf(cot, _regras(), conn)
        finally:
            conn.close()
        return send_file(caminho, as_attachment=False, download_name=f"{cot['numero']}.pdf", mimetype="application/pdf")

    # ── Aceite pelo link do e-mail (sem login; o token assinado é a prova) ──

    @app.route("/cotacao/aceite/<token>", methods=["GET", "POST"])
    def cotacao_aceite_token(token):
        conn = ct.conectar()
        try:
            v = ct.validar_token_aceite(secret, conn, token, _regras())
            cot = v["cotacao"]
            if v["estado"] == "ok" and request.method == "POST":
                nome = (request.form.get("nome") or "").strip()
                if not nome:
                    return render_template("cotacao_aceite.html", estado="ok", cotacao=cot, erro="Informe seu nome pra registrar o aceite.")
                emb = auth.buscar_embarcador(conn, cot["cnpj_embarcador"])
                cot = ct.aceitar(conn, cot, nome, "e-mail", config, list(emb["emails"]) if emb else [])
                return render_template("cotacao_aceite.html", estado="aceita_agora", cotacao=cot)
            return render_template("cotacao_aceite.html", estado=v["estado"], cotacao=cot)
        finally:
            conn.close()
