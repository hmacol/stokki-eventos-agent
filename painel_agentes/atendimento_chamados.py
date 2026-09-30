# -*- coding: utf-8 -*-
"""
painel_agentes/atendimento_chamados.py

Tela interna de Atendimento (pedido do Hugo, 09/09/2026): a fila dos
chats do portal do cliente e dos chamados por e-mail, numa tela só do
painel (/atendimento). Dados e regras em portal_cliente/chamados.py;
aqui só HTTP. Registrado em painel_agentes.py por `registrar(app, ...)`
-- módulo separado pra não engordar o painel_agentes.py.

Quem acessa (decisão do Hugo, 09/09): níveis "atendimento" (novo,
usuario_atendimento/senha_atendimento no config), "operador" (logística)
e "total". O atendente informa o nome na tela (o login é compartilhado
por nível); o nome vai nas mensagens e no histórico por e-mail.
"""
import logging
import re
import sys
from pathlib import Path

from flask import abort, g, jsonify, redirect, render_template, request, send_file, session, url_for

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ / "portal_cliente"))
sys.path.insert(0, str(_RAIZ))

import chamados as ch  # noqa: E402  (portal_cliente/chamados.py)
import envio_pedidos as ep  # noqa: E402  (portal_cliente/envio_pedidos.py)
import pedidos_dedicados  # noqa: E402  (raiz)

logger = logging.getLogger("painel.atendimento")

NIVEIS = ("total", "operador", "atendimento")

# ── Liberacao de NFs bloqueadas por area nao atendida (Hugo, 23/09) ──────────
# O chamado (area "envios") nasce no portal quando o cliente envia pedido pra
# cidade que nao atendemos; a equipe decide aqui: liberar como dedicado (com
# valor da cotacao dividido entre as NFs), liberar sem dedicado ou cancelar.

STATUS_BLOQUEADO = ep.STATUS_AGUARDANDO_LIBERACAO
MODOS_LIBERACAO = ("dedicado", "simples", "cancelar")


def valor_brl(v) -> float:
    """'1.234,56' / '150,50' / 'R$ 99' / 100 -> float > 0."""
    if isinstance(v, bool) or v is None:
        raise ValueError("Informe o valor da cotação.")
    if isinstance(v, (int, float)):
        f = float(v)
    else:
        s = str(v).strip().replace("R$", "").replace(" ", "")
        if "," in s:
            s = s.replace(".", "").replace(",", ".")
        elif re.fullmatch(r"\d{1,3}(\.\d{3})+", s):
            s = s.replace(".", "")  # "1.500" e milhar, nao decimal (revisao 23/09)
        try:
            f = float(s)
        except ValueError:
            raise ValueError("Valor inválido.")
    f = round(f, 2)
    if f <= 0:
        raise ValueError("Informe um valor maior que zero.")
    return f


def _fmt_brl(v: float) -> str:
    return "R$ " + f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def envios_do_chamado(conn_envios, chamado_id: int) -> list[dict]:
    rows = conn_envios.execute("SELECT id, numero_nf, referencia, destinatario_nome, destinatario_municipio, destinatario_uf, status, codigo_pedido "
                               "FROM portal_envios WHERE bloqueio_chamado_id = ? ORDER BY id", (chamado_id,)).fetchall()
    return [{**dict(r), "status_rotulo": ep.ROTULOS_STATUS.get(r["status"], r["status"])} for r in rows]


def liberar_envios(conn_envios, conn_dedicados, chamado: dict, ids: list[int], modo: str, valor, por: str, mensagem_fn) -> dict:
    """Aplica a decisao da equipe nas NFs bloqueadas do chamado. Levanta
    ValueError com mensagem pro atendente. mensagem_fn(texto) grava a
    mensagem de sistema no chat (visivel pro cliente)."""
    if modo not in MODOS_LIBERACAO:
        raise ValueError("Modo inválido.")
    ids = [int(i) for i in ids or []]
    if not ids:
        raise ValueError("Marque ao menos uma NF.")
    todos = {e["id"]: e for e in envios_do_chamado(conn_envios, chamado["id"])}
    alvo = []
    for i in ids:
        e = todos.get(i)
        if not e or e["status"] != STATUS_BLOQUEADO:
            raise ValueError(f"NF {e['numero_nf'] if e else i} não está mais aguardando liberação.")
        alvo.append(e)
    valores = {}
    if modo == "dedicado":
        total = valor_brl(valor)
        grupo = pedidos_dedicados.marcar(conn_dedicados, [{
            "envio_id": e["id"], "codigo_pedido": e.get("codigo_pedido"), "numero_nf": e.get("numero_nf") or e.get("referencia"),
            "sender_id": chamado.get("sender_id"), "remetente_nome": chamado.get("nome_cliente")} for e in alvo], total, por)
        for r in conn_dedicados.execute("SELECT envio_id, valor FROM pedidos_dedicados WHERE grupo_id = ?", (grupo,)):
            valores[r["envio_id"]] = r["valor"]
    novo = ep.STATUS_CANCELADO if modo == "cancelar" else ep.STATUS_NA_FILA
    agora = ep._agora()
    for e in alvo:
        conn_envios.execute("UPDATE portal_envios SET status = ?, atualizado_em = ? WHERE id = ?", (novo, agora, e["id"]))
    conn_envios.commit()
    for e in alvo:
        rot = e.get("numero_nf") or e.get("referencia") or e["id"]
        if modo == "dedicado":
            mensagem_fn(f"NF {rot} liberada como envio dedicado ({_fmt_brl(valores[e['id']])}) por {por}. O pedido segue para a Stokki.")
        elif modo == "simples":
            mensagem_fn(f"NF {rot} liberada por {por}. O pedido segue para a Stokki.")
        else:
            mensagem_fn(f"NF {rot} cancelada por {por}.")
    return {"liberados": [e["id"] for e in alvo], "valores": valores}


def registrar(app, *, requer_auth, exige_mesma_origem, carregar_config):
    # garante a tabela pedidos_dedicados antes da 1a leitura em envios-bloqueados
    try:
        pedidos_dedicados.conectar().close()
    except Exception as e:  # nao derruba o painel por isso
        logger.warning("pedidos_dedicados indisponivel: %s", e)

    def _config() -> dict:
        return carregar_config()

    def _usuario() -> str:
        return session.get("usuario") or g.nivel_acesso

    def _nome() -> str:
        return session.get("atendente_nome") or _usuario()

    def _heartbeat(conn):
        """Toda chamada da tela renova o 'visto' do atendente (mantém o
        status ONLINE valendo pro portal)."""
        atual = ch.status_atendente(conn, _usuario())
        if atual.get("status") in ("ONLINE", "ALMOCO"):
            ch.gravar_status_atendente(conn, _usuario(), _nome(), atual["status"])
        return ch.status_atendente(conn, _usuario())

    def _contexto_motorista(conn, chamado: dict) -> dict:
        """Painel lateral de um chamado aberto pelo motorista: quem é ele, a
        rota de hoje com as paradas e o extrato curto -- o mesmo que o
        assistente enxerga, pra equipe não precisar abrir outra tela."""
        from nucleo import assistente_motorista
        cpf = chamado.get("motorista_cpf") or ""
        m = conn.execute("SELECT cpf, nome, telefone, email, agent_id, tipo_veiculo, perfil FROM motoristas WHERE cpf = ?",
                         (cpf,)).fetchone()
        motorista = dict(m) if m else {"cpf": cpf, "nome": chamado.get("nome_cliente"),
                                       "agent_id": chamado.get("agent_id")}
        ctx = assistente_motorista.contexto_motorista(motorista, _config())
        outros = conn.execute("""
            SELECT id, assunto, status, atualizado_em FROM portal_chamados
            WHERE tipo = ? AND motorista_cpf = ? AND id != ? ORDER BY atualizado_em DESC LIMIT 6
        """, (ch.TIPO_MOTORISTA, cpf, chamado["id"])).fetchall()
        em30 = conn.execute("""
            SELECT COUNT(*), SUM(status = 'RESOLVIDO') FROM portal_chamados
            WHERE tipo = ? AND motorista_cpf = ? AND criado_em >= datetime('now', 'localtime', '-30 days')
        """, (ch.TIPO_MOTORISTA, cpf)).fetchone()
        pendentes = assistente_motorista.paradas_pendentes(ctx)
        return {
            "tipo": ch.TIPO_MOTORISTA,
            "motorista": {"nome": motorista.get("nome"), "cpf": cpf, "telefone": motorista.get("telefone"),
                          "email": motorista.get("email"), "agent_id": motorista.get("agent_id"),
                          "tipo_veiculo": motorista.get("tipo_veiculo"), "perfil": motorista.get("perfil"),
                          "chamados_30d": em30[0] or 0, "resolvidos_30d": em30[1] or 0},
            "rota": ctx.get("rota"),
            "paradas_pendentes": len(pendentes),
            "parada": chamado.get("pedido_dados"),
            "extrato": ctx.get("extrato"),
            "tarifa": ctx.get("tarifa"),
            "outros": [{"id": r["id"], "assunto": r["assunto"], "status": r["status"],
                        "status_rotulo": ch.ROTULOS_STATUS.get(r["status"], r["status"]),
                        "quando": ch.rotulo_quando(r["atualizado_em"])} for r in outros],
        }

    def _contexto(conn, chamado: dict, config: dict) -> dict:
        if chamado.get("tipo") == ch.TIPO_MOTORISTA:
            return _contexto_motorista(conn, chamado)
        import auth_cliente
        emb = auth_cliente.buscar_embarcador(conn, chamado["cnpj_embarcador"]) or {}
        outros = conn.execute("""
            SELECT id, assunto, status, atualizado_em FROM portal_chamados
            WHERE cnpj_embarcador = ? AND id != ? ORDER BY atualizado_em DESC LIMIT 6
        """, (chamado["cnpj_embarcador"], chamado["id"])).fetchall()
        em30 = conn.execute("""
            SELECT COUNT(*), SUM(status = 'RESOLVIDO') FROM portal_chamados
            WHERE cnpj_embarcador = ? AND criado_em >= datetime('now', 'localtime', '-30 days')
        """, (chamado["cnpj_embarcador"],)).fetchone()
        pedido = chamado.get("pedido_dados")
        kpis = None
        # pedido atualizado (cache de 5 min do portal) -- só se tiver referência
        if chamado.get("sender_id"):
            try:
                import assistente
                import dados_cliente
                from datetime import date
                d = dados_cliente.montar_dia(int(chamado["sender_id"]), date.today(), config)
                kpis = d.get("kpis")
                if chamado.get("pedido_ref"):
                    p = assistente.localizar_pedido(chamado["pedido_ref"], (d.get("pedidos") or []) + (d.get("agendados_futuros") or []))
                    if p:
                        pedido = assistente._resumo_pedido(p)
                        pedido["service_id"] = p.get("service_id")
            except Exception as e:
                logger.warning("contexto do chamado %s: %s", chamado["id"], e)
        return {
            "tipo": ch.TIPO_CLIENTE,
            "cliente": {"nome": emb.get("nome") or chamado.get("nome_cliente"), "cnpj": auth_cliente.formatar_cnpj(chamado["cnpj_embarcador"]),
                        "emails": emb.get("emails") or [], "sender_id": chamado.get("sender_id"),
                        "chamados_30d": em30[0] or 0, "resolvidos_30d": em30[1] or 0, "kpis": kpis},
            "pedido": pedido,
            "outros": [{"id": r["id"], "assunto": r["assunto"], "status": r["status"], "status_rotulo": ch.ROTULOS_STATUS.get(r["status"], r["status"]),
                        "quando": ch.rotulo_quando(r["atualizado_em"])} for r in outros],
        }

    def _payload(conn, chamado: dict, desde: int = 0, com_contexto: bool = False, config: dict | None = None) -> dict:
        out = {"chamado": chamado, "mensagens": ch.mensagens(conn, chamado["id"], desde),
               "cliente_online": ch.cliente_esta_online(chamado)}
        if com_contexto:
            out["contexto"] = _contexto(conn, chamado, config or _config())
        return out

    # ── Tela ──────────────────────────────────────────────────────────────────

    def _boot() -> dict:
        config = _config()
        conn = ch.conectar()
        try:
            meu = _heartbeat(conn)
            boot = {
                "usuario": _usuario(), "nome": _nome(), "nivel": g.nivel_acesso,
                "status": meu.get("status") or "OFFLINE",
                "horario": ch.texto_horario(config),
                "situacao": ch.situacao_atendimento(conn, config),
                "contagens": ch.contagens_fila(conn, _usuario()),
                "horario_logistica": ch.texto_horario(config, ch.PERFIL_LOGISTICA),
                "areas": ch.AREAS, "areas_motorista": ch.AREAS_MOTORISTA, "status_rotulos": ch.ROTULOS_STATUS,
                "chamado_inicial": request.args.get("chamado", type=int),
                "url_portal": ch.url_base(config),
                "respostas_rapidas": ch.cfg_chamados(config).get("respostas_rapidas") or [
                    "Estou conferindo com o motorista e já te retorno.",
                    "O comprovante (canhoto) já está disponível no portal, na linha do pedido.",
                    "A reentrega vai na rota da tarde de hoje.",
                    "Pode me mandar uma foto ou a NF pra eu conferir?",
                ],
                "respostas_rapidas_motorista": ch.cfg_chamados(config).get("respostas_rapidas_motorista") or [
                    "Já estou falando com o cliente, aguarda aí que te retorno.",
                    "Pode dar o insucesso e seguir pra próxima parada.",
                    "Aguarda 10 minutos no local, por favor.",
                    "Pode voltar pro galpão com a mercadoria.",
                    "Manda uma foto do local e da fachada, por favor.",
                    "Segue a rota que eu resolvo essa parada com o cliente.",
                ],
            }
        finally:
            conn.close()
        return boot

    @app.route("/atendimento", endpoint="atendimento")
    @requer_auth(niveis=NIVEIS)
    def tela_atendimento():
        return render_template("atendimento.html", boot=_boot())

    @app.route("/atendimento/mobile", endpoint="atendimento_mobile")
    @requer_auth(niveis=NIVEIS)
    def tela_atendimento_mobile():
        """Versão celular (23/09): fila, conversa e contexto em telas
        empilhadas, nos mesmos /api/atendimento/* da tela desktop."""
        return render_template("atendimento_mobile.html", boot=_boot(), endpoint_desktop="atendimento")

    # ── API ───────────────────────────────────────────────────────────────────

    @app.route("/api/atendimento/fila")
    @requer_auth(niveis=NIVEIS)
    def api_atd_fila():
        aba = request.args.get("aba", "fila")
        config = _config()
        conn = ch.conectar()
        try:
            meu = _heartbeat(conn)
            return jsonify({"chamados": ch.listar_fila(conn, aba, _usuario()), "contagens": ch.contagens_fila(conn, _usuario()),
                            "situacao": ch.situacao_atendimento(conn, config), "meu_status": meu.get("status") or "OFFLINE",
                            "nome": _nome()})
        finally:
            conn.close()

    @app.route("/api/atendimento/status", methods=["POST"])
    @requer_auth(niveis=NIVEIS)
    @exige_mesma_origem
    def api_atd_status():
        corpo = request.get_json(silent=True) or {}
        nome = (corpo.get("nome") or "").strip()[:60]
        if nome:
            session["atendente_nome"] = nome
        status = corpo.get("status") or "ONLINE"
        conn = ch.conectar()
        try:
            ch.gravar_status_atendente(conn, _usuario(), _nome(), status)
            return jsonify({"ok": True, "status": status, "nome": _nome(), "situacao": ch.situacao_atendimento(conn, _config())})
        finally:
            conn.close()

    @app.route("/api/atendimento/chamados/<int:chamado_id>")
    @requer_auth(niveis=NIVEIS)
    def api_atd_chamado(chamado_id):
        desde = request.args.get("desde", 0, type=int)
        conn = ch.conectar()
        try:
            _heartbeat(conn)
            chamado = ch.buscar_chamado(conn, chamado_id)
            if not chamado:
                abort(404)
            ch.marcar_lidas(conn, chamado_id, "equipe")
            chamado = ch.buscar_chamado(conn, chamado_id)
            return jsonify(_payload(conn, chamado, desde, com_contexto=(desde == 0)))
        finally:
            conn.close()

    @app.route("/api/atendimento/chamados/<int:chamado_id>/mensagens", methods=["POST"])
    @requer_auth(niveis=NIVEIS)
    @exige_mesma_origem
    def api_atd_mensagem(chamado_id):
        config = _config()
        conn = ch.conectar()
        try:
            chamado = ch.buscar_chamado(conn, chamado_id)
            if not chamado:
                abort(404)
            texto = (request.form.get("texto") or "").strip()
            arquivos = [(f.filename, f.read()) for f in request.files.getlist("anexos") if f and f.filename]
            try:
                anexos = ch.guardar_anexos(chamado_id, arquivos) if arquivos else []
                ultima = conn.execute("SELECT MAX(id) FROM portal_chamados_mensagens WHERE chamado_id = ?", (chamado_id,)).fetchone()[0] or 0
                if chamado["status"] in (ch.STATUS_NA_FILA, ch.STATUS_COM_ASSISTENTE) and not chamado.get("atendente"):
                    chamado = ch.assumir(conn, chamado, _usuario(), _nome())
                m, chamado, mandar = ch.registrar_mensagem_equipe(conn, chamado, _usuario(), _nome(), texto, anexos, config)
            except ch.ErroChamado as e:
                return jsonify({"erro": str(e)}), 400
            email_ok = False
            if mandar:
                emails = ch.emails_do_solicitante(conn, chamado)
                if emails:
                    ch.em_segundo_plano(ch.email_resposta_cliente, chamado, m, emails, config)
                    email_ok = "enviando"
                else:
                    # Motorista quase nunca tem e-mail: a resposta aparece na
                    # aba Ajuda do app dele, não é erro.
                    email_ok = "so_app" if chamado.get("tipo") == ch.TIPO_MOTORISTA else "sem_email"
            logger.info("chamado %s: resposta de %s (e-mail=%s)", chamado_id, _nome(), email_ok)
            return jsonify({**_payload(conn, chamado, ultima), "email_enviado": email_ok})
        finally:
            conn.close()

    def _acao(chamado_id, fn):
        conn = ch.conectar()
        try:
            chamado = ch.buscar_chamado(conn, chamado_id)
            if not chamado:
                abort(404)
            ultima = conn.execute("SELECT MAX(id) FROM portal_chamados_mensagens WHERE chamado_id = ?", (chamado_id,)).fetchone()[0] or 0
            try:
                chamado, extra = fn(conn, chamado)
            except ch.ErroChamado as e:
                return jsonify({"erro": str(e)}), 400
            return jsonify({**_payload(conn, chamado, ultima), **(extra or {})})
        finally:
            conn.close()

    @app.route("/api/atendimento/chamados/<int:chamado_id>/assumir", methods=["POST"])
    @requer_auth(niveis=NIVEIS)
    @exige_mesma_origem
    def api_atd_assumir(chamado_id):
        return _acao(chamado_id, lambda conn, c: (ch.assumir(conn, c, _usuario(), _nome()), None))

    @app.route("/api/atendimento/chamados/<int:chamado_id>/transferir", methods=["POST"])
    @requer_auth(niveis=NIVEIS)
    @exige_mesma_origem
    def api_atd_transferir(chamado_id):
        return _acao(chamado_id, lambda conn, c: (ch.transferir(conn, c, _nome()), None))

    @app.route("/api/atendimento/chamados/<int:chamado_id>/resolver", methods=["POST"])
    @requer_auth(niveis=NIVEIS)
    @exige_mesma_origem
    def api_atd_resolver(chamado_id):
        corpo = request.get_json(silent=True) or {}
        config = _config()

        def fn(conn, c):
            c = ch.resolver(conn, c, _nome(), corpo.get("resolucao") or "")
            emails = ch.emails_do_solicitante(conn, c)
            if emails:
                ch.em_segundo_plano(ch.email_historico, c, emails, config)
            logger.info("chamado %s resolvido por %s; histórico pra %s", c["id"], _nome(), emails)
            return ch.buscar_chamado(conn, c["id"]), {"historico_enviado": bool(emails)}
        return _acao(chamado_id, fn)

    @app.route("/api/atendimento/chamados/<int:chamado_id>/reabrir", methods=["POST"])
    @requer_auth(niveis=NIVEIS)
    @exige_mesma_origem
    def api_atd_reabrir(chamado_id):
        config = _config()
        return _acao(chamado_id, lambda conn, c: (ch.reabrir(conn, c, config, f"por {_nome()}"), None))

    # ── NFs bloqueadas por area nao atendida (Hugo, 23/09) ───────────────────

    @app.route("/api/atendimento/chamados/<int:chamado_id>/envios-bloqueados")
    @requer_auth(niveis=NIVEIS)
    def api_atd_envios_bloqueados(chamado_id):
        conn = ep.conectar()
        try:
            envs = envios_do_chamado(conn, chamado_id)
            ded = {}
            if envs:
                marcadores = ",".join("?" * len(envs))
                ded = {r["envio_id"]: r["valor"] for r in conn.execute(
                    f"SELECT envio_id, valor FROM pedidos_dedicados WHERE removido_em IS NULL AND envio_id IN ({marcadores})",
                    [e["id"] for e in envs])}
            for e in envs:
                e["dedicado_valor"] = ded.get(e["id"])
            return jsonify({"envios": envs})
        finally:
            conn.close()

    @app.route("/api/atendimento/chamados/<int:chamado_id>/liberar", methods=["POST"])
    @requer_auth(niveis=NIVEIS)
    @exige_mesma_origem
    def api_atd_liberar(chamado_id):
        corpo = request.get_json(silent=True) or {}

        def fn(conn, c):
            conn_env = ep.conectar()
            conn_ded = pedidos_dedicados.conectar()
            try:
                try:
                    r = liberar_envios(conn_env, conn_ded, c, corpo.get("ids"), corpo.get("modo"), corpo.get("valor"), _nome(),
                                       lambda texto: ch.mensagem_sistema(conn, c, texto))
                except ValueError as e:
                    raise ch.ErroChamado(str(e))
            finally:
                conn_ded.close()
                conn_env.close()
            logger.info("chamado %s: liberar %s ids=%s por %s", c["id"], corpo.get("modo"), r["liberados"], _nome())
            return ch.buscar_chamado(conn, c["id"]), r
        return _acao(chamado_id, fn)

    @app.route("/api/atendimento/chamados/<int:chamado_id>/anexos/<path:arquivo>")
    @requer_auth(niveis=NIVEIS)
    def api_atd_anexo(chamado_id, arquivo):
        p = ch.caminho_anexo(chamado_id, arquivo)
        if not p:
            abort(404)
        return send_file(p, as_attachment=p.suffix.lower() not in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".pdf"), max_age=0)
