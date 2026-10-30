# -*- coding: utf-8 -*-
"""
vuupt_checklist.py

Preenche, por API, o "Checklist de Entregas com Sucesso" da Vuupt (form 1531)
com as fotos do canhoto e da nota de retorno -- o mesmo que o app do
motorista faz. Endpoints NÃO documentados, provados na conta real em 29/10
(spec docs/superpowers/specs/2026-10-29-checklist-vuupt-pelas-telas-design.md,
seção 2). Por isso a gravação é conferida relendo o checklist: a Vuupt ignora
em silêncio (HTTP 200) arquivo enviado numa parte de nome errado.

Usado por VuuptClient.concluir_como_agente (entre o check-in e o check-out).
"""
import io
import logging
import uuid
from datetime import datetime, timezone

from http_retry import chamar_com_retry

logger = logging.getLogger(__name__)

FORM_ENTREGA = 1531
REGRA_ENTREGA = 1526
GATILHO = "finished_service"
CAMPO_CANHOTO = 15086
CAMPO_NOTA_RETORNO = 15349
BASE_LAT = -23.497015   # base (R. Zilda 288): usada quando o serviço não tem coordenada
BASE_LNG = -46.660775


FORMATOS_FOTO = ["JPEG", "PNG", "WEBP", "HEIF"]   # HEIF so existe com pillow_heif instalado
LIMITE_ERRO_VUUPT = 200     # corpo de erro da Vuupt mostrado ao usuario (o completo vai pro log)


class ChecklistNaoGravado(Exception):
    """O checklist não foi gravado (erro da Vuupt ou foto ausente na releitura)."""


def para_jpeg(conteudo: bytes) -> bytes:
    """JPEG/PNG/HEIC -> JPEG reduzido (lado maior <= LADO_MAXIMO). PDF, não-imagem ou
    foto acima de MAX_PIXELS -> ValueError."""
    from PIL import Image, ImageOps
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except ImportError:
        pass  # sem pillow_heif, HEIC cai no "não é foto" abaixo
    from nucleo.canhotos_manuais import LADO_MAXIMO, MAX_PIXELS
    try:
        # so formatos de foto: EPS/PS disfarcado nunca chega ao Ghostscript
        img = Image.open(io.BytesIO(conteudo), formats=FORMATOS_FOTO)   # preguicoso: so le o cabecalho
    except Exception as e:  # noqa: BLE001
        raise ValueError("Arquivo não é foto") from e
    if img.size[0] * img.size[1] > MAX_PIXELS:
        raise ValueError("Foto grande demais: tire de novo com resolução menor")
    try:
        img.thumbnail((LADO_MAXIMO, LADO_MAXIMO))
        img = ImageOps.exif_transpose(img).convert("RGB")
    except Exception as e:  # noqa: BLE001
        raise ValueError("Arquivo não é foto") from e
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90)
    return buf.getvalue()


def _lista_de_campos(checklist: dict) -> list[dict]:
    campos = checklist.get("fields") or []
    if isinstance(campos, dict):
        campos = campos.get("data") or []
    return campos


def _checklists_do_servico(vuupt, service_id: int) -> list[dict]:
    resp = chamar_com_retry(
        vuupt.session.get,
        f"{_api_base()}/checklists",
        params={
            "filter[0][field]": "entity_id",
            "filter[0][operator]": "eq",
            "filter[0][value]": service_id,
            "include": "fields",
        },
        timeout=30,
    )
    if not resp.ok:
        raise ChecklistNaoGravado(f"Falha ao ler checklists do serviço {service_id}: status {resp.status_code}")
    dado = resp.json() or {}
    lista = dado.get("data") if isinstance(dado, dict) else dado
    # A API pode ignorar o filtro em silencio (testar_fluxo_agente_vuupt.py ja desconfiava):
    # so vale checklist deste servico, nunca o de outro pedido da conta.
    return [c for c in (lista or []) if _inteiro(c.get("checklist_form_id")) == FORM_ENTREGA
            and _inteiro(c.get("entity_id")) == int(service_id)]


def _inteiro(valor) -> int:
    try:
        return int(valor)
    except (TypeError, ValueError):
        return 0


def checklist_entrega_do_servico(vuupt, service_id: int) -> dict | None:
    """O checklist de entrega (form 1531) mais recente do serviço, ou None."""
    achados = _checklists_do_servico(vuupt, service_id)
    return max(achados, key=lambda c: int(c.get("id") or 0)) if achados else None


def _api_base() -> str:
    from vuupt_client import API_BASE_URL
    return API_BASE_URL


def _agora_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _erro(resp) -> str:
    """Corpo do erro da Vuupt: completo no log, curto para a tela (pagina HTML de 502 vira alert enorme)."""
    try:
        corpo = str(resp.json())
    except Exception:  # noqa: BLE001
        corpo = resp.text or ""
    logger.warning(f"Vuupt respondeu {resp.status_code} no checklist: {corpo}")
    return corpo[:LIMITE_ERRO_VUUPT]


def preencher_checklist_entrega(vuupt, servico: dict, fotos: dict[int, bytes]) -> int:
    """Cria (ou reaproveita) o checklist de entrega do serviço, envia as fotos
    ({campo_id: jpeg}), finaliza e confere a releitura. Devolve o id do checklist."""
    sid = int(servico["id"])
    base = _api_base()
    sem_ct = {"Content-Type": None}   # form/multipart: tira o JSON da sessão (como _acao_servico)

    existente = checklist_entrega_do_servico(vuupt, sid)
    ja_enviados = set()
    if existente:
        ja_enviados = {int(c["checklist_form_field_id"]) for c in _lista_de_campos(existente)
                       if c.get("file_url") and c.get("checklist_form_field_id") is not None}
    # só reaproveita se o existente comporta exatamente estas fotos: mesma quantidade e
    # nenhum campo já preenchido fora do que estamos enviando (senão o finish dá 409 pra sempre)
    if (existente and int(existente.get("images_quantity") or 0) == len(fotos)
            and ja_enviados <= {int(c) for c in fotos}):
        cid = int(existente["id"])
    else:
        ja_enviados = set()
        agora = _agora_utc()
        lat = servico.get("latitude")
        lng = servico.get("longitude")
        if lat in (None, "") or lng in (None, ""):
            lat, lng = BASE_LAT, BASE_LNG
        dados = {
            "checklist_form_id": FORM_ENTREGA,
            "checklist_rule_id": REGRA_ENTREGA,
            "entity_id": sid,
            "trigger": GATILHO,
            "latitude": str(lat),
            "longitude": str(lng),
            "gps_date_time": agora,
            "filled_at": agora,
            "images_quantity": len(fotos),
        }
        resp = chamar_com_retry(vuupt.session.post, f"{base}/checklists", data=dados, headers=sem_ct, timeout=30)
        if not resp.ok:
            raise ChecklistNaoGravado(f"Criar checklist do serviço {sid}: status {resp.status_code}: {_erro(resp)}")
        cid = int(((resp.json() or {}).get("checklist_answer") or {}).get("id") or 0)
        if not cid:
            raise ChecklistNaoGravado(f"Criar checklist do serviço {sid}: resposta sem id")

    reaproveitado = bool(ja_enviados)
    enviou_algo = False
    for campo, jpeg in fotos.items():
        if int(campo) in ja_enviados:
            continue
        enviou_algo = True
        dados = {
            "checklist_form_id": FORM_ENTREGA,
            "fields[0][id]": int(campo),
            "fields[0][content_uuid]": str(uuid.uuid4()),
            "fields[0][file_source]": "gallery",
        }
        resp = chamar_com_retry(
            vuupt.session.post, f"{base}/checklists/{cid}",
            data=dados, files={"fields[0][value]": ("foto.jpg", jpeg, "image/jpeg")},
            headers=sem_ct, timeout=60,
        )
        if not resp.ok:
            raise ChecklistNaoGravado(f"Enviar foto {campo} ao checklist {cid}: status {resp.status_code}: {_erro(resp)}")

    resp = chamar_com_retry(vuupt.session.put, f"{base}/checklists/{cid}/finish", headers=sem_ct, timeout=30)
    if not resp.ok:
        if reaproveitado and not enviou_algo:
            # checklist já completo de uma tentativa anterior (finish ok, check-out falhou):
            # o finish repetido pode ser recusado; a releitura abaixo decide
            logger.warning(f"Finish repetido do checklist {cid} recusado (status {resp.status_code}); conferindo pela releitura")
        else:
            raise ChecklistNaoGravado(f"Finalizar checklist {cid}: status {resp.status_code}: {_erro(resp)}")

    # conferência: a Vuupt aceita (200) arquivo em parte errada sem gravar nada
    relido = next((c for c in _checklists_do_servico(vuupt, sid) if int(c.get("id") or 0) == cid), None)
    if relido is None:
        raise ChecklistNaoGravado(f"Checklist {cid} não apareceu na releitura do serviço {sid}")
    com_arquivo = {int(c["checklist_form_field_id"]) for c in _lista_de_campos(relido)
                   if c.get("file_url") and c.get("checklist_form_field_id") is not None}
    faltando = [int(c) for c in fotos if int(c) not in com_arquivo]
    if faltando:
        raise ChecklistNaoGravado(f"Checklist {cid}: foto do(s) campo(s) {faltando} não gravada(s) (sem file_url)")
    logger.info(f"Checklist {cid} do serviço {sid} gravado com {len(fotos)} foto(s)")
    return cid
