# -*- coding: utf-8 -*-
"""
print_agent.py

Fase 7 do plano de migração pra VPS (DOC_EXECUCAO_CLAUDE_MIGRACAO_VPS.md):
a única peça que continua rodando local, perto da impressora do
escritório -- deliberadamente burra. Toda a lógica (quais rotas, o que
vai em cada romaneio, formatação) já roda na VPS
(roteirizacao/gerar_pdf_romaneios.py, timer stokki-romaneios-manha, 04h);
aqui só pergunta "tem romaneio novo hoje?", baixa o PDF pronto e manda
pra impressora padrão do Windows.

O controle do que já foi impresso fica só aqui (dados/romaneios_impressos.json)
-- a VPS não sabe nem precisa saber o que já saiu fisicamente na impressora,
só serve os arquivos que já gerou.

Uso (Agendador do Windows, repetindo a cada poucos minutos de manhã):
    py -3.11 print_agent.py
"""
import json
import logging
import os
import sys
from pathlib import Path

import requests
import yaml

_RAIZ = Path(__file__).parent
CONFIG_PATH = _RAIZ / "config.yaml"
CONTROLE_PATH = _RAIZ / "dados" / "romaneios_impressos.json"
PASTA_DOWNLOAD = _RAIZ / "dados" / "romaneios_baixados"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(_RAIZ / "dados" / "print_agent.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


def carregar_config() -> dict:
    if not CONFIG_PATH.exists():
        raise SystemExit(
            f"config.yaml não encontrado em {CONFIG_PATH} -- copie "
            f"config.yaml.exemplo e preencha url_base/token."
        )
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def carregar_controle() -> set:
    if not CONTROLE_PATH.exists():
        return set()
    try:
        return set(json.loads(CONTROLE_PATH.read_text(encoding="utf-8")))
    except json.JSONDecodeError:
        logger.warning(f"{CONTROLE_PATH} corrompido -- tratando como vazio.")
        return set()


def salvar_controle(impressos: set):
    CONTROLE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONTROLE_PATH.write_text(
        json.dumps(sorted(impressos), indent=2, ensure_ascii=False), encoding="utf-8"
    )


def imprimir_pdf(caminho: Path):
    """Manda o PDF pra impressora padrão do Windows via o handler
    associado a .pdf (Adobe/Edge/o que estiver configurado) -- sem
    dependência extra. Se isso abrir um diálogo em vez de imprimir
    direto (o handler default não suporta bem o verbo "print"), trocar
    por SumatraPDF (`SumatraPDF.exe -print-to-default -silent caminho`)
    resolve de forma mais confiável -- não fiz isso de cara por não amarrar
    a um programa que talvez não esteja instalado nesta máquina."""
    os.startfile(str(caminho), "print")  # nosec -- caminho vem de download nosso, não de input externo


def main():
    config = carregar_config()
    base_url = config["url_base"].rstrip("/")
    token = config["token"]
    headers = {"X-Token-Impressao": token}

    impressos = carregar_controle()

    resp = requests.get(f"{base_url}/api/romaneios/pendentes", headers=headers, timeout=30)
    resp.raise_for_status()
    dados = resp.json()

    novos = [r for r in dados["romaneios"] if r["nome"] not in impressos]
    if not novos:
        logger.info(f"Nada novo pra imprimir ({len(impressos)} já impresso(s) hoje).")
        return

    PASTA_DOWNLOAD.mkdir(parents=True, exist_ok=True)
    for r in novos:
        resp_pdf = requests.get(f"{base_url}{r['url']}", headers=headers, timeout=30)
        resp_pdf.raise_for_status()
        caminho = PASTA_DOWNLOAD / r["nome"]
        caminho.write_bytes(resp_pdf.content)
        logger.info(f"  Baixado: {r['nome']} ({len(resp_pdf.content) / 1024:.0f} KB)")

        imprimir_pdf(caminho)
        logger.info(f"  Enviado pra impressora: {r['nome']}")
        impressos.add(r["nome"])
        salvar_controle(impressos)  # salva a cada um -- se um passo falhar no meio, não perde o progresso

    logger.info(f"{len(novos)} romaneio(s) impresso(s) nesta execução.")


if __name__ == "__main__":
    main()
