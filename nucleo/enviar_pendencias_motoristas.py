# -*- coding: utf-8 -*-
"""
enviar_pendencias_motoristas.py

Mensagem PESSOAL (privado do WhatsApp) pra cada motorista ativo com pendencia cadastral,
dizendo o que falta e com o link pessoal de atualizacao (nucleo/atualizacao_motorista.py).

Quem recebe: ativo, com CPF (o link pede o CPF pra abrir), com telefone, com pendencia, sem link
real em aberto e fora dos cadastros de teste/virtuais. Quem fica de fora sai listado com o motivo.

Uso (sempre na VPS como www-data, venv/bin/python -m nucleo.enviar_pendencias_motoristas):
  sem flag             so lista quem receberia e mostra um texto de exemplo (nada e gerado nem enviado)
  --piloto NUMERO      manda TODAS as mensagens pro NUMERO (com '[teste -> nome]' na frente)
  --real               manda no privado de cada motorista
  --ids 5,14           restringe a estes principal_id
Nao altera o config.yaml: o modo vem da flag. Link de piloto nao conta como link em aberto.
"""
import argparse
import logging
import re
import sys
from datetime import datetime

import notificar_whatsapp
from nucleo import atualizacao_motorista as atu, banco

logger = logging.getLogger("nucleo.enviar_pendencias_motoristas")

USUARIO = "envio_pendencias"
_TESTE = re.compile(r"teste|lalamove|virtual", re.IGNORECASE)
_PARAM_ENCERRAM = ("teto", "canal_em_pausa")


def _telefone(conn, pessoa: dict) -> str | None:
    bruto = next((l.get("TELEFONE_MOTORISTA") for l in pessoa["linhas"] if atu._digitos(l.get("TELEFONE_MOTORISTA"))), None)
    if not bruto:
        ficha = atu.ficha_vigente(conn, pessoa["principal_id"]) or {}
        bruto = ficha.get("telefone")
    digitos = atu._digitos(bruto)
    return digitos if 10 <= len(digitos) <= 13 else None


def selecionar(conn, so_ids: set | None = None) -> tuple[list[dict], list[dict]]:
    """(vao_receber, ficam_de_fora). Cada item: principal_id, nome, telefone, pendencias (+ motivo nos de fora)."""
    recebem, fora = [], []
    for pessoa in atu.pessoas(conn):
        if not pessoa["ativo"] or (so_ids and pessoa["principal_id"] not in so_ids):
            continue
        pend = atu.pendencias(conn, pessoa)
        if not (pend["documentos"] or pend["campos"]):
            continue
        item = {"principal_id": pessoa["principal_id"], "nome": pessoa["nome"], "pendencias": pend}
        telefone = _telefone(conn, pessoa)
        if _TESTE.search(pessoa["nome"]):
            motivo = "cadastro de teste/virtual"
        elif not pessoa["cpf"]:
            motivo = "sem CPF"
        elif not telefone:
            motivo = "sem telefone"
        elif atu.link_em_aberto(conn, pessoa["principal_id"]):
            motivo = "ja tem link em aberto"
        else:
            recebem.append({**item, "telefone": telefone})
            continue
        fora.append({**item, "motivo": motivo})
    return recebem, fora


def enviar(conn, config: dict, destinatarios: list[dict], url_base: str, *,
           avisar=notificar_whatsapp.avisar_motorista_pendencias_privado) -> list[dict]:
    """Um link e uma mensagem por motorista. Mensagem que nao saiu cancela o link dele e o lote segue;
    teto/pausa do canal interrompem (o resto sai 'nao_tentado'). So 'enviado' deixa o link vivo."""
    destino = notificar_whatsapp.destino_link_atualizacao(config)
    if destino is None:
        return [{"principal_id": d["principal_id"], "situacao": "desligado"} for d in destinatarios]
    validade = int(((config.get("atualizacao_motoristas") or {}).get("validade_dias")) or atu.VALIDADE_DIAS)
    resultado, parado = [], False
    for d in destinatarios:
        pid = d["principal_id"]
        if parado:
            resultado.append({"principal_id": pid, "nome": d["nome"], "situacao": "nao_tentado"})
            continue
        link_id = None
        try:
            link = atu.gerar_link(conn, pid, USUARIO, validade_dias=validade, piloto=bool(destino))
            link_id = link["id"]
            primeiro = next(iter(d["nome"].split()), "").capitalize()
            texto = notificar_whatsapp.texto_pendencias_motorista(
                primeiro, d["pendencias"]["documentos"], d["pendencias"]["campos"],
                f"{url_base.rstrip('/')}/atualizar/{link['token']}",
                datetime.strptime(link["expira_em"], "%Y-%m-%d %H:%M:%S").date())
            situacao, _ = avisar(link_id, d["telefone"], d["nome"], texto, config)
            agora = banco.agora()
            enviado = situacao == "enviado"
            conn.execute("UPDATE motoristas_links_atualizacao SET envio_destino = ?, envio_situacao = ?, "
                         "enviado_em = ?, cancelado_em = ? WHERE id = ?",
                         (f"{destino or d['telefone']}@c.us", situacao, agora if enviado else None,
                          None if enviado else agora, link_id))
            conn.commit()
        except Exception:
            logger.exception("Envio das pendencias do motorista %s falhou", pid)
            situacao = "falhou"
            if link_id is not None:
                conn.execute("UPDATE motoristas_links_atualizacao SET cancelado_em = ? WHERE id = ? "
                             "AND cancelado_em IS NULL", (banco.agora(), link_id))
                conn.commit()
        resultado.append({"principal_id": pid, "nome": d["nome"], "situacao": situacao})
        parado = situacao in _PARAM_ENCERRAM
    return resultado


def _url_base(config: dict) -> str:
    url = str((((config or {}).get("portal_cliente") or {}).get("chamados") or {}).get("url_painel")
              or "https://app.freshhub.com.br/painel").rstrip("/")
    return (url[:-len("/painel")] + "/motorista") if url.endswith("/painel") else "https://app.freshhub.com.br/motorista"


def _carregar_config() -> dict:
    from nucleo.cadastro_motorista_link import _carregar_config as carregar
    return carregar()


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Mensagem pessoal de pendencias cadastrais aos motoristas.")
    modo = parser.add_mutually_exclusive_group()
    modo.add_argument("--piloto", metavar="NUMERO", help="manda tudo pra este numero, marcado como teste")
    modo.add_argument("--real", action="store_true", help="manda no privado de cada motorista")
    parser.add_argument("--ids", help="principal_id separados por virgula")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s - %(message)s")
    so_ids = {int(x) for x in args.ids.split(",") if x.strip()} if args.ids else None
    if args.piloto and not re.sub(r"\D", "", args.piloto):
        parser.error("--piloto precisa de um numero")

    config = _carregar_config()
    conn = banco.conectar()
    try:
        recebem, fora = selecionar(conn, so_ids)
        print(f"Vao receber ({len(recebem)}):")
        for d in recebem:
            p = d["pendencias"]
            print(f"  {d['principal_id']:>3} {d['nome']} | docs: {', '.join(p['documentos']) or '-'} | dados: {', '.join(p['campos']) or '-'}")
        print(f"Ficam de fora ({len(fora)}):")
        for d in fora:
            print(f"  {d['principal_id']:>3} {d['nome']} - {d['motivo']}")
        if not (args.piloto or args.real):
            if recebem:
                p = recebem[0]["pendencias"]
                print("\nExemplo (modo lista, nada foi enviado):\n" + notificar_whatsapp.texto_pendencias_motorista(
                    recebem[0]["nome"].split()[0].capitalize(), p["documentos"], p["campos"],
                    f"{_url_base(config)}/atualizar/<token>", datetime.now().date()))
            return 0
        cfg = {**config, "atualizacao_motoristas": {**(config.get("atualizacao_motoristas") or {}),
                                                    "forcar_destino_whatsapp": args.piloto if args.piloto else ""}}
        resultado = enviar(conn, cfg, recebem, _url_base(config))
        print("\nResultado:")
        for r in resultado:
            print(f"  {r['principal_id']:>3} {r.get('nome', '')} -> {r['situacao']}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
