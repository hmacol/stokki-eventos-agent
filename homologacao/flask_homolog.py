"""Flask da homologacao: faixa laranja, erros amigaveis, saude e tela de envios."""
import email
import html
import re
from pathlib import Path

from flask import Blueprint, jsonify, render_template, request

from homologacao import ambiente, guarda, registro

_RAIZ = Path(__file__).resolve().parent.parent
_RE_BODY = re.compile(r"<body\b[^>]*>", re.IGNORECASE)
_RE_TITLE = re.compile(r"<title>", re.IGNORECASE)


def _faixa() -> str:
    v = ambiente.informacao_versao(_RAIZ)
    texto = "HOMOLOGAÇÃO · %s @ %s · dados de %s" % (v["ramo"], str(v["commit"])[:7], v["copia"])
    return ('<div id="faixa-homologacao" style="background:#f97316;color:#fff;text-align:center;'
            'padding:6px 10px;font:600 13px system-ui,sans-serif;position:sticky;top:0;'
            'z-index:2147483647">%s</div>' % html.escape(texto))


def _html_do_email(conteudo: str):
    try:
        msg = email.message_from_string(conteudo or "")
        for parte in msg.walk():
            if parte.get_content_type() == "text/html":
                carga = parte.get_payload(decode=False)
                if isinstance(carga, str) and carga.strip():
                    return carga
    except Exception:
        pass
    return None


def instalar(app, *, servico: str, requer_auth=None) -> None:
    @app.after_request
    def _injetar_faixa(resp):
        try:
            if resp.direct_passthrough or not (resp.mimetype or "").lower() == "text/html":
                return resp
            corpo = resp.get_data(as_text=True)
            m = _RE_BODY.search(corpo)
            if m:
                corpo = corpo[:m.end()] + _faixa() + corpo[m.end():]
            corpo = _RE_TITLE.sub("<title>[HOMOLOG] ", corpo, count=1)
            resp.set_data(corpo)
            resp.headers["Content-Length"] = str(len(resp.get_data()))
        except Exception:
            pass
        return resp

    def _responder(e, status):
        if "/api/" in request.path or "json" in (request.headers.get("Accept") or "").lower():
            return jsonify({"erro": str(e)}), status
        return ("<!doctype html><meta charset=utf-8><title>Homologação</title>"
                "<body style='font-family:system-ui;padding:2rem'><h2>%s</h2></body>"
                % html.escape(str(e)), status)

    @app.errorhandler(ambiente.DesligadoNaHomologacao)
    def _desligado(e):
        return _responder(e, 409)

    @app.errorhandler(guarda.EnvioBloqueadoHomolog)
    def _bloqueado(e):
        return _responder(e, 503)

    bp = Blueprint("homologacao", __name__, template_folder="templates")

    @bp.route("/homologacao/saude")
    def saude():
        v = ambiente.informacao_versao(_RAIZ)
        return jsonify({"ambiente": "homolog", "guarda": "ativa" if guarda.INSTALADA else "inativa",
                        "camadas": list(guarda.CAMADAS), "ramo": v["ramo"], "commit": v["commit"],
                        "master": v["master"], "copia": v["copia"]})

    if servico == "painel" and requer_auth is not None:
        @bp.route("/homologacao/envios")
        @requer_auth()
        def envios():
            canal = (request.args.get("canal") or "").strip() or None
            return render_template("envios.html", envios=registro.listar_envios(canal=canal, limite=200),
                                   canal=canal, detalhe=None)

        @bp.route("/homologacao/envios/<int:envio_id>")
        @requer_auth()
        def envio_detalhe(envio_id):
            e = registro.buscar_envio(envio_id)
            if not e:
                return ("Envio não encontrado", 404)
            html_email = _html_do_email(e.get("conteudo")) if e.get("canal") == "email" else None
            return render_template("envios.html", envios=[], canal=None, detalhe=e, html_email=html_email)

    app.register_blueprint(bp)
