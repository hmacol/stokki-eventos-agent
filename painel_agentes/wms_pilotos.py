"""
Embarcadores piloto do WMS do galpao (17/11/2026).

Ate aqui o WMS assumia UM piloto (wms.embarcador_piloto_id) e uma chave
global (wms.caixas_ativo). Com a MOMBAK entrando no teste de separacao antes
da Maria Dolores, cada piloto passa a dizer se opera por caixa:

    wms:
      pilotos:
        - {id: 48, nome: "MARIA DOLORES", caixas: false}
        - {id: 22, nome: "MOMBAK", caixas: true}

Sem a lista, valem as chaves antigas (um piloto so). Modulo de dados puro:
recebe o config ja carregado, nao le arquivo.
"""


def _verdadeiro(valor) -> bool:
    return str(valor).strip().lower() in ("true", "1", "sim")


def pilotos(config: dict) -> list[dict]:
    """[{id: int, nome: str, caixas: bool}] na ordem do config, sem id repetido."""
    wms = (config or {}).get("wms") or {}
    lista = wms.get("pilotos")
    if not isinstance(lista, list) or not lista:
        lista = [{"id": wms.get("embarcador_piloto_id") or 48,
                  "nome": wms.get("embarcador_piloto") or "MARIA DOLORES",
                  "caixas": wms.get("caixas_ativo", False)}]
    saida, vistos = [], set()
    for item in lista:
        if not isinstance(item, dict):
            continue
        try:
            pid = int(str(item.get("id")).strip())
        except (TypeError, ValueError):
            continue
        if pid in vistos:
            continue
        vistos.add(pid)
        saida.append({"id": pid, "nome": str(item.get("nome") or "").strip().upper(),
                      "caixas": _verdadeiro(item.get("caixas", False))})
    return saida


def ids_com_caixas(config: dict) -> set[int]:
    return {p["id"] for p in pilotos(config) if p["caixas"]}
