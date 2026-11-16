# -*- coding: utf-8 -*-
"""
nucleo/ponte_vuupt.py

Ponte nucleo -> Vuupt (Hugo, 12/09 e 12/11): ate a virada de chave, tudo
o que o motorista faz no NOSSO app e repetido na Vuupt, porque a Torre, a
expedicao na Stokki, o portal e os relatorios do financeiro ainda leem la.

Como funciona:
  - A rota continua nascendo na Vuupt (Planejamento) e espelhada no nucleo
    com provedor=VUUPT. Com a ponte ligada pro motorista, ela fica
    operavel no nosso app (nucleo/operacao.py).
  - Cada acao do app grava no nucleo (horario real do aparelho, GPS, fotos)
    e entra na fila `nucleo_ponte_vuupt`, na MESMA transacao.
  - Este modulo esvazia a fila: aceitar, iniciar deslocamento, chegada e
    resultado (com a foto do canhoto no checklist da Vuupt). Roda pelo
    timer stokki-ponte-vuupt (uma instancia por vez).

A Vuupt fora do ar nunca trava o motorista: o app fala so com a nossa
API; a fila tenta de novo com recuo e desiste depois de MAX_TENTATIVAS
(o timer sai com erro e o alerta de falha avisa).

Config (config.yaml):
    api_motorista:
      ponte_vuupt:
        ativo: true
        agentes: [28438]      # agent_id de quem opera pelo nosso app
        todos: false          # true = todo motorista com login
        espera_foto_min: 30   # quanto esperar a foto antes de concluir sem ela

Rodar:  python nucleo/ponte_vuupt.py [--modo-teste] [--situacao]
"""
import argparse
import io
import json
import logging
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
if str(_RAIZ) not in sys.path:
    sys.path.insert(0, str(_RAIZ))

from nucleo import banco

logger = logging.getLogger("nucleo.ponte_vuupt")

ACAO_ACEITAR = "ACEITAR"
ACAO_DESLOCAMENTO = "DESLOCAMENTO"
ACAO_CHEGADA = "CHEGADA"
ACAO_CONCLUIR = "CONCLUIR"

CAMPO_CANHOTO = 15086        # vuupt_checklist.CAMPO_CANHOTO
CAMPO_NOTA_RETORNO = 15349   # vuupt_checklist.CAMPO_NOTA_RETORNO
_CAMPO_DO_TIPO = {"CANHOTO": CAMPO_CANHOTO, "NF_DEVOLUCAO": CAMPO_NOTA_RETORNO}

STATUS_FECHADO = {"done", "canceled", "cancelled"}
FMT = "%Y-%m-%d %H:%M:%S"
MAX_TENTATIVAS = 10
RECUO_MIN = (1, 2, 5, 10, 15, 30, 30, 60, 60)   # minutos ate a proxima tentativa
TENTATIVAS_COM_CHECKLIST = 3                    # depois disso conclui sem a foto na Vuupt


class _Adiar(Exception):
    """Ainda nao da pra executar (foto a caminho): espera sem contar tentativa."""


# ── Config ────────────────────────────────────────────────────────────────────

_config: dict | None = None


def definir_config(config: dict | None) -> None:
    """A API e os testes dizem qual config vale; sem isso le o config.yaml."""
    global _config
    _config = config


def _config_projeto() -> dict:
    global _config
    if _config is None:
        lido = {}
        caminho = _RAIZ / "config.yaml"
        if caminho.exists():
            import yaml
            with open(caminho, encoding="utf-8") as f:
                lido = yaml.safe_load(f) or {}
        _config = lido
    return _config or {}


def config_ponte(config: dict | None = None) -> dict:
    bruto = ((config if config is not None else _config_projeto()).get("api_motorista") or {}).get("ponte_vuupt") or {}
    return {
        "ativo": bool(bruto.get("ativo")),
        "todos": bool(bruto.get("todos")),
        "agentes": {int(a) for a in (bruto.get("agentes") or [])},
        "espera_foto_min": int(bruto.get("espera_foto_min", 30)),
    }


def rota_operavel(rota, cfg: dict | None = None) -> bool:
    """Rota da Vuupt que o motorista opera pelo nosso app (a ponte repete la)."""
    cfg = cfg or config_ponte()
    return bool(cfg["ativo"] and rota["provedor"] == banco.PROVEDOR_VUUPT and rota["vuupt_route_id"]
                and (cfg["todos"] or rota["agent_id"] in cfg["agentes"]))


# ── Fila ──────────────────────────────────────────────────────────────────────

def enfileirar(conn: sqlite3.Connection, rota, acao: str, parada=None, dados: dict | None = None) -> None:
    """Sem commit: entra na transacao de quem registrou a acao do app."""
    conn.execute(
        "INSERT INTO nucleo_ponte_vuupt (rota_id, parada_id, service_id, acao, dados_json, criado_em) VALUES (?, ?, ?, ?, ?, ?)",
        (rota["id"], parada["id"] if parada is not None else None,
         parada["service_id"] if parada is not None else None, acao,
         json.dumps(dados, ensure_ascii=False, default=str) if dados else None, banco.agora()))


def marcar_operada(conn: sqlite3.Connection, rota_id: int) -> None:
    conn.execute("UPDATE nucleo_rotas SET operada_app = 1 WHERE id = ? AND operada_app = 0", (rota_id,))


def situacao(conn: sqlite3.Connection) -> dict:
    por_status = {r["status"]: r["n"] for r in conn.execute(
        "SELECT status, COUNT(*) AS n FROM nucleo_ponte_vuupt GROUP BY status")}
    pendentes = [dict(r) for r in conn.execute(
        "SELECT id, rota_id, parada_id, service_id, acao, tentativas, proxima_em, ultimo_erro, criado_em "
        "FROM nucleo_ponte_vuupt WHERE status != ? ORDER BY id DESC LIMIT 50", (banco.PONTE_FEITO,))]
    return {"por_status": por_status, "abertos": pendentes}


# ── Execucao ──────────────────────────────────────────────────────────────────

def _ler_servico(vuupt, service_id: int) -> dict:
    servico = vuupt.buscar_servico_por_id(service_id)
    if not servico:   # None no 404 E em falha de rede: nunca escreve no que nao leu
        raise RuntimeError(f"Vuupt nao devolveu o servico {service_id}")
    return servico.get("service", servico) if isinstance(servico, dict) else servico


def _status_pronto(vuupt, servico: dict, agent_id: int | None) -> str:
    """Rota agendada deixa o servico not_assigned: libera como a baixa sem app."""
    status = str(servico.get("status") or "")
    if status in ("", "not_assigned"):
        from nucleo import baixa_sem_app
        if servico.get("route_id"):
            baixa_sem_app._liberar_rota_agendada(vuupt, int(servico["route_id"]))
            status = str(_ler_servico(vuupt, int(servico["id"])).get("status") or "")
        elif agent_id:
            vuupt.atribuir_agente(int(servico["id"]), agent_id)
            status = "assigned"
    return status


def _juntar_fotos(conteudos: list[bytes]) -> bytes:
    """O campo do checklist da Vuupt aceita UMA imagem; o nosso app manda um
    canhoto por NF. Empilha as fotos numa so (as originais ficam conosco)."""
    from vuupt_checklist import para_jpeg
    if len(conteudos) == 1:
        return para_jpeg(conteudos[0])
    from PIL import Image
    imagens = [Image.open(io.BytesIO(para_jpeg(c))) for c in conteudos]
    largura = max(i.width for i in imagens)
    imagens = [i if i.width == largura else i.resize((largura, round(i.height * largura / i.width))) for i in imagens]
    tela = Image.new("RGB", (largura, sum(i.height for i in imagens)), "white")
    y = 0
    for i in imagens:
        tela.paste(i, (0, y))
        y += i.height
    buf = io.BytesIO()
    tela.save(buf, "JPEG", quality=85)
    return buf.getvalue()


def _fotos_da_parada(conn: sqlite3.Connection, parada_id: int, raiz: Path) -> tuple[int, dict[int, bytes]]:
    """(quantas fotos ja chegaram, {campo da Vuupt: jpeg}). Foto ilegivel fica de fora."""
    por_campo: dict[int, list[bytes]] = {}
    total = 0
    for c in conn.execute("SELECT tipo, caminho_local FROM nucleo_comprovantes WHERE parada_id = ? ORDER BY id", (parada_id,)):
        total += 1
        campo = _CAMPO_DO_TIPO.get(c["tipo"])
        if not campo or not c["caminho_local"]:
            continue
        caminho = Path(c["caminho_local"])
        try:
            por_campo.setdefault(campo, []).append((caminho if caminho.is_absolute() else raiz / caminho).read_bytes())
        except OSError as e:
            logger.warning("Ponte: foto %s da parada %s ilegivel: %s", c["caminho_local"], parada_id, e)
    fotos = {}
    for campo, conteudos in por_campo.items():
        try:
            fotos[campo] = _juntar_fotos(conteudos)
        except Exception as e:  # noqa: BLE001 -- arquivo que nao e foto nao trava a entrega
            logger.warning("Ponte: fotos do campo %s da parada %s nao viraram JPEG: %s", campo, parada_id, e)
    return total, fotos


def _executar(conn: sqlite3.Connection, vuupt, cfg: dict, item, raiz: Path, agora: datetime) -> str:
    rota = conn.execute("SELECT * FROM nucleo_rotas WHERE id = ?", (item["rota_id"],)).fetchone()
    if rota is None:
        return "rota_sumiu"
    dados = json.loads(item["dados_json"]) if item["dados_json"] else {}

    if item["acao"] == ACAO_ACEITAR:
        # So aceita o que ja esta atribuido: liberar rota agendada mudaria o start_at na vespera.
        aceitos = 0
        for p in conn.execute("SELECT service_id FROM nucleo_paradas WHERE rota_id = ? AND service_id IS NOT NULL "
                              "AND situacao = ? ORDER BY ordem", (rota["id"], banco.PARADA_PENDENTE)).fetchall():
            if str(_ler_servico(vuupt, p["service_id"]).get("status") or "") == "assigned":
                vuupt.avancar_como_agente(p["service_id"], "accepted", "assigned")
                aceitos += 1
        return f"aceitos={aceitos}"

    service_id = item["service_id"]
    if not service_id:
        return "sem_service_id"
    servico = _ler_servico(vuupt, service_id)
    if str(servico.get("status") or "") in STATUS_FECHADO:
        return "ja_fechado"

    if item["acao"] in (ACAO_DESLOCAMENTO, ACAO_CHEGADA):
        status = _status_pronto(vuupt, servico, rota["agent_id"])
        vuupt.avancar_como_agente(service_id, "on_route" if item["acao"] == ACAO_DESLOCAMENTO else "arrived", status)
        return "ok"

    if item["acao"] == ACAO_CONCLUIR:
        from nucleo import baixa_sem_app
        sucesso = dados.get("situacao") in (banco.PARADA_ENTREGUE, banco.PARADA_PARCIAL)
        fotos = None
        if sucesso:
            chegaram, fotos = _fotos_da_parada(conn, item["parada_id"], raiz)
            esperadas = int(dados.get("fotos_esperadas") or 0)
            criado = datetime.strptime(item["criado_em"], FMT)
            if chegaram < esperadas and agora - criado < timedelta(minutes=cfg["espera_foto_min"]):
                raise _Adiar(f"esperando foto ({chegaram}/{esperadas})")
        failed_reason_id = None
        if not sucesso and dados.get("motivo_id") is not None:
            m = conn.execute("SELECT vuupt_failed_reason_id FROM motivos_ocorrencia WHERE id = ?",
                             (dados["motivo_id"],)).fetchone()
            failed_reason_id = m["vuupt_failed_reason_id"] if m else None
        info: dict = {}
        try:
            r = baixa_sem_app.concluir_servico(vuupt, service_id, sucesso, failed_reason_id, rota["agent_id"],
                                               fotos=fotos or None, info=info)
        except Exception as e:  # noqa: BLE001
            # Checklist que a Vuupt recusa nao pode segurar a entrega pra sempre:
            # depois de algumas tentativas conclui sem a foto la (a foto esta conosco).
            if not (fotos and type(e).__name__ == "ChecklistNaoGravado"
                    and item["tentativas"] + 1 >= TENTATIVAS_COM_CHECKLIST):
                raise
            logger.warning("Ponte: checklist do servico %s recusado (%s); concluindo sem foto na Vuupt.", service_id, e)
            r = baixa_sem_app.concluir_servico(vuupt, service_id, sucesso, failed_reason_id, rota["agent_id"])
            return f"{r} sem_checklist"
        if not sucesso:
            return r
        return f"{r} checklist={info['checklist_id']}" if info.get("checklist_id") else f"{r} sem_foto"

    return "acao_desconhecida"


def processar_fila(conn: sqlite3.Connection, vuupt, cfg: dict | None = None, raiz: Path | None = None,
                   agora: datetime | None = None, limite: int = 60) -> dict:
    """Esvazia a fila em ordem. Item que falhou ou espera segura os seguintes
    do MESMO servico (a ordem aceitar -> chegar -> concluir importa)."""
    cfg = cfg or config_ponte()
    raiz = raiz or _RAIZ
    agora = agora or datetime.now()
    agora_txt = agora.strftime(FMT)
    stats = {"feitos": 0, "adiados": 0, "erros": 0, "desistiu": 0}
    segurados: set = set()
    itens = conn.execute("SELECT * FROM nucleo_ponte_vuupt WHERE status = ? ORDER BY id", (banco.PONTE_PENDENTE,)).fetchall()
    for item in itens:
        chave = item["service_id"] or f"rota:{item['rota_id']}"
        if chave in segurados:
            continue
        if item["proxima_em"] and item["proxima_em"] > agora_txt:
            segurados.add(chave)
            continue
        if stats["feitos"] + stats["erros"] >= limite:
            break
        try:
            resultado = _executar(conn, vuupt, cfg, item, raiz, agora)
        except _Adiar as a:
            conn.execute("UPDATE nucleo_ponte_vuupt SET proxima_em = ?, ultimo_erro = ? WHERE id = ?",
                         ((agora + timedelta(minutes=1)).strftime(FMT), str(a), item["id"]))
            conn.commit()
            segurados.add(chave)
            stats["adiados"] += 1
            continue
        except Exception as e:  # noqa: BLE001 -- um item com erro nao para a fila
            tentativas = item["tentativas"] + 1
            desistiu = tentativas >= MAX_TENTATIVAS
            proxima = agora + timedelta(minutes=RECUO_MIN[min(tentativas, len(RECUO_MIN)) - 1])
            conn.execute("UPDATE nucleo_ponte_vuupt SET tentativas = ?, ultimo_erro = ?, proxima_em = ?, status = ? WHERE id = ?",
                         (tentativas, str(e)[:500], proxima.strftime(FMT),
                          banco.PONTE_DESISTIU if desistiu else banco.PONTE_PENDENTE, item["id"]))
            conn.commit()
            logger.warning("Ponte: item %s (%s, servico %s) falhou (%s/%s): %s", item["id"], item["acao"],
                           item["service_id"], tentativas, MAX_TENTATIVAS, e)
            stats["desistiu" if desistiu else "erros"] += 1
            if not desistiu:
                segurados.add(chave)
            continue
        conn.execute("UPDATE nucleo_ponte_vuupt SET status = ?, resultado = ?, feito_em = ?, ultimo_erro = NULL WHERE id = ?",
                     (banco.PONTE_FEITO, resultado, banco.agora(), item["id"]))
        conn.commit()
        stats["feitos"] += 1
        logger.info("Ponte: item %s (%s, servico %s) -> %s", item["id"], item["acao"], item["service_id"], resultado)
    return stats


# ── CLI (timer stokki-ponte-vuupt) ────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Repete na Vuupt o que os motoristas fizeram no app.")
    parser.add_argument("--modo-teste", action="store_true", help="so mostra a fila; nao chama a Vuupt")
    parser.add_argument("--situacao", action="store_true", help="mostra a fila e sai")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s - %(message)s")

    config = _config_projeto()
    cfg = config_ponte(config)
    conn = banco.conectar()
    try:
        if args.situacao or args.modo_teste:
            s = situacao(conn)
            print(f"ponte ativa={cfg['ativo']} todos={cfg['todos']} agentes={sorted(cfg['agentes'])} fila={s['por_status']}")
            for i in s["abertos"]:
                print(f"  #{i['id']} {i['acao']} rota={i['rota_id']} servico={i['service_id']} "
                      f"tentativas={i['tentativas']} proxima={i['proxima_em']} erro={i['ultimo_erro']}")
            return 0
        if not conn.execute("SELECT 1 FROM nucleo_ponte_vuupt WHERE status = ? LIMIT 1", (banco.PONTE_PENDENTE,)).fetchone():
            return 0
        token = (config.get("vuupt_api") or {}).get("token")
        if not token:
            logger.error("vuupt_api.token ausente no config.yaml")
            return 1
        from vuupt_client import VuuptClient
        stats = processar_fila(conn, VuuptClient(token), cfg)
        logger.info("Ponte: %s", stats)
        return 2 if stats["desistiu"] else 0   # saida 2 = alguem precisa olhar (alerta de falha)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
