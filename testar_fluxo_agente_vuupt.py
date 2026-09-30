"""
Teste ponta a ponta do fluxo "agente" na Vuupt (28/08/2026).

A API pública da Vuupt NÃO tem endpoints de ação do agente (aceitar /
iniciar / finalizar / preencher checklist) -- isso só existe no app.
Então o teste é dividido:

  python testar_fluxo_agente_vuupt.py criar
      -> cria o serviço PS-TESTE-<hhmm> pro cliente "usuario teste"
         (endereço da base, Rua Zilda 288) e uma rota de hoje atribuída
         ao agente Hugo Maçol Sousa (28438). Imprime service_id/route_id.

  (no app da Vuupt, como agente: aceitar, iniciar, finalizar, checklist)

  python testar_fluxo_agente_vuupt.py status <service_id>
      -> mostra status/timestamps do serviço, a checklist preenchida
         (GET /checklists?include=fields -> valores, fotos, assinatura)
         e baixa o PDF do canhoto em dados/teste_agente_vuupt/.

  python testar_fluxo_agente_vuupt.py simular <service_id> [falha [failed_reason_id]]
      -> percorre accept/start/check-in/check-out por API (endpoints não
         documentados, ver simular()). Não preenche checklist.

  python testar_fluxo_agente_vuupt.py limpar <service_id> [route_id]
      -> cancela a rota (services_action=unassign) e o serviço.
"""
import json
import sys
from datetime import date, datetime
from pathlib import Path

import requests
import yaml

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "roteirizacao"))

from http_retry import chamar_com_retry  # noqa: E402
from rotas_client import buscar_rota, cancelar_rota, criar_rota  # noqa: E402
from vuupt_client import VuuptClient  # noqa: E402

API_BASE = "https://api.vuupt.com/api/v1"
AGENT_ID_HUGO = 28438  # "Hugo Maçol Sousa" (GET /agents, 28/08/2026)
CUSTOMER_ID_TESTE = 14425519  # "usuario teste" -- R. Zilda, 288, Casa Verde Alta (endereço da base)
BASE_LOCATION_ID = 6950
PASTA_SAIDA = Path(__file__).parent / "dados" / "teste_agente_vuupt"


def _token() -> str:
    cfg = yaml.safe_load(open(Path(__file__).parent / "config.yaml", encoding="utf-8")) or {}
    tok = cfg.get("vuupt_api", {}).get("token", "")
    if not tok:
        raise SystemExit("config.yaml sem vuupt_api.token")
    return tok


def _headers(tok: str) -> dict:
    return {"Authorization": f"Bearer {tok}", "Accept": "application/json"}


def criar():
    tok = _token()
    vuupt = VuuptClient(tok)
    agora = datetime.now()
    code = f"PS-TESTE-{agora:%d%m-%H%M}"
    payload = {
        "title": f"TESTE FLUXO AGENTE {agora:%d/%m %H:%M}",
        "code": code,
        "type": "delivery",
        "customer_id": CUSTOMER_ID_TESTE,
        "note": "Serviço de TESTE de integração -- pode finalizar com sucesso e preencher o checklist.",
        "dimension_3": 1,
    }
    resultado = vuupt.criar_servico(payload)
    servico = resultado.get("service", resultado)
    service_id = servico["id"]
    print(f"Serviço criado: id={service_id} code={code} status={servico.get('status')}")

    hoje = date.today()
    nome_rota = f"TESTE FLUXO AGENTE - {hoje:%d/%m/%Y} {agora:%H:%M}"
    rota = criar_rota(
        tok, nome=nome_rota, start_at=f"{hoje:%Y-%m-%d}T{agora:%H:%M}:00Z",
        start_location_base_id=BASE_LOCATION_ID, service_ids=[service_id],
        end_location_base_id=BASE_LOCATION_ID, agent_id=AGENT_ID_HUGO,
    )
    print(f"Rota criada: id={rota['id']} nome='{nome_rota}' agent_id={AGENT_ID_HUGO}")

    servico = vuupt.buscar_servico_por_id(service_id) or {}
    servico = servico.get("service") or servico  # a API embrulha em {"service": {...}}
    print(f"Serviço após rota: status={servico.get('status')} driver_id={servico.get('driver_id')} "
          f"route_id={servico.get('route_id')}")
    print(f"\nAgora, no app da Vuupt (agente Hugo Maçol Sousa): aceitar, iniciar, finalizar e preencher o checklist.")
    print(f"Depois: python testar_fluxo_agente_vuupt.py status {service_id}")


def status(service_id: int):
    tok = _token()
    h = _headers(tok)
    resp = chamar_com_retry(
        requests.get, f"https://app.vuupt.com/api/v1/services/{service_id}",
        headers=h, params={"include": "checklistAnswers,failedReason"}, timeout=20,
    )
    resp.raise_for_status()
    s = resp.json()
    s = s.get("service") or s.get("data") or s  # GET /services/{id} devolve {"service": {...}}
    campos =["id", "code", "status", "status_done", "driver_id", "route_id", "route_sequence",
              "assigned_at", "accepted_at", "started_at", "arrived_at", "completed_at",
              "outside_radius", "validated_by_agent", "failed_reason_id"]
    print("== Serviço ==")
    for c in campos:
        print(f"  {c}: {s.get(c)}")
    if s.get("failedReason"):
        print(f"  failedReason: {s['failedReason']}")

    if s.get("route_id"):
        rota = buscar_rota(tok, s["route_id"], include=["agent", "activities"])
        r = rota.get("route", rota.get("data", rota))
        print("== Rota ==")
        for c in ["id", "name", "status", "started_at", "finished_at"]:
            print(f"  {c}: {r.get(c)}")
        ag = (r.get("agent") or {}).get("data") or r.get("agent")
        if ag:
            print(f"  agent: {ag.get('id')} {ag.get('name')}")

    print("== Checklists (GET /checklists, entity_id = serviço) ==")
    resp = chamar_com_retry(
        requests.get, f"{API_BASE}/checklists", headers=h, timeout=30,
        params={
            "filter[0][field]": "entity_id", "filter[0][operator]": "eq", "filter[0][value]": service_id,
            "include": "fields", "per_page": 20,
        },
    )
    resp.raise_for_status()
    checklists = resp.json().get("data", [])
    # Filtro pode ser ignorado silenciosamente pela API -- confere entity_id de cada item
    checklists = [c for c in checklists if str(c.get("entity_id")) == str(service_id)]
    if not checklists:
        print("  (nenhuma checklist preenchida ainda)")
    PASTA_SAIDA.mkdir(parents=True, exist_ok=True)
    for c in checklists:
        print(f"  checklist id={c['id']} trigger={c.get('trigger')} entity_type={c.get('entity_type')} "
              f"user_id={c.get('user_id')} filled_at={c.get('filled_at')} lat/long={c.get('latitude')},{c.get('longitude')}")
        for f in (c.get("fields") or {}).get("data", []):
            nome = ((f.get("checklist_form_field") or {}).get("data") or {}).get("name") \
                if isinstance((f.get("checklist_form_field") or {}).get("data"), dict) else f.get("checklist_form_field_id")
            print(f"    - campo {nome}: value={f.get('value')!r} file_url={f.get('file_url')} image_source={f.get('image_source')}")
        pdf = chamar_com_retry(requests.get, f"{API_BASE}/checklists/{c['id']}/print",
                               headers={**h, "Accept": "application/pdf"}, timeout=60)
        if pdf.ok and pdf.content[:4] == b"%PDF":
            destino = PASTA_SAIDA / f"checklist_{c['id']}_servico_{service_id}.pdf"
            destino.write_bytes(pdf.content)
            print(f"    PDF salvo em {destino}")
        else:
            print(f"    PDF não disponível ({pdf.status_code})")
        (PASTA_SAIDA / f"checklist_{c['id']}.json").write_text(json.dumps(c, ensure_ascii=False, indent=2), encoding="utf-8")
    (PASTA_SAIDA / f"servico_{service_id}.json").write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")


def simular(service_id: int, sucesso: bool = True, failed_reason_id: int | None = None):
    """Percorre o ciclo do agente por API usando endpoints NÃO documentados
    (descobertos 28/08/2026, funcionam com o token de conta):
      PUT /services/{id}/accept  -> accepted
      PUT /services/{id}/start   -> on_route
      PUT /services/{id}/check-in  -> arrived
      PUT /services/{id}/check-out -> done (form-urlencoded!)
    ARMADILHA: no check-out, `accomplished` tem que ir como STRING "true"
    em form-urlencoded. JSON true / 1 / "1" viram done+failed com o
    primeiro motivo da conta (5431 'Local fechado'). Não gera checklist."""
    tok = _token()
    h = _headers(tok)
    for etapa in ("accept", "start", "check-in"):
        resp = chamar_com_retry(requests.put, f"{API_BASE}/services/{service_id}/{etapa}", headers=h, timeout=20)
        print(f"PUT /{etapa} -> {resp.status_code} {resp.text[:100]}")
    data = {"accomplished": "true"} if sucesso else {"accomplished": "false"}
    if not sucesso and failed_reason_id:
        data["failed_reason_id"] = str(failed_reason_id)
    resp = chamar_com_retry(requests.put, f"{API_BASE}/services/{service_id}/check-out", headers=h, data=data, timeout=20)
    print(f"PUT /check-out {data} -> {resp.status_code} {resp.text[:100]}")
    status(service_id)


def limpar(service_id: int, route_id: int | None):
    tok = _token()
    vuupt = VuuptClient(tok)
    if route_id is None:
        s = vuupt.buscar_servico_por_id(service_id) or {}
        s = s.get("service") or s
        route_id = s.get("route_id")
    if route_id:
        try:
            cancelar_rota(tok, route_id, services_action="unassign")
            print(f"Rota {route_id} cancelada (serviços desatribuídos).")
        except requests.exceptions.HTTPError as e:
            # Rota 'Finalizada' não pode ser cancelada (400) -- mas DELETE /routes/{id} exclui (204, visto 28/08)
            print(f"Cancelar rota falhou ({str(e)[:120]}...); tentando DELETE /routes/{route_id}")
            resp = chamar_com_retry(requests.delete, f"{API_BASE}/routes/{route_id}", headers=_headers(tok), timeout=30)
            print(f"DELETE /routes/{route_id} -> {resp.status_code}")
    resp = chamar_com_retry(requests.put, f"{API_BASE}/services/{service_id}/cancel", headers=_headers(tok), timeout=20)
    print(f"PUT /services/{service_id}/cancel -> {resp.status_code} {resp.text[:200]}")
    if not resp.ok:
        # 409 quando 'done' -- DELETE exclui mesmo finalizado (204, visto 28/08)
        vuupt.cancelar_servico(service_id)
        print(f"Serviço {service_id} excluído via DELETE.")


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in ("criar", "status", "simular", "limpar"):
        print(__doc__)
        sys.exit(1)
    cmd = sys.argv[1]
    if cmd == "criar":
        criar()
    elif cmd == "status":
        status(int(sys.argv[2]))
    elif cmd == "simular":
        # python testar_fluxo_agente_vuupt.py simular <service_id> [falha [failed_reason_id]]
        simular(int(sys.argv[2]), sucesso=(len(sys.argv) < 4 or sys.argv[3] != "falha"),
                failed_reason_id=int(sys.argv[4]) if len(sys.argv) > 4 else None)
    else:
        limpar(int(sys.argv[2]), int(sys.argv[3]) if len(sys.argv) > 3 else None)