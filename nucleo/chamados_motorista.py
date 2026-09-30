# -*- coding: utf-8 -*-
"""
nucleo/chamados_motorista.py

Rotas HTTP do chat do MOTORISTA (aba Ajuda do app) -- pedido do Hugo,
12/09/2026: "uma sessão chat linkada com o nosso atendimento; será um
atendimento para logística".

O motorista conversa primeiro com o assistente
(nucleo/assistente_motorista.py); quando ele pede a logística (ou o
assistente encaminha), o chamado entra na MESMA fila da tela /atendimento
do painel, na aba "Motoristas". Dados e transições são os mesmos do portal
do cliente (portal_cliente/chamados.py) -- aqui só muda quem é o
solicitante (tipo=MOTORISTA) e o perfil de horário (logística).

Registrado por nucleo/api_motorista.py via `registrar(app, ...)` -- módulo
separado de propósito, pra não engordar o api_motorista.py que outra frente
edita ao mesmo tempo.

Endpoints (todos exigem motorista logado, prefixo /api do serviço):
    GET  /api/atendimento/estado                       situação + conversa ativa + lista
    POST /api/atendimento/conversas                    inicia conversa com o assistente
    GET  /api/atendimento/chamados/<id>?desde=<msg>    mensagens novas (marca lidas)
    POST /api/atendimento/chamados/<id>/mensagens      texto + fotos (+ chip)
    POST /api/atendimento/chamados/<id>/acao           atendente | resolvido
    GET  /api/atendimento/chamados/<id>/anexos/<arq>
"""
import logging
import sys
from datetime import datetime
from pathlib import Path

from flask import abort, g, jsonify, request, send_file

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(_RAIZ / "portal_cliente"))

import chamados as ch  # noqa: E402  (portal_cliente/chamados.py)
from nucleo import assistente_motorista as assistente  # noqa: E402

logger = logging.getLogger("nucleo.chamados_motorista")

# Campos do chamado que o app enxerga (nunca dados de outro solicitante).
_CAMPOS = ("id", "assunto", "area", "area_rotulo", "pedido_ref", "status", "status_rotulo", "origem",
           "etapa_assistente", "atendente", "criado_em", "atualizado_em", "ultima_msg_em", "resolvido_em",
           "resolvido_por", "quando", "aberto", "nao_lidas", "ultima_texto", "ultima_origem_msg", "rota_id")


def registrar(app, *, requer_motorista, carregar_config):
    """`requer_motorista`: decorator do api_motorista que põe o motorista em
    g.motorista. `carregar_config`: devolve o config.yaml já lido."""

    def _motorista() -> dict:
        m = g.motorista
        return {"tipo": ch.TIPO_MOTORISTA, "cpf": m["cpf"], "nome": m.get("nome"),
                "agent_id": m.get("agent_id"), "tipo_veiculo": m.get("tipo_veiculo")}

    def _limpar(c: dict) -> dict:
        return {k: c.get(k) for k in _CAMPOS}

    def _payload(conn, chamado: dict, desde: int = 0) -> dict:
        return {"chamado": _limpar(chamado), "mensagens": ch.mensagens(conn, chamado["id"], desde)}

    def _situacao(conn, config) -> dict:
        return ch.situacao_atendimento(conn, config, perfil=ch.PERFIL_LOGISTICA)

    def _meu_chamado(conn, chamado_id: int) -> dict:
        c = ch.buscar_chamado_motorista(conn, chamado_id, g.motorista["cpf"])
        if not c:
            abort(404)
        return c

    def _anexos_do_form(chamado_id: int) -> list[dict]:
        # O app manda a foto no campo `anexos` (multipart) ou `arquivo` (o
        # uploader nativo do Expo usa esse nome) -- aceita os dois.
        arquivos = [(f.filename, f.read())
                    for f in (request.files.getlist("anexos") + request.files.getlist("arquivo"))
                    if f and f.filename]
        return ch.guardar_anexos(chamado_id, arquivos) if arquivos else []

    def _campo(nome: str) -> str:
        """Mensagem sem foto vai como JSON (o FormData do React Native é
        problemático); com foto vai multipart pelo uploader nativo."""
        if request.form.get(nome) is not None:
            return (request.form.get(nome) or "").strip()
        corpo = request.get_json(silent=True) or {}
        return str(corpo.get(nome) or "").strip()

    def _erro(e, status=400):
        return jsonify({"erro": str(e)}), status

    def _avisar_logistica(conn, chamado: dict, config: dict, novo: bool = False) -> None:
        """E-mail pra caixa da logística com a última fala do motorista."""
        msgs = [m for m in ch.mensagens(conn, chamado["id"]) if m["origem"] == ch.ORIGEM_CLIENTE]
        if msgs:
            ch.em_segundo_plano(ch.email_para_atendimento, chamado, msgs[-1], config, novo=novo)

    # ── estado ────────────────────────────────────────────────────────────────

    @app.get("/api/atendimento/estado")
    @requer_motorista
    def atd_estado():
        config = carregar_config()
        conn = ch.conectar()
        try:
            cpf = g.motorista["cpf"]
            ativo = ch.chamado_ativo_motorista(conn, cpf)
            pedido = request.args.get("chamado", type=int)
            if pedido:
                alvo = ch.buscar_chamado_motorista(conn, pedido, cpf)
                if alvo:
                    ativo = alvo
            return jsonify({
                "situacao": _situacao(conn, config),
                "ativo": _payload(conn, ativo) if ativo else None,
                "chamados": [_limpar(c) for c in ch.listar_chamados_motorista(conn, cpf)],
                "nao_lidas": ch.nao_lidas_motorista(conn, cpf),
                "areas": [{"valor": k, "rotulo": v} for k, v in ch.AREAS_MOTORISTA.items()],
                "agora": datetime.now().strftime("%H:%M"),
            })
        finally:
            conn.close()

    @app.get("/api/atendimento/nao-lidas")
    @requer_motorista
    def atd_nao_lidas():
        """Badge da aba Ajuda -- chamada leve, o app consulta de vez em quando."""
        conn = ch.conectar()
        try:
            return jsonify({"nao_lidas": ch.nao_lidas_motorista(conn, g.motorista["cpf"])})
        finally:
            conn.close()

    # ── conversa ──────────────────────────────────────────────────────────────

    @app.post("/api/atendimento/conversas")
    @requer_motorista
    def atd_iniciar():
        conn = ch.conectar()
        try:
            mot = _motorista()
            # "Solicitar ajuda para este pedido" (Hugo, 13/09): o botão do
            # cartão da parada abre a conversa já no pedido.
            parada_id = (request.get_json(silent=True) or {}).get("parada_id")
            parada = None
            if parada_id:
                parada = assistente.parada_do_motorista(conn, int(parada_id), mot.get("agent_id")) \
                    if str(parada_id).isdigit() else None
                if not parada:
                    return _erro("Pedido não encontrado nas suas rotas.", 404)
                aberto = conn.execute("""
                    SELECT id FROM portal_chamados WHERE tipo = ? AND motorista_cpf = ? AND parada_id = ?
                      AND status != ? ORDER BY id DESC LIMIT 1
                """, (ch.TIPO_MOTORISTA, mot["cpf"], parada["id"], ch.STATUS_RESOLVIDO)).fetchone()
                if aberto:   # tocou de novo: volta pra conversa desse pedido
                    chamado = ch.buscar_chamado(conn, aberto[0])
                    ch.marcar_lidas(conn, chamado["id"], "cliente")
                    return jsonify({**_payload(conn, chamado), "situacao": _situacao(conn, carregar_config())})
            chamado = ch.criar_chamado(conn, {**mot, "rota_id": parada["rota_id"] if parada else None}, origem="chat",
                                       status=ch.STATUS_COM_ASSISTENTE, etapa_assistente=assistente.ETAPA_AREA)
            if parada:
                assistente.iniciar_com_parada(conn, chamado, mot, parada)
            else:
                assistente.iniciar(conn, chamado, mot)
            # Ele está olhando a tela agora: a saudação não pode acender badge.
            ch.marcar_lidas(conn, chamado["id"], "cliente")
            chamado = ch.buscar_chamado(conn, chamado["id"])
            logger.info("chat do motorista iniciado cpf=%s chamado=%s", mot["cpf"], chamado["id"])
            return jsonify({**_payload(conn, chamado), "situacao": _situacao(conn, carregar_config())})
        finally:
            conn.close()

    @app.get("/api/atendimento/chamados/<int:chamado_id>")
    @requer_motorista
    def atd_chamado(chamado_id):
        config = carregar_config()
        conn = ch.conectar()
        try:
            chamado = _meu_chamado(conn, chamado_id)
            desde = request.args.get("desde", 0, type=int)
            ch.marcar_lidas(conn, chamado_id, "cliente")
            return jsonify({**_payload(conn, chamado, desde), "situacao": _situacao(conn, config)})
        finally:
            conn.close()

    @app.post("/api/atendimento/chamados/<int:chamado_id>/mensagens")
    @requer_motorista
    def atd_mensagem(chamado_id):
        config = carregar_config()
        conn = ch.conectar()
        try:
            mot = _motorista()
            chamado = _meu_chamado(conn, chamado_id)
            texto = _campo("texto")
            chip = _campo("chip") or None
            try:
                anexos = _anexos_do_form(chamado_id)
            except ch.ErroChamado as e:
                return _erro(e)
            if not texto and not anexos:
                return _erro("Escreva uma mensagem ou anexe uma foto.")
            ultima = conn.execute("SELECT MAX(id) FROM portal_chamados_mensagens WHERE chamado_id = ?",
                                  (chamado_id,)).fetchone()[0] or 0
            if chamado["status"] == ch.STATUS_COM_ASSISTENTE:
                ch.adicionar_mensagem(conn, chamado, ch.ORIGEM_CLIENTE, mot.get("nome") or "Motorista", texto, anexos=anexos)
                chamado = ch.buscar_chamado(conn, chamado_id)
                assistente.responder(conn, chamado, mot, texto, chip, config)
            else:
                m, chamado, avisar = ch.registrar_mensagem_cliente(conn, chamado, mot, texto, anexos, config)
                if avisar:
                    ch.em_segundo_plano(ch.email_para_atendimento, chamado, m, config)
            resposta = _payload(conn, ch.buscar_chamado(conn, chamado_id), ultima)
            # Tudo que veio nesta resposta já está na tela dele.
            ch.marcar_lidas(conn, chamado_id, "cliente")
            return jsonify({**resposta, "situacao": _situacao(conn, config)})
        finally:
            conn.close()

    @app.post("/api/atendimento/chamados/<int:chamado_id>/acao")
    @requer_motorista
    def atd_acao(chamado_id):
        config = carregar_config()
        corpo = request.get_json(silent=True) or {}
        tipo = corpo.get("tipo")
        conn = ch.conectar()
        try:
            mot = _motorista()
            chamado = _meu_chamado(conn, chamado_id)
            ultima = conn.execute("SELECT MAX(id) FROM portal_chamados_mensagens WHERE chamado_id = ?",
                                  (chamado_id,)).fetchone()[0] or 0
            if tipo == "atendente":
                if chamado["status"] in (ch.STATUS_NA_FILA, ch.STATUS_EM_ATENDIMENTO):
                    return jsonify({**_payload(conn, chamado, ultima), "situacao": _situacao(conn, config)})
                if chamado["status"] == ch.STATUS_RESOLVIDO:
                    chamado = ch.reabrir(conn, chamado, config, "motorista pediu a logística")
                chamado = assistente.garantir_resumo(conn, chamado, mot, config)
                r = ch.entrar_na_fila(conn, chamado, config)
                chamado = ch.buscar_chamado(conn, chamado_id)
                # Na fila com gente online a tela do painel já apita; fora do
                # horário (ou sem ninguém) o e-mail é o que garante a leitura.
                if not r["online"]:
                    _avisar_logistica(conn, chamado, config, novo=True)
                logger.info("chamado %s: motorista pediu a logística -> %s", chamado_id, chamado["status"])
            elif tipo == "resolvido":
                por = "assistente" if chamado["status"] == ch.STATUS_COM_ASSISTENTE else "motorista"
                if chamado["status"] == ch.STATUS_COM_ASSISTENTE:
                    chamado = assistente.garantir_resumo(conn, chamado, mot, config)
                chamado = ch.resolver(conn, chamado, por)
                emails = ch.emails_do_solicitante(conn, chamado)
                if emails:
                    ch.em_segundo_plano(ch.email_historico, chamado, emails, config)
            else:
                return _erro("Ação inválida.")
            return jsonify({**_payload(conn, chamado, ultima), "situacao": _situacao(conn, config)})
        finally:
            conn.close()

    @app.get("/api/atendimento/chamados/<int:chamado_id>/anexos/<path:arquivo>")
    @requer_motorista
    def atd_anexo(chamado_id, arquivo):
        conn = ch.conectar()
        try:
            _meu_chamado(conn, chamado_id)
        finally:
            conn.close()
        p = ch.caminho_anexo(chamado_id, arquivo)
        if not p:
            abort(404)
        return send_file(p, as_attachment=p.suffix.lower() not in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".pdf"),
                         max_age=0)
