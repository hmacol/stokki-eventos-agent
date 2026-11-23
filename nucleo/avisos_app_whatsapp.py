# -*- coding: utf-8 -*-
"""
avisos_app_whatsapp.py

Avisos por WhatsApp (privado) pro motorista que usa o app pelo NAVEGADOR (PWA, iPhone). O servidor nao manda
push (nem pro Android); no iPhone o PWA nao recebe nada com o app fechado, entao o WhatsApp faz esse papel.
Quem e "web": motoristas.plataforma = 'web' (o app informa no header X-App-Plataforma).

Avisos (rotas de hoje e de amanha do motorista, e o chat de Ajuda):
  nova_rota       rota que o motorista ainda nao sabia que tinha
  rota_alterada   entrou/saiu pedido ou mudou o horario de saida (baixa do proprio motorista NAO conta)
  rota_cancelada  rota que ele ja sabia e foi cancelada
  chamado         a logistica (equipe) respondeu no chat de Ajuda e ele nao leu no app em 3 minutos

Regras: a rota so e avisada depois de ficar igual por duas rodadas seguidas (a operacao costuma mexer logo
depois de enviar); motorista que acabou de virar 'web' nao recebe o que ja existia (semente silenciosa);
nada e gravado como avisado antes do envio dar certo.

Trava e teto proprios (notificar_whatsapp.avisar_motorista_app_privado): config.yaml
  avisos_app_motoristas:
    forcar_destino_whatsapp: ausente = DESLIGADO | "NUMERO" = piloto (tudo vai pra la) | "" = real no privado
    teto_diario: 40

Uso (na VPS como www-data, venv/bin/python -m nucleo.avisos_app_whatsapp):
  sem flag             mostra o que avisaria agora (nada e enviado, nada e gravado)
  --exemplos [NUMERO]  manda os 4 textos de exemplo pro NUMERO (padrao: numero de piloto do config), marcados como teste
  --real               roda de verdade conforme a trava do config (timer a cada 5 min)
"""
import argparse
import hashlib
import json
import logging
import random
import re
import sys
import time
from datetime import date, datetime, timedelta

import notificar_whatsapp
from nucleo import banco

logger = logging.getLogger("nucleo.avisos_app_whatsapp")

LINK_APP = "https://motorista.freshhub.com.br"
ESPERA_CHAMADO_MIN = 3                       # da tempo do motorista ler no app antes de mandar o WhatsApp
_PARAM_ENCERRAM = ("teto", "canal_em_pausa")
_SITUACOES_FINAIS = ("enviado", "numero_sem_whatsapp")    # nao adianta tentar de novo

_DDL = """
CREATE TABLE IF NOT EXISTS avisos_app_motoristas (
    cpf          TEXT PRIMARY KEY,
    iniciado_em  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS avisos_app_rotas (
    cpf               TEXT NOT NULL,
    rota_id           INTEGER NOT NULL,
    vista             TEXT,                       -- assinatura vista na ultima rodada (estabilidade)
    avisada           TEXT,                       -- assinatura de que o motorista ja foi avisado (NULL = nunca)
    estado_json       TEXT,                       -- {"saida": "07:00", "codigos": [...]} do que foi avisado
    cancelada_avisada INTEGER NOT NULL DEFAULT 0,
    atualizado_em     TEXT NOT NULL,
    PRIMARY KEY (cpf, rota_id)
);
CREATE TABLE IF NOT EXISTS avisos_app_chamados (
    cpf            TEXT NOT NULL,
    chamado_id     INTEGER NOT NULL,
    ultima_msg_id  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (cpf, chamado_id)
);
"""


# ── textos ──────────────────────────────────────────────────────────────────────────

def _primeiro_nome(nome: str) -> str:
    return next(iter(str(nome or "").split()), "").capitalize() or "motorista"


def _quando(data_rota: str, hoje: date) -> str:
    d = date.fromisoformat(data_rota)
    if d == hoje:
        return f"hoje ({d:%d/%m})"
    if d == hoje + timedelta(days=1):
        return f"amanhã ({d:%d/%m})"
    return f"do dia {d:%d/%m}"


def _paradas(n: int) -> str:
    return "1 parada" if n == 1 else f"{n} paradas"


def _pedidos(n: int) -> str:
    return "1 pedido" if n == 1 else f"{n} pedidos"


def texto_nova_rota(nome: str, quando: str, n: int, saida: str | None) -> str:
    return (f"Olá, {_primeiro_nome(nome)}! Aqui é a Fresh Log. Você tem uma rota {quando}: {_paradas(n)}"
            f"{f', saída às {saida}' if saida else ''}.\nAbra o app para ver os detalhes: {LINK_APP}")


def texto_rota_alterada(nome: str, quando: str, n: int, entraram: int, sairam: int,
                        saida: str | None, saida_mudou: bool) -> str:
    mudou = []
    if entraram:
        mudou.append(f"entraram {_pedidos(entraram)}" if entraram > 1 else "entrou 1 pedido")
    if sairam:
        mudou.append(f"saiu {_pedidos(sairam)}" if sairam == 1 else f"saíram {_pedidos(sairam)}")
    if saida_mudou and saida:
        mudou.append(f"a saída agora é às {saida}")
    detalhe = f" Mudou: {', '.join(mudou)}." if mudou else ""
    return (f"Olá, {_primeiro_nome(nome)}! A sua rota {quando} foi alterada.{detalhe} "
            f"Agora são {_paradas(n)}.\nAbra o app para conferir: {LINK_APP}")


def texto_rota_cancelada(nome: str, quando: str) -> str:
    return (f"Olá, {_primeiro_nome(nome)}! A sua rota {quando} foi cancelada. Você não precisa fazer essa rota. "
            f"Se tiver qualquer dúvida, é só responder esta mensagem.")


def texto_chamado(nome: str, trecho: str) -> str:
    trecho = re.sub(r"\s+", " ", str(trecho or "")).strip()
    if len(trecho) > 220:
        trecho = trecho[:217].rstrip() + "..."
    return (f"Olá, {_primeiro_nome(nome)}! A logística respondeu o seu pedido de ajuda no app:\n“{trecho}”\n"
            f"Abra o app (aba Ajuda) para continuar a conversa: {LINK_APP}")


def exemplos() -> list[tuple[str, str]]:
    """Os quatro textos com dados inventados (pra o Hugo ver como chegam)."""
    hoje = date.today()
    return [
        ("nova_rota", texto_nova_rota("Maria", _quando(hoje.isoformat(), hoje), 8, "07:00")),
        ("rota_alterada", texto_rota_alterada("Maria", _quando((hoje + timedelta(days=1)).isoformat(), hoje), 9, 2, 1, "07:30", True)),
        ("rota_cancelada", texto_rota_cancelada("Maria", _quando(hoje.isoformat(), hoje))),
        ("chamado", texto_chamado("Maria", "Pode entregar na portaria, já avisamos o cliente que você está a caminho.")),
    ]


# ── o que vira aviso ────────────────────────────────────────────────────────────────

def _agora_str(agora: datetime) -> str:
    return agora.strftime("%Y-%m-%d %H:%M:%S")


def _motoristas_web(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT cpf, nome, telefone, agent_id FROM motoristas WHERE ativo = 1 AND plataforma = 'web' ORDER BY nome")]


def _rotas_do_motorista(conn, m: dict, hoje: date) -> list[dict]:
    """Rotas de hoje e de amanha (menos as concluidas) com a lista de pedidos que ainda valem."""
    rotas = []
    for r in conn.execute(
            "SELECT id, data_rota, start_at, status FROM nucleo_rotas WHERE data_rota IN (?, ?) AND status <> 'CONCLUIDA' "
            "AND (motorista_cpf = ? OR (agent_id IS NOT NULL AND agent_id = ?)) ORDER BY data_rota, id",
            (hoje.isoformat(), (hoje + timedelta(days=1)).isoformat(), m["cpf"], m["agent_id"])):
        codigos = [c[0] or f"#{c[1]}" for c in conn.execute(
            "SELECT codigo, id FROM nucleo_paradas WHERE rota_id = ? AND situacao <> 'CANCELADA' ORDER BY ordem, id", (r["id"],))]
        saida = (r["start_at"] or "")[11:16] or None
        assinatura = hashlib.sha1(json.dumps([saida, codigos]).encode()).hexdigest()[:12]
        rotas.append({"id": r["id"], "data": r["data_rota"], "status": r["status"], "saida": saida,
                      "codigos": codigos, "assinatura": assinatura})
    return rotas


def _semear(conn, m: dict, hoje: date, agora: datetime) -> None:
    """Motorista que acabou de virar 'web': o que ja existe conta como conhecido (nada de aviso do passado)."""
    cpf = m["cpf"]
    conn.execute("INSERT INTO avisos_app_motoristas (cpf, iniciado_em) VALUES (?, ?)", (cpf, _agora_str(agora)))
    for r in _rotas_do_motorista(conn, m, hoje):
        conn.execute(
            "INSERT OR IGNORE INTO avisos_app_rotas (cpf, rota_id, vista, avisada, estado_json, cancelada_avisada, atualizado_em) "
            "VALUES (?, ?, ?, ?, ?, 0, ?)",
            (cpf, r["id"], r["assinatura"], r["assinatura"], json.dumps({"saida": r["saida"], "codigos": r["codigos"]}),
             _agora_str(agora)))
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'portal_chamados_mensagens'").fetchone():
        for c in conn.execute(
                "SELECT c.id, COALESCE(MAX(m.id), 0) FROM portal_chamados c LEFT JOIN portal_chamados_mensagens m ON m.chamado_id = c.id "
                "WHERE c.tipo = 'MOTORISTA' AND c.motorista_cpf = ? GROUP BY c.id", (cpf,)).fetchall():
            conn.execute("INSERT OR IGNORE INTO avisos_app_chamados (cpf, chamado_id, ultima_msg_id) VALUES (?, ?, ?)", (cpf, c[0], c[1]))


def _avisos_de_rotas(conn, m: dict, hoje: date, agora: datetime) -> list[dict]:
    cpf, nome, avisos = m["cpf"], m["nome"], []
    for r in _rotas_do_motorista(conn, m, hoje):
        cancelada = r["status"] == "CANCELADA"
        if not r["codigos"] and not cancelada:
            continue                                         # rota ainda sem pedidos: espera
        linha = conn.execute("SELECT * FROM avisos_app_rotas WHERE cpf = ? AND rota_id = ?", (cpf, r["id"])).fetchone()
        quando = _quando(r["data"], hoje)
        if linha is None:
            if not cancelada:                                # 1a vez que vê: so guarda, espera estabilizar
                conn.execute("INSERT INTO avisos_app_rotas (cpf, rota_id, vista, atualizado_em) VALUES (?, ?, ?, ?)",
                             (cpf, r["id"], r["assinatura"], _agora_str(agora)))
            continue
        if cancelada:
            if linha["avisada"] and not linha["cancelada_avisada"]:
                avisos.append(_aviso(m, "rota_cancelada", f"rota:{r['id']}:cancelada", texto_rota_cancelada(nome, quando),
                                     lambda conn=conn, cpf=cpf, rid=r["id"], ag=agora: conn.execute(
                                         "UPDATE avisos_app_rotas SET cancelada_avisada = 1, atualizado_em = ? WHERE cpf = ? AND rota_id = ?",
                                         (_agora_str(ag), cpf, rid))))
            continue
        if linha["vista"] != r["assinatura"]:                # ainda mudando: espera a proxima rodada
            conn.execute("UPDATE avisos_app_rotas SET vista = ?, atualizado_em = ? WHERE cpf = ? AND rota_id = ?",
                         (r["assinatura"], _agora_str(agora), cpf, r["id"]))
            continue
        if linha["avisada"] == r["assinatura"]:
            continue
        guardar = (lambda conn=conn, cpf=cpf, r=r, ag=agora: conn.execute(
            "UPDATE avisos_app_rotas SET avisada = ?, estado_json = ?, atualizado_em = ? WHERE cpf = ? AND rota_id = ?",
            (r["assinatura"], json.dumps({"saida": r["saida"], "codigos": r["codigos"]}), _agora_str(ag), cpf, r["id"])))
        if not linha["avisada"]:
            avisos.append(_aviso(m, "nova_rota", f"rota:{r['id']}:{r['assinatura']}",
                                 texto_nova_rota(nome, quando, len(r["codigos"]), r["saida"]), guardar))
        else:
            antes = json.loads(linha["estado_json"] or "{}")
            antigos, novos = antes.get("codigos") or [], r["codigos"]
            avisos.append(_aviso(m, "rota_alterada", f"rota:{r['id']}:{r['assinatura']}",
                                 texto_rota_alterada(nome, quando, len(novos), len(set(novos) - set(antigos)),
                                                     len(set(antigos) - set(novos)), r["saida"], antes.get("saida") != r["saida"]),
                                 guardar))
    return avisos


def _avisos_de_chamados(conn, m: dict, agora: datetime) -> list[dict]:
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'portal_chamados_mensagens'").fetchone():
        return []
    corte = _agora_str(agora - timedelta(minutes=ESPERA_CHAMADO_MIN))
    avisos = []
    for c in conn.execute(
            "SELECT m.chamado_id AS chamado, MAX(m.id) AS ultimo FROM portal_chamados_mensagens m "
            "JOIN portal_chamados c ON c.id = m.chamado_id "
            "WHERE c.tipo = 'MOTORISTA' AND c.motorista_cpf = ? AND m.origem = 'equipe' AND m.lido_cliente = 0 AND m.criado_em <= ? "
            "AND m.id > COALESCE((SELECT a.ultima_msg_id FROM avisos_app_chamados a WHERE a.cpf = ? AND a.chamado_id = m.chamado_id), 0) "
            "GROUP BY m.chamado_id", (m["cpf"], corte, m["cpf"])).fetchall():
        texto = conn.execute("SELECT texto FROM portal_chamados_mensagens WHERE id = ?", (c["ultimo"],)).fetchone()[0]
        avisos.append(_aviso(m, "chamado", f"chamado:{c['chamado']}:{c['ultimo']}", texto_chamado(m["nome"], texto),
                             lambda conn=conn, cpf=m["cpf"], cid=c["chamado"], ult=c["ultimo"]: conn.execute(
                                 "INSERT INTO avisos_app_chamados (cpf, chamado_id, ultima_msg_id) VALUES (?, ?, ?) "
                                 "ON CONFLICT(cpf, chamado_id) DO UPDATE SET ultima_msg_id = excluded.ultima_msg_id", (cpf, cid, ult))))
    return avisos


def _aviso(m: dict, tipo: str, chave: str, texto: str, aplicar) -> dict:
    return {"cpf": m["cpf"], "nome": m["nome"], "telefone": m["telefone"], "tipo": tipo,
            "assinatura": f"aviso_app:{m['cpf']}:{tipo}:{chave}", "texto": texto, "aplicar": aplicar}


def coletar(conn, config: dict, agora: datetime | None = None) -> list[dict]:
    """O que deve ser avisado agora. Cada item: cpf, nome, telefone, tipo, assinatura, texto e `aplicar()`
    (grava que foi avisado -- so chamar depois do envio dar certo). Pode gravar estado interno (semente,
    estabilidade) mas nao faz commit: quem chama decide. Sem a trava no config: nao faz nada."""
    conn.executescript(_DDL)
    if notificar_whatsapp.destino_aviso_app(config) is None:
        return []
    agora = agora or datetime.now()
    hoje = agora.date()
    avisos = []
    for m in _motoristas_web(conn):
        if not conn.execute("SELECT 1 FROM avisos_app_motoristas WHERE cpf = ?", (m["cpf"],)).fetchone():
            _semear(conn, m, hoje, agora)
            continue
        avisos += _avisos_de_rotas(conn, m, hoje, agora) + _avisos_de_chamados(conn, m, agora)
    return avisos


# ── envio ───────────────────────────────────────────────────────────────────────────

def enviar(conn, config: dict, avisos: list[dict], *, avisar=notificar_whatsapp.avisar_motorista_app_privado,
           pausa=(0, 0), dormir=time.sleep) -> list[dict]:
    """Um WhatsApp por aviso. So marca como avisado quando enviou (ou o numero nao tem WhatsApp: tentar de
    novo nao adianta). Teto/pausa do canal interrompem o lote: o resto fica pra proxima rodada."""
    resultado, parado, tentou = [], False, False
    for a in avisos:
        if parado:
            resultado.append({**_resumo(a), "situacao": "nao_tentado"})
            continue
        if tentou:
            dormir(random.uniform(*pausa))
        tentou = True
        try:
            situacao, _ = avisar(a["nome"], a["telefone"], a["texto"], config, a["tipo"], a["assinatura"])
        except Exception:
            logger.exception("Aviso do app ao motorista %s falhou", a["cpf"])
            situacao = "falhou"
        if situacao in _SITUACOES_FINAIS:
            a["aplicar"]()
            conn.commit()
        if situacao == "numero_sem_whatsapp":
            logger.warning("Motorista %s sem WhatsApp no numero cadastrado: aviso %s descartado", a["cpf"], a["tipo"])
        resultado.append({**_resumo(a), "situacao": situacao})
        parado = situacao in _PARAM_ENCERRAM
    return resultado


def _resumo(a: dict) -> dict:
    return {"cpf": a["cpf"], "nome": a["nome"], "tipo": a["tipo"]}


# ── linha de comando ────────────────────────────────────────────────────────────────

def _carregar_config() -> dict:
    from nucleo.cadastro_motorista_link import _carregar_config as carregar
    return carregar()


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Avisos por WhatsApp pro motorista que usa o app no navegador (iPhone).")
    modo = parser.add_mutually_exclusive_group()
    modo.add_argument("--exemplos", nargs="?", const="", metavar="NUMERO", help="manda os 4 textos de exemplo pro NUMERO, marcados como teste")
    modo.add_argument("--real", action="store_true", help="roda conforme a trava do config (avisos_app_motoristas)")
    parser.add_argument("--pausa", default="5,15", help="MIN,MAX segundos sorteados entre um envio e outro")
    args = parser.parse_args(argv)
    pausa = tuple(float(x) for x in args.pausa.split(","))
    if len(pausa) != 2 or pausa[0] < 0 or pausa[1] < pausa[0]:
        parser.error("--pausa precisa ser MIN,MAX com MIN <= MAX")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s - %(message)s")

    config = _carregar_config()
    if args.exemplos is not None:
        return _enviar_exemplos(config, args.exemplos, pausa)

    conn = banco.conectar()
    try:
        if args.real:
            if notificar_whatsapp.destino_aviso_app(config) is None:
                print("DESLIGADO: sem avisos_app_motoristas.forcar_destino_whatsapp no config. Nada foi feito.")
                return 0
            avisos = coletar(conn, config)
            conn.commit()                                    # semente e estabilidade ficam gravadas
            for r in enviar(conn, config, avisos, pausa=pausa):
                print(f"{r['nome']} | {r['tipo']} -> {r['situacao']}")
            print(f"{len(avisos)} aviso(s) nesta rodada.")
            return 0
        simulado = {**config, "avisos_app_motoristas": {**(config.get("avisos_app_motoristas") or {}), "forcar_destino_whatsapp": "0"}}
        avisos = coletar(conn, simulado)
        conn.rollback()                                      # simulacao nao grava nada
        web = _motoristas_web(conn)
        print(f"Motoristas no app web: {len(web)} ({', '.join(m['nome'] for m in web) or 'nenhum'})")
        print(f"Avisaria agora: {len(avisos)}")
        for a in avisos:
            print(f"\n[{a['tipo']}] {a['nome']}:\n{a['texto']}")
        trava = notificar_whatsapp.destino_aviso_app(config)
        print("\nTrava no config:", "DESLIGADO (nada sai)" if trava is None else "piloto (tudo vai pro numero de teste)" if trava else "REAL (vai pro motorista)")
        return 0
    finally:
        conn.close()


def _enviar_exemplos(config: dict, numero: str, pausa: tuple) -> int:
    numero = re.sub(r"\D", "", numero or "") or notificar_whatsapp.destino_link_atualizacao(config) or ""
    if not numero:
        print("Informe o numero: --exemplos 5511999999999 (o config nao tem numero de piloto).")
        return 2
    cfg = {**config, "avisos_app_motoristas": {**(config.get("avisos_app_motoristas") or {}), "forcar_destino_whatsapp": numero}}
    carimbo = datetime.now().strftime("%Y%m%d%H%M%S")
    for i, (tipo, texto) in enumerate(exemplos()):
        if i:
            time.sleep(random.uniform(*pausa))
        situacao, _ = notificar_whatsapp.avisar_motorista_app_privado(
            "Exemplo", None, f"(exemplo de como chega)\n{texto}", cfg, tipo, f"aviso_app:exemplo:{tipo}:{carimbo}")
        print(f"{tipo} -> {situacao}")
        if situacao in _PARAM_ENCERRAM or situacao == "desligado":
            break
    return 0


if __name__ == "__main__":
    sys.exit(main())
