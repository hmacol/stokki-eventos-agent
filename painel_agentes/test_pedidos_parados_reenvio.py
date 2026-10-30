# -*- coding: utf-8 -*-
"""
test_pedidos_parados_reenvio.py

"Reenvio" da triagem segue a cadeia de reentregas (achado 28/09): se a
R1 também falhou, gera a R2 em vez de responder "já duplicado → R1" e
dar a tratativa por concluída. E a sugestão via Vuupt não chama mais de
"Em Rota" o pedido que está no pool: desde 30/10 ele vira "Na Vuupt"
(continua pendente).

Rodar (da raiz):
    py -3.11 -m unittest painel_agentes.test_pedidos_parados_reenvio -v
"""
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(Path(__file__).parent))

import pedidos_parados_triagem as ppt  # noqa: E402

ORIGINAL = {"id": 1, "code": "#PS-100", "status": "done", "completed_at": "x", "failed_reason_id": 7}


class _Fingerprint:
    def __init__(self, duplicados: dict):
        self.duplicados = dict(duplicados)  # service_id -> novo_code

    def ja_duplicado(self, sid):
        return sid in self.duplicados

    def buscar_novo_code(self, sid):
        return self.duplicados.get(sid)

    def marcar_duplicado(self, sid, code):
        self.duplicados[sid] = code


class TestReenvio(unittest.TestCase):
    def _rodar(self, servicos_por_code: dict, duplicados: dict):
        fp = _Fingerprint(duplicados)
        vuupt = mock.Mock()
        vuupt.buscar_servico_por_code.side_effect = lambda c: servicos_por_code.get(c.lstrip("#"))
        expedir = mock.Mock()
        expedir.duplicar_servico_por_insucesso.side_effect = lambda v, s: {"code": "#PS-100-R2"}
        acoes = []
        with mock.patch.dict(sys.modules, {"fingerprint_duplicacao_insucesso": fp}), \
             mock.patch.object(ppt, "_vuupt", return_value=vuupt), \
             mock.patch.object(ppt, "_resolver_pedido", return_value=(ORIGINAL, "PS-100")), \
             mock.patch.object(ppt, "_expedir_pedidos_raiz", return_value=expedir), \
             mock.patch.object(ppt, "_marcar_acao", side_effect=lambda *a: acoes.append(a)), \
             mock.patch.object(ppt.tratativas, "registrar_evento"):
            resultado = ppt.duplicar("100", "teste")
        return resultado, expedir, fp, acoes

    def test_r1_falhou_gera_r2(self):
        r1 = {"id": 2, "code": "#PS-100-R1", "status": "done", "completed_at": "x", "failed_reason_id": 7}
        resultado, expedir, fp, _ = self._rodar({"PS-100-R1": r1}, {1: "#PS-100-R1"})
        self.assertFalse(resultado["ja_existia"])
        self.assertEqual(resultado["novo_code"], "#PS-100-R2")
        expedir.duplicar_servico_por_insucesso.assert_called_once()
        self.assertIs(expedir.duplicar_servico_por_insucesso.call_args[0][1], r1)
        self.assertEqual(fp.duplicados[2], "#PS-100-R2")

    def test_r1_ativa_nao_duplica(self):
        r1 = {"id": 2, "code": "#PS-100-R1", "status": "not_assigned"}
        resultado, expedir, _, acoes = self._rodar({"PS-100-R1": r1}, {1: "#PS-100-R1"})
        self.assertTrue(resultado["ja_existia"])
        expedir.duplicar_servico_por_insucesso.assert_not_called()
        self.assertIn("já ativa", acoes[-1][2])

    def test_r1_entregue_nao_duplica(self):
        r1 = {"id": 2, "code": "#PS-100-R1", "status": "done", "completed_at": "x"}
        resultado, expedir, _, _ = self._rodar({"PS-100-R1": r1}, {1: "#PS-100-R1"})
        self.assertTrue(resultado["ja_existia"])
        expedir.duplicar_servico_por_insucesso.assert_not_called()

    def test_original_sem_reentrega_duplica(self):
        resultado, expedir, _, _ = self._rodar({}, {})
        self.assertFalse(resultado["ja_existia"])
        expedir.duplicar_servico_por_insucesso.assert_called_once()


class TestSugestaoVuupt(unittest.TestCase):
    """not_assigned está no POOL, não em rota: vira "Na Vuupt" (30/10)."""
    HOJE = __import__("datetime").date(2026, 9, 28)

    def test_pool_sem_agendamento_vira_na_vuupt(self):
        alvo, motivo = ppt._sugestao_vuupt({"status": "not_assigned"}, self.HOJE)
        self.assertEqual(alvo, "Na Vuupt")
        self.assertTrue(motivo.startswith(ppt.MOTIVO_NO_POOL))

    def test_pool_agendado_hoje_vira_na_vuupt(self):
        alvo, _ = ppt._sugestao_vuupt({"status": "not_assigned", "scheduled_start": "2026-09-28 12:00:00"}, self.HOJE)
        self.assertEqual(alvo, "Na Vuupt")

    def test_pool_agendamento_vencido_vira_na_vuupt(self):
        alvo, _ = ppt._sugestao_vuupt({"status": "not_assigned", "scheduled_start": "2026-09-25 12:00:00"}, self.HOJE)
        self.assertEqual(alvo, "Na Vuupt")

    def test_agendado_pra_depois_segue_agendado(self):
        alvo, _ = ppt._sugestao_vuupt({"status": "not_assigned", "scheduled_start": "2026-09-30 12:00:00"}, self.HOJE)
        self.assertEqual(alvo, "Agendado")

    def test_atribuido_segue_em_rota(self):
        alvo, _ = ppt._sugestao_vuupt({"status": "assigned"}, self.HOJE)
        self.assertEqual(alvo, "Em Rota")


class TestConsultaVuuptNaVuupt(unittest.TestCase):
    """Consultar Vuupt com serviço no pool: "Em Rota" falso vira "Na Vuupt";
    classificação de outra fonte (Cancelados) não é sobrescrita."""

    def test_em_rota_falso_vira_na_vuupt_e_cancelado_fica(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(ppt, "DB_PATH", Path(tmp) / "t.db"), \
             mock.patch.object(ppt.tratativas, "registrar_evento"), \
             mock.patch.object(ppt, "VUUPT_PAUSA_ENTRE_CONSULTAS_SEG", 0), \
             mock.patch.object(ppt, "_vuupt", return_value=mock.Mock()), \
             mock.patch.object(ppt, "_servico_mais_recente_da_cadeia", side_effect=lambda v, s: s), \
             mock.patch.object(ppt, "_resolver_pedido",
                               side_effect=lambda v, n: ({"id": int(n), "code": f"#PS-{n}", "status": "not_assigned"}, f"PS-{n}")):
            ppt.classificar("100", "f1", "Em Rota", "teste")
            ppt.classificar("200", "f2", "Cancelados", "teste")
            resumo = ppt.verificar_na_vuupt(
                [{"order_number": "100", "freshhub_id": "f1"}, {"order_number": "200", "freshhub_id": "f2"},
                 {"order_number": "300", "freshhub_id": "f3"}], "teste")
            conn = ppt._conectar()
            try:
                atual = {r["order_number"]: r["classificacao"] for r in conn.execute(
                    "SELECT order_number, classificacao FROM pedidos_parados_classificacao")}
            finally:
                conn.close()
        self.assertEqual(resumo["na_vuupt"], ["100", "300"])
        self.assertEqual(atual, {"100": "Na Vuupt", "200": "Cancelados", "300": "Na Vuupt"})
        self.assertEqual(len(resumo["divergentes"]), 1)
        self.assertTrue(resumo["divergentes"][0].startswith("200:"))


if __name__ == "__main__":
    unittest.main()
