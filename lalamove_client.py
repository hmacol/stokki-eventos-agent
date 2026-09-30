# -*- coding: utf-8 -*-
"""
lalamove_client.py

Cliente da API pública da Lalamove (v3) -- https://developers.lalamove.com/

Uso no projeto (pedido do Hugo, 28/08): quando o motorista escolhido pra
uma rota no /planejamento for o motorista virtual "LALAMOVE", além de
criar a rota na Vuupt o painel cria também o pedido na Lalamove (1 pedido
= 1 rota, base como coleta + até 15 entregas), guarda o orderId e passa a
refletir o status da Lalamove na Vuupt (título do serviço recebe o código
Lalamove, serviço é finalizado quando a Lalamove entrega).

Autenticação: HMAC-SHA256.
    string assinada = "{ts_ms}\r\n{METHOD}\r\n{PATH}\r\n\r\n{BODY_JSON}"
    Authorization: hmac {API_KEY}:{ts_ms}:{hex(hmac_sha256(secret, string))}
    Market: BR
    Request-ID: <uuid>

Ambientes:
    sandbox   https://rest.sandbox.lalamove.com  (chaves pk_test_/sk_test_)
    produção  https://rest.lalamove.com          (chaves pk_prod_/sk_prod_)

Fluxo obrigatório: POST /v3/quotations (válida 5 min) -> POST /v3/orders
com o quotationId. Máximo 16 paradas por pedido (1 coleta + 15 entregas).
Telefones em E.164 (+55...). Datas em UTC ISO-8601 (scheduleAt).

Status de pedido: ASSIGNING_DRIVER, ON_GOING, PICKED_UP, COMPLETED,
CANCELED, REJECTED, EXPIRED.
"""
import hashlib
import hmac
import json
import logging
import time
import uuid
from datetime import datetime, timezone

import requests

from http_retry import chamar_com_retry

logger = logging.getLogger(__name__)

URL_SANDBOX = "https://rest.sandbox.lalamove.com"
URL_PRODUCAO = "https://rest.lalamove.com"

MAX_PARADAS = 16  # limite da API (inclui a coleta)

STATUS_ATIVOS = {"ASSIGNING_DRIVER", "ON_GOING", "PICKED_UP"}
STATUS_FINAIS = {"COMPLETED", "CANCELED", "REJECTED", "EXPIRED"}


class LalamoveAPIError(Exception):
    def __init__(self, mensagem: str, status_code: int | None = None, erros: list | None = None):
        super().__init__(mensagem)
        self.status_code = status_code
        self.erros = erros or []


class LalamoveClient:
    def __init__(self, api_key: str, api_secret: str, sandbox: bool = True,
                 market: str = "BR", language: str = "pt_BR", timeout: int = 30):
        if not api_key or not api_secret:
            raise ValueError("Lalamove: api_key/api_secret não configurados.")
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = URL_SANDBOX if sandbox else URL_PRODUCAO
        self.sandbox = sandbox
        self.market = market
        self.language = language
        self.timeout = timeout
        self.session = requests.Session()

    # ------------------------------------------------------------------
    # Infra: assinatura e chamada
    # ------------------------------------------------------------------
    def _assinar(self, metodo: str, path: str, corpo: str) -> dict:
        ts = str(int(time.time() * 1000))
        raw = f"{ts}\r\n{metodo.upper()}\r\n{path}\r\n\r\n{corpo}"
        assinatura = hmac.new(self.api_secret.encode("utf-8"), raw.encode("utf-8"),
                              hashlib.sha256).hexdigest()
        return {
            "Authorization": f"hmac {self.api_key}:{ts}:{assinatura}",
            "Market": self.market,
            "Request-ID": str(uuid.uuid4()),
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _chamar(self, metodo: str, path: str, dados: dict | None = None) -> dict:
        # O corpo assinado tem que ser BYTE A BYTE o corpo enviado -- por isso
        # serializa uma vez e manda como `data`, nunca `json=`.
        corpo = json.dumps({"data": dados}, separators=(",", ":"), ensure_ascii=False) if dados is not None else ""
        headers = self._assinar(metodo, path, corpo)
        func = getattr(self.session, metodo.lower())
        kwargs = {"headers": headers, "timeout": self.timeout}
        if corpo:
            kwargs["data"] = corpo.encode("utf-8")
        resp = chamar_com_retry(func, f"{self.base_url}{path}", **kwargs)
        if resp.status_code == 204 or not resp.content:
            if resp.ok:
                return {}
        try:
            payload = resp.json()
        except ValueError:
            payload = {}
        if not resp.ok:
            erros = payload.get("errors") or []
            detalhe = "; ".join(f"{e.get('id', '')}: {e.get('message', '')} {e.get('detail', '')}".strip()
                                for e in erros) or resp.text[:300]
            logger.error(f"Lalamove {metodo} {path} -> HTTP {resp.status_code}: {detalhe}")
            raise LalamoveAPIError(f"HTTP {resp.status_code}: {detalhe}", resp.status_code, erros)
        return payload.get("data", payload)

    # ------------------------------------------------------------------
    # Endpoints
    # ------------------------------------------------------------------
    def info_cidades(self) -> list[dict]:
        """GET /v3/cities -- tipos de serviço (serviceType) e specialRequests
        disponíveis por cidade no mercado configurado."""
        dados = self._chamar("GET", "/v3/cities")
        return dados if isinstance(dados, list) else [dados]

    def cotar(self, stops: list[dict], service_type: str, schedule_at: datetime | str | None = None,
              is_route_optimized: bool = False, special_requests: list[str] | None = None) -> dict:
        """POST /v3/quotations. `stops` = [{'coordinates': {'lat': '...', 'lng': '...'},
        'address': '...'}, ...] (2 a 16). Retorna dict com quotationId,
        expiresAt, priceBreakdown, distance e stops (com stopId)."""
        if not 2 <= len(stops) <= MAX_PARADAS:
            raise ValueError(f"Lalamove: pedido precisa ter entre 2 e {MAX_PARADAS} paradas (recebeu {len(stops)}).")
        dados = {
            "serviceType": service_type,
            "language": self.language,
            "stops": stops,
        }
        if schedule_at:
            dados["scheduleAt"] = _para_iso_utc(schedule_at)
        if is_route_optimized and len(stops) > 2:
            dados["isRouteOptimized"] = True
        if special_requests:
            dados["specialRequests"] = list(special_requests)
        return self._chamar("POST", "/v3/quotations", dados)

    def consultar_cotacao(self, quotation_id: str) -> dict:
        return self._chamar("GET", f"/v3/quotations/{quotation_id}")

    def criar_pedido(self, quotation_id: str, sender: dict, recipients: list[dict],
                     is_pod_enabled: bool = True, metadata: dict | None = None,
                     partner: str | None = None) -> dict:
        """POST /v3/orders. sender = {'stopId','name','phone'};
        recipients = [{'stopId','name','phone','remarks'?}, ...] (um por
        parada de entrega, na mesma ordem dos stopIds da cotação)."""
        dados = {
            "quotationId": quotation_id,
            "sender": sender,
            "recipients": recipients,
            "isPODEnabled": bool(is_pod_enabled),
        }
        if metadata:
            dados["metadata"] = {str(k): str(v) for k, v in metadata.items()}
        if partner:
            dados["partner"] = partner
        return self._chamar("POST", "/v3/orders", dados)

    def consultar_pedido(self, order_id: str) -> dict:
        return self._chamar("GET", f"/v3/orders/{order_id}")

    def cancelar_pedido(self, order_id: str) -> None:
        """DELETE /v3/orders/{id}. Só funciona em ASSIGNING_DRIVER ou até
        5 min após o motorista aceitar."""
        self._chamar("DELETE", f"/v3/orders/{order_id}")

    def consultar_motorista(self, order_id: str, driver_id: str) -> dict:
        return self._chamar("GET", f"/v3/orders/{order_id}/drivers/{driver_id}")

    def configurar_webhook(self, url: str) -> dict:
        return self._chamar("PATCH", "/v3/webhook", {"url": url})


# ----------------------------------------------------------------------
# Helpers de conversão
# ----------------------------------------------------------------------
def _para_iso_utc(valor: datetime | str) -> str:
    """Lalamove exige scheduleAt em UTC, formato 'YYYY-MM-DDTHH:MM:00Z'."""
    if isinstance(valor, str):
        return valor
    if valor.tzinfo is None:
        # Datas do projeto são horário de Brasília (UTC-3, sem DST desde 2019).
        from datetime import timedelta
        valor = valor.replace(tzinfo=timezone(timedelta(hours=-3)))
    return valor.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:00Z")


def montar_stop(lat: float, lng: float, endereco: str) -> dict:
    return {
        "coordinates": {"lat": f"{float(lat):.6f}", "lng": f"{float(lng):.6f}"},
        "address": (endereco or "").strip()[:255],
    }


def stops_da_rota(base: dict, entregas: list[dict]) -> list[dict]:
    """
    base = {'latitude','longitude','endereco'}
    entregas = [{'latitude','longitude','endereco', ...}, ...] em ordem de rota.
    Retorna a lista de stops (coleta + entregas) pronta pra cotar().
    """
    if len(entregas) + 1 > MAX_PARADAS:
        raise ValueError(
            f"Lalamove: rota com {len(entregas)} entregas excede o limite de {MAX_PARADAS - 1} "
            "entregas por pedido."
        )
    faltando = [e.get("code") or e.get("id") for e in entregas
                if not e.get("latitude") or not e.get("longitude")]
    if faltando:
        raise ValueError(f"Lalamove: pedidos sem coordenada geocodificada: {faltando}")
    stops = [montar_stop(base["latitude"], base["longitude"], base["endereco"])]
    for e in entregas:
        stops.append(montar_stop(e["latitude"], e["longitude"], e["endereco"]))
    return stops


def resumir_pedido(pedido: dict) -> dict:
    """Extrai o que a operação precisa de um GET /v3/orders/{id}."""
    paradas = []
    for s in pedido.get("stops", []) or []:
        pod = s.get("POD") or {}
        paradas.append({
            "stopId": s.get("stopId"),
            "address": s.get("address"),
            "pod_status": pod.get("status"),
            "pod_image": pod.get("image"),
            "pod_delivered_at": pod.get("deliveredAt"),
        })
    return {
        "orderId": str(pedido.get("orderId", "")),
        "status": pedido.get("status"),
        "driverId": pedido.get("driverId") or "",
        "shareLink": pedido.get("shareLink"),
        "total": (pedido.get("priceBreakdown") or {}).get("total"),
        "currency": (pedido.get("priceBreakdown") or {}).get("currency"),
        "paradas": paradas,
    }