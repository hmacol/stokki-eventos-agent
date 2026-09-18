# -*- coding: utf-8 -*-
"""
portal_cliente/link_canhoto.py

Link publico do canhoto, sem login -- pedido do Hugo, 17/09: o e-mail de
resumo diario (resumo_diario_embarcador.py) leva um "Ver canhoto" por nota
entregue, e a maioria dos embarcadores nao tem conta no portal.

O token (itsdangerous, mesmo segredo do portal com salt proprio) carrega
{service_id, sender_id} assinado. Na hora de abrir, a rota confere de novo
na VUUPT que o servico e daquele sender_id -- o token so prova que o link
saiu daqui, nao substitui a checagem de dono. Canhoto que ainda nao chegou
mostra a mesma mensagem da rota logada (/api/canhoto) e passa a abrir quando
chegar: o e-mail nao precisa verificar canhoto por canhoto.

Registrado em app.py por `registrar(app, ...)`, mesmo padrao de
cotacao_web.py / chamados_web.py.
"""
import logging

from itsdangerous import BadData, URLSafeTimedSerializer

logger = logging.getLogger("portal_cliente.link_canhoto")

_SALT = "portal-cliente-canhoto-email"
VALIDADE_DIAS = 90


def gerar(service_id: int, sender_id: int, segredo: str) -> str:
    return URLSafeTimedSerializer(segredo).dumps({"s": int(service_id), "r": int(sender_id)}, salt=_SALT)


def ler(token: str, segredo: str, validade_dias: int = VALIDADE_DIAS) -> dict | None:
    """{service_id, sender_id}, ou None se adulterado, vencido ou de outra
    finalidade (outro salt)."""
    try:
        dados = URLSafeTimedSerializer(segredo).loads(token, salt=_SALT, max_age=validade_dias * 86400)
        return {"service_id": int(dados["s"]), "sender_id": int(dados["r"])}
    except (BadData, KeyError, TypeError, ValueError):
        return None


def url(service_id: int, sender_id: int, segredo: str, url_base: str) -> str:
    return f"{url_base.rstrip('/')}/c/{gerar(service_id, sender_id, segredo)}"


def registrar(app, *, config: dict, secret: str):
    from flask import render_template, send_file

    import dados_cliente as dados

    def _indisponivel(titulo: str, texto: str, status: int):
        return render_template("mensagem.html", titulo=titulo, texto=texto), status

    @app.route("/c/<token>")
    def canhoto_por_link(token):
        alvo = ler(token, secret)
        if not alvo:
            return _indisponivel("Link inválido ou vencido",
                                 "Este link de comprovante não é mais válido. Os comprovantes continuam "
                                 "disponíveis no portal do cliente.", 404)
        token_vuupt = config.get("vuupt_api", {}).get("token", "")
        servico = dados.buscar_servico(token_vuupt, alvo["service_id"])
        if not servico or servico.get("sender_id") != alvo["sender_id"]:
            logger.warning(f"link de canhoto recusado service_id={alvo['service_id']} sender_id={alvo['sender_id']}")
            return _indisponivel("Comprovante não encontrado", "Não encontramos o comprovante deste link.", 404)
        codigo = servico.get("code") or str(alvo["service_id"])
        checklist_id = dados.checklist_id_do_servico(servico)
        if not checklist_id:
            return _indisponivel("Comprovante ainda não disponível",
                                 "O motorista ainda não enviou a foto do canhoto deste pedido, ou ela ainda "
                                 "está sendo processada. Tente de novo mais tarde, pelo mesmo link.", 404)
        pdf = dados.baixar_canhoto_pdf(token_vuupt, checklist_id, codigo)
        if not pdf:
            return _indisponivel("Comprovante indisponível",
                                 "Não conseguimos gerar o comprovante agora. Tente de novo em alguns minutos.", 502)
        return send_file(pdf, mimetype="application/pdf", as_attachment=False,
                         download_name=f"comprovante_{codigo.lstrip('#')}.pdf", max_age=0)
