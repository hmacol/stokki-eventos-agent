# -*- coding: utf-8 -*-
"""
testar_lalamove_e2e.py -- teste ponta a ponta da integração Lalamove
(sandbox) usando o MESMO caminho do botão "Confirmar e enviar".

    python testar_lalamove_e2e.py criar
        -> cria 2 serviços de teste na VUUPT (LALA-TESTE-*), monta um
           rascunho pra amanhã com o motorista virtual LALAMOVE (50258),
           chama rascunhos_rota.enviar_rascunho (rota na VUUPT + pedido
           Lalamove sandbox + código no título). Imprime ids.

    python testar_lalamove_e2e.py sync
        -> roda lalamove_integracao.sincronizar_pedidos (o que o timer faz).

    python testar_lalamove_e2e.py limpar <rascunho_id>
        -> cancela o pedido Lalamove, a rota e os serviços na VUUPT e
           descarta o rascunho.
"""
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "roteirizacao"))
sys.path.insert(0, str(Path(__file__).parent / "painel_agentes"))

import rascunhos_rota  # noqa: E402
from lalamove_integracao import _carregar_config, cfg_lalamove, cliente_de_config, sincronizar_pedidos  # noqa: E402
from rotas_client import cancelar_rota  # noqa: E402
from vuupt_client import VuuptClient  # noqa: E402

ENDERECOS_TESTE = [
    ("Av. Paulista, 1578, Bela Vista, São Paulo, SP", -23.561414, -46.655881, "+5511900000001"),
    ("Rua Augusta, 1500, Consolação, São Paulo, SP", -23.556000, -46.661000, "+5511900000002"),
]


def criar():
    config = _carregar_config()
    token = config["vuupt_api"]["token"]
    cfg = cfg_lalamove(config)
    vuupt = VuuptClient(token)
    agora = datetime.now()
    paradas = []
    for i, (end, lat, lng, tel) in enumerate(ENDERECOS_TESTE, start=1):
        code = f"LALA-TESTE-{agora:%d%m-%H%M}-{i}"
        payload = {
            "title": f"TESTE LALAMOVE {i} {agora:%d/%m %H:%M}", "code": code, "type": "delivery",
            "customer": {"name": f"Cliente teste Lalamove {i}", "code": f"LALA-TESTE-CLI-{i}",
                         "address": end, "latitude": lat, "longitude": lng, "phone_number": tel},
            "phone_number": tel, "dimension_3": 1,
            "note": "Serviço de TESTE da integração Lalamove -- pode cancelar.",
        }
        r = vuupt.criar_servico(payload)
        s = r.get("service", r)
        print(f"Serviço VUUPT criado: id={s['id']} code={code}")
        paradas.append({"service_id": s["id"], "codigo": code, "titulo": payload["title"], "endereco": end,
                        "latitude": lat, "longitude": lng, "sender_id": None, "remetente_nome": "Fresh Log (teste)",
                        "destinatario_nome": payload["customer"]["name"], "nivel_dificuldade": 1, "volume_caixas": 1})

    amanha = date.today() + timedelta(days=1)
    ref = rascunhos_rota.referencia_para_rascunho_manual(amanha)
    rid = rascunhos_rota.criar_rascunho_com_paradas(
        amanha, ref["lote_id"], ref["particao"], ref["tipo_rota"],
        ref["start_location_base_id"], ref["end_location_base_id"], f"{amanha.isoformat()}T13:00:00Z", paradas,
    )
    conn = rascunhos_rota._conectar()
    conn.execute("UPDATE rascunhos_rota SET nome = ? WHERE id = ?", (f"TESTE LALAMOVE - {agora:%d/%m %H:%M}", rid))
    conn.commit(); conn.close()
    rascunhos_rota.trocar_motorista(rid, int(cfg["agent_id_vuupt"]), None, "LALAMOVE (virtual)")
    print(f"Rascunho {rid} criado pra {amanha} com motorista LALAMOVE ({cfg['agent_id_vuupt']}).")

    print("\n== enviar_rascunho (mesmo caminho do botão Confirmar e enviar) ==")
    res = rascunhos_rota.enviar_rascunho(rid, token)
    print(res)
    r = rascunhos_rota.buscar_rascunho(rid)
    print(f"\nRascunho: status={r['status']} vuupt_route_id={r['vuupt_route_id']} "
          f"lalamove_order_id={r['lalamove_order_id']} lalamove_status={r['lalamove_status']} "
          f"preco={r['lalamove_preco']} erro={r['lalamove_erro']}\nlink={r['lalamove_share_link']}")
    for p in r["paradas"]:
        s = vuupt.buscar_servico_por_id(p["service_id"]) or {}
        print(f"  serviço {p['service_id']}: status={s.get('status')} title={s.get('title')!r}")
    print(f"\nLimpar depois: python testar_lalamove_e2e.py limpar {rid}")


def sync():
    config = _carregar_config()
    print(sincronizar_pedidos(config["vuupt_api"]["token"], config))
    for r in rascunhos_rota.listar_rascunhos_lalamove_abertos():
        print(f"aberto: rascunho {r['id']} pedido {r['lalamove_order_id']} status {r['lalamove_status']}")


def limpar(rid: int):
    config = _carregar_config()
    token = config["vuupt_api"]["token"]
    vuupt = VuuptClient(token)
    r = rascunhos_rota.buscar_rascunho(rid)
    if not r:
        print("rascunho não encontrado"); return
    if r.get("lalamove_order_id"):
        try:
            cliente_de_config(cfg_lalamove(config)).cancelar_pedido(r["lalamove_order_id"])
            print(f"Pedido Lalamove {r['lalamove_order_id']} cancelado.")
        except Exception as e:
            print(f"Cancelar pedido Lalamove falhou: {e}")
    if r.get("vuupt_route_id"):
        try:
            cancelar_rota(token, r["vuupt_route_id"], services_action="unassign")
            print(f"Rota VUUPT {r['vuupt_route_id']} cancelada.")
        except Exception as e:
            print(f"Cancelar rota falhou: {e}")
    for p in r["paradas"]:
        try:
            vuupt.cancelar_servico_oficial(p["service_id"])
        except Exception:
            pass
        try:
            vuupt.cancelar_servico(p["service_id"])
            print(f"Serviço {p['service_id']} excluído.")
        except Exception as e:
            print(f"Excluir serviço {p['service_id']} falhou: {e}")
    rascunhos_rota.descartar_rascunho(rid)
    print(f"Rascunho {rid} descartado.")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "criar":
        criar()
    elif cmd == "sync":
        sync()
    elif cmd == "limpar":
        limpar(int(sys.argv[2]))
    else:
        print(__doc__)
