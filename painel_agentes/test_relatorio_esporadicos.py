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


    def test_fonte_do_km_e_rotas_sem_gps(self):
        self.motorista(1, "Ana", "ESPORADICO")
        self.rota(1, "2026-11-10", combinado=100.0, motivo="a")                    # GPS_APP
        outra = self.rota(1, "2026-11-11", combinado=100.0, motivo="b")
        self.conn.execute("UPDATE nucleo_rotas SET km_fonte = 'INFORMADO' WHERE id = ?", (outra,))
        self.conn.commit()
        r = rel.gerar("2026-11-01", "2026-11-30", conn=self.conn)
        self.assertEqual(r["rotas_sem_gps"], 1)
        self.assertEqual(r["motoristas"][0]["rotas_sem_gps"], 1)
        detalhe = rel.csv_detalhe(r)
        self.assertIn("GPS do app", detalhe)
        self.assertIn("Informado pelo motorista", detalhe)

    def test_csv_neutraliza_formula_mas_preserva_pix_telefone(self):
        self.assertEqual(rel._txt("=HYPERLINK(\"http://x\")"), "'=HYPERLINK(\"http://x\")")
        self.assertEqual(rel._txt("@cmd"), "'@cmd")
        self.assertEqual(rel._txt("-1+1|cmd"), "'-1+1|cmd")
        self.assertEqual(rel._txt("+5511999998888"), "+5511999998888")
        self.assertEqual(rel._txt("ana@pix.com"), "ana@pix.com")
        self.motorista(1, "=cmd|calc", "ESPORADICO")
        self.rota(1, "2026-11-10", combinado=10.0, motivo="=1+1")
        r = rel.gerar("2026-11-01", "2026-11-30", conn=self.conn)
        for csv_ in (rel.csv_detalhe(r), rel.csv_resumo(r)):
            self.assertNotIn(";=cmd", csv_)
            self.assertTrue(csv_.splitlines()[1].startswith("'=cmd|calc;"))
        self.assertIn(";'=1+1;", rel.csv_detalhe(r))

    def _duas_rotas(self):
        self.motorista(1, "Ana", "ESPORADICO", cpf="123")
        self.rota(1, "2026-11-10", combinado=500.0, motivo="a")
        self.rota(1, "2026-11-11", combinado=300.0, motivo="b")

    def test_marcar_pago_tira_dos_pendentes_e_aparece_em_pagos(self):
        self._duas_rotas()
        res = rel.marcar_pago("123", "2026-11-01", "2026-11-30", "financeiro", "PIX 123", conn=self.conn)
        self.assertEqual((res["valor"], res["rotas"]), (800.0, 2))
        pend = rel.gerar("2026-11-01", "2026-11-30", "pendentes", conn=self.conn)
        self.assertEqual(pend["motoristas"], [])
        pagos = rel.gerar("2026-11-01", "2026-11-30", "pagos", conn=self.conn)
        m = pagos["motoristas"][0]
        self.assertEqual((m["total_pago"], m["total_pendente"], m["pode_pagar"]), (800.0, 0.0, False))
        self.assertEqual(m["pagamentos"][0]["pago_por"], "financeiro")
        self.assertEqual(rel.gerar("2026-11-01", "2026-11-30", "todos", conn=self.conn)["total_pago"], 800.0)
        self.assertIn("Pago em", rel.csv_detalhe(pagos))

    def test_nao_paga_duas_vezes_e_rota_nova_volta_pendente(self):
        self._duas_rotas()
        rel.marcar_pago("123", "2026-11-01", "2026-11-30", "f", conn=self.conn)
        with self.assertRaises(rel.ErroPagamento):
            rel.marcar_pago("123", "2026-11-01", "2026-11-30", "f", conn=self.conn)
        self.rota(1, "2026-11-12", combinado=100.0, motivo="c")
        pend = rel.gerar("2026-11-01", "2026-11-30", "pendentes", conn=self.conn)
        self.assertEqual(pend["motoristas"][0]["total_pendente"], 100.0)
        self.assertEqual(pend["motoristas"][0]["rotas"], 1)
        self.assertEqual(rel.marcar_pago("123", "2026-11-01", "2026-11-30", "f", conn=self.conn)["valor"], 100.0)

    def test_desfazer_devolve_as_rotas_aos_pendentes(self):
        self._duas_rotas()
        pid = rel.marcar_pago("123", "2026-11-01", "2026-11-30", "f", conn=self.conn)["id"]
        rel.desfazer_pago(pid, "f", conn=self.conn)
        pend = rel.gerar("2026-11-01", "2026-11-30", "pendentes", conn=self.conn)
        self.assertEqual(pend["motoristas"][0]["total_pendente"], 800.0)
        with self.assertRaises(rel.ErroPagamento):
            rel.desfazer_pago(pid, "f", conn=self.conn)

    def test_rota_sem_valor_nao_e_paga(self):
        self.motorista(1, "Ana", "ESPORADICO", cpf="123", tipo="TRUCK")
        self.rota(1, "2026-11-10")
        with self.assertRaises(rel.ErroPagamento):
            rel.marcar_pago("123", "2026-11-01", "2026-11-30", "f", conn=self.conn)
        m = rel.gerar("2026-11-01", "2026-11-30", conn=self.conn)["motoristas"][0]
        self.assertFalse(m["pode_pagar"])

    def test_tela_renderiza_com_dados(self):
        import painel_agentes
        self.motorista(1, "Ana", "ESPORADICO")
        self.rota(1, "2026-11-10", combinado=500.0, motivo="Diaria acordada")
        r = rel.gerar("2026-11-01", "2026-11-30", conn=self.conn)
        with painel_agentes.app.test_request_context("/financeiro/esporadicos"):
            html = painel_agentes.render_template("relatorio_esporadicos.html", rel=r, fonte_km=rel.fonte_km)
        self.assertIn("Ana", html)
        self.assertIn("Combinado: Diaria acordada", html)
        self.assertIn("500,00", html)
        self.assertIn('data-nome="Ana"', html)
        rel.marcar_pago("111.222.333-44".replace(".", "").replace("-", ""), "2026-11-01", "2026-11-30", "f", conn=self.conn)
        r2 = rel.gerar("2026-11-01", "2026-11-30", "todos", conn=self.conn)
        with painel_agentes.app.test_request_context("/financeiro/esporadicos"):
            html2 = painel_agentes.render_template("relatorio_esporadicos.html", rel=r2, fonte_km=rel.fonte_km)
        self.assertIn("Desfazer", html2)
        self.assertNotIn('data-nome="Ana"', html2)


class TestRotasEsporadicos(unittest.TestCase):
    """Rotas HTTP: login, nivel, CSRF e o fluxo pagar/desfazer de ponta a ponta."""

    def setUp(self):
        from pathlib import Path as _P
        from unittest import mock
        import acesso_telas
        import painel_agentes
        self._tmp = tempfile.TemporaryDirectory()
        base = _P(self._tmp.name)
        self.db = base / "dados.db"
        c = banco.conectar(self.db)
        c.execute("INSERT INTO bd_motoristas (NOME_MOTORISTA, AGENT_ID_VUUPT, ATIVO, CPF_MOTORISTA, TIPO_VEICULO, POSICAO, criado_em, atualizado_em) "
                  "VALUES ('Ana', 1, 'TRUE', '123', 'FIORINO', 'ESPORADICO', 'x', 'x')")
        c.execute("INSERT INTO nucleo_rotas (data_rota, nome, agent_id, km_real, km_fonte, valor_combinado, valor_combinado_motivo) "
                  "VALUES ('2026-11-10', 'R', 1, 10, 'GPS_APP', 500, 'a')")
        c.commit()
        c.close()
        original = banco.conectar
        self._ps = [
            mock.patch.object(acesso_telas, "DB_PATH", base / "acesso.db"),
            mock.patch.object(rel.banco, "conectar", lambda *a, **k: original(self.db)),
        ]
        for p in self._ps:
            p.start()
        conn = acesso_telas.conectar()
        try:
            acesso_telas.importar_do_config(conn, {"usuarios_extras": [
                {"usuario": "fin", "senha": "s1", "nivel": "atendimento"},
                {"usuario": "log", "senha": "s2", "nivel": "logistica"}]})
        finally:
            conn.close()
        painel_agentes.app.config["TESTING"] = True
        self.app = painel_agentes.app

    def tearDown(self):
        for p in self._ps:
            p.stop()
        self._tmp.cleanup()

    def cliente(self, usuario, senha):
        c = self.app.test_client()
        self.assertEqual(c.post("/login", data={"usuario": usuario, "senha": senha}).status_code, 302)
        return c

    def test_tela_csv_e_nivel(self):
        fin = self.cliente("fin", "s1")
        q = "?de=2026-11-01&ate=2026-11-30"
        self.assertEqual(fin.get("/financeiro/esporadicos" + q).status_code, 200)
        r = fin.get("/financeiro/esporadicos.csv" + q + "&tipo=resumo")
        self.assertEqual(r.status_code, 200)
        self.assertIn("attachment", r.headers["Content-Disposition"])
        self.assertIn("Ana", r.get_data(as_text=True))
        log = self.cliente("log", "s2")
        self.assertEqual(log.get("/financeiro/esporadicos" + q).status_code, 403)
        self.assertEqual(log.post("/api/financeiro/esporadicos/pagar", json={}).status_code, 403)

    def test_pagar_e_desfazer_pela_api(self):
        fin = self.cliente("fin", "s1")
        corpo = {"chave": "123", "de": "2026-11-01", "ate": "2026-11-30", "observacao": "pix 1"}
        # sem Origin/Referer = bloqueio CSRF
        self.assertEqual(fin.post("/api/financeiro/esporadicos/pagar", json=corpo).status_code, 403)
        origem = {"Origin": "http://localhost"}
        r = fin.post("/api/financeiro/esporadicos/pagar", json=corpo, headers=origem)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["valor"], 500.0)
        r2 = fin.post("/api/financeiro/esporadicos/pagar", json=corpo, headers=origem)
        self.assertEqual(r2.status_code, 400)
        self.assertIn("erro", r2.get_json())
        self.assertEqual(fin.post("/api/financeiro/esporadicos/pagar", json={**corpo, "de": "x"}, headers=origem).status_code, 400)
        pago = fin.get("/financeiro/esporadicos?de=2026-11-01&ate=2026-11-30&filtro=pagos").get_data(as_text=True)
        self.assertIn("Desfazer", pago)
        d = fin.post(f"/api/financeiro/esporadicos/{r.get_json()['id']}/desfazer", headers=origem)
        self.assertEqual(d.status_code, 200)
        self.assertEqual(fin.post("/api/financeiro/esporadicos/999/desfazer", headers=origem).status_code, 400)


if __name__ == "__main__":
    unittest.main()
