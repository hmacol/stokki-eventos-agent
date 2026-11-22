# -*- coding: utf-8 -*-
"""
Botao "Cancelar finalizacao" da tela /acompanhamento (22/11).

    cd painel_agentes && py -3.11 -m unittest test_cancelar_finalizacao
"""
import sys
import tempfile
import unittest
from pathlib import Path

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from nucleo import banco  # noqa: E402

import cancelar_finalizacao as cf  # noqa: E402


class VuuptFalsa:
    def __init__(self, servico=None):
        self.servico = servico or {"id": 900, "code": "#PS-10", "route_id": 5000, "status": "done"}

    def buscar_servico_por_id(self, service_id):
        return {"service": self.servico} if self.servico else None


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.conn = banco.conectar(Path(self._tmp.name) / "dados.db")
        self.addCleanup(self.conn.close)
        self.rota = self.conn.execute(
            "INSERT INTO nucleo_rotas (data_rota, nome, provedor, vuupt_route_id, agent_id, motorista_nome, status,"
            " iniciada_em, total_paradas, entregues) VALUES ('2026-11-22', 'Rota 1', 'VUUPT', 5000, 7, 'Joao', 'EM_ROTA',"
            " '2026-11-22 08:00:00', 2, 1)").lastrowid
        self.parada = self.conn.execute(
            "INSERT INTO nucleo_paradas (rota_id, ordem, service_id, codigo, titulo, destinatario_nome, endereco, latitude,"
            " longitude, situacao, completed_at, started_at, arrived_at, tempo_deslocamento_s, tempo_no_local_s,"
            " motivo_texto, tentativas, dados_json) VALUES (?, 1, 900, 'PS-10', 'Mercado', 'Mercado Bom', 'Rua A, 1',"
            " -23.5, -46.6, 'ENTREGUE', '2026-11-22 09:00:00', '2026-11-22 08:10:00', '2026-11-22 08:50:00', 2400, 600,"
            " NULL, 0, '{\"x\": 1}')", (self.rota,)).lastrowid
        self.conn.execute("INSERT INTO nucleo_paradas (rota_id, ordem, service_id, codigo, situacao) VALUES (?, 2, 901, 'PS-11', 'PENDENTE')",
                          (self.rota,))
        self.conn.commit()
        self.chamadas = []
        self.duplicados = {}
        self.vuupt = VuuptFalsa()
        self.novo = {"id": 950, "code": "#PS-10-R1"}
        self.inclusao = {"ok": True, "falharam": []}
        self.operavel = True

    def executar(self, parada_id=None, motivo="Motorista baixou o pedido errado", usuario="ana"):
        def duplicar(vuupt, original):
            self.chamadas.append(("duplicar", original["id"]))
            return self.novo

        def adicionar(route_id, service_ids):
            self.chamadas.append(("adicionar", route_id, tuple(service_ids)))
            return self.inclusao

        return cf.cancelar_finalizacao(
            self.conn, parada_id or self.parada, usuario, motivo, vuupt=self.vuupt, duplicar=duplicar,
            adicionar=adicionar, rota_operavel=lambda rota: self.operavel,
            ja_duplicado=lambda sid: self.duplicados.get(sid),
            marcar_duplicado=lambda sid, code: self.duplicados.__setitem__(sid, code))

    def paradas(self):
        return [dict(r) for r in self.conn.execute("SELECT * FROM nucleo_paradas WHERE rota_id = ? ORDER BY id", (self.rota,))]


class TestCancelar(Base):
    def test_cria_parada_nova_e_guarda_a_antiga(self):
        r = self.executar()
        self.assertEqual(r["novo_codigo"], "PS-10-R1")
        self.assertEqual(self.chamadas, [("duplicar", 900), ("adicionar", 5000, (950,))])
        antiga, _, nova = self.paradas()
        # a baixa anterior fica como estava: e o historico da tentativa
        self.assertEqual((antiga["situacao"], antiga["completed_at"], antiga["service_id"]), ("ENTREGUE", "2026-11-22 09:00:00", 900))
        # a nova nasce limpa, pro motorista refazer do inicio
        self.assertEqual((nova["service_id"], nova["codigo"], nova["situacao"]), (950, "PS-10-R1", "PENDENTE"))
        for campo in ("completed_at", "started_at", "arrived_at", "tempo_deslocamento_s", "tempo_no_local_s", "motivo_texto"):
            self.assertIsNone(nova[campo], campo)
        self.assertEqual((nova["destinatario_nome"], nova["endereco"], nova["latitude"]), ("Mercado Bom", "Rua A, 1", -23.5))
        self.assertEqual(nova["ordem"], 3)
        self.assertEqual(self.duplicados, {900: "#PS-10-R1"})

    def test_registra_quem_e_por_que(self):
        self.executar()
        ev = self.conn.execute("SELECT * FROM nucleo_eventos WHERE tipo = 'FINALIZACAO_CANCELADA'").fetchone()
        self.assertEqual((ev["rota_id"], ev["parada_id"], ev["origem"]), (self.rota, self.parada, "PAINEL"))
        for trecho in ("ana", "Motorista baixou o pedido errado", "PS-10-R1", "ENTREGUE"):
            self.assertIn(trecho, ev["dados_json"])

    def test_contadores_da_rota_contam_a_parada_nova(self):
        self.executar()
        r = self.conn.execute("SELECT total_paradas, entregues FROM nucleo_rotas WHERE id = ?", (self.rota,)).fetchone()
        self.assertEqual((r["total_paradas"], r["entregues"]), (3, 1))

    def test_id_do_servico_novo_aninhado(self):
        self.novo = {"service": {"id": 951}, "code": "#PS-10-R1"}
        self.executar()
        self.assertEqual(self.paradas()[-1]["service_id"], 951)

    def _recusa(self, trecho, **kw):
        with self.assertRaises(cf.Recusado) as c:
            self.executar(**kw)
        self.assertIn(trecho, str(c.exception))
        self.assertEqual(self.chamadas, [])
        self.assertEqual(len(self.paradas()), 2)

    def test_exige_motivo(self):
        self._recusa("motivo", motivo="  ok ")

    def test_parada_sem_resultado(self):
        pendente = self.conn.execute("SELECT id FROM nucleo_paradas WHERE codigo = 'PS-11'").fetchone()["id"]
        self._recusa("ainda não foi finalizada", parada_id=pendente)

    def test_parada_inexistente(self):
        self._recusa("não encontrada", parada_id=9999)

    def test_so_com_a_rota_em_andamento(self):
        for status in ("CONCLUIDA", "PLANEJADA", "ACEITA", "CANCELADA"):
            self.conn.execute("UPDATE nucleo_rotas SET status = ? WHERE id = ?", (status, self.rota))
            self.conn.commit()
            self._recusa("em andamento")

    def test_so_rota_feita_no_nosso_app(self):
        self.operavel = False
        self._recusa("app da Vuupt")

    def test_nao_cancela_duas_vezes(self):
        self.duplicados[900] = "#PS-10-R1"
        self._recusa("PS-10-R1")

    def test_falha_ao_duplicar_nao_grava_nada(self):
        self.novo = None
        with self.assertRaises(cf.Recusado):
            self.executar()
        self.assertEqual(len(self.paradas()), 2)
        self.assertEqual(self.duplicados, {})
        self.assertIsNone(self.conn.execute("SELECT 1 FROM nucleo_eventos WHERE tipo = 'FINALIZACAO_CANCELADA'").fetchone())

    def test_servico_que_saiu_da_rota_na_vuupt(self):
        self.vuupt.servico["route_id"] = 7777
        self._recusa("outra rota")

    def test_vuupt_recusa_incluir_na_rota_avisa_do_servico_solto(self):
        self.inclusao = {"ok": True, "falharam": [950]}
        with self.assertRaises(cf.Recusado) as c:
            self.executar()
        self.assertIn("PS-10-R1", str(c.exception))
        self.assertIn("fora da rota", str(c.exception))
        self.assertEqual(len(self.paradas()), 2)
        # o servico novo existe na Vuupt: nao deixa duplicar de novo por cima
        self.assertEqual(self.duplicados, {900: "#PS-10-R1"})
        ev = self.conn.execute("SELECT dados_json FROM nucleo_eventos WHERE tipo = 'FINALIZACAO_CANCELADA'").fetchone()
        self.assertIn("fora_da_rota", ev["dados_json"])


if __name__ == "__main__":
    unittest.main()
