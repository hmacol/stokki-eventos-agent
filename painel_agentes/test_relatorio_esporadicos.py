# -*- coding: utf-8 -*-
"""
Relatorio de pagamento dos esporadicos (/financeiro/esporadicos).

    py -3.11 -m unittest test_relatorio_esporadicos   (de dentro de painel_agentes/)
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
import relatorio_esporadicos as rel  # noqa: E402


class TestRelatorioEsporadicos(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = banco.conectar(Path(self._tmp.name) / "dados.db")

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def motorista(self, agent_id, nome, posicao, cpf="111.222.333-44", ativo="TRUE", tipo="FIORINO"):
        self.conn.execute(
            "INSERT INTO bd_motoristas (NOME_MOTORISTA, AGENT_ID_VUUPT, ATIVO, CPF_MOTORISTA, TIPO_VEICULO, POSICAO, "
            "criado_em, atualizado_em) VALUES (?, ?, ?, ?, ?, ?, 'x', 'x')",
            (nome, agent_id, ativo, cpf, tipo, posicao))

    def rota(self, agent_id, dia, km=50.0, combinado=None, motivo=None):
        cur = self.conn.execute(
            "INSERT INTO nucleo_rotas (data_rota, nome, agent_id, km_real, km_fonte, valor_combinado, "
            "valor_combinado_motivo) VALUES (?, 'Rota', ?, ?, 'GPS_APP', ?, ?)", (dia, agent_id, km, combinado, motivo))
        return cur.lastrowid

    def test_so_esporadico_ativo_com_rota_no_periodo(self):
        self.motorista(1, "Ana Esporadica", "ESPORADICO")
        self.motorista(2, "Bia Fixa", "FIXO", cpf="999")
        self.motorista(3, "Carla Inativa", "ESPORADICO", cpf="888", ativo="FALSE")
        self.motorista(4, "Dani Sem Rota", "ESPORADICO", cpf="777")
        for agente in (1, 2, 3):
            self.rota(agente, "2026-11-10")
        self.rota(1, "2026-12-30")      # fora do periodo
        self.conn.execute("UPDATE motoristas SET chave_pix = 'ana@pix' WHERE 0")
        self.conn.commit()
        r = rel.gerar("2026-11-01", "2026-11-30", conn=self.conn)
        self.assertEqual([m["nome"] for m in r["motoristas"]], ["Ana Esporadica"])
        self.assertEqual(r["motoristas"][0]["rotas"], 1)

    def test_valor_combinado_vence_e_entra_no_total_e_no_csv(self):
        self.motorista(1, "Ana", "ESPORADICO")
        self.rota(1, "2026-11-10", combinado=500.0, motivo="Diaria acordada")
        self.rota(1, "2026-11-11", combinado=300.0, motivo="Meio periodo")
        r = rel.gerar("2026-11-01", "2026-11-30", conn=self.conn)
        m = r["motoristas"][0]
        self.assertEqual(m["total_rotas"], 800.0)
        self.assertEqual(m["total"], 800.0)
        self.assertEqual(r["total"], 800.0)
        detalhe = rel.csv_detalhe(r).splitlines()
        self.assertEqual(len(detalhe), 3)                # cabecalho + 2 rotas
        self.assertIn("Combinado;Diaria acordada;500,00", detalhe[1])
        resumo = rel.csv_resumo(r).splitlines()
        self.assertTrue(resumo[1].startswith("Ana;11122233344;"))
        self.assertTrue(resumo[-1].startswith("TOTAL;"))
        self.assertIn("800,00", resumo[-1])

    def test_mesma_pessoa_em_duas_linhas_vira_um_motorista(self):
        self.motorista(1, "Ana", "ESPORADICO", cpf="123")
        self.motorista(2, "Ana", "ESPORADICO", cpf="123")
        self.rota(1, "2026-11-10", combinado=100.0, motivo="a")
        self.rota(2, "2026-11-11", combinado=50.0, motivo="b")
        r = rel.gerar("2026-11-01", "2026-11-30", conn=self.conn)
        self.assertEqual(len(r["motoristas"]), 1)
        self.assertEqual(r["motoristas"][0]["total"], 150.0)

    def test_veiculo_sem_tarifa_conta_como_rota_sem_valor(self):
        self.motorista(1, "Ana", "ESPORADICO", tipo="TRUCK")
        self.rota(1, "2026-11-10")
        r = rel.gerar("2026-11-01", "2026-11-30", conn=self.conn)
        self.assertEqual(r["rotas_sem_valor"], 1)
        self.assertEqual(r["total"], 0.0)
        self.assertIn("A definir", rel.csv_detalhe(r))


    def test_tela_renderiza_com_dados(self):
        import painel_agentes
        self.motorista(1, "Ana", "ESPORADICO")
        self.rota(1, "2026-11-10", combinado=500.0, motivo="Diaria acordada")
        r = rel.gerar("2026-11-01", "2026-11-30", conn=self.conn)
        with painel_agentes.app.test_request_context("/financeiro/esporadicos"):
            html = painel_agentes.render_template("relatorio_esporadicos.html", rel=r)
        self.assertIn("Ana", html)
        self.assertIn("Combinado: Diaria acordada", html)
        self.assertIn("500,00", html)


if __name__ == "__main__":
    unittest.main()
