# -*- coding: utf-8 -*-
"""
documentacao_rota.py

Preparação AUTOMÁTICA da documentação de uma rota (pedido do Hugo,
09/09/2026): toda vez que uma rota é criada na VUUPT (ou muda de
conteúdo depois de criada), o romaneio dela -- capa + NFs + boletos +
canhoteira, o mesmo PDF do job das 04h -- é montado em segundo plano e
deixado pronto na pasta de romaneios da data, sem aparecer em tela
nenhuma. Quando alguém precisar (print-agent de manhã, botão da
expedição, consulta manual da pasta), já está lá.

O que a preparação faz, por rota:
  1. Busca a rota AO VIVO na VUUPT (buscar_rotas_do_dia) -- o romaneio
     sempre reflete o que está na rota de verdade, não o rascunho.
  2. Se algum pedido da rota ainda está SEM Nota Fiscal no banco de
     documentos (documentos_processados), roda o fluxo de documentos
     escopado só nesses pedidos (documentos_pedido/processar_documentos
     .py, mesma coisa que o botão "Imprimir rota" do planejamento faz),
     respeitando a trava cooperativa da Stokki (stokki/sessao_uso.py):
     se a Stokki estiver ocupada, espera a vez; se não liberar a tempo,
     monta o PDF com o que já existe (a rodada das 22h/04h completa).
  3. Monta o PDF em roteirizacao/dados/romaneios/<AAAA-MM-DD>/, com o
     MESMO nome de arquivo do job das 04h (gerar_pdf_romaneios.
     nome_arquivo_saida), apagando antes qualquer PDF antigo da mesma
     rota (motorista trocado -> slug diferente -> arquivo órfão).

Fila única em segundo plano (thread do processo chamador): rotas
enfileiradas juntas (ex: "Confirmar e enviar" de 8 rascunhos de uma vez)
saem num LOTE só -- uma ida à VUUPT por data, uma sessão de Stokki pra
todos os pedidos sem NF -- em vez de 8 logins na Stokki em paralelo (que
derrubariam uns aos outros, ver memória stokki-sessao-e-anexo-limitacoes).
Rota já na fila não entra duas vezes.

Scripts de linha de comando (criar_rotas_diarias, enviar_rascunhos_
pendentes, incrementar_rotas) chamam aguardar() antes de encerrar, pra
o processo não morrer com a fila pela metade. No painel (processo
longo) a thread simplesmente segue.

Best-effort por construção: NADA daqui pode desfazer ou atrasar a
criação da rota -- agendar() nunca levanta exceção, e qualquer falha na
preparação fica só no log (dados/documentacao_rota.log).
"""
import logging
import re
import sys
import threading
import time
from collections import deque
from datetime import date
from pathlib import Path

_RAIZ_LOCAL = Path(__file__).parent
_RAIZ_PROJETO = _RAIZ_LOCAL.parent
for _p in (_RAIZ_PROJETO, _RAIZ_LOCAL):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

logger = logging.getLogger("documentacao_rota")
logger.setLevel(logging.INFO)

# Log próprio, além do log de quem chamou (painel, script das 18h...):
# a preparação roda escondida, então precisa de um lugar fixo pra
# conferir depois "o romaneio da rota X foi preparado? quando? deu erro?".
_LOG_PATH = _RAIZ_LOCAL / "dados" / "documentacao_rota.log"
try:
    _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not any(isinstance(h, logging.FileHandler) and getattr(h, "baseFilename", "") == str(_LOG_PATH)
               for h in logger.handlers):
        _fh = logging.FileHandler(_LOG_PATH, encoding="utf-8")
        _fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logger.addHandler(_fh)
except OSError:
    pass

# Dono da trava cooperativa da Stokki (stokki/sessao_uso.py) e quanto
# esperar a vez antes de desistir da etapa de documentos (o PDF sai
# mesmo assim, só sem as NFs que ainda faltavam).
DONO_TRAVA_STOKKI = "documentacao-rota"
ESPERA_STOKKI_SEGUNDOS = 10 * 60
TTL_TRAVA_STOKKI_SEGUNDOS = 15 * 60

# Quanto o worker espera depois da 1ª rota entrar na fila antes de
# fechar o lote (rotas confirmadas juntas saem numa sessão só de Stokki).
JANELA_LOTE_SEGUNDOS = 2.0

# Mesmo critério de incrementar_rotas._data_inicio_rota / buscar_rotas_
# do_dia: a data da rota é o prefixo AAAA-MM-DD do start_at, sem
# conversão de fuso (rotas começam de manhã, o dia não vira).
_PADRAO_DATA_START_AT = re.compile(r"(\d{4})-(\d{2})-(\d{2})")

_fila: deque = deque()
_na_fila: dict = {}          # rota_id -> item (dedupe)
_cond = threading.Condition()
_worker: threading.Thread | None = None
_worker_ativo = False        # decidido SEMPRE com _cond em mãos (evita corrida com o worker ocioso saindo)
_em_andamento = 0            # itens já tirados da fila e ainda sendo preparados


def _carregar_config() -> dict:
    import yaml
    with open(_RAIZ_PROJETO / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _cfg() -> dict:
    """Bloco opcional `documentacao_rota:` do config.yaml:
        documentacao_rota:
          ativo: true                    # desliga tudo se false
          buscar_documentos_stokki: true # etapa 2 (Stokki) ligada/desligada
    """
    try:
        return _carregar_config().get("documentacao_rota", {}) or {}
    except Exception:
        return {}


def _ativo() -> bool:
    return bool(_cfg().get("ativo", True))


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------

def agendar(rota_id: int, data_alvo: date | str | None = None, motivo: str = "") -> bool:
    """Põe a rota na fila de preparação e volta na hora. data_alvo pode
    ser date, 'AAAA-MM-DD' ou None (aí a data é lida do start_at da
    rota na VUUPT). Devolve True se entrou na fila, False se já estava
    lá, se a preparação está desligada no config ou se rota_id é
    inválido. NUNCA levanta exceção -- quem chama acabou de criar uma
    rota de verdade e isso não pode ser desfeito por um detalhe daqui."""
    try:
        if not rota_id:
            return False
        if not _ativo():
            logger.info(f"Preparação desligada no config -- rota {rota_id} não enfileirada.")
            return False
        data_alvo = _normalizar_data(data_alvo)
        with _cond:
            if rota_id in _na_fila:
                # já esperando: só atualiza o motivo pro log
                _na_fila[rota_id]["motivo"] = motivo or _na_fila[rota_id]["motivo"]
                return False
            item = {"rota_id": int(rota_id), "data_alvo": data_alvo, "motivo": motivo,
                    "agendado_em": time.time()}
            _fila.append(item)
            _na_fila[rota_id] = item
            _garantir_worker()
            _cond.notify_all()
        logger.info(f"Rota {rota_id} ({data_alvo.isoformat() if data_alvo else 'data a resolver'}) "
                    f"enfileirada pra preparar documentação"
                    + (f" -- {motivo}" if motivo else "") + ".")
        return True
    except Exception as e:  # pragma: no cover - rede de segurança
        logger.warning(f"Falha ao enfileirar rota {rota_id} (ignorada): {e}")
        return False


def agendar_varias(rota_ids, data_alvo: date | str | None = None, motivo: str = "") -> int:
    """agendar() em lote; devolve quantas entraram de fato na fila."""
    return sum(1 for rid in rota_ids if agendar(rid, data_alvo, motivo))


def descartar(rota_id: int, data_alvo: date | str | None) -> int:
    """Apaga o(s) PDF(s) já preparado(s) da rota na pasta da data --
    pra rota cancelada, cujo romaneio virou órfão. Devolve quantos
    arquivos saíram. Sem data conhecida não faz nada (o job das 04h
    limpa órfãos de qualquer forma)."""
    try:
        data_alvo = _normalizar_data(data_alvo)
        if not rota_id or data_alvo is None:
            return 0
        with _cond:
            item = _na_fila.pop(rota_id, None)
            if item is not None and item in _fila:
                _fila.remove(item)
        import gerar_pdf_romaneios as gpr
        pasta = gpr.PASTA_ROMANEIOS / data_alvo.isoformat()
        removidos = 0
        if pasta.is_dir():
            for antigo in pasta.glob(f"*_id{int(rota_id)}_*.pdf"):
                antigo.unlink()
                removidos += 1
        if removidos:
            logger.info(f"Rota {rota_id} cancelada -- {removidos} PDF(s) de romaneio descartado(s) "
                        f"em {pasta}.")
        return removidos
    except Exception as e:
        logger.warning(f"Falha ao descartar romaneio da rota {rota_id} (ignorada): {e}")
        return 0


def pendentes() -> int:
    """Quantas rotas ainda esperam ou estão sendo preparadas."""
    with _cond:
        return len(_fila) + _em_andamento


def aguardar(timeout: float | None = 15 * 60) -> bool:
    """Bloqueia até a fila esvaziar (ou até `timeout` s). Pra scripts de
    linha de comando, que morreriam com a thread no meio. Devolve True
    se esvaziou."""
    limite = None if timeout is None else time.monotonic() + timeout
    with _cond:
        while _fila or _em_andamento:
            restante = None if limite is None else limite - time.monotonic()
            if restante is not None and restante <= 0:
                logger.warning(f"aguardar(): tempo esgotado com {len(_fila) + _em_andamento} "
                               f"rota(s) ainda pendente(s).")
                return False
            _cond.wait(timeout=restante if restante is None else min(restante, 5.0))
    return True


def preparar(rota_id: int, data_alvo: date | str | None = None,
             buscar_documentos: bool | None = None) -> Path | None:
    """Versão SÍNCRONA (sem fila): prepara a documentação de uma rota
    agora e devolve o caminho do PDF (None se a rota não existe na data
    ou está sem paradas). Usada pelo worker e por testes/uso manual."""
    resultados = _preparar_lote([{"rota_id": int(rota_id), "data_alvo": _normalizar_data(data_alvo),
                                  "motivo": "chamada direta"}],
                                buscar_documentos=buscar_documentos)
    return resultados.get(int(rota_id))


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

def _garantir_worker():
    """Chamar com _cond em mãos. Usa a flag _worker_ativo (e não
    Thread.is_alive) porque o worker decide sair ocioso segurando o
    mesmo _cond -- assim nunca acontece de agendar() ver uma thread
    "viva" que já decidiu morrer e deixar a rota presa na fila."""
    global _worker, _worker_ativo
    if not _worker_ativo:
        _worker_ativo = True
        _worker = threading.Thread(target=_loop_worker, name="documentacao-rota", daemon=True)
        _worker.start()


def _loop_worker():
    global _em_andamento, _worker_ativo
    while True:
        with _cond:
            while not _fila:
                if not _cond.wait(timeout=60) and not _fila:
                    _worker_ativo = False
                    return  # ocioso -- a próxima agendar() sobe outra thread
        # Pequena pausa (fora do lock e sem ser acordado por notify) pra
        # juntar num lote só as rotas que chegam em rajada -- loop de
        # "Confirmar e enviar" com várias rotas, ou agendar_varias().
        time.sleep(JANELA_LOTE_SEGUNDOS)
        with _cond:
            lote = list(_fila)
            _fila.clear()
            for item in lote:
                _na_fila.pop(item["rota_id"], None)
            _em_andamento = len(lote)
        try:
            _preparar_lote(lote)
        except Exception as e:
            logger.exception(f"Falha inesperada preparando lote de {len(lote)} rota(s): {e}")
        finally:
            with _cond:
                _em_andamento = 0
                _cond.notify_all()


# ---------------------------------------------------------------------------
# Preparação de fato
# ---------------------------------------------------------------------------

def _normalizar_data(valor) -> date | None:
    if valor is None or valor == "":
        return None
    if isinstance(valor, date):
        return valor
    return date.fromisoformat(str(valor)[:10])


def _data_da_rota(rota: dict) -> date | None:
    m = _PADRAO_DATA_START_AT.search(str(rota.get("start_at") or ""))
    if not m:
        return None
    try:
        return date(*(int(x) for x in m.groups()))
    except ValueError:
        return None


def _resolver_data(token: str, rota_id: int) -> date | None:
    """GET /routes/{id} só pra saber em que dia a rota começa."""
    from rotas_client import buscar_rota
    dados = buscar_rota(token, rota_id)
    if not isinstance(dados, dict):
        return None
    rota = next((dados[k] for k in ("route", "data") if isinstance(dados.get(k), dict)), dados)
    return _data_da_rota(rota)


def _codigos_sem_nf(servicos: list[dict], docs_por_pedido: dict, gpr) -> set[str]:
    """Pedidos da rota que precisam de NF e ainda não têm nenhuma no
    banco (nem 'Pedido de Venda', que substitui NF pra De Tommaso).
    Embarcadores de SENDERS_SEM_NF (canhoteira) ficam de fora -- pra
    eles a falta de NF não é pendência."""
    faltantes: set[str] = set()
    for s in servicos:
        if s.get("sender_id") in gpr.SENDERS_SEM_NF:
            continue
        for codigo in gpr._codigos_base_lista(s.get("code") or ""):
            docs = docs_por_pedido.get(codigo, [])
            if not any(d.get("tipo") in ("Nota Fiscal", "Pedido de Venda") for d in docs):
                faltantes.add(codigo)
    return faltantes


def _buscar_documentos_stokki(codigos: set[str]) -> bool:
    """Etapa 2: fluxo de documentos escopado nos pedidos sem NF, dentro
    da trava cooperativa da Stokki. Devolve True se rodou."""
    from stokki import sessao_uso

    if not sessao_uso.adquirir(DONO_TRAVA_STOKKI, ttl_segundos=TTL_TRAVA_STOKKI_SEGUNDOS,
                               esperar_segundos=ESPERA_STOKKI_SEGUNDOS):
        logger.warning(f"Stokki ocupada por '{sessao_uso.em_uso()}' há mais de "
                       f"{ESPERA_STOKKI_SEGUNDOS // 60} min -- etapa de documentos pulada pra "
                       f"{len(codigos)} pedido(s) sem NF: {sorted(codigos)}. O PDF sai com o que já existe.")
        return False
    try:
        pasta_docs = _RAIZ_PROJETO / "documentos_pedido"
        if str(pasta_docs) not in sys.path:
            sys.path.insert(0, str(pasta_docs))
        import processar_documentos as pdoc
        logger.info(f"Buscando documentos na Stokki/e-mail pra {len(codigos)} pedido(s) sem NF: "
                    f"{sorted(codigos)}")
        contadores = pdoc.main(modo_teste=False, pedidos_stokki=sorted(codigos), notificar=False)
        logger.info(f"Documentos: {contadores}")
        return True
    except Exception as e:
        logger.exception(f"Falha na etapa de documentos (PDF sai com o que já existe): {e}")
        return False
    finally:
        sessao_uso.liberar(DONO_TRAVA_STOKKI)


def _preparar_lote(itens: list[dict], buscar_documentos: bool | None = None) -> dict:
    """Prepara várias rotas de uma vez: uma listagem da VUUPT por data,
    uma etapa de documentos pra união dos pedidos sem NF, um PDF por
    rota. Devolve {rota_id: Path do PDF} (rota que não deu pra preparar
    fica de fora)."""
    import gerar_pdf_romaneios as gpr
    from avisar_motoristas_rotas import buscar_rotas_do_dia, _extrair_servicos_da_rota
    from regras.preferencias_motoristas import CatalogoMotoristas

    inicio = time.time()
    config = _carregar_config()
    token = config.get("vuupt_api", {}).get("token", "")
    if buscar_documentos is None:
        buscar_documentos = bool(_cfg().get("buscar_documentos_stokki", True))

    cfg_motoristas = config.get("motoristas", {})
    try:
        catalogo = CatalogoMotoristas.carregar(cfg_motoristas.get("planilha", ""),
                                               cfg_motoristas.get("json_fallback", ""))
        nome_por_agent_id = {m.agent_id: m.nome for m in catalogo.motoristas}
    except Exception as e:
        logger.warning(f"Catálogo de motoristas indisponível ({e}) -- capa sai sem nome.")
        nome_por_agent_id = {}

    # 1) rota ao vivo, por data (uma listagem por data do lote)
    rotas_por_data: dict[date, list[dict]] = {}
    alvos: list[tuple[dict, dict, list[dict], date]] = []
    for item in itens:
        rota_id, data_alvo = item["rota_id"], item.get("data_alvo")
        try:
            if data_alvo is None:
                data_alvo = _resolver_data(token, rota_id)
                if data_alvo is None:
                    logger.warning(f"Rota {rota_id}: sem start_at reconhecível -- não preparada.")
                    continue
            if data_alvo not in rotas_por_data:
                rotas_por_data[data_alvo] = buscar_rotas_do_dia(token, data_alvo)
            rota = next((r for r in rotas_por_data[data_alvo] if r.get("id") == rota_id), None)
            if rota is None:
                logger.warning(f"Rota {rota_id} não encontrada em {data_alvo.isoformat()} "
                               f"(cancelada ou data errada) -- não preparada.")
                continue
            servicos = _extrair_servicos_da_rota(rota)
            if not servicos:
                logger.warning(f"Rota {rota_id} ({rota.get('name')}) sem paradas -- não preparada.")
                continue
            alvos.append((item, rota, servicos, data_alvo))
        except Exception as e:
            logger.exception(f"Rota {rota_id}: falha ao consultar a VUUPT -- não preparada: {e}")

    if not alvos:
        return {}

    # 2) documentos que faltam (uma sessão de Stokki pra todo o lote)
    codigos: set[str] = set()
    for _item, _rota, servicos, _d in alvos:
        for s in servicos:
            codigos.update(gpr._codigos_base_lista(s.get("code") or ""))
    docs_por_pedido, _em_revisao = gpr.carregar_documentos_por_pedido(codigos)

    if buscar_documentos:
        faltantes: set[str] = set()
        for _item, _rota, servicos, _d in alvos:
            faltantes |= _codigos_sem_nf(servicos, docs_por_pedido, gpr)
        if faltantes and _buscar_documentos_stokki(faltantes):
            docs_por_pedido, _em_revisao = gpr.carregar_documentos_por_pedido(codigos)

    # 3) um PDF por rota, no mesmo lugar/nome do job das 04h
    embarcadores, fatores = gpr.carregar_embarcadores()
    resultados: dict[int, Path] = {}
    for item, rota, servicos, data_alvo in alvos:
        rota_id = item["rota_id"]
        try:
            nome_motorista = nome_por_agent_id.get(rota.get("agent_id"), "(sem motorista)")
            pasta = gpr.PASTA_ROMANEIOS / data_alvo.isoformat()
            pasta.mkdir(parents=True, exist_ok=True)
            caminho_saida = pasta / gpr.nome_arquivo_saida(rota, nome_motorista, data_alvo)
            for antigo in pasta.glob(f"*_id{rota_id}_*.pdf"):
                if antigo != caminho_saida:
                    antigo.unlink()
            stats = gpr.montar_pdf_rota(rota, servicos, docs_por_pedido, embarcadores, fatores,
                                        nome_motorista, data_alvo, caminho_saida)
            resultados[rota_id] = caminho_saida
            logger.info(f"[OK] {rota.get('name')} — {nome_motorista}: {stats['pedidos']} pedido(s), "
                        f"{stats['nfs']} NF(s), {stats['boletos']} boleto(s), "
                        f"{len(stats['pendencias'])} pendência(s) -> {caminho_saida}"
                        + (f" ({item.get('motivo')})" if item.get("motivo") else ""))
        except Exception as e:
            logger.exception(f"[ERRO] Rota {rota_id} ({rota.get('name')}): PDF não gerado: {e}")

    logger.info(f"Lote concluído em {time.time() - inicio:.1f}s: {len(resultados)}/{len(itens)} "
                f"romaneio(s) preparado(s).")
    return resultados


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(description="Prepara (agora, sem fila) o romaneio de uma rota da VUUPT")
    parser.add_argument("rota_id", type=int, help="route_id da VUUPT")
    parser.add_argument("--data", default=None, help="AAAA-MM-DD (padrão: lê do start_at da rota)")
    parser.add_argument("--sem-stokki", action="store_true",
                        help="Não busca documentos que faltam na Stokki; monta o PDF com o que já existe")
    args = parser.parse_args()
    caminho = preparar(args.rota_id, args.data, buscar_documentos=not args.sem_stokki)
    print(caminho or "Rota não preparada (ver log).")
    sys.exit(0 if caminho else 1)
