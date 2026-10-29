# -*- coding: utf-8 -*-
"""
test_cadastro_motoristas.py

Gravadores de motorista (cadastro novo e "Meus dados" do app) agora na tabela
bd_motoristas. SQLite temporario; Vuupt mockada.
    py -3.11 -m unittest regras.test_cadastro_motoristas -v
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))

from nucleo import banco  # noqa: E402
from regras import cadastro_motoristas as cm  # noqa: E402
from regras import motoristas_banco as mb  # noqa: E402


def _linha(**kw):
    base = {c: None for c in mb.COLUNAS}
    base.update(kw)
    return base


class TestCadastroMotoristas(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.dir = Path(self._tmp.name)
        self._db = mock.patch.object(banco, "DB_PATH", self.dir / "t.db")
        self._db.start()
        self.conn = banco.conectar()

    def tearDown(self):
        self.conn.close()
        self._db.stop()
        self._tmp.cleanup()

    def _dados(self, **kw):
        base = {"agent_id": 77, "nome": "Carlos", "aceita_viagens": True, "ativo": True, "max_rotas_dia": 2,
                "dias_disponiveis": ["SEG", "TER"], "zonas_preferidas": ["SUL"], "cpf": "094.741.427-42",
                "placa": "abc1d23", "telefone": "11999998888"}
        base.update(kw)
        return base

    def test_cadastrar_grava_no_banco_e_devolve_dict(self):
        r = cm.cadastrar_motorista({}, self._dados(), conn=self.conn)
        self.assertEqual(r, {"agent_id": 77, "nome": "Carlos"})
        reg = mb.listar(self.conn)[0]
        self.assertEqual((reg["AGENT_ID_VUUPT"], reg["ACEITA_VIAGENS"], reg["ATIVO"]), (77, "SIM", "SIM"))
        self.assertEqual((reg["DIAS_DISPONIVEIS"], reg["ZONAS_PREFERIDAS"]), ("SEG,TER", "SUL"))
        self.assertEqual((reg["CPF_MOTORISTA"], reg["PLACA"], reg["MAX_ROTAS_DIA"]), ("09474142742", "ABC1D23", 2))
        self.assertIsNone(reg["TIPO_VEICULO"])

    def test_cadastrar_recusa_agent_id_existente(self):
        cm.cadastrar_motorista({}, self._dados(), conn=self.conn)
        with self.assertRaises(ValueError):
            cm.cadastrar_motorista({}, self._dados(nome="Outro"), conn=self.conn)
        self.assertEqual(len(mb.listar(self.conn)), 1)

    def test_contato_telefone_em_todas_as_linhas_do_cpf(self):
        for ag in (1, 2):
            mb.inserir(self.conn, _linha(AGENT_ID_VUUPT=ag, NOME_MOTORISTA="A", ATIVO="SIM",
                                         CPF_MOTORISTA="12345678901", TELEFONE_MOTORISTA="1"), "teste")
        self.assertTrue(cm.atualizar_contato_planilha({}, "123.456.789-01", telefone="11977776666",
                                                      email="a@b.com", conn=self.conn))
        regs = mb.listar(self.conn)
        self.assertEqual([r["TELEFONE_MOTORISTA"] for r in regs], ["11977776666"] * 2)
        self.assertEqual([r["EMAIL_MOTORISTA"] for r in regs], ["a@b.com"] * 2)

    def test_contato_cpf_desconhecido(self):
        self.assertFalse(cm.atualizar_contato_planilha({}, "99999999999", telefone="1", conn=self.conn))

    def test_placa_nao_repassa_com_duas_linhas(self):
        for ag, placa in ((1, "AAA1111"), (2, "BBB2222")):
            mb.inserir(self.conn, _linha(AGENT_ID_VUUPT=ag, NOME_MOTORISTA="A", ATIVO="SIM",
                                         CPF_MOTORISTA="12345678901", PLACA=placa), "teste")
        self.assertTrue(cm.atualizar_contato_planilha({}, "12345678901", placa="ccc3333", conn=self.conn))
        self.assertEqual([r["PLACA"] for r in mb.listar(self.conn)], ["AAA1111", "BBB2222"])
        h = self.conn.execute("SELECT valor_novo, origem FROM bd_motoristas_alteracoes WHERE campo = 'PLACA'").fetchall()
        self.assertEqual([tuple(x) for x in h], [("CCC3333", "app:8901:nao_repassada")] * 2)

    def test_placa_repassa_com_uma_linha(self):
        mb.inserir(self.conn, _linha(AGENT_ID_VUUPT=1, NOME_MOTORISTA="A", ATIVO="SIM",
                                     CPF_MOTORISTA="12345678901", PLACA="AAA1111"), "teste")
        cm.atualizar_contato_planilha({}, "12345678901", placa="ccc3333", conn=self.conn)
        self.assertEqual(mb.listar(self.conn)[0]["PLACA"], "CCC3333")

    def test_agentes_nao_cadastrados_usa_o_banco(self):
        mb.inserir(self.conn, _linha(AGENT_ID_VUUPT=1, NOME_MOTORISTA="A", ATIVO="SIM"), "teste")
        agentes = [{"id": 2, "name": "Zeca"}, {"id": 1, "name": "A"}, {"id": 3, "name": "Beto"}]
        with mock.patch.object(cm, "_listar_tudo_vuupt", return_value=agentes):
            r = cm.listar_agentes_vuupt_nao_cadastrados({}, conn=self.conn)
        self.assertEqual([a["agent_id"] for a in r], [3, 2])

    def test_falha_do_espelho_nao_derruba_a_gravacao(self):
        with mock.patch.object(mb, "escrever_espelho", side_effect=OSError("x")):
            cm.cadastrar_motorista({}, self._dados(), conn=self.conn)
        self.assertEqual(len(mb.listar(self.conn)), 1)


if __name__ == "__main__":
    unittest.main()
