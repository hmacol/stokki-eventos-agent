# -*- coding: utf-8 -*-
"""Testes dos 3 scripts manuais de motoristas (sem rede, banco temporario)."""
import logging
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from nucleo import banco
from regras import motoristas_banco as mb

import sincronizar_placas_motoristas as sp
import sincronizar_cpf_motoristas as sc
import vincular_veiculos_motoristas as vv


def setUpModule():
    logging.disable(logging.CRITICAL)  # silencia migracoes/INFO dos scripts


def tearDownModule():
    logging.disable(logging.NOTSET)


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        db = Path(self._tmp.name) / "dados.db"
        p = mock.patch.object(banco, "DB_PATH", db)  # o espelho vai pro diretorio temporario
        p.start()
        self.addCleanup(p.stop)
        self.conn = banco.conectar(db)
        self.addCleanup(self.conn.close)

    def novo(self, **campos):
        base = {"AGENT_ID_VUUPT": 1, "NOME_MOTORISTA": "Fulano", "ATIVO": "SIM"}
        base.update(campos)
        return mb.inserir(self.conn, base, "teste")

    def n_alteracoes(self):
        return self.conn.execute("SELECT COUNT(*) FROM bd_motoristas_alteracoes").fetchone()[0]

    def linha(self, mid):
        return next(l for l in mb.listar(self.conn) if l["id"] == mid)

    def origens(self, depois_de):
        return {r[0] for r in self.conn.execute(
            "SELECT origem FROM bd_motoristas_alteracoes WHERE id > ?", (depois_de,))}


class TestPlacas(Base):
    AGENTES = [{"id": 1, "vehicle_id": 10}, {"id": 2, "vehicle_id": 20}]
    VEICULOS = [{"id": 10, "license_plate": " abc1d23 "}, {"id": 20, "license_plate": "ZZZ9Z99"}]

    def test_a_seco_nao_grava(self):
        self.novo()
        antes = self.n_alteracoes()
        r = sp.sincronizar(self.conn, self.AGENTES, self.VEICULOS, aplicar=False)
        self.assertEqual(len(r["atualizados"]), 1)
        self.assertEqual(self.n_alteracoes(), antes)
        self.assertIsNone(self.linha(1)["PLACA"])

    def test_aplicar_grava_e_nao_sobrescreve(self):
        a = self.novo()
        b = self.novo(AGENT_ID_VUUPT=2, NOME_MOTORISTA="Beltrano", PLACA="MAN0A00")
        antes = self.n_alteracoes()
        r = sp.sincronizar(self.conn, self.AGENTES, self.VEICULOS, aplicar=True)
        self.assertEqual(self.linha(a)["PLACA"], "ABC1D23")
        self.assertEqual(self.linha(b)["PLACA"], "MAN0A00")
        self.assertEqual(r["ja_tinha"], 1)
        self.assertEqual(self.origens(antes), {"sincronizar_placas_motoristas.py"})
        self.assertEqual(self.n_alteracoes(), antes + 1)


class TestCpf(Base):
    AGENTES = [{"id": 1, "code": "012.345.678-90"}, {"id": 2, "code": "00000000001"},
               {"id": 3, "code": "caioerick2"}, {"id": 4, "code": "123456"}]

    def test_a_seco_nao_grava(self):
        self.novo()
        antes = self.n_alteracoes()
        r = sc.sincronizar(self.conn, self.AGENTES, aplicar=False)
        self.assertEqual(len(r["atualizados"]), 1)
        self.assertEqual(self.n_alteracoes(), antes)
        self.assertIsNone(self.linha(1)["CPF_MOTORISTA"])

    def test_aplicar_grava_digitos_com_zero_e_nao_sobrescreve(self):
        a = self.novo()
        b = self.novo(AGENT_ID_VUUPT=2, NOME_MOTORISTA="B", CPF_MOTORISTA="99999999999")
        c = self.novo(AGENT_ID_VUUPT=3, NOME_MOTORISTA="C")
        d = self.novo(AGENT_ID_VUUPT=4, NOME_MOTORISTA="D")
        antes = self.n_alteracoes()
        r = sc.sincronizar(self.conn, self.AGENTES, aplicar=True)
        self.assertEqual(self.linha(a)["CPF_MOTORISTA"], "01234567890")
        self.assertEqual(self.linha(b)["CPF_MOTORISTA"], "99999999999")
        self.assertIsNone(self.linha(c)["CPF_MOTORISTA"])  # apelido: nao e CPF
        self.assertIsNone(self.linha(d)["CPF_MOTORISTA"])  # menos de 11 digitos
        self.assertEqual(len(r["sem_dado"]), 2)
        self.assertEqual(self.origens(antes), {"sincronizar_cpf_motoristas.py"})


class TestVincular(Base):
    VEICULOS = [{"id": 10, "license_plate": "ABC-1D23", "code": "V10"},
                {"id": 20, "license_plate": "ZZZ9Z99", "code": "V20"}]

    def test_dry_run_nao_chama_put_nem_grava(self):
        self.novo(PLACA="ABC1D23")
        antes = self.n_alteracoes()
        put = mock.Mock()
        r = vv.vincular(self.conn, [{"id": 1, "vehicle_id": None}], self.VEICULOS, True, put)
        put.assert_not_called()
        self.assertEqual(len(r["vinculados_vuupt"]), 1)
        self.assertEqual(len(r["planilha_preenchida"]), 1)
        self.assertEqual(self.n_alteracoes(), antes)
        self.assertIsNone(self.linha(1)["VEHICLE_ID_VUUPT"])

    def test_aplicar_grava_chama_put_e_nao_sobrescreve(self):
        a = self.novo(PLACA="ABC1D23")
        b = self.novo(AGENT_ID_VUUPT=2, NOME_MOTORISTA="B", PLACA="ZZZ9Z99", VEHICLE_ID_VUUPT=77)
        antes = self.n_alteracoes()
        put = mock.Mock()
        agentes = [{"id": 1, "vehicle_id": None}, {"id": 2, "vehicle_id": 20}]
        r = vv.vincular(self.conn, agentes, self.VEICULOS, False, put)
        put.assert_called_once_with(1, 10)
        self.assertEqual(self.linha(a)["VEHICLE_ID_VUUPT"], 10)
        self.assertEqual(self.linha(b)["VEHICLE_ID_VUUPT"], 77)  # preservado
        self.assertEqual(len(r["avisos"]), 1)
        self.assertEqual(self.origens(antes), {"vincular_veiculos_motoristas.py"})


if __name__ == "__main__":
    unittest.main()
