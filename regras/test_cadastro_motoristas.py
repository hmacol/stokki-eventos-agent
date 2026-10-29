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

    def _cadastrado(self, **kw):
        base = dict(AGENT_ID_VUUPT=5, NOME_MOTORISTA="Ana", ATIVO="SIM", ACEITA_VIAGENS="SIM",
                    ZONAS_PREFERIDAS="SUL", TELEFONE_MOTORISTA="1111", CPF_MOTORISTA="12345678901",
                    TIPO_VEICULO="FIORINO", PLACA="AAA1111")
        base.update(kw)
        return mb.inserir(self.conn, _linha(**base), "teste")

    def test_editar_campo_ausente_nao_mexe_vazio_limpa(self):
        mid = self._cadastrado()
        r = cm.editar_motorista({}, mid, {"zonas_preferidas": ["LESTE", "NORTE"], "telefone": ""}, "hugo", conn=self.conn)
        reg = mb.listar(self.conn)[0]
        self.assertEqual(reg["ZONAS_PREFERIDAS"], "LESTE,NORTE")
        self.assertIsNone(reg["TELEFONE_MOTORISTA"])
        self.assertEqual((reg["PLACA"], reg["ACEITA_VIAGENS"], reg["TIPO_VEICULO"]), ("AAA1111", "SIM", "FIORINO"))
        self.assertEqual({m["campo"] for m in r["mudancas"]}, {"ZONAS_PREFERIDAS", "TELEFONE_MOTORISTA"})
        self.assertEqual(r["motorista"]["id"], mid)
        orig = self.conn.execute("SELECT DISTINCT origem FROM bd_motoristas_alteracoes WHERE campo = 'TELEFONE_MOTORISTA'").fetchall()
        self.assertEqual([tuple(x) for x in orig], [("painel:hugo",)])

    def test_editar_traduz_booleanos_e_listas(self):
        mid = self._cadastrado()
        cm.editar_motorista({}, mid, {"aceita_viagens": False, "ativo": False, "apenas_carga_seca": True,
                                      "dias_disponiveis": ["SEGUNDA", "TERCA"], "max_rotas_dia": "3"}, "hugo", conn=self.conn)
        reg = mb.listar(self.conn)[0]
        self.assertEqual((reg["ACEITA_VIAGENS"], reg["ATIVO"], reg["APENAS_CARGA_SECA"]), ("NAO", "NAO", "SIM"))
        self.assertEqual((reg["DIAS_DISPONIVEIS"], reg["MAX_ROTAS_DIA"]), ("SEGUNDA,TERCA", 3))

    def test_editar_agent_id_e_tipo_invalido_levantam(self):
        mid = self._cadastrado()
        with self.assertRaises(ValueError):
            cm.editar_motorista({}, mid, {"agent_id": 99}, "hugo", conn=self.conn)
        with self.assertRaises(ValueError):
            cm.editar_motorista({}, mid, {"tipo_veiculo": "NAOEXISTE"}, "hugo", conn=self.conn)
        with self.assertRaises(ValueError):
            cm.editar_motorista({}, 9999, {"nome": "x"}, "hugo", conn=self.conn)

    def _login(self, cpf="12345678901"):
        from nucleo import auth_motorista as auth
        auth.criar_ou_atualizar_motorista(self.conn, cpf, "Ana", pin="123456", tipo_veiculo="FIORINO")
        self.conn.execute("UPDATE motoristas SET placa = 'AAA1111' WHERE cpf = ?", (cpf,))
        self.conn.commit()

    def test_editar_repassa_tipo_veiculo_ao_login_mas_nao_ativo(self):
        from nucleo import auth_motorista as auth
        mid = self._cadastrado()
        self._login()
        cm.editar_motorista({}, mid, {"tipo_veiculo": "VUC", "ativo": False, "nome": "Ana Maria",
                                      "placa": "zzz9999"}, "hugo", conn=self.conn)
        login = auth.buscar_motorista(self.conn, "12345678901")
        self.assertEqual((login["tipo_veiculo"], login["nome"], login["placa"]), ("VUC", "Ana Maria", "ZZZ9999"))
        self.assertEqual(login["ativo"], 1)
        self.assertEqual(mb.listar(self.conn)[0]["ATIVO"], "NAO")

    def test_editar_nao_repassa_placa_com_duas_linhas_nem_sem_login(self):
        from nucleo import auth_motorista as auth
        mid = self._cadastrado()
        self._cadastrado(AGENT_ID_VUUPT=6, PLACA="BBB2222")
        self._login()
        cm.editar_motorista({}, mid, {"placa": "CCC3333", "telefone": "999"}, "hugo", conn=self.conn)
        login = auth.buscar_motorista(self.conn, "12345678901")
        self.assertEqual((login["placa"], login["telefone"]), ("AAA1111", "999"))
        # sem login: nao cria nada
        mid2 = self._cadastrado(AGENT_ID_VUUPT=7, CPF_MOTORISTA="98765432100")
        cm.editar_motorista({}, mid2, {"telefone": "888"}, "hugo", conn=self.conn)
        self.assertIsNone(auth.buscar_motorista(self.conn, "98765432100"))

    def test_editar_nao_repassa_tipo_veiculo_nem_vehicle_id_com_duas_linhas(self):
        from nucleo import auth_motorista as auth
        mid = self._cadastrado()
        self._cadastrado(AGENT_ID_VUUPT=6, PLACA="BBB2222")
        self._login()
        cm.editar_motorista({}, mid, {"tipo_veiculo": "VUC", "vehicle_id": 9, "nome": "Ana Z"}, "hugo", conn=self.conn)
        login = auth.buscar_motorista(self.conn, "12345678901")
        self.assertEqual((login["tipo_veiculo"], login["vehicle_id"], login["nome"]), ("FIORINO", None, "Ana Z"))

    def test_editar_payload_completo_so_muda_o_que_mudou(self):
        # linha com apelido de tipo e CPF de 10 digitos: reenviar os mesmos valores nao pode mudar nada.
        # tipo_veiculo fica de fora: o JS so o manda quando o usuario troca o select (reenviar o
        # codigo canonico TRES_QUARTOS reescreveria o "3/4" gravado).
        mid = self._cadastrado(TIPO_VEICULO="3/4", CPF_MOTORISTA="1234567890", TELEFONE_MOTORISTA="11 9999",
                               DIAS_DISPONIVEIS="SEGUNDA,TERCA", MAX_ROTAS_DIA=2, VEHICLE_ID_VUUPT=44)
        atual = mb.listar(self.conn)[0]
        payload = {"nome": atual["NOME_MOTORISTA"], "aceita_viagens": True, "ativo": True, "max_rotas_dia": 2,
                   "dias_disponiveis": ["SEGUNDA", "TERCA"], "zonas_preferidas": ["SUL"],
                   "telefone": "11 9999", "email": "", "placa": "ZZZ9999", "cpf": "1234567890",
                   "vehicle_id": "44"}   # apenas_carga_seca/sem_app tambem ficam de fora (nao mudaram)
        r = cm.editar_motorista({}, mid, payload, "hugo", conn=self.conn)
        self.assertEqual([m["campo"] for m in r["mudancas"]], ["PLACA"])

    def test_editar_falha_do_repasse_nao_derruba_e_espelha(self):
        mid = self._cadastrado()
        with mock.patch.object(cm, "_repassar_ao_login", side_effect=RuntimeError("x")), \
                mock.patch.object(mb, "escrever_espelho") as espelho, self.assertLogs(cm.logger, "ERROR"):
            r = cm.editar_motorista({}, mid, {"nome": "Novo"}, "hugo", conn=self.conn)
        self.assertEqual(len(r["mudancas"]), 1)
        espelho.assert_called_once()

    def test_editar_falha_do_espelho_nao_derruba(self):
        mid = self._cadastrado()
        with mock.patch.object(mb, "escrever_espelho", side_effect=OSError("x")), self.assertLogs(cm.logger, "ERROR"):
            cm.editar_motorista({}, mid, {"nome": "Novo"}, "hugo", conn=self.conn)
        self.assertEqual(mb.listar(self.conn)[0]["NOME_MOTORISTA"], "Novo")


if __name__ == "__main__":
    unittest.main()
