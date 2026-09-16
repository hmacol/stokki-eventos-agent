# -*- coding: utf-8 -*-
"""
nucleo/exportar_historico_vuupt.py

Etapa 1 do DOC_EXECUCAO_CLAUDE_SAIDA_VUUPT.md: tirar da Vuupt, AGORA, o
histórico que só existe lá. Medido em 12/09: 7.844 rotas, 62.527 serviços,
11.948 clientes, 149 veículos, 94 usuários e 59.740 checklists (os
canhotos). O núcleo só tem de 15/07/2026 em diante.

Por que antes do corte, e não no fim do contrato: são horas de download
(cada PDF de checklist leva ~2 s), e depois do fim do contrato a conta
pode simplesmente parar de responder. Com o arquivo no nosso bucket, o
portal e a expedição conseguem servir canhoto antigo sem a Vuupt.

O QUE SAI, em gs://<bucket>/arquivo_vuupt/:
    rotas/<AAAA-MM>.jsonl.gz          rotas por mês do start_at
    servicos/<AAAA-MM>.jsonl.gz       serviços por mês do created_at (com customer e failedReason)
    checklists/<AAAA-MM>.jsonl.gz     respostas de checklist, com os campos preenchidos
    clientes/completo.jsonl.gz        customers
    usuarios/completo.jsonl.gz        users (agentes)
    veiculos/completo.jsonl.gz        vehicles
    checklists_pdf/<AAAA-MM>/<id>.pdf o canhoto renderizado (é onde a FOTO mora:
                                      a imagem só sai por URL assinada da CDN)
    manifesto.json                    quantas linhas saíram × quantas a API diz que existem

RETOMÁVEL: o estado fica em dados/arquivo_vuupt_estado.json (janelas
prontas) e dados/arquivo_vuupt_pdfs.txt (ids de PDF já salvos). Rodar de
novo continua de onde parou; janela pronta não é baixada duas vezes.

COMO USAR (VPS, como www-data):
    venv/bin/python nucleo/exportar_historico_vuupt.py --fase entidades
    venv/bin/python nucleo/exportar_historico_vuupt.py --fase pdfs --limite 8000
    venv/bin/python nucleo/exportar_historico_vuupt.py --estado
O timer stokki-exportar-vuupt roda um lote por noite até acabar.
"""
import argparse
import gzip
import json
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path

import requests

_RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_RAIZ))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from backup_dados_gcs import _cliente_gcs, carregar_config
from http_retry import chamar_com_retry

logger = logging.getLogger("nucleo.exportar_historico_vuupt")

API = "https://api.vuupt.com/api/v1"
APP = "https://app.vuupt.com/api/v1"
PREFIXO = "arquivo_vuupt"
PASTA_DADOS = _RAIZ / "dados"
ESTADO = PASTA_DADOS / "arquivo_vuupt_estado.json"
PDFS_FEITOS = PASTA_DADOS / "arquivo_vuupt_pdfs.txt"
PRIMEIRO_MES = "2023-07"          # 1ª rota na conta (24 rotas no 2º semestre de 2023)
POR_PAGINA = 100

# entidade -> (base, caminho, campo de data pra janela mensal, include)
ENTIDADES = {
    "rotas":      (API, "/routes", "start_at", None),
    "servicos":   (APP, "/services", "created_at", "customer,failedReason"),
    "checklists": (API, "/checklists", "created_at", None),
    "clientes":   (APP, "/customers", None, None),
    "usuarios":   (API, "/users", None, None),
    "veiculos":   (API, "/vehicles", None, None),
}


def _cabecalhos(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def ler_estado() -> dict:
    try:
        return json.loads(ESTADO.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"janelas": {}, "manifesto": {}}


def gravar_estado(estado: dict):
    ESTADO.parent.mkdir(parents=True, exist_ok=True)
    ESTADO.write_text(json.dumps(estado, ensure_ascii=False, indent=1), encoding="utf-8")


def meses(desde: str, ate: str) -> list[str]:
    ano, mes = (int(x) for x in desde.split("-"))
    fim_ano, fim_mes = (int(x) for x in ate.split("-"))
    saida = []
    while (ano, mes) <= (fim_ano, fim_mes):
        saida.append(f"{ano:04d}-{mes:02d}")
        ano, mes = (ano + 1, 1) if mes == 12 else (ano, mes + 1)
    return saida


def _limites(mes: str) -> tuple[str, str]:
    ano, m = (int(x) for x in mes.split("-"))
    inicio = f"{ano:04d}-{m:02d}-01 00:00:00"
    fim = f"{ano + 1:04d}-01-01 00:00:00" if m == 12 else f"{ano:04d}-{m + 1:02d}-01 00:00:00"
    return inicio, fim


def baixar_pagina(token: str, base: str, caminho: str, params: dict) -> dict:
    resp = chamar_com_retry(requests.get, f"{base}{caminho}", headers=_cabecalhos(token), params=params, timeout=90)
    resp.raise_for_status()
    return resp.json()


def baixar_tudo(token: str, entidade: str, mes: str | None, rps: float) -> tuple[list[dict], int]:
    """Todas as páginas de uma janela. Devolve (linhas, total que a API diz)."""
    base, caminho, campo_data, include = ENTIDADES[entidade]
    params: dict = {"per_page": POR_PAGINA, "page": 1}
    if include:
        params["include"] = include
    if mes and campo_data:
        inicio, fim = _limites(mes)
        params.update({"filter[0][field]": campo_data, "filter[0][operator]": "gte", "filter[0][value]": inicio,
                       "filter[1][field]": campo_data, "filter[1][operator]": "lt", "filter[1][value]": fim})
    linhas, total, pagina = [], 0, 1
    while True:
        params["page"] = pagina
        corpo = baixar_pagina(token, base, caminho, params)
        linhas.extend(corpo.get("data", []))
        paginacao = (corpo.get("meta") or {}).get("pagination") or {}
        total = paginacao.get("total", len(linhas))
        if pagina >= (paginacao.get("total_pages") or 1):
            break
        pagina += 1
        time.sleep(1.0 / rps if rps else 0)
    return linhas, total


def enviar_jsonl(config: dict, linhas: list[dict], caminho_gcs: str) -> int:
    cliente = _cliente_gcs(config)
    bucket = cliente.bucket(config["gcs"]["bucket_name"])
    conteudo = gzip.compress("\n".join(json.dumps(l, ensure_ascii=False, default=str) for l in linhas).encode("utf-8"))
    bucket.blob(caminho_gcs).upload_from_string(conteudo, content_type="application/gzip")
    return len(conteudo)


def exportar_entidades(token: str, config: dict, estado: dict, desde: str, ate: str, rps: float,
                       modo_teste: bool, apenas: list[str] | None) -> dict:
    resumo = {}
    for entidade, (_, _, campo_data, _) in ENTIDADES.items():
        if apenas and entidade not in apenas:
            continue
        janelas = meses(desde, ate) if campo_data else ["completo"]
        for janela in janelas:
            chave = f"{entidade}/{janela}"
            if chave in estado["janelas"]:
                continue
            linhas, total = baixar_tudo(token, entidade, None if janela == "completo" else janela, rps)
            if not linhas:
                # Mês sem nada (a conta só tem rota de dez/2023 em diante):
                # marca como pronto pra não voltar na API todo dia.
                if not modo_teste:
                    estado["janelas"][chave] = {"linhas": 0, "total_api": total,
                                                "em": datetime.now().isoformat(timespec="seconds")}
                    gravar_estado(estado)
                continue
            caminho_gcs = f"{PREFIXO}/{entidade}/{janela}.jsonl.gz"
            bytes_enviados = 0 if modo_teste else enviar_jsonl(config, linhas, caminho_gcs)
            registro = {"linhas": len(linhas), "total_api": total, "bytes": bytes_enviados,
                        "em": datetime.now().isoformat(timespec="seconds"), "gcs": caminho_gcs}
            if len(linhas) != total:
                registro["ATENCAO"] = "quantidade baixada != total da API"
                logger.warning(f"{chave}: baixadas {len(linhas)} de {total} -- confira.")
            if not modo_teste:
                estado["janelas"][chave] = registro
                gravar_estado(estado)
            resumo[chave] = registro
            logger.info(f"{chave}: {len(linhas)} linha(s), {bytes_enviados / 1e6:.1f} MB")
    return resumo


# ── PDFs dos checklists ───────────────────────────────────────────────────────

def _ids_de_checklist(estado: dict, config: dict) -> list[tuple[int, str]]:
    """(id, mês) de tudo que já foi exportado em checklists/<mês>.jsonl.gz --
    a lista de PDFs a baixar sai do próprio arquivo, sem voltar na API."""
    cliente = _cliente_gcs(config)
    bucket = cliente.bucket(config["gcs"]["bucket_name"])
    saida = []
    for chave, registro in sorted(estado["janelas"].items()):
        if not chave.startswith("checklists/") or not registro.get("gcs"):
            continue
        mes = chave.split("/", 1)[1]
        bruto = bucket.blob(registro["gcs"]).download_as_bytes()
        for linha in gzip.decompress(bruto).decode("utf-8").splitlines():
            if linha.strip():
                item = json.loads(linha)
                if item.get("id"):
                    saida.append((int(item["id"]), mes))
    return saida


def _ja_feitos() -> set[int]:
    try:
        return {int(x) for x in PDFS_FEITOS.read_text(encoding="utf-8").split() if x.strip()}
    except (OSError, ValueError):
        return set()


def exportar_pdfs(token: str, config: dict, estado: dict, limite: int, rps: float, paralelo: int,
                  modo_teste: bool) -> dict:
    pendentes = [(i, m) for i, m in _ids_de_checklist(estado, config) if i not in _ja_feitos()]
    if limite:
        pendentes = pendentes[:limite]
    if not pendentes:
        return {"pendentes": 0}
    cliente = _cliente_gcs(config)
    bucket = cliente.bucket(config["gcs"]["bucket_name"])
    intervalo = (1.0 / rps) if rps else 0
    stats = {"salvos": 0, "erros": 0, "bytes": 0, "pendentes": len(pendentes)}

    def baixar(par):
        checklist_id, mes = par
        try:
            resp = chamar_com_retry(requests.get, f"{API}/checklists/{checklist_id}/print",
                                    headers={"Authorization": f"Bearer {token}"}, timeout=120)
            if resp.status_code != 200 or not resp.content:
                return checklist_id, None, f"HTTP {resp.status_code}"
            if not modo_teste:
                bucket.blob(f"{PREFIXO}/checklists_pdf/{mes}/{checklist_id}.pdf").upload_from_string(
                    resp.content, content_type="application/pdf")
            return checklist_id, len(resp.content), None
        except Exception as exc:  # noqa: BLE001 -- um PDF que falha não pode parar o lote
            return checklist_id, None, str(exc)[:120]

    with ThreadPoolExecutor(max_workers=max(1, paralelo)) as executor, \
            open(PDFS_FEITOS, "a", encoding="utf-8") as feitos:
        futuros = []
        for par in pendentes:
            futuros.append(executor.submit(baixar, par))
            if intervalo:
                time.sleep(intervalo)
        for futuro in futuros:
            checklist_id, tamanho, erro = futuro.result()
            if erro:
                stats["erros"] += 1
                logger.warning(f"checklist {checklist_id}: {erro}")
                continue
            stats["salvos"] += 1
            stats["bytes"] += tamanho or 0
            if not modo_teste:
                feitos.write(f"{checklist_id}\n")
                feitos.flush()
    return stats


def escrever_manifesto(config: dict, estado: dict, modo_teste: bool):
    manifesto = {"gerado_em": datetime.now().isoformat(timespec="seconds"), "janelas": estado["janelas"],
                 "pdfs_salvos": len(_ja_feitos())}
    if modo_teste:
        return
    cliente = _cliente_gcs(config)
    cliente.bucket(config["gcs"]["bucket_name"]).blob(f"{PREFIXO}/manifesto.json").upload_from_string(
        json.dumps(manifesto, ensure_ascii=False, indent=1), content_type="application/json")


def mostrar_estado(estado: dict):
    por_entidade: dict[str, dict] = {}
    for chave, registro in estado["janelas"].items():
        entidade = chave.split("/", 1)[0]
        acumulado = por_entidade.setdefault(entidade, {"janelas": 0, "linhas": 0, "bytes": 0, "alertas": 0})
        acumulado["janelas"] += 1
        acumulado["linhas"] += registro.get("linhas", 0)
        acumulado["bytes"] += registro.get("bytes", 0)
        acumulado["alertas"] += 1 if registro.get("ATENCAO") else 0
    for entidade, a in sorted(por_entidade.items()):
        print(f"  {entidade:12s} {a['janelas']:3d} janela(s)  {a['linhas']:7d} linha(s)  "
              f"{a['bytes'] / 1e6:8.1f} MB" + (f"  ATENCAO em {a['alertas']}" if a["alertas"] else ""))
    print(f"  {'pdfs':12s} {len(_ja_feitos()):7d} salvo(s)")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Exporta o histórico da Vuupt pro GCS (Etapa 1).")
    parser.add_argument("--fase", choices=["entidades", "pdfs", "auto"], default="auto")
    parser.add_argument("--desde", default=PRIMEIRO_MES, help=f"mês inicial (padrão {PRIMEIRO_MES})")
    parser.add_argument("--ate", default=date.today().strftime("%Y-%m"))
    parser.add_argument("--entidade", action="append", help="só esta entidade (pode repetir)")
    parser.add_argument("--limite", type=int, default=8000, help="máximo de PDFs por execução (0 = sem limite)")
    parser.add_argument("--rps", type=float, default=2.0, help="requisições por segundo (padrão 2)")
    parser.add_argument("--paralelo", type=int, default=3, help="downloads de PDF em paralelo (padrão 3)")
    parser.add_argument("--modo-teste", action="store_true", help="não envia nada pro GCS nem grava estado")
    parser.add_argument("--estado", action="store_true", help="só mostra o progresso")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
                        handlers=[logging.StreamHandler(),
                                  logging.FileHandler(PASTA_DADOS / "arquivo_vuupt.log", encoding="utf-8")])

    estado = ler_estado()
    if args.estado:
        print("Progresso do arquivo da Vuupt:")
        mostrar_estado(estado)
        return 0

    config = carregar_config()
    token = (config.get("vuupt_api") or {}).get("token", "")
    if not token:
        logger.error("vuupt_api.token ausente no config.yaml.")
        return 1

    inicio = time.monotonic()
    if args.fase in ("entidades", "auto"):
        resumo = exportar_entidades(token, config, estado, args.desde, args.ate, args.rps,
                                    args.modo_teste, args.entidade)
        logger.info(f"Entidades: {len(resumo)} janela(s) nova(s).")
    if args.fase in ("pdfs", "auto"):
        stats = exportar_pdfs(token, config, estado, args.limite, args.rps, args.paralelo, args.modo_teste)
        logger.info(f"PDFs: {stats}")
    escrever_manifesto(config, estado, args.modo_teste)
    logger.info(f"Concluído em {(time.monotonic() - inicio) / 60:.1f} min.")
    mostrar_estado(estado)
    return 0


if __name__ == "__main__":
    sys.exit(main())
