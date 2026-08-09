# -*- coding: utf-8 -*-
"""
storage_gcs.py

Envia documentos classificados pro Google Cloud Storage -- pedido do
Hugo, 05/08. Mesmo padrão de caminho do agente de documentos anterior:
`pedidos/{codigo_pedido}/{tipo}/{nome_arquivo}`.

Precisa de bucket + conta de serviço criados do zero pro projeto
agente_stokki_eventos (confirmado com o Hugo -- não reaproveita o do
agente anterior). Configuração esperada em config.yaml:

    gcs:
      bucket_name: "nome-do-bucket-aqui"
      credenciais_json: "C:\\caminho\\para\\service-account.json"

Ver SETUP_GCS.md (nesta pasta) pro passo a passo de criar o bucket e
a conta de serviço no Google Cloud Console.
"""
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def _cliente_gcs(config: dict):
    """Cria o cliente do GCS a partir das credenciais no config.yaml.
    Import do google-cloud-storage fica AQUI DENTRO (não no topo do
    arquivo) -- assim o resto do módulo (e o resto do projeto que
    importa esse arquivo) não quebra se a biblioteca ainda não tiver
    sido instalada, só essa função específica."""
    from google.cloud import storage as gcs_sdk
    from google.oauth2 import service_account

    cfg_gcs = config.get("gcs", {})
    caminho_credenciais = cfg_gcs.get("credenciais_json", "")
    if not caminho_credenciais or not Path(caminho_credenciais).exists():
        raise FileNotFoundError(
            f"Credenciais do GCS não encontradas em '{caminho_credenciais}' -- "
            f"confira gcs.credenciais_json no config.yaml. Ver SETUP_GCS.md."
        )

    credenciais = service_account.Credentials.from_service_account_file(caminho_credenciais)
    return gcs_sdk.Client(credentials=credenciais, project=credenciais.project_id)


def montar_caminho_gcs(codigo_pedido: str, tipo_documento: str, nome_arquivo: str) -> str:
    """pedidos/{codigo_pedido}/{tipo}/{nome_arquivo} -- mesmo padrão do
    agente de documentos anterior. Tipo com espaço vira underscore
    (nomes de pasta mais limpos: 'Nota Fiscal' -> 'Nota_Fiscal')."""
    tipo_limpo = tipo_documento.replace(" ", "_")
    return f"pedidos/{codigo_pedido}/{tipo_limpo}/{nome_arquivo}"


def enviar_documento(config: dict, caminho_local: Path, codigo_pedido: str,
                     tipo_documento: str) -> str:
    """
    Envia o arquivo pro bucket configurado, no caminho padrão. Retorna
    o caminho completo dentro do bucket (gs://bucket/caminho).
    """
    cfg_gcs = config.get("gcs", {})
    bucket_name = cfg_gcs.get("bucket_name", "")
    if not bucket_name:
        raise ValueError("gcs.bucket_name não configurado no config.yaml.")

    cliente = _cliente_gcs(config)
    bucket = cliente.bucket(bucket_name)

    caminho_gcs = montar_caminho_gcs(codigo_pedido, tipo_documento, caminho_local.name)
    blob = bucket.blob(caminho_gcs)
    blob.upload_from_filename(str(caminho_local))

    logger.info(f"  Enviado pro GCS: {caminho_local.name} -> gs://{bucket_name}/{caminho_gcs}")
    return f"gs://{bucket_name}/{caminho_gcs}"
