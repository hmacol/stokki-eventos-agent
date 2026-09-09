# -*- coding: utf-8 -*-
"""
portal_cliente/chamados_web.py

Rotas HTTP do atendimento no portal do cliente (widget de chat no canto da
tela de acompanhamento). Registradas em app.py por `registrar(app, ...)`
-- módulo separado de propósito, pra não engordar o app.py que outra
frente edita ao mesmo tempo. Regras e dados em chamados.py; triagem em
assistente.py.

Endpoints (todos exigem cliente logado; equipe operando como cliente NÃO
vê o widget -- o chat é do embarcador):
    GET  /api/atendimento/estado                       situação (online/almoço), chamado ativo, lista
    POST /api/atendimento/conversas                    inicia conversa com o assistente
    GET  /api/atendimento/chamados/<id>?desde=<msg>    mensagens (marca lidas, heartbeat do cliente)
    POST /api/atendimento/chamados/<id>/mensagens      texto + anexos (+ chip)
    POST /api/atendimento/chamados/<id>/acao           atendente | resolvido
    POST /api/atendimento/chamados                     deixar chamado (fora do horário)
    GET  /api/atendimento/chamados/<id>/anexos/<arq>
"""
import logging
from datetime import datetime

from flask import abort, g, jsonify, request, send_file

import assistente
import chamados as ch

logger = logging.getLogger("portal_cliente.chamados_web")


def registrar(app, *, requer_cliente, exige_mesma_origem, config: dict):

    def _cliente() -> dict:
        return g.cliente

    def _so_cliente():
        if g.get("equipe"):
            abort(403)

    def _payload_chamado(conn, chamado: dict, desde: int = 0) -> dict:
        return {"chamado": _limpar(chamado), "mensagens": ch.mensagens(conn, chamado["id"], desde)}

    def _limpar(c: dict) -> dict:
        campos = ("id", "assunto", "area", "area_rotulo", "pedido_ref", "status", "status_rotulo", "origem", "etapa_assistente",
                  "atendente", "criado_em", "atualizado_em", "ultima_msg_em", "resolvido_em", "resolvido_por", "quando",
                  "aberto", "nao_lidas", "ultima_texto", "ultima_origem_msg", "historico_enviado_em")
        return {k: c.get(k) for k in campos}

    def _anexos_do_form(chamado_id: int) -> list[dict]:
        arquivos = [(f.filename, f.read()) for f in request.files.getlist("anexos") if f and f.filename]
        return ch.guardar_anexos(chamado_id, arquivos) if arquivos else []

    def _erro(e, status=400):
        return jsonify({"erro": str(e)}), status

    @app.route("/api/atendimento/estado")
    @requer_cliente
    def atendimento_estado():
        _so_cliente()
        conn = ch.conectar()
        try:
            cli = _cliente()
            ativo = ch.chamado_ativo_cliente(conn, cli["cnpj"])
            pedido_id = request.args.get("chamado", type=int)
            if pedido_id:
                alvo = ch.buscar_chamado(conn, pedido_id, cli["cnpj"])
                if alvo:
                    ativo = alvo
            return jsonify({
                "situacao": ch.situacao_atendimento(conn, config),
                "ativo": _payload_chamado(conn, ativo) if ativo else None,
                "chamados": [_limpar(c) for c in ch.listar_chamados_cliente(conn, cli["cnpj"])],
                "nao_lidas": ch.nao_lidas_cliente(conn, cli["cnpj"]),
                "areas": [{"valor": k, "rotulo": v} for k, v in ch.AREAS.items()],
                "emails": ch.emails_do_cliente(conn, cli["cnpj"]),
                "agora": datetime.now().strftime("%H:%M"),
            })
        finally:
            conn.close()

    @app.route("/api/atendimento/conversas", methods=["POST"])
    @requer_cliente
    @exige_mesma_origem
    def atendimento_iniciar():
        _so_cliente()
        conn = ch.conectar()
        try:
            cli = _cliente()
            chamado = ch.criar_chamado(conn, cli, origem="chat", status=ch.STATUS_COM_ASSISTENTE,
                                       etapa_assistente=assistente.ETAPA_AREA)
            assistente.iniciar(conn, chamado, cli)
            chamado = ch.buscar_chamado(conn, chamado["id"])
            logger.info("chat iniciado cnpj=%s chamado=%s", cli["cnpj"], chamado["id"])
            return jsonify(_payload_chamado(conn, chamado))
        finally:
            conn.close()

    @app.route("/api/atendimento/chamados/<int:chamado_id>")
    @requer_cliente
    def atendimento_chamado(chamado_id):
        _so_cliente()
        conn = ch.conectar()
        try:
            chamado = ch.buscar_chamado(conn, chamado_id, _cliente()["cnpj"])
            if not chamado:
                abort(404)
            desde = request.args.get("desde", 0, type=int)
            ch.marcar_lidas(conn, chamado_id, "cliente")
            return jsonify({**_payload_chamado(conn, chamado, desde), "situacao": ch.situacao_atendimento(conn, config)})
        finally:
            conn.close()

    @app.route("/api/atendimento/chamados/<int:chamado_id>/mensagens", methods=["POST"])
    @requer_cliente
    @exige_mesma_origem
    def atendimento_mensagem(chamado_id):
        _so_cliente()
        conn = ch.conectar()
        try:
            cli = _cliente()
            chamado = ch.buscar_chamado(conn, chamado_id, cli["cnpj"])
            if not chamado:
                abort(404)
            texto = (request.form.get("texto") or "").strip()
            chip = (request.form.get("chip") or "").strip() or None
            try:
                anexos = _anexos_do_form(chamado_id)
            except ch.ErroChamado as e:
                return _erro(e)
            if not texto and not anexos:
                return _erro("Escreva uma mensagem ou anexe um arquivo.")
            ultima = conn.execute("SELECT MAX(id) FROM portal_chamados_mensagens WHERE chamado_id = ?", (chamado_id,)).fetchone()[0] or 0
            if chamado["status"] == ch.STATUS_COM_ASSISTENTE:
                ch.adicionar_mensagem(conn, chamado, ch.ORIGEM_CLIENTE, cli.get("nome") or "Cliente", texto, anexos=anexos)
                chamado = ch.buscar_chamado(conn, chamado_id)
                assistente.responder(conn, chamado, cli, texto, chip, config)
            else:
                m, chamado, avisar = ch.registrar_mensagem_cliente(conn, chamado, cli, texto, anexos, config)
                if avisar:
                    ch.em_segundo_plano(ch.email_para_atendimento, chamado, m, config)
            chamado = ch.buscar_chamado(conn, chamado_id)
            return jsonify({**_payload_chamado(conn, chamado, ultima), "situacao": ch.situacao_atendimento(conn, config)})
        finally:
            conn.close()

    @app.route("/api/atendimento/chamados/<int:chamado_id>/acao", methods=["POST"])
    @requer_cliente
    @exige_mesma_origem
    def atendimento_acao(chamado_id):
        _so_cliente()
        corpo = request.get_json(silent=True) or {}
        tipo = corpo.get("tipo")
        conn = ch.conectar()
        try:
            cli = _cliente()
            chamado = ch.buscar_chamado(conn, chamado_id, cli["cnpj"])
            if not chamado:
                abort(404)
            ultima = conn.execute("SELECT MAX(id) FROM portal_chamados_mensagens WHERE chamado_id = ?", (chamado_id,)).fetchone()[0] or 0
            if tipo == "atendente":
                if chamado["status"] in (ch.STATUS_NA_FILA, ch.STATUS_EM_ATENDIMENTO):
                    return jsonify(_payload_chamado(conn, chamado, ultima))
                if chamado["status"] == ch.STATUS_RESOLVIDO:
                    chamado = ch.reabrir(conn, chamado, config, "cliente pediu atendente")
                chamado = assistente.garantir_resumo(conn, chamado, cli, config)
                r = ch.entrar_na_fila(conn, chamado, config)
                chamado = ch.buscar_chamado(conn, chamado_id)
                if not r["online"]:
                    msgs_cli = [m for m in ch.mensagens(conn, chamado_id) if m["origem"] == ch.ORIGEM_CLIENTE]
                    if msgs_cli:
                        ch.em_segundo_plano(ch.email_para_atendimento, chamado, msgs_cli[-1], config, novo=True)
                        emails = ch.emails_do_cliente(conn, cli["cnpj"])
                        if emails:
                            ch.em_segundo_plano(ch.email_confirmacao_cliente, chamado, msgs_cli[-1], emails, config)
                logger.info("chamado %s: cliente pediu atendente -> %s", chamado_id, chamado["status"])
            elif tipo == "resolvido":
                por = "assistente" if chamado["status"] == ch.STATUS_COM_ASSISTENTE else "cliente"
                if chamado["status"] == ch.STATUS_COM_ASSISTENTE:
                    chamado = assistente.garantir_resumo(conn, chamado, cli, config)
                chamado = ch.resolver(conn, chamado, por)
                emails = ch.emails_do_cliente(conn, cli["cnpj"])
                if emails:
                    ch.em_segundo_plano(ch.email_historico, chamado, emails, config)
                chamado = ch.buscar_chamado(conn, chamado_id)
            else:
                return _erro("Ação inválida.")
            return jsonify({**_payload_chamado(conn, chamado, ultima), "situacao": ch.situacao_atendimento(conn, config)})
        finally:
            conn.close()

    @app.route("/api/atendimento/chamados", methods=["POST"])
    @requer_cliente
    @exige_mesma_origem
    def atendimento_deixar_chamado():
        """Formulário 'Deixar chamado' (fora do horário ou por escolha)."""
        _so_cliente()
        conn = ch.conectar()
        try:
            cli = _cliente()
            assunto = (request.form.get("assunto") or "").strip()
            area = (request.form.get("area") or "").strip()
            pedido = (request.form.get("pedido") or "").strip()
            texto = (request.form.get("mensagem") or "").strip()
            if not assunto:
                return _erro("Informe o assunto.")
            if not texto:
                return _erro("Escreva a mensagem.")
            if area not in ch.AREAS:
                area = "outro"
            chamado = ch.criar_chamado(conn, cli, origem="chamado", status=ch.STATUS_AGUARDANDO_FL, assunto=assunto,
                                       area=area, pedido_ref=pedido)
            try:
                anexos = _anexos_do_form(chamado["id"])
            except ch.ErroChamado as e:
                conn.execute("DELETE FROM portal_chamados WHERE id = ?", (chamado["id"],))
                conn.commit()
                return _erro(e)
            m = ch.adicionar_mensagem(conn, chamado, ch.ORIGEM_CLIENTE, cli.get("nome") or "Cliente", texto, anexos=anexos)
            sit = ch.situacao_atendimento(conn, config)
            if sit["estado"] == "online":
                ch.atualizar_chamado(conn, chamado["id"], status=ch.STATUS_NA_FILA)
                ch.mensagem_sistema(conn, chamado, "Chamado registrado. A equipe está online e responde em instantes.")
            else:
                ch.mensagem_sistema(conn, chamado, f"Chamado registrado. {sit['texto']}. A equipe responde aqui e no seu e-mail ({sit['horario']}).")
            chamado = ch.buscar_chamado(conn, chamado["id"])
            ch.em_segundo_plano(ch.email_para_atendimento, chamado, m, config, novo=True)
            emails = ch.emails_do_cliente(conn, cli["cnpj"])
            if emails:
                ch.em_segundo_plano(ch.email_confirmacao_cliente, chamado, m, emails, config)
            logger.info("chamado %s deixado cnpj=%s", chamado["id"], cli["cnpj"])
            return jsonify(_payload_chamado(conn, chamado))
        finally:
            conn.close()

    @app.route("/api/atendimento/chamados/<int:chamado_id>/anexos/<path:arquivo>")
    @requer_cliente
    def atendimento_anexo(chamado_id, arquivo):
        conn = ch.conectar()
        try:
            chamado = ch.buscar_chamado(conn, chamado_id, _cliente()["cnpj"])
        finally:
            conn.close()
        if not chamado:
            abort(404)
        p = ch.caminho_anexo(chamado_id, arquivo)
        if not p:
            abort(404)
        return send_file(p, as_attachment=p.suffix.lower() not in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".pdf"), max_age=0)

    @app.context_processor
    def _atendimento_globais():
        try:
            if g.get("cliente") and not g.get("equipe"):
                conn = ch.conectar()
                try:
                    return {"atendimento_nao_lidas": ch.nao_lidas_cliente(conn, g.cliente["cnpj"]),
                            "atendimento_situacao": ch.situacao_atendimento(conn, config)}
                finally:
                    conn.close()
        except Exception:
            logger.exception("context processor do atendimento")
        return {"atendimento_nao_lidas": 0, "atendimento_situacao": None}
