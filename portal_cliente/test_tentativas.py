# -*- coding: utf-8 -*-
"""
Testes da lista do portal com uma linha por pedido (Hugo, 05/11: "cada nota
fiscal uma só vez para os clientes, com um histórico de tentativas" e
"ignorar os casos de não coleta para a visão do cliente"). Os casos saem
da contagem de produção de 05/11 (380 pedidos em 419 linhas).

    py -3.11 -m unittest portal_cliente.test_tentativas
"""
import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import dados_cliente as dc  # noqa: E402

HOJE = date(2026, 11, 5)
REMETENTE = 100
NAO_COLETADO, FORA_DO_HORARIO = 5744, 5452
_ids = iter(range(1, 10000))


def _servico(codigo, status="done", falha=None, completed_at="2026-11-05 10:12:00"):
    return {"id": next(_ids), "code": codigo, "sender_id": REMETENTE, "status": status,
            "status_done": "failed" if falha else None, "failed_reason_id": falha,
            "dimension_3": 1, "address": "Rua X 10, Sao Paulo - SP",
            "completed_at": completed_at if status == "done" else None,
            "customer": {"data": {"name": "DESTINO"}}}


def _rota(rota_id, dia: date, *servicos, nome=None):
    utc = datetime.combine(dia, datetime.min.time()).replace(hour=9)   # 06:00 em SP
    return {"id": rota_id, "name": nome or f"Rota {rota_id}", "status": "finished", "agent_id": None,
            "start_at": utc.strftime("%Y-%m-%d %H:%M:%S"), "services": {"data": list(servicos)}}


def _sem_rota(codigo):
    return dc._linha({"id": next(_ids), "code": codigo, "sender_id": REMETENTE}, "aguardando_saida", {},
                     detalhe="Aguardando roteirização")


def _unir(rotas, sem_rota=()):
    linhas = dc._carimbar_dia(dc.linhas_das_rotas(rotas, REMETENTE, {}, {}) + list(sem_rota), HOJE)
    return dc.unir_tentativas(linhas, HOJE)


def _dia(n):
    return HOJE - timedelta(days=n)


class UmaLinhaPorPedidoTest(unittest.TestCase):
    def test_insucesso_e_reentrega_entregue_viram_uma_linha_com_duas_tentativas(self):
        # PS-43009: 04/11 fora do horário, 05/11 entregue na -R1.
        linhas = _unir([_rota(50, _dia(1), _servico("#PS-43009", falha=FORA_DO_HORARIO)),
                        _rota(10, HOJE, _servico("#PS-43009-R1"))])
        self.assertEqual(len(linhas), 1)
        p = linhas[0]
        self.assertEqual((p["codigo"], p["codigo_servico"], p["situacao"], p["data"]),
                         ("#PS-43009", "#PS-43009-R1", "entregue", HOJE.isoformat()))
        self.assertEqual([(t["data"], t["situacao"], t["motivo"]) for t in p["tentativas"]],
                         [(_dia(1).isoformat(), "insucesso", "Fora do horário de recebimento"),
                          (HOJE.isoformat(), "entregue", "")])

    def test_so_nao_coletado_vira_aguardando_saida_sem_historico(self):
        # PS-43014: não coletado em 03/11 e de novo em 05/11.
        linhas = _unir([_rota(6, _dia(2), _servico("#PS-43014", falha=NAO_COLETADO)),
                        _rota(5, HOJE, _servico("#PS-43014-R1", falha=NAO_COLETADO))])
        self.assertEqual(len(linhas), 1)
        p = linhas[0]
        self.assertEqual((p["situacao"], p["situacao_rotulo"], p["data"]),
                         ("aguardando_saida", "Aguardando saída", HOJE.isoformat()))
        self.assertEqual((p["motorista"], p["rota"], p["motivo"], p["comprovante"]), ("", "", "", False))
        self.assertEqual(len(p["tentativas"]), 1)

    def test_nao_coletado_depois_da_entrega_nao_apaga_a_entrega(self):
        # PS-43158: -R1 entregue em 04/11, original "não coletado" em 05/11.
        linhas = _unir([_rota(14, _dia(1), _servico("#PS-43158-R1")),
                        _rota(9, HOJE, _servico("#PS-43158", falha=NAO_COLETADO))])
        self.assertEqual([(p["situacao"], p["codigo_servico"]) for p in linhas], [("entregue", "#PS-43158-R1")])

    def test_nao_coletado_some_do_historico(self):
        linhas = _unir([_rota(1, _dia(2), _servico("#PS-7", falha=FORA_DO_HORARIO)),
                        _rota(2, _dia(1), _servico("#PS-7-R1", falha=NAO_COLETADO)),
                        _rota(3, HOJE, _servico("#PS-7-R2"))])
        self.assertEqual([t["situacao"] for t in linhas[0]["tentativas"]], ["insucesso", "entregue"])

    def test_insucesso_e_nova_tentativa_sem_rota_mostra_aguardando_saida(self):
        linhas = _unir([_rota(1, _dia(1), _servico("#PS-8", falha=FORA_DO_HORARIO))],
                       sem_rota=[_sem_rota("#PS-8-R1")])
        self.assertEqual(len(linhas), 1)
        self.assertEqual(linhas[0]["situacao"], "aguardando_saida")
        self.assertEqual([t["situacao"] for t in linhas[0]["tentativas"]], ["insucesso", "aguardando_saida"])

    def test_rota_duplicada_no_mesmo_dia_vira_uma_linha_sem_selo(self):
        # 31/10: duas "Rota 31" com os mesmos pedidos, uma esquecida na Vuupt.
        linhas = _unir([_rota(31, _dia(5), _servico("#PS-42625", status="assigned")),
                        _rota(32, _dia(5), _servico("#PS-42625", status="assigned"), nome="Rota 31")])
        self.assertEqual(len(linhas), 1)
        self.assertEqual(len(linhas[0]["tentativas"]), 1)

    def test_programado_em_rota_antiga_perde_pra_rota_nova(self):
        # PS-42982: "Programado" na Rota 1 de 03/11 e na Rota 12 de 04/11.
        linhas = _unir([_rota(1, _dia(2), _servico("#PS-42982", status="assigned")),
                        _rota(12, _dia(1), _servico("#PS-42982", status="assigned"))])
        self.assertEqual([(p["data"], p["rota"]) for p in linhas], [(_dia(1).isoformat(), "Rota 12")])
        self.assertEqual(len(linhas[0]["tentativas"]), 1)

    def test_entregue_em_dobro_vira_uma_tentativa(self):
        # PS-42899 e PS-42899-R1 baixados juntos como entregues na Rota 22.
        linhas = _unir([_rota(22, _dia(6), _servico("#PS-42899-R1", completed_at="2026-10-30 09:44:00"),
                              _servico("#PS-42899", completed_at="2026-10-30 09:44:00"))])
        self.assertEqual(len(linhas), 1)
        self.assertEqual(len(linhas[0]["tentativas"]), 1)

    def test_entregue_vence_tentativa_posterior(self):
        linhas = _unir([_rota(1, _dia(1), _servico("#PS-9")),
                        _rota(2, HOJE, _servico("#PS-9-R1", status="assigned"))])
        self.assertEqual(linhas[0]["situacao"], "entregue")

    def test_pedidos_diferentes_seguem_separados(self):
        linhas = _unir([_rota(1, HOJE, _servico("#PS-1"), _servico("#PS-2", falha=FORA_DO_HORARIO))])
        self.assertEqual(sorted(p["codigo"] for p in linhas), ["#PS-1", "#PS-2"])
        self.assertTrue(all(len(p["tentativas"]) == 1 for p in linhas))

    def test_texto_das_tentativas_pra_planilha(self):
        linhas = _unir([_rota(50, _dia(1), _servico("#PS-43009", falha=FORA_DO_HORARIO)),
                        _rota(10, HOJE, _servico("#PS-43009-R1"))])
        self.assertEqual(linhas[0]["tentativas_texto"],
                         "04/11 Insucesso (Fora do horário de recebimento); 05/11 Entregue")


if __name__ == "__main__":
    unittest.main()
