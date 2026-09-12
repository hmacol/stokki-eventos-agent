# -*- coding: utf-8 -*-
"""
nucleo/api_motorista.py

API JSON do app de motoristas (Fase B, seção 3.3 do
DOC_EXECUCAO_CLAUDE_APP_MOTORISTAS.md). Casca HTTP fina sobre
nucleo/operacao.py + nucleo/auth_motorista.py + nucleo/financeiro.py.

Autenticação: `Authorization: Bearer <token de acesso>` (POST /api/login
com CPF + PIN). Toda resposta é JSON; erro = {"erro": "..."} com o status
HTTP certo (400 regra, 401 auth, 403, 404, 409 conflito, 429 bloqueio).

COMO USAR (desenvolvimento, porta livre -- nunca a 8070/8071 do painel):
    py -3.11 nucleo/api_motorista.py --porta 8073
COMO USAR (produção, VPS -- infra/motorista-api.service):
    waitress-serve --host=127.0.0.1 --port=8073 --call nucleo.api_motorista:criar_app
Atrás do Caddy em app.freshhub.com.br/motorista (infra/Caddyfile-motorista),
mesmo padrão handle_path + X-Forwarded-Prefix + ProxyFix do painel.

config.yaml:
    api_motorista:
      secret_key: "<aleatório longo>"   # assina os tokens; trocar desloga todo mundo
      porta: 8073
      pasta_comprovantes: dados/comprovantes   # opcional
Fotos: gravadas em disco (pasta acima) e, best-effort, no bucket GCS já
usado pelos documentos (documentos_pedido/storage_gcs.py, mesmo caminho
pedidos/{codigo}/{tipo}/...).
"""
import hashlib
import logging
import secrets
import sys
import time
from datetime import date, datetime, timedelta
from functools import wraps
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))

from flask import Flask, g, jsonify, request
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename

from nucleo import auth_motorista as auth, banco, financeiro, operacao, validacao_fotos
from nucleo.auth_motorista import AutenticacaoInvalida
from nucleo.operacao import OperacaoInvalida
from regras import tarifa_motorista

logger = logging.getLogger("nucleo.api_motorista")

EXTENSOES_FOTO = {"jpg", "jpeg", "png", "webp", "heic"}
TAMANHO_MAX_FOTO = 15 * 1024 * 1024


def _carregar_config() -> dict:
    import yaml
    caminho = _RAIZ / "config.yaml"
    if not caminho.exists():
        return {}
    with open(caminho, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _data(valor: str | None, padrao: date) -> date:
    if not valor:
        return padrao
    try:
        return date.fromisoformat(valor)
    except ValueError:
        raise OperacaoInvalida(f"Data inválida: {valor!r} (use YYYY-MM-DD).")


def criar_app(config: dict | None = None) -> Flask:
    config = config if config is not None else _carregar_config()
    cfg = config.get("api_motorista", {}) or {}

    app = Flask(__name__)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_prefix=1, x_proto=1, x_for=1, x_host=1)
    app.config["MAX_CONTENT_LENGTH"] = TAMANHO_MAX_FOTO
    app.config["JSON_AS_ASCII"] = False
    app.config["CONFIG_PROJETO"] = config

    secret = cfg.get("secret_key")
    if not secret:
        # Sem segredo configurado os tokens morrem a cada restart -- serve
        # pra desenvolvimento, nunca pra produção (aviso alto no log).
        secret = secrets.token_hex(32)
        logger.warning("api_motorista.secret_key ausente no config.yaml -- usando segredo temporário (tokens não sobrevivem a restart).")
    app.config["SECRET_TOKENS"] = secret
    app.config["PASTA_COMPROVANTES"] = Path(cfg.get("pasta_comprovantes") or (_RAIZ / "dados" / "comprovantes"))
    app.config["GCS_ATIVO"] = bool(cfg.get("gcs_ativo", True))
    # Validação automática de nitidez/NF das fotos (Hugo, 12/09):
    # DESLIGADA por padrão -- liga em api_motorista.validacao_fotos.ativo.
    app.config["VALIDACAO_FOTOS"] = validacao_fotos.config_validacao(config)
    if app.config["VALIDACAO_FOTOS"]["ativo"]:
        logger.info("Validação automática de fotos LIGADA (modelo %s, nitidez mínima %s).",
                    app.config["VALIDACAO_FOTOS"]["modelo"], app.config["VALIDACAO_FOTOS"]["nitidez_minima"])

    # ── conexão por request ──────────────────────────────────────────────────
    def conn():
        if "conn" not in g:
            g.conn = banco.conectar()
        return g.conn

    @app.teardown_appcontext
    def _fechar(_exc):
        c = g.pop("conn", None)
        if c is not None:
            c.close()

    # ── log de requisições (11/09: pra enxergar o que o celular manda) ───────
    # waitress não loga requisição; sem isso o piloto fica cego (o teste do
    # Hugo mandou eventos que nunca chegaram e não havia como saber se
    # bateram na API). Uma linha por request, exceto /api/saude.
    logger.setLevel(logging.INFO)

    @app.before_request
    def _marcar_inicio():
        g.inicio = time.monotonic()

    @app.after_request
    def _logar(resp):
        if request.path != "/api/saude":
            ms = int((time.monotonic() - getattr(g, "inicio", time.monotonic())) * 1000)
            agente = (getattr(g, "motorista", None) or {}).get("agent_id")
            logger.info("%s %s -> %s %dms agent=%s ip=%s len=%s",
                        request.method, request.full_path.rstrip("?"), resp.status_code, ms, agente,
                        request.remote_addr, request.content_length)
        return resp

    # ── erros ────────────────────────────────────────────────────────────────
    @app.errorhandler(OperacaoInvalida)
    def _erro_operacao(e):
        logger.info("Regra recusou %s %s: %s", request.method, request.path, e.mensagem)
        return jsonify({"erro": e.mensagem}), e.codigo

    @app.errorhandler(AutenticacaoInvalida)
    def _erro_auth(e):
        logger.info("Auth recusou %s %s: %s", request.method, request.path, e.mensagem)
        return jsonify({"erro": e.mensagem}), e.codigo

    @app.errorhandler(404)
    def _404(_e):
        return jsonify({"erro": "Rota não encontrada."}), 404

    @app.errorhandler(413)
    def _413(_e):
        return jsonify({"erro": "Arquivo grande demais (máx. 15 MB)."}), 413

    @app.errorhandler(Exception)
    def _erro_geral(e):
        logger.exception("Erro não tratado na API do motorista")
        return jsonify({"erro": "Erro interno."}), 500

    # ── auth ─────────────────────────────────────────────────────────────────
    def requer_motorista(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            cabecalho = request.headers.get("Authorization", "")
            if not cabecalho.startswith("Bearer "):
                raise AutenticacaoInvalida("Faça login.")
            g.motorista = auth.motorista_do_token_acesso(app.config["SECRET_TOKENS"], cabecalho[7:].strip(), conn())
            return f(*args, **kwargs)
        return wrapper

    def corpo() -> dict:
        dados = request.get_json(silent=True)
        return dados if isinstance(dados, dict) else {}

    def agent_id() -> int | None:
        return g.motorista.get("agent_id")

    # ── endpoints ────────────────────────────────────────────────────────────
    @app.get("/api/saude")
    def saude():
        conn().execute("SELECT 1")
        return jsonify({"ok": True, "hora": banco.agora()})

    @app.post("/api/login")
    def login():
        d = corpo()
        m = auth.autenticar(conn(), d.get("cpf", ""), d.get("pin", ""))
        return jsonify({**auth.emitir_tokens(app.config["SECRET_TOKENS"], m), "motorista": auth.publico(m)})

    @app.post("/api/refresh")
    def refresh():
        d = corpo()
        tokens = auth.renovar(app.config["SECRET_TOKENS"], d.get("refresh", ""), conn())
        return jsonify(tokens)

    @app.get("/api/eu")
    @requer_motorista
    def eu():
        return jsonify(auth.publico(g.motorista))

    @app.post("/api/push-token")
    @requer_motorista
    def push_token():
        token = (corpo().get("token") or "").strip()[:300] or None
        conn().execute("UPDATE motoristas SET push_token = ?, atualizado_em = ? WHERE cpf = ?",
                       (token, banco.agora(), g.motorista["cpf"]))
        conn().commit()
        return jsonify({"ok": True})

    @app.get("/api/checklist")
    @requer_motorista
    def checklist():
        return jsonify({**operacao.carregar_checklist(conn()),
                        "validacao_fotos": validacao_fotos.publico(app.config["VALIDACAO_FOTOS"])})

    @app.get("/api/rotas")
    @requer_motorista
    def rotas():
        hoje = date.today()
        de = _data(request.args.get("de"), hoje)
        ate = _data(request.args.get("ate"), hoje + timedelta(days=1))
        return jsonify({"rotas": operacao.listar_rotas_motorista(conn(), agent_id(), de, ate)})

    @app.get("/api/rotas/<int:rota_id>")
    @requer_motorista
    def rota(rota_id):
        r = operacao.rota_do_motorista(conn(), rota_id, agent_id())
        return jsonify(operacao.montar_rota(conn(), r))

    @app.post("/api/rotas/<int:rota_id>/aceitar")
    @requer_motorista
    def aceitar(rota_id):
        d = corpo()
        return jsonify(operacao.aceitar_rota(conn(), rota_id, agent_id(), d.get("ocorrido_em"),
                                             d.get("latitude"), d.get("longitude"), d.get("uuid")))

    @app.post("/api/rotas/<int:rota_id>/recusar")
    @requer_motorista
    def recusar(rota_id):
        d = corpo()
        return jsonify(operacao.recusar_rota(conn(), rota_id, agent_id(), d.get("motivo"), d.get("ocorrido_em"), d.get("uuid")))

    @app.post("/api/rotas/<int:rota_id>/iniciar")
    @requer_motorista
    def iniciar(rota_id):
        d = corpo()
        return jsonify(operacao.iniciar_rota(conn(), rota_id, agent_id(), d.get("ocorrido_em"),
                                             d.get("latitude"), d.get("longitude"), d.get("uuid")))

    @app.post("/api/rotas/<int:rota_id>/finalizar")
    @requer_motorista
    def finalizar(rota_id):
        d = corpo()
        return jsonify(operacao.finalizar_rota(conn(), rota_id, agent_id(), d.get("km_informado"), d.get("pedagio"),
                                               d.get("observacoes"), d.get("ocorrido_em"), d.get("uuid")))

    @app.post("/api/paradas/<int:parada_id>/eventos")
    @requer_motorista
    def evento_parada(parada_id):
        resultado = operacao.registrar_evento_parada(conn(), parada_id, agent_id(), corpo())
        return jsonify(resultado), (200 if resultado["ja_registrado"] else 201)

    def _extensao_foto(arquivo) -> str:
        ext = (arquivo.filename.rsplit(".", 1)[-1] if "." in arquivo.filename else "jpg").lower()
        if ext not in EXTENSOES_FOTO:
            raise OperacaoInvalida(f"Extensão não aceita: .{ext}")
        return ext

    def _guardar_foto(conteudo: bytes, codigo_pasta: str, tipo: str, uuid: str, ext: str) -> tuple[str, str | None]:
        """Grava em disco (pasta_comprovantes/<codigo>/) e, best-effort, no
        GCS. Devolve (caminho relativo em disco, caminho GCS ou None)."""
        sha = hashlib.sha256(conteudo).hexdigest()
        pasta = app.config["PASTA_COMPROVANTES"] / secure_filename(codigo_pasta)
        pasta.mkdir(parents=True, exist_ok=True)
        nome = f"{tipo.lower()}_{secure_filename(uuid or sha[:16])}.{ext}"
        caminho = pasta / nome
        caminho.write_bytes(conteudo)

        caminho_gcs = None
        if app.config["GCS_ATIVO"] and app.config["CONFIG_PROJETO"].get("gcs"):
            try:
                from documentos_pedido.storage_gcs import enviar_documento
                caminho_gcs = enviar_documento(app.config["CONFIG_PROJETO"], caminho, codigo_pasta, tipo.capitalize())
            except Exception as e:
                logger.warning(f"Foto {nome} salva em disco, mas não subiu pro GCS: {e}")
        relativo = str(caminho.relative_to(_RAIZ)) if _RAIZ in caminho.parents else str(caminho)
        return relativo, caminho_gcs

    def _validar_conteudo(tipo: str, conteudo: bytes, sha: str, *, parada=None, rota_id=None,
                          nf=None, valor=None, no_ato=True) -> dict | None:
        """Roda (ou reaproveita) a validação da foto. Devolve o dict do
        resultado ou None quando não há nada a registrar.

        Reaproveita por sha256: a foto conferida no ato (POST /api/fotos/validar,
        com o motorista ainda no cliente) chega depois pela fila offline e
        NÃO é cobrada de novo no modelo."""
        cfg = app.config["VALIDACAO_FOTOS"]
        ja = validacao_fotos.buscar_validacao(conn(), sha)
        if ja:
            return {"resultado": ja["resultado"], "motivo": ja["motivo"], "nitidez": ja["nitidez"],
                    "modelo": ja["modelo"], "no_ato": bool(ja["no_ato"]), **ja.get("dados", {})}
        if not cfg["ativo"]:
            return None
        nfs = validacao_fotos.nfs_do_pedido(conn(), parada["codigo"]) if parada is not None else []
        r = validacao_fotos.validar_foto(cfg, tipo, conteudo, nfs_esperadas=nfs, nf_alvo=nf, valor_informado=valor)
        validacao_fotos.registrar_validacao(
            conn(), sha, tipo, r, agent_id(),
            f"parada:{parada['id']}" if parada is not None else (f"rota:{rota_id}" if rota_id else None),
            no_ato=no_ato)
        conn().commit()
        return {**r, "no_ato": no_ato}

    @app.post("/api/fotos/validar")
    @requer_motorista
    def validar_foto_endpoint():
        """Confere a foto ANTES do envio definitivo, com o motorista ainda
        no cliente (Hugo, 12/09). Multipart: arquivo, tipo (CANHOTO |
        PEDAGIO), parada_id (canhoto) ou rota_id (pedágio), nf (a NF que
        ESTA foto deve mostrar -- um canhoto por NF), valor (pedágio),
        tentativa (1, 2, ...).

        Nunca grava a foto: só devolve o veredito e guarda o resultado
        por sha256 pra a fila não pagar o modelo de novo. Com a validação
        desligada devolve NAO_VERIFICADO e pode_seguir=true -- o app segue
        exatamente como hoje."""
        cfg = app.config["VALIDACAO_FOTOS"]
        arquivo = request.files.get("arquivo")
        if arquivo is None or not arquivo.filename:
            raise OperacaoInvalida("Envie o arquivo no campo 'arquivo' (multipart).")
        tipo = (request.form.get("tipo") or validacao_fotos.TIPO_CANHOTO).upper()
        _extensao_foto(arquivo)
        try:
            tentativa = int(request.form.get("tentativa") or 1)
        except ValueError:
            tentativa = 1

        parada = None
        rota_id = None
        if request.form.get("parada_id"):
            parada = conn().execute("SELECT id, codigo, rota_id FROM nucleo_paradas WHERE id = ?",
                                    (request.form["parada_id"],)).fetchone()
            if not parada:
                raise OperacaoInvalida("Parada não encontrada.", 404)
            operacao.rota_do_motorista(conn(), parada["rota_id"], agent_id())
        elif request.form.get("rota_id"):
            rota_id = operacao.rota_do_motorista(conn(), int(request.form["rota_id"]), agent_id())["id"]

        conteudo = arquivo.read()
        sha = hashlib.sha256(conteudo).hexdigest()
        valor = request.form.get("valor")
        try:
            valor_f = float(str(valor).replace(",", ".")) if valor else None
        except ValueError:
            valor_f = None
        r = _validar_conteudo(tipo, conteudo, sha, parada=parada, rota_id=rota_id,
                              nf=request.form.get("nf"), valor=valor_f, no_ato=True)
        if r is None:
            r = {"resultado": validacao_fotos.NAO_VERIFICADO, "motivo": "Validação automática desligada."}
        return jsonify({**validacao_fotos.aplicar_tentativas(cfg, r, tentativa), "sha256": sha})

    @app.post("/api/paradas/<int:parada_id>/comprovantes")
    @requer_motorista
    def comprovante(parada_id):
        arquivo = request.files.get("arquivo")
        if arquivo is None or not arquivo.filename:
            raise OperacaoInvalida("Envie o arquivo no campo 'arquivo' (multipart).")
        tipo = (request.form.get("tipo") or "CANHOTO").upper()
        uuid = (request.form.get("uuid") or "").strip()
        capturado_em = request.form.get("capturado_em") or banco.agora()
        ext = _extensao_foto(arquivo)

        # Valida posse/idempotência ANTES de gravar em disco
        ja = conn().execute("SELECT id FROM nucleo_comprovantes WHERE uuid = ?", (uuid,)).fetchone() if uuid else None
        if ja:
            return jsonify({"id": ja["id"], "ja_registrado": True})
        p = conn().execute("SELECT id, codigo, rota_id FROM nucleo_paradas WHERE id = ?", (parada_id,)).fetchone()
        if not p:
            raise OperacaoInvalida("Parada não encontrada.", 404)
        operacao.rota_do_motorista(conn(), p["rota_id"], agent_id())

        conteudo = arquivo.read()
        sha = hashlib.sha256(conteudo).hexdigest()
        codigo = p["codigo"] or f"parada-{parada_id}"
        # Já validada no ato? reaproveita. Senão (motorista sem sinal na
        # hora), valida agora marcando no_ato=false -- o painel vê que a
        # conferência não aconteceu com o motorista no cliente.
        validacao = _validar_conteudo(tipo, conteudo, sha, parada=p, nf=request.form.get("nf"), no_ato=False) \
            if tipo == validacao_fotos.TIPO_CANHOTO else None
        caminho, caminho_gcs = _guardar_foto(conteudo, codigo, tipo, uuid or sha[:16], ext)
        resultado = operacao.registrar_comprovante(
            conn(), parada_id, agent_id(), tipo, uuid or sha, caminho, sha, len(conteudo), capturado_em, caminho_gcs,
            validacao=validacao,
        )
        return jsonify({**resultado, "gcs": caminho_gcs is not None,
                        "validacao": (validacao or {}).get("resultado")}), 201

    @app.post("/api/rotas/<int:rota_id>/pedagios")
    @requer_motorista
    def pedagio(rota_id):
        """Pedágio da rota (Hugo, 11/09): valor + foto do recibo, um por
        recibo. Fica PENDENTE até o painel aprovar. Multipart: arquivo
        (foto, obrigatória), valor, uuid, capturado_em."""
        arquivo = request.files.get("arquivo")
        if arquivo is None or not arquivo.filename:
            raise OperacaoInvalida("Envie a foto do recibo no campo 'arquivo' (multipart).")
        uuid = (request.form.get("uuid") or "").strip()
        valor = request.form.get("valor")
        capturado_em = request.form.get("capturado_em") or banco.agora()
        ext = _extensao_foto(arquivo)

        ja = conn().execute("SELECT id FROM nucleo_pedagios WHERE uuid = ?", (uuid,)).fetchone() if uuid else None
        if ja:
            return jsonify({"id": ja["id"], "ja_registrado": True})
        rota = operacao.rota_do_motorista(conn(), rota_id, agent_id())

        conteudo = arquivo.read()
        sha = hashlib.sha256(conteudo).hexdigest()
        try:
            valor_f = float(str(valor).replace(",", ".")) if valor else None
        except ValueError:
            valor_f = None
        validacao = _validar_conteudo(validacao_fotos.TIPO_PEDAGIO, conteudo, sha, rota_id=rota["id"],
                                      valor=valor_f, no_ato=False)
        caminho, caminho_gcs = _guardar_foto(conteudo, f"rota-{rota['id']}", "PEDAGIO", uuid or sha[:16], ext)
        resultado = operacao.registrar_pedagio(
            conn(), rota_id, agent_id(), uuid or sha, valor, caminho, sha, len(conteudo), capturado_em, caminho_gcs,
            validacao=validacao,
        )
        return jsonify({**resultado, "gcs": caminho_gcs is not None, "pedagios": operacao.listar_pedagios(conn(), rota_id)}), 201

    @app.post("/api/rotas/<int:rota_id>/pedagios/<int:pedagio_id>/cancelar")
    @requer_motorista
    def cancelar_pedagio(rota_id, pedagio_id):
        """Motorista desiste de um pedágio que mandou por engano. Só enquanto
        PENDENTE (409 se o painel já revisou). Devolve a lista atualizada."""
        return jsonify({"pedagios": operacao.cancelar_pedagio(conn(), rota_id, pedagio_id, agent_id())})

    @app.post("/api/gps")
    @requer_motorista
    def gps():
        pontos = corpo().get("pontos") or []
        if not isinstance(pontos, list):
            raise OperacaoInvalida("Envie {'pontos': [...]}.")
        return jsonify({"novos": operacao.registrar_gps(conn(), agent_id(), pontos[:2000])})

    @app.get("/api/ofertas")
    @requer_motorista
    def ofertas():
        return jsonify(operacao.listar_ofertas(conn(), agent_id()))

    @app.post("/api/ofertas/<int:rascunho_id>/escolher")
    @requer_motorista
    def escolher(rascunho_id):
        return jsonify(operacao.escolher_oferta(conn(), agent_id(), rascunho_id))

    @app.post("/api/ofertas/<int:rascunho_id>/cancelar")
    @requer_motorista
    def cancelar_escolha(rascunho_id):
        return jsonify(operacao.cancelar_escolha_oferta(conn(), agent_id(), rascunho_id))

    @app.get("/api/disponibilidade")
    @requer_motorista
    def disponibilidade():
        hoje = date.today()
        de = _data(request.args.get("de"), hoje)
        ate = _data(request.args.get("ate"), hoje + timedelta(days=30))
        return jsonify({"ajustes": operacao.listar_disponibilidade(conn(), agent_id(), de, ate)})

    @app.put("/api/disponibilidade")
    @requer_motorista
    def definir_disponibilidade():
        d = corpo()
        de = _data(d.get("de") or d.get("data"), date.today())
        ate = _data(d.get("ate"), de)
        n = operacao.definir_disponibilidade(conn(), agent_id(), de, ate, d.get("disponivel"), d.get("motivo"))
        return jsonify({"dias": n, "ajustes": operacao.listar_disponibilidade(conn(), agent_id(), de, ate)})

    @app.get("/api/financeiro")
    @requer_motorista
    def extrato():
        hoje = date.today()
        de = _data(request.args.get("de"), hoje.replace(day=1))
        ate = _data(request.args.get("ate"), hoje)
        tipo = g.motorista.get("tipo_veiculo")
        if agent_id() is None:
            return jsonify({"linhas": [], "por_dia": [], "total": 0.0, "aviso": "Motorista sem agent_id."})
        ex = financeiro.extrato_motorista(agent_id(), de, ate, tipo, conn=conn())
        ex["tarifa"] = (tarifa_motorista.calcular_valor_rota(tipo, 0, tarifa_motorista.carregar_tarifas(conn())) or None)
        if ex["tarifa"]:
            ex["tarifa"] = ex["tarifa"].como_dict()
        return jsonify(ex)

    return app


# `waitress-serve --call nucleo.api_motorista:criar_app` chama a factory;
# `app` abaixo serve pro modo desenvolvimento e pra quem preferir
# `waitress-serve nucleo.api_motorista:app`.
app = None
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--porta", type=int, default=8073)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s - %(message)s")
    app = criar_app()
    app.run(host="127.0.0.1", port=args.porta, debug=False)
else:
    app = None
