# -*- coding: utf-8 -*-
"""
fila_app_motoristas.py

Fila de mensagens PESSOAIS (privado do WhatsApp) pra levar os motoristas ao app novo (22/11):
  atualizar = ja entrou no app, mas nunca informou a versao ou informou uma abaixo do alvo (APK antigo);
  adotar    = ativo que nunca entrou no app (roda pela Vuupt/grupo): o app novo passa a ser o padrao.
Quem passa a informar versao >= alvo sai da fila como 'adotou'. Quem nao adotou recebe 1 lembrete
depois de LEMBRETE_DIAS (no maximo 2 mensagens por motorista). O app informa a versao no header
X-App-Versao (auth_motorista.registrar_versao_app -> motoristas.app_versao).

Uso (na VPS como www-data, venv/bin/python -m nucleo.fila_app_motoristas):
  sem flag              enfileira quem falta e mostra a fila e os textos (nada e enviado)
  --piloto NUMERO       manda o lote do dia pro NUMERO, marcado como teste (a fila nao muda)
  --real                manda o lote do dia no privado de cada motorista e atualiza a fila
  --limite 20           tamanho do lote do dia (padrao 20)
  --pausa 45,180        pausa aleatoria (segundos) entre um envio e outro
Mesma trava e teto diario do envio de pendencias (atualizacao_motoristas.forcar_destino_whatsapp):
o modo vem da flag, o config.yaml nao e alterado.
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
from nucleo.enviar_pendencias_motoristas import _TESTE, _PARAM_ENCERRAM, _carregar_config, _telefone

logger = logging.getLogger("nucleo.fila_app_motoristas")

VERSAO_ALVO = "1.2.0"
LEMBRETE_DIAS = 3
MAX_ENVIOS = 2
LINK_APK = "https://app.freshhub.com.br/motorista/app.apk"
LINK_IPHONE = "https://app.freshhub.com.br/motorista/web"

_DDL = """
CREATE TABLE IF NOT EXISTS motoristas_fila_app (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    cpf             TEXT NOT NULL UNIQUE,
    nome            TEXT NOT NULL,
    campanha        TEXT NOT NULL,                 -- atualizar | adotar
    situacao        TEXT NOT NULL DEFAULT 'pendente',  -- pendente | enviado | adotou | sem_whatsapp
    envios          INTEGER NOT NULL DEFAULT 0,
    ultimo_envio_em TEXT,
    criado_em       TEXT NOT NULL
)
"""


def texto_atualizar(primeiro_nome: str) -> str:
    return (f"Olá, {primeiro_nome}! Aqui é a Fresh Log. Você está com uma versão antiga do app de motoristas "
            f"e ela vai parar de funcionar direito. Atualize agora, é rápido:\n"
            f"• Android: baixe a versão nova em {LINK_APK} e instale por cima.\n"
            f"• iPhone: abra {LINK_IPHONE} no Safari.\n"
            f"Qualquer dúvida, é só responder esta mensagem.")


def texto_adotar(primeiro_nome: str) -> str:
    return (f"Olá, {primeiro_nome}! Aqui é a Fresh Log. A partir de agora o app de motoristas da Fresh Log "
            f"é o nosso padrão para receber e fazer as rotas. Instale e entre com seu CPF:\n"
            f"• Android: {LINK_APK}\n"
            f"• iPhone: abra {LINK_IPHONE} no Safari.\n"
            f"Se não souber seu PIN de acesso ou tiver qualquer dúvida, responda esta mensagem que a gente ajuda.")


def _versao(texto) -> tuple:
    try:
        return tuple(int(p) for p in str(texto or "").split("."))
    except ValueError:
        return ()


def adotou(app_versao) -> bool:
    v = _versao(app_versao)
    return bool(v) and v >= _versao(VERSAO_ALVO)


def _estado_app(conn, cpf: str) -> dict | None:
    row = conn.execute("SELECT app_versao, ultimo_login_em FROM motoristas WHERE cpf = ?", (cpf,)).fetchone()
    return dict(row) if row else None


def enfileirar(conn) -> dict:
    """Poe na fila todo motorista ativo que ainda nao esta no app novo e nao esta na fila.
    Devolve {'novos': [...], 'fora': [(nome, motivo)...]}."""
    conn.execute(_DDL)
    ja = {r[0] for r in conn.execute("SELECT cpf FROM motoristas_fila_app")}
    novos, fora = [], []
    for pessoa in atu.pessoas(conn):
        if not pessoa["ativo"] or not pessoa["cpf"] or pessoa["cpf"] in ja:
            continue
        if _TESTE.search(pessoa["nome"]):
            continue
        estado = _estado_app(conn, pessoa["cpf"])
        if estado and adotou(estado["app_versao"]):
            continue
        if not _telefone(conn, pessoa):
            fora.append((pessoa["nome"], "sem telefone"))
            continue
        campanha = "atualizar" if estado and estado["ultimo_login_em"] else "adotar"
        conn.execute("INSERT INTO motoristas_fila_app (cpf, nome, campanha, criado_em) VALUES (?, ?, ?, ?)",
                     (pessoa["cpf"], pessoa["nome"], campanha, banco.agora()))
        novos.append((pessoa["nome"], campanha))
    conn.commit()
    return {"novos": novos, "fora": fora}


def lote_do_dia(conn, limite: int, agora: datetime | None = None) -> list[dict]:
    """Primeiro quem nunca recebeu (ordem de entrada); depois os lembretes vencidos."""
    conn.execute(_DDL)
    corte = ((agora or datetime.now()) - timedelta(days=LEMBRETE_DIAS)).strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute(
        "SELECT * FROM motoristas_fila_app WHERE situacao = 'pendente' "
        "OR (situacao = 'enviado' AND envios < ? AND ultimo_envio_em <= ?) "
        "ORDER BY envios, id LIMIT ?", (MAX_ENVIOS, corte, limite * 3)).fetchall()
    pessoas = {p["cpf"]: p for p in atu.pessoas(conn) if p["cpf"]}
    lote = []
    for r in rows:
        estado = _estado_app(conn, r["cpf"])
        if estado and adotou(estado["app_versao"]):
            conn.execute("UPDATE motoristas_fila_app SET situacao = 'adotou' WHERE id = ?", (r["id"],))
            continue
        pessoa = pessoas.get(r["cpf"])
        telefone = _telefone(conn, pessoa) if pessoa and pessoa["ativo"] else None
        if telefone and len(lote) < limite:
            lote.append({**dict(r), "telefone": telefone})
    conn.commit()
    return lote


def enviar(conn, config: dict, lote: list[dict], *, real: bool,
           avisar=notificar_whatsapp.avisar_motorista_pendencias_privado, pausa=(0, 0),
           dormir=time.sleep) -> list[dict]:
    """Uma mensagem por motorista. No modo real atualiza a fila; no piloto a fila nao muda.
    Teto/pausa do canal interrompem o lote (o resto fica pendente pra amanha)."""
    if notificar_whatsapp.destino_link_atualizacao(config) is None:
        return [{"cpf": d["cpf"], "situacao": "desligado"} for d in lote]
    resultado, parado, tentou = [], False, False
    for d in lote:
        if parado:
            resultado.append({"cpf": d["cpf"], "nome": d["nome"], "situacao": "nao_tentado"})
            continue
        if tentou:
            dormir(random.uniform(*pausa))
        tentou = True
        primeiro = next(iter(d["nome"].split()), "").capitalize()
        texto = (texto_atualizar if d["campanha"] == "atualizar" else texto_adotar)(primeiro)
        try:
            situacao, _ = avisar(d["id"], d["telefone"], d["nome"], texto, config,
                                 assinatura=f"fila_app:{d['cpf']}:{d['envios'] + 1}")
        except Exception:
            logger.exception("Envio da fila do app ao motorista %s falhou", d["cpf"])
            situacao = "falhou"
        if real and situacao == "enviado":
            conn.execute("UPDATE motoristas_fila_app SET situacao = 'enviado', envios = envios + 1, "
                         "ultimo_envio_em = ? WHERE id = ?", (banco.agora(), d["id"]))
        elif real and situacao == "numero_sem_whatsapp":
            conn.execute("UPDATE motoristas_fila_app SET situacao = 'sem_whatsapp' WHERE id = ?", (d["id"],))
        conn.commit()
        resultado.append({"cpf": d["cpf"], "nome": d["nome"], "situacao": situacao})
        parado = situacao in _PARAM_ENCERRAM
    return resultado


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Fila de mensagens pra levar os motoristas ao app novo.")
    modo = parser.add_mutually_exclusive_group()
    modo.add_argument("--piloto", metavar="NUMERO", help="manda o lote pra este numero, marcado como teste")
    modo.add_argument("--real", action="store_true", help="manda no privado de cada motorista")
    parser.add_argument("--limite", type=int, default=20, help="tamanho do lote do dia")
    parser.add_argument("--pausa", default="0,0", help="MIN,MAX segundos sorteados entre um envio e outro")
    args = parser.parse_args(argv)
    pausa = tuple(float(x) for x in args.pausa.split(","))
    if len(pausa) != 2 or pausa[0] < 0 or pausa[1] < pausa[0]:
        parser.error("--pausa precisa ser MIN,MAX com MIN <= MAX")
    if args.piloto and not re.sub(r"\D", "", args.piloto):
        parser.error("--piloto precisa de um numero")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s - %(message)s")

    config = _carregar_config()
    conn = banco.conectar()
    try:
        fila = enfileirar(conn)
        print(f"Entraram na fila agora: {len(fila['novos'])} | de fora (sem telefone): {len(fila['fora'])}")
        total = dict(conn.execute("SELECT situacao, COUNT(*) FROM motoristas_fila_app GROUP BY situacao").fetchall())
        print(f"Fila: {total}")
        lote = lote_do_dia(conn, args.limite)
        print(f"Lote do dia ({len(lote)}):")
        for d in lote:
            print(f"  {d['nome']} | {d['campanha']} | envios={d['envios']}")
        if not (args.piloto or args.real):
            print("\nTexto 'atualizar':\n" + texto_atualizar("Ana") + "\n\nTexto 'adotar':\n" + texto_adotar("Ana"))
            return 0
        atu_cfg = {**(config.get("atualizacao_motoristas") or {}),
                   "forcar_destino_whatsapp": args.piloto if args.piloto else ""}
        if args.real:
            atu_cfg["teto_diario"] = max(int(atu_cfg.get("teto_diario") or 15), len(lote))
        resultado = enviar(conn, {**config, "atualizacao_motoristas": atu_cfg}, lote, real=bool(args.real), pausa=pausa)
        print("\nResultado:")
        for r in resultado:
            print(f"  {r.get('nome', r['cpf'])} -> {r['situacao']}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
