# -*- coding: utf-8 -*-
"""
test_relatorios_financeiro.py

Trava as regras de formatação da CÓPIA FIEL dos relatórios do financeiro.
Cada uma saiu de conferir célula a célula a exportação real da Vuupt de
01-16/09 (175 rotas, 1.211 serviços) -- se alguém "arrumar" uma delas sem
querer, o relatório deixa de bater e o financeiro sente.

    python -m unittest nucleo.test_relatorios_financeiro -v
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_RAIZ))

from nucleo import banco, relatorios_financeiro as rel


class TestFormatos(unittest.TestCase):
    def test_numero_com_virgula_e_milhar(self):
        self.assertEqual(rel._num(25, 2), "25,00")
        self.assertEqual(rel._num(1500, 3), "1.500,000")       # a Vuupt escreve 1.500,000
        self.assertEqual(rel._num(None), "")

    def test_duracao_passa_de_24h(self):
        self.assertEqual(rel._dur(3661), "01:01:01")
        self.assertEqual(rel._dur(96459), "26:47:39")          # rota que virou o dia
        self.assertEqual(rel._dur(None), "")
        self.assertEqual(rel._dur0(None), "00:00:00")          # a Vuupt sempre imprime a duração

    def test_custo_sem_valor_sai_zero_e_dimensao_sai_vazia(self):
        self.assertEqual(rel._num0(None), "0,00")
        self.assertEqual(rel._dim(0), "")                      # ocupação zerada sai em branco
        self.assertEqual(rel._dim(42), "42,000")

    def test_percentual_zero_sai_vazio(self):
        self.assertEqual(rel._pct(100), "100%")
        self.assertEqual(rel._pct(0), "")                      # rota cancelada: estatísticas em branco
        self.assertEqual(rel._pct(None), "")

    def test_km_vem_de_metros(self):
        self.assertEqual(rel._km(36042), "36,0")
        self.assertEqual(rel._km(None), "")

    def test_data_da_vuupt_vira_hora_local(self):
        self.assertEqual(rel._dt("2026-09-16 13:00:00"), "16/09/2026 10:00")
        self.assertEqual(rel._dt("2026-09-16 13:00:00", com_segundos=True), "16/09/2026 10:00:00")
        self.assertEqual(rel._dt(None), "")

    def test_mapa_do_fora_do_raio(self):
        """Conferido contra a exportação: 2 é ALTA precisão, 1 é baixa."""
        self.assertEqual(rel.FORA_DO_RAIO[0], "Não")
        self.assertEqual(rel.FORA_DO_RAIO[1], "Sim (baixa precisão)")
        self.assertEqual(rel.FORA_DO_RAIO[2], "Sim (alta precisão)")
        self.assertEqual(rel.FORA_DO_RAIO[3], "dados insuficientes")


class TestColunas(unittest.TestCase):
    def test_ordem_e_quantidade_iguais_as_da_vuupt(self):
        self.assertEqual(len(rel.COLUNAS_ROTAS), 77)
        self.assertEqual(len(rel.COLUNAS_SERVICOS), 84)
        self.assertEqual(rel.COLUNAS_ROTAS[0], "#")
        self.assertEqual(rel.COLUNAS_ROTAS[-1], "Estatísticas - Link para mapa")
        self.assertEqual(rel.COLUNAS_SERVICOS[0], "Serviço #")
        self.assertEqual(rel.COLUNAS_SERVICOS[-1], "Zona - Nome")
        self.assertEqual(len(set(rel.COLUNAS_ROTAS)), 77)      # sem nome repetido
        self.assertEqual(len(set(rel.COLUNAS_SERVICOS)), 84)


class TestGeracao(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._patch = mock.patch.object(banco, "DB_PATH", Path(self._tmp.name) / "t.db")
        self._patch.start()
        self.conn = banco.conectar()

    def tearDown(self):
        self.conn.close()
        self._patch.stop()
        self._tmp.cleanup()

    def _rota(self):
        self.conn.execute("""INSERT INTO nucleo_rotas (id, data_rota, nome, provedor, vuupt_route_id, agent_id,
                                                       vehicle_id, motorista_nome, status, status_provedor,
                                                       start_at, dados_json)
                             VALUES (1, '2026-09-16', 'Planejamento - 16/09/2026 - #1', 'VUUPT', 5222980, 47084,
                                     103182, 'Vinícius - CARRO 3', 'CONCLUIDA', 'finished', '2026-09-16 06:00:00',
                                     '{"source": "manual", "created_at": "2026-09-16 01:36:34",
                                       "prevision_initial_trip_distance": 95374, "prevision_initial_number_services": 8,
                                       "done_trip_distance": 36042, "done_cost_per_agent_per_route": 25,
                                       "stat_adherence": 100, "start_dimension_2": 0}')""")
        self.conn.commit()

    def test_linha_de_rota_sai_no_formato_da_vuupt(self):
        self._rota()
        linhas = rel.gerar_rotas(self.conn, "2026-09-16", "2026-09-16", rel.Cadastros(self.conn), por="rota")
        self.assertEqual(len(linhas), 1)
        l = linhas[0]
        self.assertEqual(l["#"], "5222980")
        self.assertEqual(l["Situação"], "Finalizada")
        self.assertEqual(l["Origem"], "Manual")
        self.assertEqual(l["Local de início"], "Freshlog")
        self.assertEqual(l["Indicadores gerais (previsão) - Distância (km)"], "95,4")
        self.assertEqual(l["Indicadores gerais (realizado) - Distância (km)"], "36,0")
        self.assertEqual(l["Custos (previsão) - Por agente - Por rota"], "0,00")      # sem valor = 0,00
        self.assertEqual(l["Custos (realizado) - Por agente - Por rota"], "25,00")
        self.assertEqual(l["Ocupação (inicial) - Peso (kg)"], "")                     # zero = vazio
        self.assertEqual(l["Estatísticas - Aderência"], "100%")
        self.assertEqual(set(l) , set(rel.COLUNAS_ROTAS))

    def test_rota_de_teste_do_piloto_fica_de_fora(self):
        self.conn.execute("""INSERT INTO nucleo_rotas (id, data_rota, nome, provedor, agent_id, status, dados_json)
                             VALUES (9, '2026-09-16', '[TESTE] cópia', 'APP', 999001, 'PLANEJADA',
                                     '{"replica_de": {"rota_id": 1}}')""")
        self.conn.commit()
        self.assertEqual(rel.gerar_rotas(self.conn, "2026-09-16", "2026-09-16", rel.Cadastros(self.conn),
                                         por="rota"), [])

    def test_filtro_por_criacao_pega_rota_montada_na_vespera(self):
        self._rota()      # rota do dia 16, criada em 16/09 01:36 (hora local: 15/09 22:36)
        cad = rel.Cadastros(self.conn)
        self.assertEqual(len(rel.gerar_rotas(self.conn, "2026-09-15", "2026-09-15", cad, por="criacao")), 1)
        self.assertEqual(len(rel.gerar_rotas(self.conn, "2026-09-16", "2026-09-16", cad, por="criacao")), 0)
        self.assertEqual(len(rel.gerar_rotas(self.conn, "2026-09-16", "2026-09-16", cad, por="rota")), 1)


if __name__ == "__main__":
    unittest.main()
