# -*- coding: utf-8 -*-
"""
backup_dados_gcs.py

Backup diário do banco operacional (dados/dados.db) e das planilhas mestras
(BD_MOTORISTAS/BD_CLIENTES/BD_TRANSPORTADORAS) pro Google Cloud Storage --
Fase 0 do plano de migração pra VPS (DOC_EXECUCAO_CLAUDE_MIGRACAO_VPS.md):
rede de segurança ANTES de qualquer dado sair da máquina local, já que
dados/* nunca foi versionado (.gitignore).

dados.db é copiado com `VACUUM INTO`, não uma cópia de bytes crua -- isso
garante um snapshot consistente mesmo com o arquivo sendo escrito por outro
processo ao mesmo tempo (ExpedicaoFrequente roda a cada 30 min, 08h-19h35).

Reaproveita a MESMA credencial/bucket de documentos_pedido/storage_gcs.py
(gcs.bucket_name / gcs.credenciais_json no config.yaml), só que num prefixo
próprio (backups/) pra não misturar com os documentos de pedido.
"""
import argparse
import logging
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

RAIZ = Path(__file__).resolve().parent
CONFIG_PATH = RAIZ / "config.yaml"
PASTA_DADOS = RAIZ / "dados"
PASTA_TMP = PASTA_DADOS / "backup_tmp"

ARQUIVOS_PLANILHA = ["BD_MOTORISTAS.xlsx", "BD_CLIENTES.xlsx", "BD_TRANSPORTADORAS.xlsx"]
DIAS_RETENCAO = 30

_clientes_gcs = {}


def carregar_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _cliente_gcs(config: dict):
    """Mesmo padrão de documentos_pedido/storage_gcs.py -- import só aqui
    dentro pra não quebrar o resto do script se a lib não estiver instalada."""
    cfg_gcs = config.get("gcs", {})
    caminho_credenciais = cfg_gcs.get("credenciais_json", "")
    if not caminho_credenciais or not Path(caminho_credenciais).exists():
        raise FileNotFoundError(
            f"Credenciais do GCS não encontradas em '{caminho_credenciais}' -- "
            f"confira gcs.credenciais_json no config.yaml."
        )
    if caminho_credenciais in _clientes_gcs:
        return _clientes_gcs[caminho_credenciais]

    from google.cloud import storage as gcs_sdk
    from google.oauth2 import service_account

    credenciais = service_account.Credentials.from_service_account_file(caminho_credenciais)
    cliente = gcs_sdk.Client(credentials=credenciais, project=credenciais.project_id)
    _clientes_gcs[caminho_credenciais] = cliente
    return cliente


def snapshot_sqlite(nome_arquivo: str, carimbo: str) -> Path | None:
    """VACUUM INTO em vez de cópia de bytes -- snapshot consistente mesmo
    com o banco sendo escrito por outro processo ao mesmo tempo. Retorna
    None se o arquivo não existir (ex.: atendimento.db antes da central
    de atendimento entrar no ar) -- não é erro, só nada a fazer ainda."""
    origem = PASTA_DADOS / nome_arquivo
    if not origem.exists():
        return None
    destino = PASTA_TMP / f"{origem.stem}_{carimbo}.db"
    destino.parent.mkdir(parents=True, exist_ok=True)
    if destino.exists():
        destino.unlink()

    uri_origem = f"{origem.resolve().as_uri()}?mode=ro"
    con = sqlite3.connect(uri_origem, uri=True)
    try:
        con.execute("VACUUM INTO ?", (str(destino),))
    finally:
        con.close()
    return destino


def snapshot_dados_db(carimbo: str) -> Path:
    return snapshot_sqlite("dados.db", carimbo)


def enviar_para_gcs(config: dict, caminho_local: Path, caminho_gcs: str):
    cfg_gcs = config.get("gcs", {})
    bucket_name = cfg_gcs.get("bucket_name", "")
    if not bucket_name:
        raise ValueError("gcs.bucket_name não configurado no config.yaml.")

    cliente = _cliente_gcs(config)
    bucket = cliente.bucket(bucket_name)
    blob = bucket.blob(caminho_gcs)
    blob.upload_from_filename(str(caminho_local))
    logger.info(f"  Enviado: {caminho_local.name} -> gs://{bucket_name}/{caminho_gcs}")


def limpar_backups_antigos(config: dict, prefixo: str, dias_retencao: int):
    """Apaga do bucket os backups desse prefixo com mais de N dias -- sem
    isso o bucket cresce pra sempre com um snapshot novo por dia."""
    cfg_gcs = config.get("gcs", {})
    bucket_name = cfg_gcs.get("bucket_name", "")
    cliente = _cliente_gcs(config)

    limite = datetime.now(timezone.utc) - timedelta(days=dias_retencao)
    apagados = 0
    for blob in cliente.list_blobs(bucket_name, prefix=prefixo):
        if blob.time_created and blob.time_created < limite:
            blob.delete()
            apagados += 1
    if apagados:
        logger.info(f"  Retenção: {apagados} backup(s) com mais de {dias_retencao} dias apagado(s) de {prefixo}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--modo-teste", action="store_true",
                         help="Gera o snapshot local mas não envia pro GCS nem apaga nada.")
    args = parser.parse_args()

    config = carregar_config()
    carimbo = datetime.now().strftime("%Y%m%d_%H%M%S")

    logger.info("=== Backup de dados.db + planilhas pro GCS ===")

    logger.info("Gerando snapshot consistente de dados.db (VACUUM INTO)...")
    snapshot = snapshot_dados_db(carimbo)
    tamanho_mb = snapshot.stat().st_size / 1024 / 1024
    logger.info(f"  Snapshot gerado: {snapshot.name} ({tamanho_mb:.1f} MB)")

    logger.info("Gerando snapshot de atendimento.db (histórico de WhatsApp da central de atendimento)...")
    snapshot_atendimento = snapshot_sqlite("atendimento.db", carimbo)
    if snapshot_atendimento:
        tamanho_mb_at = snapshot_atendimento.stat().st_size / 1024 / 1024
        logger.info(f"  Snapshot gerado: {snapshot_atendimento.name} ({tamanho_mb_at:.1f} MB)")
    else:
        logger.info("  atendimento.db ainda não existe -- pulando (normal antes da central de atendimento entrar no ar).")

    if args.modo_teste:
        logger.info("--modo-teste: não envia pro GCS. Snapshot fica em dados/backup_tmp/ pra inspeção manual.")
        return

    enviar_para_gcs(config, snapshot, f"backups/dados_db/dados_{carimbo}.db")
    snapshot.unlink()
    if snapshot_atendimento:
        enviar_para_gcs(config, snapshot_atendimento, f"backups/atendimento_db/atendimento_{carimbo}.db")
        snapshot_atendimento.unlink()

    for nome in ARQUIVOS_PLANILHA:
        caminho = PASTA_DADOS / nome
        if not caminho.exists():
            logger.warning(f"  Planilha não encontrada, pulando: {nome}")
            continue
        enviar_para_gcs(config, caminho, f"backups/planilhas/{carimbo}/{nome}")

    logger.info("Limpando backups antigos (retenção de %d dias)...", DIAS_RETENCAO)
    limpar_backups_antigos(config, "backups/dados_db/", DIAS_RETENCAO)
    limpar_backups_antigos(config, "backups/atendimento_db/", DIAS_RETENCAO)
    limpar_backups_antigos(config, "backups/planilhas/", DIAS_RETENCAO)

    logger.info("=== Backup concluído ===")


if __name__ == "__main__":
    main()
