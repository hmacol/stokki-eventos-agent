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
  --pausa 45,180       pausa aleatoria (segundos) entre um envio e outro
Nao altera o config.yaml: o modo vem da flag. Link de piloto nao conta como link em aberto.
"""
import argparse
import logging
import random
import re
import sys
import time
from datetime import datetime, timedelta

import notificar_whatsapp
from nucleo import atualizacao_motorista as atu, banco

logger = logging.getLogger("nucleo.enviar_pendencias_motoristas")

USUARIO = "envio_pendencias"
_TESTE = re.compile(r"teste|lalamove|virtual", re.IGNORECASE)
_PARAM_ENCERRAM = ("teto", "canal_em_pausa")
INTERVALO_DIAS = 2             # modo --recorrente: dias entre uma mensagem e a seguinte (Hugo, 23/11)
TENTATIVA_AVISO_BLOQUEIO = 4   # da 4a mensagem em diante o texto avisa o bloqueio no dia seguinte


def _telefone(conn, pessoa: dict) -> str | None:
    bruto = next((l.get("TELEFONE_MOTORISTA") for l in pessoa["linhas"] if atu._digitos(l.get("TELEFONE_MOTORISTA"))), None)
    if not bruto:
        ficha = atu.ficha_vigente(conn, pessoa["principal_id"]) or {}
        bruto = ficha.get("telefone")
    digitos = atu._digitos(bruto)
    return digitos if 10 <= len(digitos) <= 13 else None


def historico(conn, principal_id: int) -> tuple[int, datetime | None]:
    """(mensagens reais ja enviadas, data/hora da ultima) a esta pessoa, por qualquer id dela. Piloto nao conta."""
    ids = atu._ids_da_pessoa(conn, principal_id)
    row = conn.execute(f"SELECT COUNT(*), MAX(enviado_em) FROM motoristas_links_atualizacao "
                       f"WHERE bd_motorista_id IN ({','.join('?' * len(ids))}) AND piloto = 0 "
                       "AND envio_situacao = 'enviado'", ids).fetchone()
    return row[0], (datetime.strptime(row[1], "%Y-%m-%d %H:%M:%S") if row[1] else None)


def _preenchendo(conn, principal_id: int, agora: datetime) -> bool:
    """Abriu o link nos ultimos INTERVALO_DIAS: gerar outro descartaria o rascunho dele."""
    link = atu.link_em_aberto(conn, principal_id)
    return bool(link and link.get("aberto_em") and
                datetime.strptime(link["aberto_em"], "%Y-%m-%d %H:%M:%S") > agora - timedelta(days=INTERVALO_DIAS))


def selecionar(conn, so_ids: set | None = None, recorrente: bool = False,
               agora: datetime | None = None) -> tuple[list[dict], list[dict]]:
    """(vao_receber, ficam_de_fora). Cada item: principal_id, nome, telefone, pendencias, tentativa
    (+ motivo nos de fora). `recorrente`: quem ja tem link em aberto volta a receber depois de
    INTERVALO_DIAS da ultima mensagem (o link novo cancela o antigo), salvo quem esta preenchendo."""
    agora = agora or datetime.now()
    recebem, fora = [], []
    for pessoa in atu.pessoas(conn):
        if not pessoa["ativo"] or (so_ids and pessoa["principal_id"] not in so_ids):
            continue
        pend = atu.pendencias(conn, pessoa)
        if not (pend["documentos"] or pend["campos"]):
            continue
        enviados, ultimo = historico(conn, pessoa["principal_id"])
        item = {"principal_id": pessoa["principal_id"], "nome": pessoa["nome"], "pendencias": pend,
                "tentativa": enviados + 1}
        telefone = _telefone(conn, pessoa)
        if _TESTE.search(pessoa["nome"]):
            motivo = "cadastro de teste/virtual"
        elif not pessoa["cpf"]:
            motivo = "sem CPF"
        elif not telefone:
            motivo = "sem telefone"
        elif recorrente and ultimo and (agora.date() - ultimo.date()).days < INTERVALO_DIAS:
            motivo = f"mensagem enviada ha menos de {INTERVALO_DIAS} dias"
        elif recorrente and _preenchendo(conn, pessoa["principal_id"], agora):
            motivo = "esta preenchendo (abriu o link ha pouco)"
        elif not recorrente and atu.link_em_aberto(conn, pessoa["principal_id"]):
            motivo = "ja tem link em aberto"
        else:
            recebem.append({**item, "telefone": telefone})
            continue
        fora.append({**item, "motivo": motivo})
    return recebem, fora


def enviar(conn, config: dict, destinatarios: list[dict], url_base: str, *,
           avisar=notificar_whatsapp.avisar_motorista_pendencias_privado, pausa=(0, 0),
           dormir=time.sleep) -> list[dict]:
    """Um link e uma mensagem por motorista. Mensagem que nao saiu cancela o link dele e o lote segue;
    teto/pausa do canal interrompem (o resto sai 'nao_tentado'). So 'enviado' deixa o link vivo.
    `pausa` = (min, max) segundos sorteados entre uma tentativa e a proxima."""
    destino = notificar_whatsapp.destino_link_atualizacao(config)
    if destino is None:
        return [{"principal_id": d["principal_id"], "situacao": "desligado"} for d in destinatarios]
    validade = int(((config.get("atualizacao_motoristas") or {}).get("validade_dias")) or atu.VALIDADE_DIAS)
    resultado, parado, tentou = [], False, False
    for d in destinatarios:
        pid = d["principal_id"]
        if parado:
            resultado.append({"principal_id": pid, "nome": d["nome"], "situacao": "nao_tentado"})
            continue
        if tentou:
            dormir(random.uniform(*pausa))
        tentou = True
        link_id = None
        try:
            link = atu.gerar_link(conn, pid, USUARIO, validade_dias=validade, piloto=bool(destino))
            link_id = link["id"]
            primeiro = next(iter(d["nome"].split()), "").capitalize()
            texto = notificar_whatsapp.texto_pendencias_motorista(
                primeiro, d["pendencias"]["documentos"], d["pendencias"]["campos"],
                f"{url_base.rstrip('/')}/atualizar/{link['token']}",
                datetime.strptime(link["expira_em"], "%Y-%m-%d %H:%M:%S").date(),
                bloqueio=(datetime.now() + timedelta(days=1)).date()
                if d.get("tentativa", 1) >= TENTATIVA_AVISO_BLOQUEIO else None)
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
        resultado.append({"principal_id": pid, "nome": d["nome"], "situacao": situacao,
                          "tentativa": d.get("tentativa", 1)})
        parado = situacao in _PARAM_ENCERRAM
    return resultado


def teto_do_lote(conn, atu_cfg: dict, tamanho: int) -> int:
    """Teto do dia pro envio real: o lote sai inteiro, entao o teto tem de cobrir o que JA saiu hoje
    (mesma origem, que o teto conta) mais o tamanho do lote. Comparar so com o tamanho do lote
    barrava o 1o envio quando o dia ja tinha mensagens (22/11: 5 motoristas ficaram sem link)."""
    ja_enviados = notificar_whatsapp._enviados_hoje(conn, datetime.now(), notificar_whatsapp.ORIGEM_MOTORISTA_ATUALIZACAO)
    return max(int(atu_cfg.get("teto_diario") or notificar_whatsapp.TETO_ATUALIZACAO_PADRAO), ja_enviados + tamanho)


def avisar_hugo(config: dict, resultado: list[dict], avisar=notificar_whatsapp.avisar_hugo_pendencias_motoristas) -> str | None:
    """WhatsApp pro Hugo com quem recebeu a mensagem que avisa o bloqueio (so as 'enviado' dessa rodada)."""
    casos = [r for r in resultado if r["situacao"] == "enviado" and r.get("tentativa", 1) >= TENTATIVA_AVISO_BLOQUEIO]
    if not casos:
        return None
    return avisar(casos, (datetime.now() + timedelta(days=1)).date(), config)


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
    parser.add_argument("--recorrente", action="store_true",
                        help=f"rotina: reenvia a quem segue com pendencia a cada {INTERVALO_DIAS} dias (novo link cancela o antigo); "
                             f"da {TENTATIVA_AVISO_BLOQUEIO}a mensagem avisa o bloqueio e o Hugo e informado por WhatsApp")
    parser.add_argument("--ids", help="principal_id separados por virgula")
    parser.add_argument("--pausa", default="0,0", help="MIN,MAX segundos sorteados entre um envio e outro")
    args = parser.parse_args(argv)
    pausa = tuple(float(x) for x in args.pausa.split(","))
    if len(pausa) != 2 or pausa[0] < 0 or pausa[1] < pausa[0]:
        parser.error("--pausa precisa ser MIN,MAX com MIN <= MAX")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s - %(message)s")
    so_ids = {int(x) for x in args.ids.split(",") if x.strip()} if args.ids else None
    if args.piloto and not re.sub(r"\D", "", args.piloto):
        parser.error("--piloto precisa de um numero")

    config = _carregar_config()
    conn = banco.conectar()
    try:
        recebem, fora = selecionar(conn, so_ids, recorrente=args.recorrente)
        print(f"Vao receber ({len(recebem)}):")
        for d in recebem:
            p = d["pendencias"]
            print(f"  {d['principal_id']:>3} {d['nome']} | {d['tentativa']}a mensagem | docs: {', '.join(p['documentos']) or '-'} | dados: {', '.join(p['campos']) or '-'}")
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
        atu_cfg = {**(config.get("atualizacao_motoristas") or {}),
                   "forcar_destino_whatsapp": args.piloto if args.piloto else ""}
        if args.real:   # o lote sai inteiro (as pausas espalham o envio)
            atu_cfg["teto_diario"] = teto_do_lote(conn, atu_cfg, len(recebem))
        resultado = enviar(conn, {**config, "atualizacao_motoristas": atu_cfg}, recebem, _url_base(config),
                           pausa=pausa)
        print("\nResultado:")
        for r in resultado:
            print(f"  {r['principal_id']:>3} {r.get('nome', '')} -> {r['situacao']}")
        if args.recorrente and args.real:
            avisar_hugo(config, resultado)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
