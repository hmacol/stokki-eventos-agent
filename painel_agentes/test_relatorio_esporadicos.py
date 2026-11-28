# -*- coding: utf-8 -*-
"""
Relatorio de pagamento dos esporadicos (/financeiro/esporadicos).

    py -3.11 -m unittest test_relatorio_esporadicos   (de dentro de painel_agentes/)
"""
import io
import sys
import tempfile
import unittest
from datetime import date
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
        self.assertEqual([m["nome"] for m in r["motoristas"]], ["Ana Esporadica", "Bia Fixa"])
        self.assertEqual(r["motoristas"][0]["rotas"], 1)
        self.assertEqual([m["posicao"] for m in r["motoristas"]], ["ESPORADICO", "FIXO"])
        esp = rel.gerar("2026-11-01", "2026-11-30", quem="esporadicos", conn=self.conn)
        self.assertEqual([m["nome"] for m in esp["motoristas"]], ["Ana Esporadica"])
        fixos = rel.gerar("2026-11-01", "2026-11-30", quem="fixos", conn=self.conn)
        self.assertEqual([m["nome"] for m in fixos["motoristas"]], ["Bia Fixa"])
        self.assertIn(";Fixo;", rel.csv_detalhe(r))
        self.assertIn(";Esporadico;", rel.csv_detalhe(r))

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

    def test_ciclos_fixos_10_a_24_e_25_a_09(self):
        from datetime import date
        iso = lambda t: (t[0].isoformat(), t[1].isoformat())
        self.assertEqual(iso(rel.ciclo_atual(date(2026, 11, 10))), ("2026-11-10", "2026-11-24"))
        self.assertEqual(iso(rel.ciclo_atual(date(2026, 11, 24))), ("2026-11-10", "2026-11-24"))
        self.assertEqual(iso(rel.ciclo_atual(date(2026, 11, 25))), ("2026-11-25", "2026-12-09"))
        self.assertEqual(iso(rel.ciclo_atual(date(2026, 12, 9))), ("2026-11-25", "2026-12-09"))
        self.assertEqual(iso(rel.ciclo_atual(date(2027, 1, 3))), ("2026-12-25", "2027-01-09"))
        lista = rel.ciclos_recentes(date(2026, 11, 27), 4)
        self.assertEqual([(c["de"], c["ate"]) for c in lista], [
            ("2026-11-25", "2026-12-09"), ("2026-11-10", "2026-11-24"),
            ("2026-10-25", "2026-11-09"), ("2026-10-10", "2026-10-24")])

    def test_tela_renderiza_com_dados(self):
        import painel_agentes
        self.motorista(1, "Ana", "ESPORADICO")
        self.rota(1, "2026-11-10", combinado=500.0, motivo="Diaria acordada")
        r = rel.gerar("2026-11-01", "2026-11-30", conn=self.conn)
        with painel_agentes.app.test_request_context("/financeiro/esporadicos"):
            html = painel_agentes.render_template("relatorio_esporadicos.html", rel=r, fonte_km=rel.fonte_km,
                                                  ciclos=rel.ciclos_recentes(date(2026, 11, 27)))
        self.assertIn("Ana", html)
        self.assertIn("25/11 a 09/12/2026", html)
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


class TestRotasRemessa(unittest.TestCase):
    """Remessa Itau pela web: so nivel total, so com itau_remessa.ativo, CSRF, tela e fluxo completo."""

    ITAU = {"itau_remessa": {"ativo": True, "cnpj": "12345678000195", "agencia": "1234", "conta": "123456",
                             "dac": "7", "nome_empresa": "FRESH LOG TRANSPORTES LTDA", "tipo_pagamento": "20"}}

    def setUp(self):
        from pathlib import Path as _P
        from unittest import mock
        import acesso_telas
        import painel_agentes
        self.pa = painel_agentes
        self._tmp = tempfile.TemporaryDirectory()
        base = _P(self._tmp.name)
        self.db = base / "dados.db"
        c = banco.conectar(self.db)
        for agente, nome, cpf, pos, pix in ((1, "Ana Silva", "52998224725", "ESPORADICO", "ana@pix.com"),
                                            (2, "Bruno Lima", "12345678909", "FIXO", None)):
            c.execute("INSERT INTO bd_motoristas (NOME_MOTORISTA, AGENT_ID_VUUPT, ATIVO, CPF_MOTORISTA, TIPO_VEICULO, POSICAO, "
                      "criado_em, atualizado_em) VALUES (?, ?, 'TRUE', ?, 'FIORINO', ?, 'x', 'x')", (nome, agente, cpf, pos))
            if pix:
                c.execute("INSERT INTO motoristas (cpf, nome, pin_hash, pin_salt, chave_pix) VALUES (?, ?, 'x', 'x', ?)", (cpf, nome, pix))
            c.execute("INSERT INTO nucleo_rotas (data_rota, nome, agent_id, km_real, km_fonte, valor_combinado, valor_combinado_motivo) "
                      "VALUES ('2026-11-10', 'R', ?, 10, 'GPS_APP', ?, 'a')", (agente, 500.0 if agente == 1 else 300.0))
        c.commit()
        c.close()
        self.config = dict(self.ITAU)
        original = banco.conectar
        config_real = painel_agentes._carregar_config
        self._ps = [
            mock.patch.object(acesso_telas, "DB_PATH", base / "acesso.db"),
            mock.patch.object(rel.banco, "conectar", lambda *a, **k: original(self.db)),
            mock.patch.object(painel_agentes, "_carregar_config", lambda: {**config_real(), **self.config}),
            mock.patch.object(painel_agentes, "PASTA_REMESSAS_ITAU", base / "rem"),
        ]
        for p in self._ps:
            p.start()
        conn = acesso_telas.conectar()
        try:
            acesso_telas.importar_do_config(conn, {"usuarios_extras": [
                {"usuario": "tot", "senha": "s0", "nivel": "total"},
                {"usuario": "fin", "senha": "s1", "nivel": "atendimento"}]})
            acesso_telas.importar_do_config(conn, {"usuarios_extras": [
                {"usuario": "cust", "senha": "s2", "nivel": "atendimento"}]})
            acesso_telas.salvar(conn, "cust", ["relatorio_esporadicos"], "teste")
        finally:
            conn.close()
        painel_agentes.app.config["TESTING"] = True
        self.app = painel_agentes.app
        self.origem = {"Origin": "http://localhost"}
        self.q = "?de=2026-11-01&ate=2026-11-27"

    def tearDown(self):
        for p in self._ps:
            p.stop()
        self._tmp.cleanup()

    def consulta(self, sql):
        c = banco.conectar(self.db)
        try:
            return c.execute(sql).fetchone()[0]
        finally:
            c.close()

    def cliente(self, usuario, senha):
        c = self.app.test_client()
        self.assertEqual(c.post("/login", data={"usuario": usuario, "senha": senha}).status_code, 302)
        return c

    def gerar_remessa(self, c, chaves=("52998224725",)):
        from datetime import date as _d, timedelta
        corpo = {"chaves": list(chaves), "de": "2026-11-01", "ate": "2026-11-27",
                 "data_pagamento": (_d.today() + timedelta(days=1)).isoformat()}
        return c.post("/api/financeiro/esporadicos/remessa", json=corpo, headers=self.origem)

    def test_desligado_a_tela_nao_mostra_remessa_e_a_api_da_404(self):
        self.config = {"itau_remessa": {}}
        tot = self.cliente("tot", "s0")
        html = tot.get("/financeiro/esporadicos" + self.q).get_data(as_text=True)
        self.assertNotIn("Gerar remessa", html)
        self.assertEqual(self.gerar_remessa(tot).status_code, 404)
        self.assertEqual(tot.get("/financeiro/esporadicos/remessa/1/arquivo").status_code, 404)

    def test_so_nivel_total_atendimento_nao_ve_nem_gera(self):
        fin = self.cliente("fin", "s1")
        html = fin.get("/financeiro/esporadicos" + self.q).get_data(as_text=True)
        self.assertNotIn("Gerar remessa", html)
        self.assertEqual(self.gerar_remessa(fin).status_code, 403)
        self.assertEqual(fin.get("/financeiro/esporadicos/remessa/1/arquivo").status_code, 403)
        for caminho in ("remessa/1/cancelar", "remessa/1/retorno", "1/confirmar-pago", "1/rejeitar"):
            self.assertEqual(fin.post("/api/financeiro/esporadicos/" + caminho, headers=self.origem).status_code, 403, caminho)

    def test_post_sem_origin_e_barrado(self):
        tot = self.cliente("tot", "s0")
        r = tot.post("/api/financeiro/esporadicos/remessa", json={"chaves": ["52998224725"]})
        self.assertEqual(r.status_code, 403)

    def test_tela_lista_elegiveis_inelegiveis_com_motivo_e_data_padrao(self):
        from datetime import date as _d
        from regras import feriados
        tot = self.cliente("tot", "s0")
        html = tot.get("/financeiro/esporadicos" + self.q).get_data(as_text=True)
        self.assertIn("Gerar remessa", html)
        self.assertEqual(html.count('class="js-sel-remessa"'), 1)              # so a Ana tem caixa de marcacao
        self.assertIn('class="js-sel-remessa" data-chave="52998224725"', html)
        self.assertIn("sem chave Pix cadastrada", html)                        # Bruno: motivo na tela
        self.assertIn(feriados.proximo_dia_util(_d.today()).isoformat(), html)
        self.assertIn("tarifa por km", html)                                   # aviso dos fixos

    def test_config_incompleta_avisa_e_nao_gera(self):
        self.config = {"itau_remessa": {"ativo": True, "cnpj": "12345678000195"}}
        tot = self.cliente("tot", "s0")
        r = self.gerar_remessa(tot)
        self.assertEqual(r.status_code, 400)
        self.assertIn("incompleta", r.get_json()["erro"])

    def test_fluxo_completo_gerar_baixar_retorno_pago(self):
        from test_remessa_itau_db import retorno_de
        tot = self.cliente("tot", "s0")
        r = self.gerar_remessa(tot)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        dados = r.get_json()
        self.assertEqual((dados["qtd"], dados["valor_total"]), (1, 500.0))
        arq = tot.get(f"/financeiro/esporadicos/remessa/{dados['remessa_id']}/arquivo")
        self.assertEqual(arq.status_code, 200)
        self.assertIn("attachment", arq.headers["Content-Disposition"])
        texto = arq.get_data().decode("ascii")
        self.assertEqual(len(texto.split(chr(13) + chr(10))) - 1, 6)         # 0,1,A,B,5,9
        # depois de baixar nao cancela
        c = tot.post(f"/api/financeiro/esporadicos/remessa/{dados['remessa_id']}/cancelar", headers=self.origem)
        self.assertEqual(c.status_code, 400)
        self.assertIn("baixad", c.get_json()["erro"])
        # retorno do banco (sintetico): 00 -> pago
        pid = self.consulta("SELECT id FROM pagamentos_esporadicos")
        retorno = retorno_de(texto, {pid: ("00", "30112026", 50000)})
        up = tot.post(f"/api/financeiro/esporadicos/remessa/{dados['remessa_id']}/retorno", headers=self.origem,
                      data={"arquivo": (io.BytesIO(retorno.encode("ascii")), "retorno.ret")}, content_type="multipart/form-data")
        self.assertEqual(up.status_code, 200, up.get_data(as_text=True))
        self.assertEqual(up.get_json()["pagos"], 1)
        html = tot.get("/financeiro/esporadicos" + self.q + "&filtro=pagos").get_data(as_text=True)
        self.assertIn("Ana Silva", html)

    def test_cancelar_antes_de_baixar_e_confirmar_rejeitar_manuais(self):
        tot = self.cliente("tot", "s0")
        rid = self.gerar_remessa(tot).get_json()["remessa_id"]
        self.assertEqual(tot.post(f"/api/financeiro/esporadicos/remessa/{rid}/cancelar", headers=self.origem).status_code, 200)
        rid2 = self.gerar_remessa(tot).get_json()["remessa_id"]
        pid = self.consulta("SELECT id FROM pagamentos_esporadicos WHERE situacao = 'ENVIADO'")
        sem_motivo = tot.post(f"/api/financeiro/esporadicos/{pid}/rejeitar", json={"motivo": " "}, headers=self.origem)
        self.assertEqual(sem_motivo.status_code, 400)
        ok = tot.post(f"/api/financeiro/esporadicos/{pid}/confirmar-pago", headers=self.origem)
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(tot.post(f"/api/financeiro/esporadicos/{pid}/confirmar-pago", headers=self.origem).status_code, 400)
        self.assertGreater(rid2, rid)

    def test_conta_com_telas_personalizadas_nao_e_total_de_verdade(self):
        cust = self.cliente("cust", "s2")
        self.assertEqual(cust.get("/financeiro/esporadicos" + self.q).status_code, 200)   # a tela do relatorio ela tem
        html = cust.get("/financeiro/esporadicos" + self.q).get_data(as_text=True)
        self.assertNotIn("Gerar remessa", html)
        self.assertEqual(self.gerar_remessa(cust).status_code, 403)
        self.assertEqual(cust.get("/financeiro/esporadicos/remessa/1/arquivo").status_code, 403)
        for caminho in ("remessa/1/cancelar", "remessa/1/retorno", "1/confirmar-pago", "1/rejeitar"):
            self.assertEqual(cust.post("/api/financeiro/esporadicos/" + caminho, headers=self.origem).status_code, 403, caminho)

    def test_yaml_sem_aspas_e_recusado_com_mensagem_clara(self):
        for campo, valor in (("agencia", 156), ("conta", 1234), ("dac", 0), ("cnpj", 12345678000195)):
            with self.subTest(campo):
                self.config = {"itau_remessa": {**self.ITAU["itau_remessa"], campo: valor}}
                r = self.gerar_remessa(self.cliente("tot", "s0"))
                self.assertEqual(r.status_code, 400)
                self.assertIn("aspas", r.get_json()["erro"])
                self.assertIn(campo, r.get_json()["erro"])

    def test_valores_que_mudaram_desde_a_tela_recusam_a_remessa(self):
        from datetime import date as _d, timedelta
        tot = self.cliente("tot", "s0")
        corpo = {"chaves": ["52998224725"], "de": "2026-11-01", "ate": "2026-11-27",
                 "data_pagamento": (_d.today() + timedelta(days=1)).isoformat(), "esperados": {"52998224725": 12345}}
        r = tot.post("/api/financeiro/esporadicos/remessa", json=corpo, headers=self.origem)
        self.assertEqual(r.status_code, 400)
        self.assertIn("valores mudaram", r.get_json()["erro"])

    def test_gerar_nao_baixa_sozinho_e_a_tela_oferece_cancelar_e_avisos(self):
        tot = self.cliente("tot", "s0")
        self.assertEqual(self.gerar_remessa(tot).status_code, 200)
        html = tot.get("/financeiro/esporadicos" + self.q + "&filtro=todos").get_data(as_text=True)
        self.assertIn("js-cancelar-remessa", html)                      # ainda nao baixada: da pra cancelar
        self.assertNotIn("window.location.href = j.url", html)          # o JS nao navega pro download sozinho
        self.assertIn("NÃO está agendado", html)                        # confirmacao de rejeitar manual

    def test_remessa_cancelada_some_o_baixar_e_mostra_cancelada(self):
        tot = self.cliente("tot", "s0")
        rid = self.gerar_remessa(tot).get_json()["remessa_id"]
        self.assertEqual(tot.post(f"/api/financeiro/esporadicos/remessa/{rid}/cancelar", headers=self.origem).status_code, 200)
        html = tot.get("/financeiro/esporadicos" + self.q).get_data(as_text=True)
        self.assertNotIn(f"remessa/{rid}/arquivo", html)
        self.assertIn("cancelada", html)
        self.assertEqual(tot.get(f"/financeiro/esporadicos/remessa/{rid}/arquivo").status_code, 404)

    def test_download_nao_fica_em_cache(self):
        tot = self.cliente("tot", "s0")
        rid = self.gerar_remessa(tot).get_json()["remessa_id"]
        arq = tot.get(f"/financeiro/esporadicos/remessa/{rid}/arquivo")
        self.assertIn("no-store", arq.headers.get("Cache-Control", ""))

    def test_opcoes_enviados_e_rejeitados_so_para_quem_ve_a_remessa(self):
        html_fin = self.cliente("fin", "s1").get("/financeiro/esporadicos" + self.q).get_data(as_text=True)
        self.assertNotIn('value="enviados"', html_fin)
        html_tot = self.cliente("tot", "s0").get("/financeiro/esporadicos" + self.q).get_data(as_text=True)
        self.assertIn('value="enviados"', html_tot)
        self.config = {"itau_remessa": {}}
        html_off = self.cliente("tot", "s0").get("/financeiro/esporadicos" + self.q).get_data(as_text=True)
        self.assertNotIn('value="rejeitados"', html_off)

    def test_retorno_sem_arquivo_ou_ilegivel_da_400(self):
        tot = self.cliente("tot", "s0")
        rid = self.gerar_remessa(tot).get_json()["remessa_id"]
        sem = tot.post(f"/api/financeiro/esporadicos/remessa/{rid}/retorno", headers=self.origem)
        self.assertEqual(sem.status_code, 400)
        ruim = tot.post(f"/api/financeiro/esporadicos/remessa/{rid}/retorno", headers=self.origem,
                        data={"arquivo": (io.BytesIO(b"lixo"), "x.ret")}, content_type="multipart/form-data")
        self.assertEqual(ruim.status_code, 400)


if __name__ == "__main__":
    unittest.main()
