# -*- coding: utf-8 -*-
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from galpao3d import banco, estado

AGORA = datetime(2026, 10, 22, 10, 0)  # quinta, dia util
CFG = {"galpao_3d": {"equipe": {"Ana": "recebimento_expedicao", "Beto": "conferencia"}}}


def _t(dt):
    return dt.strftime(banco.FMT)


class TestEstado(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = banco.conectar(Path(self._tmp.name) / "t.db")

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def _coleta(self, fonte, resultado, minutos_atras):
        self.conn.execute("INSERT INTO galpao_coletas (fonte, quando, resultado) VALUES (?,?,?)",
                          (fonte, _t(AGORA - timedelta(minutes=minutos_atras)), resultado))

    def test_idade_dos_dados(self):
        self._coleta("stokki", "ok", 7)
        self._coleta("stokki", "ocupada", 1)
        e = estado.montar_estado(self.conn, CFG, AGORA)
        self.assertEqual(e["idade"]["stokki_min"], 7)
        self.assertEqual(e["idade"]["stokki_nivel"], "amarelo")
        self.assertIsNone(e["idade"]["vuupt_min"])
        json.dumps(e)

    def test_operador_desconhecido_vira_extra(self):
        self.conn.execute("INSERT INTO galpao_pedidos (id_stokki, codigo, situacao, operador_tarefa)"
                          " VALUES (1,'PS-1','Em Conferência','Fulano Novo')")
        self.conn.execute("INSERT INTO galpao_eventos (id_stokki, situacao, usuario, quando)"
                          " VALUES (1,'Em Conferência','Fulano Novo',?)", (_t(AGORA - timedelta(minutes=5)),))
        e = estado.montar_estado(self.conn, CFG, AGORA)
        p = [x for x in e["pessoas"] if x["nome"] == "Fulano Novo"][0]
        self.assertTrue(p["extra"])
        self.assertEqual(p["area"], "separacao")
        self.assertIn("PS-1", p["tarefa"])

    def test_fila_traz_numero_real(self):
        for i in range(200):
            self.conn.execute("INSERT INTO galpao_pedidos (id_stokki, codigo, situacao)"
                              " VALUES (?,?, 'Em Conferência')", (i + 1, f"PS-{i}"))
        e = estado.montar_estado(self.conn, CFG, AGORA)
        self.assertEqual(e["filas"]["sep_conf"], 200)

    def test_fora_do_turno(self):
        e = estado.montar_estado(self.conn, CFG, AGORA.replace(hour=15))
        ana = [x for x in e["pessoas"] if x["nome"] == "Ana"][0]
        self.assertEqual(ana["status"], "fora")
        self.assertFalse(ana["extra"])

    def test_eventos_ordenados_e_limitados(self):
        base = AGORA - timedelta(hours=3)
        for i in range(80):
            self.conn.execute("INSERT INTO galpao_eventos (id_stokki, situacao, usuario, quando)"
                              " VALUES (?,?,?,?)", (i % 5 + 1, f"S{i}", "Beto", _t(base + timedelta(minutes=i))))
        e = estado.montar_estado(self.conn, CFG, AGORA)
        ids = [x["id"] for x in e["eventos"]]
        self.assertEqual(len(ids), 50)
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(ids[-1], 80)
        self.assertEqual(e["eventos"][-1]["de"], "S74")

    def test_placar_rua_e_parado(self):
        self.conn.execute("INSERT INTO galpao_contagens VALUES (?,?)", (_t(AGORA), json.dumps(
            {"count_separating": 4, "count_pack": 3, "count_waiting_carrier": 9, "count_open": 12})))
        self.conn.execute("INSERT INTO galpao_rua VALUES (?,?)", (_t(AGORA), json.dumps(
            {"rotas": [], "carros_na_rua": 5, "entregues": 20, "insucessos": 2})))
        for m in (120, 110, 100):  # media 10 min, ultimo ha 100 min
            self.conn.execute("INSERT INTO galpao_eventos (id_stokki, situacao, usuario, quando)"
                              " VALUES (?,?,?,?)", (m, "Aguardando Transportador", "Ana",
                                                    _t(AGORA - timedelta(minutes=m))))
        e = estado.montar_estado(self.conn, CFG, AGORA)
        self.assertEqual((e["placar"]["separacao"], e["placar"]["conferencia"], e["placar"]["expedicao"]), (4, 3, 9))
        self.assertEqual((e["placar"]["na_rua"], e["placar"]["entregues"], e["placar"]["voltando"]), (5, 20, 2))
        self.assertEqual(e["filas"]["conf_exp"], 9)
        ana = [x for x in e["pessoas"] if x["nome"] == "Ana"][0]
        self.assertEqual(ana["status"], "parado")
        json.dumps(e)


    def test_cliente_que_so_abre_pedido_nao_vira_boneco(self):
        from unittest import mock
        self.conn.execute("INSERT INTO galpao_pedidos (id_stokki, codigo, situacao) VALUES (1,'PS-1','Aberto')")
        self.conn.execute("INSERT INTO galpao_eventos (id_stokki, situacao, usuario, quando)"
                          " VALUES (1,'Aberto','FABENNE',?)", (_t(AGORA - timedelta(minutes=5)),))
        with mock.patch.object(estado.remanejamento, "sugerir", return_value=[]) as sug:
            e = estado.montar_estado(self.conn, CFG, AGORA)
        self.assertNotIn("FABENNE", [x["nome"] for x in e["pessoas"]])
        self.assertEqual(sug.call_args[0][2].get("separacao", 0), 0)

    def test_operador_diferente_do_usuario_do_evento(self):
        self.conn.execute("INSERT INTO galpao_pedidos (id_stokki, codigo, situacao, operador_tarefa)"
                          " VALUES (1,'PS-7','Em Conferência','Carla')")
        self.conn.execute("INSERT INTO galpao_eventos (id_stokki, situacao, usuario, quando)"
                          " VALUES (1,'Em Conferência','Sistema',?)", (_t(AGORA - timedelta(minutes=5)),))
        e = estado.montar_estado(self.conn, CFG, AGORA)
        self.assertNotIn("Sistema", [x["nome"] for x in e["pessoas"]])
        c = [x for x in e["pessoas"] if x["nome"] == "Carla"][0]
        self.assertTrue(c["extra"])
        self.assertIn("PS-7", c["tarefa"])
        self.assertEqual(c["status"], "ok")

    def test_operador_igual_ao_usuario_nao_conta_evento_duas_vezes(self):
        for i in range(6):  # um pedido a cada 10 min; o ultimo ha 12 min
            quando = AGORA - timedelta(minutes=12 + 10 * (5 - i))
            self.conn.execute("INSERT INTO galpao_pedidos (id_stokki, codigo, situacao, operador_tarefa)"
                              " VALUES (?,?,'Em Conferência','Dora')", (i + 1, f"PS-{i + 1}"))
            self.conn.execute("INSERT INTO galpao_eventos (id_stokki, situacao, usuario, quando)"
                              " VALUES (?,'Em Conferência','Dora',?)", (i + 1, _t(quando)))
        e = estado.montar_estado(self.conn, CFG, AGORA)
        d = [x for x in e["pessoas"] if x["nome"] == "Dora"][0]
        self.assertEqual(d["status"], "ok")

    def test_espera_da_noite_entra_na_mediana(self):
        from unittest import mock
        agora = datetime(2026, 10, 23, 8, 10)
        self.conn.execute("INSERT INTO galpao_pedidos (id_stokki, codigo, situacao) VALUES (42355,'PS-42355','Em Conferência')")
        self.conn.execute("INSERT INTO galpao_eventos (id_stokki, situacao, usuario, quando)"
                          " VALUES (42355,'Em Conferência','Werlly',?)", (_t(datetime(2026, 10, 22, 17, 50)),))
        with mock.patch.object(estado.remanejamento, "sugerir", return_value=[]) as sug:
            estado.montar_estado(self.conn, CFG, agora)
        esperas = sug.call_args[0][1]
        self.assertEqual([(e["id_stokki"], e["minutos"]) for e in esperas], [(42355, 20)])

    def test_verbo_separando(self):
        self.conn.execute("INSERT INTO galpao_pedidos (id_stokki, codigo, situacao) VALUES (1,'PS-1','Em separação')")
        self.conn.execute("INSERT INTO galpao_eventos (id_stokki, situacao, usuario, quando)"
                          " VALUES (1,'Em separação','Beto',?)", (_t(AGORA - timedelta(minutes=5)),))
        e = estado.montar_estado(self.conn, CFG, AGORA)
        beto = [x for x in e["pessoas"] if x["nome"] == "Beto"][0]
        self.assertEqual(beto["tarefa"], "PS-1 · separando")

    def test_conferencias_longas_no_estado(self):
        self.conn.execute("INSERT INTO galpao_pedidos (id_stokki, codigo, situacao, unidades)"
                          " VALUES (1,'PS-1','Aguardando Transportador',10)")
        self.conn.execute("INSERT INTO galpao_conferencias (id_stokki, estacao, usuario, inicio, fim, segundos, problema)"
                          " VALUES (1,'E1','Beto',?,?,8914,0)", (_t(AGORA - timedelta(hours=1)),) * 2)
        e = estado.montar_estado(self.conn, CFG, AGORA)
        longas = e["indicadores"]["conferencias_longas"]
        self.assertEqual([(c["codigo"], c["usuario"], c["segundos"]) for c in longas], [("PS-1", "Beto", 8914)])
        self.assertEqual(e["indicadores"]["conferencia_geral"]["pedidos"], 0)
        json.dumps(e)


if __name__ == "__main__":
    unittest.main()
