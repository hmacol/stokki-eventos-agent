# -*- coding: utf-8 -*-
"""
Tela /adesao-motoristas (adesão ao app novo, 23/11).

    py -3.11 -m unittest test_adesao_motoristas      (de dentro de painel_agentes/)
"""
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

_AQUI = Path(__file__).parent
for _p in (_AQUI.parent, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import adesao_motoristas as adesao  # noqa: E402

AGORA = datetime(2026, 11, 23, 12, 0)


class TestAdesaoMotoristas(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.banco = Path(self._tmp.name) / "dados.db"
        c = sqlite3.connect(self.banco)
        c.executescript("""
            CREATE TABLE bd_motoristas (NOME_MOTORISTA TEXT, AGENT_ID_VUUPT INTEGER, CPF_MOTORISTA TEXT,
                ATIVO TEXT, POSICAO TEXT, SEM_APP TEXT);
            CREATE TABLE motoristas (cpf TEXT, ultimo_login_em TEXT, app_versao TEXT, plataforma TEXT);
            CREATE TABLE motoristas_fila_app (cpf TEXT, envios INTEGER, ultimo_envio_em TEXT, situacao TEXT);
            CREATE TABLE nucleo_rotas (agent_id INTEGER, data_rota TEXT, status TEXT, operada_app INTEGER);
        """)
        c.executemany("INSERT INTO bd_motoristas VALUES (?,?,?,?,?,?)", [
            ("Na Ponte", 1, "111", "SIM", "FIXO", ""),
            ("Usando", 2, "222", "SIM", "FIXO", ""),
            ("Velho", 3, "333", "SIM", "FIXO", ""),
            ("So Pin", 4, "444", "SIM", "ESPORADICO", ""),
            ("Fixo Sem Conta", 5, "555", "SIM", "FIXO", ""),
            ("Espor Com Rota", 6, "666", "SIM", "ESPORADICO", "SIM"),
            ("Espor Parado", 7, "777", "SIM", "ESPORADICO", ""),
            ("Lalamove", 50258, "", "SIM", "ESPORADICO", ""),
            ("Motorista TESTE", 8, "888", "SIM", "ESPORADICO", ""),
            ("Inativo", 9, "999", "NAO", "FIXO", ""),
        ])
        c.executemany("INSERT INTO motoristas VALUES (?,?,?,?)", [
            ("111", "2026-11-23 08:00:00", "1.2.0", None),
            ("222", "2026-11-22 08:00:00", "1.2.0", "web"),
            ("333", "2026-10-08 08:00:00", None, None),
            ("444", None, None, None),
        ])
        c.execute("INSERT INTO motoristas_fila_app VALUES ('444', 1, '2026-11-23 10:00:00', 'enviado')")
        c.executemany("INSERT INTO nucleo_rotas VALUES (?,?,?,?)", [
            (1, "2026-11-22", "CONCLUIDA", 1), (1, "2026-11-21", "CANCELADA", 0),
            (6, "2026-11-20", "CONCLUIDA", 0), (7, "2026-10-01", "CONCLUIDA", 0),
        ])
        c.commit()
        c.close()
        self.linhas = {l["nome"]: l for l in adesao.listar(self.banco, AGORA, ponte={1})}

    def test_exclui_lalamove_teste_e_inativo(self):
        self.assertEqual(set(self.linhas), {"Na Ponte", "Usando", "Velho", "So Pin", "Fixo Sem Conta",
                                            "Espor Com Rota", "Espor Parado"})

    def test_situacao_e_leva(self):
        esperado = {"Na Ponte": (adesao.NA_PONTE, 0), "Usando": (adesao.USANDO, 1), "Velho": (adesao.DESATUALIZADO, 1),
                    "So Pin": (adesao.PIN_SEM_ENTRAR, 2), "Fixo Sem Conta": (adesao.SEM_LOGIN, 2),
                    "Espor Com Rota": (adesao.SEM_LOGIN, 3), "Espor Parado": (adesao.SEM_LOGIN, 4)}
        self.assertEqual({n: (l["situacao"], l["leva"]) for n, l in self.linhas.items()}, esperado)

    def test_rotas_mensagem_e_plataforma(self):
        p = self.linhas["Na Ponte"]
        self.assertEqual((p["rotas_recentes"], p["rotas_app"]), (1, 1))   # cancelada nao conta
        self.assertEqual(self.linhas["Espor Parado"]["rotas_recentes"], 0)  # fora dos 14 dias
        self.assertEqual(self.linhas["So Pin"]["msg_envios"], 1)
        self.assertEqual(self.linhas["Usando"]["plataforma"], "PWA")
        self.assertEqual(self.linhas["Espor Com Rota"]["plataforma"], "iPhone")

    def test_resumo_soma_tudo(self):
        linhas = list(self.linhas.values())
        self.assertEqual(sum(r["qtd"] for r in adesao.resumo(linhas)), 7)
        self.assertEqual(sum(r["qtd"] for r in adesao.resumo_levas(linhas)), 7)


class TestTela(TestAdesaoMotoristas):
    def test_tela_abre_e_mostra_o_funil(self):
        import importlib.util
        from unittest import mock
        spec = importlib.util.spec_from_file_location("painel_agentes_app", _AQUI / "painel_agentes.py")
        painel = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = painel
        spec.loader.exec_module(painel)
        with mock.patch.object(adesao, "DB_PATH", self.banco), \
                mock.patch.object(adesao, "agentes_da_ponte", return_value={1}):
            painel.app.config["TESTING"] = True
            cliente = painel.app.test_client()
            with cliente.session_transaction() as sess:
                sess["nivel_acesso"] = "leitura"
                sess["usuario"] = "teste"
            resp = cliente.get("/adesao-motoristas")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Na ponte", html)
        self.assertIn("Tem PIN, nunca entrou", html)
        self.assertIn("So Pin", html)


if __name__ == "__main__":
    unittest.main()
