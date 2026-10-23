# -*- coding: utf-8 -*-
"""
portal_cliente/consulta_transportadora_web.py

Pagina publica do link que o embarcador recebe por e-mail/WhatsApp quando um
pedido dele sai com transportadora desconhecida (consulta_transportadora.py).
Sem login: o token assinado e a prova. O embarcador diz se a transportadora
vem retirar no galpao (RETIRADA), se levamos ate ela (TERCEIROS, com o
endereco) ou se o pedido segue pro endereco dele mesmo (ENTREGA).

    GET|POST /transportadora/<token>           formulario / confirmacao (PUBLICO)
    GET      /transportadora/<token>/cep/<cep> preenche o endereco pelo CEP (PUBLICO)

Registrado em app.py por `registrar(app, ...)`, mesmo padrao de cotacao_web.py.
"""
import logging
import sqlite3

from flask import jsonify, render_template, request

import consulta_transportadora as ctr
import cotacao as ct
import transportadoras_cadastro as tc

logger = logging.getLogger("portal_cliente.consulta_transportadora_web")

_OPCOES = {"retirada": "RETIRADA", "redespacho": "TERCEIROS", "entrega": "ENTREGA"}
_ROTULOS = {"RETIRADA": "Retirada no galpão", "TERCEIROS": "Redespacho (levamos até a transportadora)",
            "ENTREGA": "Entrega no endereço do pedido"}
_OBRIGATORIOS = (("cep", "CEP"), ("logradouro", "rua"), ("numero", "número"), ("municipio", "cidade"), ("uf", "UF"))
_MAX_CAMPO = 200
_MAX_NOME = 120
_CAMPOS_ENDERECO = ("cep", "logradouro", "numero", "complemento", "bairro", "municipio", "uf")


def _apelido(conn, embarcador_cnpj: str) -> str:
    chave = str(embarcador_cnpj or "")
    por_id = chave.startswith("stkkc:")
    try:
        row = conn.execute(
            "SELECT COALESCE(NULLIF(apelido,''), NULLIF(nome_remetente,'')) FROM interno WHERE "
            + ("stkkc_id=?" if por_id else "cnpj_embarcador=?"), (chave[6:] if por_id else chave,)).fetchone()
    except sqlite3.OperationalError:
        row = None
    return row[0] if row and row[0] else chave


def _endereco_texto(t: dict) -> str:
    rua = " ".join(x for x in (t.get("logradouro"), t.get("numero")) if x)
    if t.get("complemento"):
        rua += f" ({t['complemento']})"
    cidade = "/".join(x for x in (t.get("municipio"), t.get("uf")) if x)
    return " - ".join(x for x in (rua, t.get("bairro"), cidade, t.get("cep")) if x)


def registrar(app, *, config: dict, secret: str, db_path=None):
    validade = ctr.config_consulta(config)["validade_dias"]

    def _conectar():
        return ctr.conectar_consultas(db_path) if db_path else ctr.conectar_consultas()

    def _pagina(estado, **ctx):
        return render_template("consulta_transportadora.html", estado=estado, **ctx)

    def _definida(transp):
        return _pagina("ja_respondida", transp=transp, tipo_rotulo=_ROTULOS.get(transp["tipo"], transp["tipo"]),
                       endereco=_endereco_texto(transp) if transp["tipo"] == "TERCEIROS" else "")

    @app.route("/transportadora/<token>/cep/<cep>")
    def consulta_transportadora_cep(token, cep):
        cid = ctr.ler_token(secret, token, validade)
        if cid is None or cid < 0:
            return jsonify({"erro": "Link inválido."}), 404
        d = ct.normalizar_cep(cep)
        if not d:
            return jsonify({"erro": "CEP inválido: use 8 dígitos."}), 400
        achado = ct.consultar_cep(d)
        if not achado:
            return jsonify({"erro": "CEP não encontrado. Preencha o endereço manualmente."}), 404
        return jsonify(achado)

    @app.route("/transportadora/<token>", methods=["GET", "POST"])
    def consulta_transportadora_token(token):
        cid = ctr.ler_token(secret, token, validade)
        if cid == -1:
            return _pagina("expirado")
        if cid is None:
            return _pagina("invalido")
        conn = _conectar()
        try:
            consulta = conn.execute("SELECT * FROM transportadora_consultas WHERE id=?", (cid,)).fetchone()
            transp = tc.buscar(conn, consulta["transportadora_id"]) if consulta else None
            if not transp:
                return _pagina("invalido")
            if transp["situacao"] == "ATIVA":
                return _definida(transp)
            if transp["situacao"] != "PENDENTE" or consulta["respondido_em"]:
                return _pagina("indisponivel")
            emb = consulta["embarcador_cnpj"]
            ctx = dict(transp=transp, pedidos=ctr.pedidos_parados(conn, transp["id"], emb), form={})
            if request.method != "POST":
                return _pagina("ok", erro=None, **ctx)

            f = {k: (request.form.get(k) or "").strip()[:_MAX_CAMPO]
                 for k in ("opcao", "nome", "email_transportadora") + _CAMPOS_ENDERECO}
            f["nome"] = f["nome"][:_MAX_NOME]
            ctx["form"] = f
            if f["opcao"] not in _OPCOES:
                return _pagina("ok", erro="Escolha uma das opções.", **ctx)
            if not f["nome"]:
                return _pagina("ok", erro="Informe seu nome pra registrar a resposta.", **ctx)
            tipo = _OPCOES[f["opcao"]]
            endereco = None
            if tipo == "TERCEIROS":
                faltam = [rot for k, rot in _OBRIGATORIOS if not f[k]]
                if faltam:
                    return _pagina("ok", erro="Preencha o endereço da transportadora: falta " + ", ".join(faltam) + ".", **ctx)
                endereco = {k: f[k] for k in _CAMPOS_ENDERECO}
                endereco["uf"] = endereco["uf"].upper()
                endereco["cep"] = ct.formatar_cep(endereco["cep"])
            por = f"{f['nome']} ({_apelido(conn, emb)}) pelo link"
            try:
                novo = ctr.responder(conn, transp["id"], tipo, endereco,
                                     f["email_transportadora"] if tipo == "TERCEIROS" else "", por, "link")
            except tc.ErroCadastro as e:
                if "ja respondida" in str(e):  # outra pessoa respondeu entre o GET e o POST
                    return _definida(tc.buscar(conn, transp["id"]))
                # o texto pode citar outra transportadora (conflito): so vai pro log
                logger.warning("consulta %s: resposta recusada: %s", cid, e)
                return _pagina("ok", erro="Não foi possível registrar agora. Fale com a equipe Fresh Log.", **ctx)
            return _pagina("respondida_agora", transp=novo, tipo_rotulo=_ROTULOS[tipo], nome=f["nome"],
                           endereco=_endereco_texto(novo) if tipo == "TERCEIROS" else "")
        finally:
            conn.close()
