# -*- coding: utf-8 -*-
"""
embarcadores_whatsapp.py

Tela /embarcadores/whatsapp: grupo de WhatsApp de cada embarcador
(interno.whatsapp_grupo_id), usado pelo botao "Avisar clientes" do
planejamento (avisar_fora_area.py). Os grupos vem do numero do Hugo pelo
OpenWA (integracao_openwa.listar_grupos); se o gateway nao responder, a
tela deixa colar o id (...@g.us) a mao.

Mesmo padrao de atendimento_chamados.py: modulo separado que recebe os
decoradores do painel em registrar().
"""
import logging
import re
import sys
from pathlib import Path

from flask import g, jsonify, render_template, request, session

_RAIZ = Path(__file__).resolve().parent.parent
if str(_RAIZ) not in sys.path:
    sys.path.insert(0, str(_RAIZ))

import avisar_fora_area  # noqa: E402
import integracao_openwa  # noqa: E402

logger = logging.getLogger(__name__)

NIVEIS = ("total", "operador")
_FORMATO_GRUPO = re.compile(r"^\d+@g\.us$")


def listar(conn) -> list[dict]:
    avisar_fora_area.garantir_coluna_grupo(conn)
    rows = conn.execute(
        "SELECT cnpj_embarcador, nome_remetente, apelido, email, whatsapp_grupo_id FROM interno").fetchall()
    itens = [{"cnpj": r["cnpj_embarcador"], "nome": r["apelido"] or r["nome_remetente"] or "",
              "email": r["email"] or "", "whatsapp_grupo_id": (r["whatsapp_grupo_id"] or "").strip() or None}
             for r in rows]
    return sorted(itens, key=lambda e: e["nome"].lower())


def salvar_grupo(conn, cnpj: str, grupo_id: str | None) -> None:
    """grupo_id vazio/None tira o grupo. Formato exigido: <numeros>@g.us
    (link de convite ou @c.us nao valem)."""
    avisar_fora_area.garantir_coluna_grupo(conn)
    grupo_id = grupo_id or ""
    if grupo_id and not _FORMATO_GRUPO.match(grupo_id):
        raise ValueError("id de grupo invalido: use o formato 123456789@g.us")
    cur = conn.execute("UPDATE interno SET whatsapp_grupo_id = ? WHERE cnpj_embarcador = ?",
                       (grupo_id or None, cnpj))
    if cur.rowcount == 0:
        raise ValueError("embarcador nao encontrado")
    conn.commit()


def registrar(app, *, requer_auth, exige_mesma_origem, carregar_config):
    def _cfg_wa() -> dict:
        return (carregar_config() or {}).get("whatsapp_notificacoes") or {}

    @app.route("/embarcadores/whatsapp")
    @requer_auth(niveis=NIVEIS)
    def embarcadores_whatsapp():
        conn = avisar_fora_area.conectar()
        try:
            embarcadores = listar(conn)
        finally:
            conn.close()
        return render_template("embarcadores_whatsapp.html", embarcadores=embarcadores)

    @app.route("/api/embarcadores/whatsapp/grupos")
    @requer_auth(niveis=NIVEIS)
    def api_embarcadores_whatsapp_grupos():
        grupos = integracao_openwa.listar_grupos(_cfg_wa())
        return jsonify({"grupos": grupos or [], "indisponivel": grupos is None})

    @app.route("/api/embarcadores/<cnpj>/whatsapp-grupo", methods=["POST"])
    @requer_auth(niveis=NIVEIS)
    @exige_mesma_origem
    def api_embarcadores_whatsapp_grupo(cnpj):
        body = request.get_json(force=True, silent=True) or {}
        grupo_id = str(body.get("grupo_id") or "").strip()
        conn = avisar_fora_area.conectar()
        try:
            salvar_grupo(conn, cnpj, grupo_id)
        except ValueError as e:
            return jsonify({"erro": str(e)}), 400
        finally:
            conn.close()
        logger.info("Grupo de WhatsApp do embarcador %s = %r (por %s)", cnpj, grupo_id or None,
                    session.get("usuario") or g.nivel_acesso)
        return jsonify({"ok": True, "whatsapp_grupo_id": grupo_id or None})
