# -*- coding: utf-8 -*-
"""
sincronizar_painel_vps.py

Fase 1 do plano de migração pra VPS (DOC_EXECUCAO_CLAUDE_MIGRACAO_VPS.md):
mantém a réplica de dados/dados.db + planilhas mestras que o painel_agentes
em /opt/stokki-eventos (VPS) lê. A VPS roda em modo SOMENTE LEITURA -- a
cópia local continua sendo a única fonte da verdade, escrita pelos jobs do
Agendador do Windows. Este script empurra (nunca puxa) uma foto nova por
cima da réplica remota.

Reaproveita o mesmo snapshot consistente (VACUUM INTO) de
backup_dados_gcs.py, só que o destino é a VPS via scp em vez do GCS.
"""
import logging
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

from backup_dados_gcs import snapshot_dados_db, PASTA_DADOS, RAIZ

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

VPS_HOST = "root@187.127.52.197"
VPS_CHAVE = str(Path.home() / ".ssh" / "freshlog_confirmacao_vps")
VPS_DESTINO_DADOS = "/opt/stokki-eventos/dados"
ARQUIVOS_PLANILHA = ["BD_MOTORISTAS.xlsx", "BD_CLIENTES.xlsx"]

# Onde roteirizacao/gerar_pdf_romaneios.py busca os PDFs físicos de NF/
# boleto pra montar o romaneio (ver documentos_pedido/localizar_arquivos.py)
# -- nunca são apagados depois do upload pro GCS, só acumulam, então dá
# pra sincronizar por "o que ainda não existe no destino" (sem precisar
# de rsync, que não está disponível neste Windows).
PASTA_DOCUMENTOS_LOCAL = RAIZ / "documentos_pedido" / "dados"
SUBPASTAS_DOCUMENTOS = ["downloads_stokki_temp", "boletos_separados", "nfs_separadas", "anexos_temp"]
VPS_DESTINO_DOCUMENTOS = "/opt/stokki-eventos/documentos_pedido/dados"

SSH_OPTS = ["-i", VPS_CHAVE, "-o", "BatchMode=yes", "-o", "ConnectTimeout=15"]


def scp(caminho_local: Path, destino_remoto: str):
    cmd = ["scp"] + SSH_OPTS + [str(caminho_local), f"{VPS_HOST}:{destino_remoto}"]
    subprocess.run(cmd, check=True, capture_output=True, text=True)


def ssh(comando_remoto: str):
    cmd = ["ssh"] + SSH_OPTS + [VPS_HOST, comando_remoto]
    return subprocess.run(cmd, check=True, capture_output=True, text=True)


def sincronizar_documentos():
    """Empurra só os arquivos de NF/boleto que ainda não existem na VPS
    -- pastas só crescem (nada é apagado/alterado depois de criado), então
    comparar por NOME já basta, sem precisar de checksum/mtime."""
    ssh(f"mkdir -p " + " ".join(f"{VPS_DESTINO_DOCUMENTOS}/{sub}" for sub in SUBPASTAS_DOCUMENTOS))
    total_novos = 0
    for sub in SUBPASTAS_DOCUMENTOS:
        pasta_local = PASTA_DOCUMENTOS_LOCAL / sub
        if not pasta_local.is_dir():
            continue
        arquivos_locais = [f for f in pasta_local.iterdir() if f.is_file()]
        if not arquivos_locais:
            continue

        r = ssh(f"find {VPS_DESTINO_DOCUMENTOS}/{sub} -maxdepth 1 -type f -printf '%f\\n'")
        existentes_remoto = set(r.stdout.splitlines())

        novos = [f for f in arquivos_locais if f.name not in existentes_remoto]
        if not novos:
            continue

        with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
            caminho_tar = Path(tmp.name)
        try:
            with tarfile.open(caminho_tar, "w:gz") as tar:
                for f in novos:
                    tar.add(f, arcname=f.name)
            scp(caminho_tar, f"{VPS_DESTINO_DOCUMENTOS}/{sub}/_novos.tar.gz")
            ssh(f"tar -xzf {VPS_DESTINO_DOCUMENTOS}/{sub}/_novos.tar.gz -C {VPS_DESTINO_DOCUMENTOS}/{sub} "
                f"&& rm {VPS_DESTINO_DOCUMENTOS}/{sub}/_novos.tar.gz")
        finally:
            caminho_tar.unlink(missing_ok=True)

        total_novos += len(novos)
        logger.info(f"  {sub}: {len(novos)} arquivo(s) novo(s) enviado(s).")

    if total_novos:
        ssh(f"chown -R www-data:www-data {VPS_DESTINO_DOCUMENTOS}")
    else:
        logger.info("  Nenhum documento novo pra enviar.")
    return total_novos


def main():
    logger.info("=== Sincronizando réplica do painel na VPS ===")

    logger.info("Gerando snapshot consistente de dados.db (VACUUM INTO)...")
    snapshot = snapshot_dados_db("sync_vps")
    try:
        scp(snapshot, f"{VPS_DESTINO_DADOS}/dados.db")
        logger.info("  dados.db enviado.")
    finally:
        snapshot.unlink(missing_ok=True)

    for nome in ARQUIVOS_PLANILHA:
        caminho = PASTA_DADOS / nome
        if not caminho.exists():
            logger.warning(f"  Planilha não encontrada, pulando: {nome}")
            continue
        scp(caminho, f"{VPS_DESTINO_DADOS}/{nome}")
        logger.info(f"  {nome} enviado.")

    # www-data precisa ser dono (o serviço systemd roda como www-data);
    # 644 (não 444) -- painel_agentes.py grava em dados.db no startup
    # (limpar_execucoes_travadas), e essa réplica é descartável mesmo.
    ssh(
        f"chown www-data:www-data {VPS_DESTINO_DADOS}/dados.db "
        f"{VPS_DESTINO_DADOS}/BD_MOTORISTAS.xlsx {VPS_DESTINO_DADOS}/BD_CLIENTES.xlsx "
        f"&& chmod 644 {VPS_DESTINO_DADOS}/dados.db "
        f"{VPS_DESTINO_DADOS}/BD_MOTORISTAS.xlsx {VPS_DESTINO_DADOS}/BD_CLIENTES.xlsx"
    )
    logger.info("  Permissões ajustadas na VPS.")

    logger.info("Sincronizando documentos (NF/boleto) novos...")
    sincronizar_documentos()

    logger.info("=== Sincronização concluída ===")


if __name__ == "__main__":
    main()
