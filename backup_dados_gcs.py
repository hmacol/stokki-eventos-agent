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

15/09 (Etapa 0 do DOC_EXECUCAO_CLAUDE_SAIDA_VUUPT.md): o diário deixava
até 24 h de entregas, eventos e despesas do app sem cópia. Agora há dois
modos, cada um no seu timer:
  - padrão (03:00): snapshot + planilhas, retenção de 30 dias;
  - --horario (de hora em hora): só os bancos, em backups/*_horario/,
    retenção de 48 h.
Os dois sobem as fotos novas de dados/comprovantes (canhotos e recibos do
app) pra backups/comprovantes/ -- o upload que a API faz na hora é
best-effort e sem reenvio. As fotos não mudam depois de gravadas (nome com
uuid), então basta subir o que ainda não subiu (manifesto local).
"""
import argparse
import json
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
HORAS_RETENCAO_HORARIO = 48
PASTA_COMPROVANTES = PASTA_DADOS / "comprovantes"
MANIFESTO_COMPROVANTES = PASTA_TMP / "comprovantes_no_gcs.json"

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


def limpar_backups_antigos(config: dict, prefixo: str, dias_retencao: int = 0, horas_retencao: int = 0):
    """Apaga do bucket os backups desse prefixo mais velhos que a retenção
    (dias OU horas) -- sem isso o bucket cresce pra sempre."""
    janela = timedelta(days=dias_retencao, hours=horas_retencao)
    if janela <= timedelta(0):
        raise ValueError("retenção precisa ser positiva")

    cfg_gcs = config.get("gcs", {})
    bucket_name = cfg_gcs.get("bucket_name", "")
    cliente = _cliente_gcs(config)
    limite = datetime.now(timezone.utc) - janela
    apagados = 0
    for blob in cliente.list_blobs(bucket_name, prefix=prefixo):
        if blob.time_created and blob.time_created < limite:
            blob.delete()
            apagados += 1
    if apagados:
        logger.info(f"  Retenção: {apagados} backup(s) mais velhos que {janela} apagado(s) de {prefixo}")


def _ler_manifesto() -> dict[str, int]:
    try:
        return json.loads(MANIFESTO_COMPROVANTES.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def comprovantes_pendentes(manifesto: dict[str, int]) -> list[tuple[Path, str]]:
    """Fotos de dados/comprovantes que ainda não subiram (ou mudaram de
    tamanho): [(caminho_local, caminho_relativo)]."""
    if not PASTA_COMPROVANTES.exists():
        return []
    pendentes = []
    for arquivo in sorted(PASTA_COMPROVANTES.rglob("*")):
        if not arquivo.is_file():
            continue
        relativo = arquivo.relative_to(PASTA_COMPROVANTES).as_posix()
        if manifesto.get(relativo) != arquivo.stat().st_size:
            pendentes.append((arquivo, relativo))
    return pendentes


def enviar_comprovantes(config: dict) -> int:
    """Sobe as fotos novas pra backups/comprovantes/ e grava o manifesto a
    cada arquivo (uma falha no meio não faz reenviar tudo). Devolve quantas
    subiram."""
    manifesto = _ler_manifesto()
    pendentes = comprovantes_pendentes(manifesto)
    if not pendentes:
        return 0
    MANIFESTO_COMPROVANTES.parent.mkdir(parents=True, exist_ok=True)
    for arquivo, relativo in pendentes:
        enviar_para_gcs(config, arquivo, f"backups/comprovantes/{relativo}")
        manifesto[relativo] = arquivo.stat().st_size
        MANIFESTO_COMPROVANTES.write_text(json.dumps(manifesto, ensure_ascii=False), encoding="utf-8")
    return len(pendentes)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--modo-teste", action="store_true",
                         help="Gera o snapshot local mas não envia pro GCS nem apaga nada.")
    parser.add_argument("--horario", action="store_true",
                        help="Backup de hora em hora: só os bancos (retenção de 48 h) + fotos novas.")
    args = parser.parse_args()

    config = carregar_config()
    carimbo = datetime.now().strftime("%Y%m%d_%H%M%S")
    sufixo = "_horario" if args.horario else ""

    logger.info("=== Backup %s pro GCS ===", "HORÁRIO de dados.db" if args.horario else "de dados.db + planilhas")

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
        pendentes = comprovantes_pendentes(_ler_manifesto())
        logger.info("--modo-teste: não envia pro GCS. Snapshot fica em dados/backup_tmp/ pra inspeção manual. "
                    "%d foto(s) de comprovante subiriam.", len(pendentes))
        return

    enviar_para_gcs(config, snapshot, f"backups/dados_db{sufixo}/dados_{carimbo}.db")
    snapshot.unlink()
    if snapshot_atendimento:
        enviar_para_gcs(config, snapshot_atendimento, f"backups/atendimento_db{sufixo}/atendimento_{carimbo}.db")
        snapshot_atendimento.unlink()

    logger.info("Enviando fotos novas de dados/comprovantes...")
    logger.info("  %d foto(s) enviada(s).", enviar_comprovantes(config))

    if args.horario:
        logger.info("Limpando backups horários antigos (retenção de %d h)...", HORAS_RETENCAO_HORARIO)
        limpar_backups_antigos(config, "backups/dados_db_horario/", horas_retencao=HORAS_RETENCAO_HORARIO)
        limpar_backups_antigos(config, "backups/atendimento_db_horario/", horas_retencao=HORAS_RETENCAO_HORARIO)
        logger.info("=== Backup horário concluído ===")
        return

    for nome in ARQUIVOS_PLANILHA:
        caminho = PASTA_DADOS / nome
        if not caminho.exists():
            logger.warning(f"  Planilha não encontrada, pulando: {nome}")
            continue
        enviar_para_gcs(config, caminho, f"backups/planilhas/{carimbo}/{nome}")

    logger.info("Limpando backups antigos (retenção de %d dias)...", DIAS_RETENCAO)
    limpar_backups_antigos(config, "backups/dados_db/", dias_retencao=DIAS_RETENCAO)
    limpar_backups_antigos(config, "backups/atendimento_db/", dias_retencao=DIAS_RETENCAO)
    limpar_backups_antigos(config, "backups/planilhas/", dias_retencao=DIAS_RETENCAO)

    logger.info("=== Backup concluído ===")


if __name__ == "__main__":
    main()
