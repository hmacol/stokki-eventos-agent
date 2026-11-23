# -*- coding: utf-8 -*-
"""
test_torre_acumulada.py

Testes da regra que decide se uma rota continua na Torre de Controle
ou ja foi pro Historico de Rotas (Hugo, 15/09: a torre deixa de ser
filtrada por data e passa a acumular as rotas ainda abertas de
qualquer dia; a rota so sai quando todas as paradas estao resolvidas E
nao tem insucesso pendente na Fila de acao, ou quando alguem clica
"Encerrar").

Nenhum teste toca no SQLite nem na rede: a regra e uma funcao pura.
Rodar (da raiz):
    py -3.11 -m unittest painel_agentes.test_torre_acumulada -v
"""
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(Path(__file__).parent))

import torre_controle  # noqa: E402

HOJE = date(2026, 9, 15)
ONTEM = date(2026, 9, 14)


def _rota(estado, data_rota, pedidos=None, rota_id=1):
    return {
        "id": rota_id, "nome": f"Planejamento - {data_rota.strftime('%d/%m/%Y')} - #1",
        "estado": estado, "data_rota": data_rota.isoformat(),
        "pedidos": pedidos or [],
    }


class TestClassificarRotaTorre(unittest.TestCase):

    def test_concluida_sem_pendencia_sai_da_torre(self):
        rota = _rota("concluida", HOJE, [{"codigo": "#PS-1", "situacao": "entregue"}])
        resultado = torre_controle.classificar_rota_torre(rota, HOJE, set(), set())
        self.assertFalse(resultado["fica"])
        self.assertFalse(resultado["atrasada"])

    def test_concluida_com_insucesso_nao_tratado_fica(self):
        rota = _rota("concluida", HOJE, [
            {"codigo": "#PS-1", "situacao": "entregue"},
            {"codigo": "#PS-2", "situacao": "insucesso"},
        ])
        resultado = torre_controle.classificar_rota_torre(rota, HOJE, set(), set())
        self.assertTrue(resultado["fica"])
        self.assertEqual(resultado["pendencias"], 1)

    def test_concluida_com_insucesso_ja_tratado_sai(self):
        rota = _rota("concluida", HOJE, [{"codigo": "#PS-2", "situacao": "insucesso"}])
        tratadas = {"insucesso:#PS-2"}
        resultado = torre_controle.classificar_rota_torre(rota, HOJE, tratadas, set())
        self.assertFalse(resultado["fica"])

    def test_nao_iniciada_de_ontem_fica_como_atrasada(self):
        rota = _rota("nao_iniciada", ONTEM, [{"codigo": "#PS-3", "situacao": "pendente"}])
        resultado = torre_controle.classificar_rota_torre(rota, HOJE, set(), set())
        self.assertTrue(resultado["fica"])
        self.assertTrue(resultado["atrasada"])

    def test_em_andamento_de_hoje_fica_sem_ser_atrasada(self):
        rota = _rota("em_andamento", HOJE, [{"codigo": "#PS-3", "situacao": "em_rota"}])
        resultado = torre_controle.classificar_rota_torre(rota, HOJE, set(), set())
        self.assertTrue(resultado["fica"])
        self.assertFalse(resultado["atrasada"])

    def test_encerrada_manualmente_sai_mesmo_com_pendencia(self):
        rota = _rota("nao_iniciada", ONTEM, [{"codigo": "#PS-3", "situacao": "pendente"}], rota_id=77)
        resultado = torre_controle.classificar_rota_torre(rota, HOJE, set(), {77})
        self.assertFalse(resultado["fica"])

    def test_cancelada_reconstruida_so_de_hoje_fica(self):
        # Card reconstruido de rota cancelada por exclusao (estado
        # 'vazia', cancelada=True) so interessa no proprio dia.
        hoje = dict(_rota("vazia", HOJE), cancelada=True)
        ontem = dict(_rota("vazia", ONTEM), cancelada=True)
        self.assertTrue(torre_controle.classificar_rota_torre(hoje, HOJE, set(), set())["fica"])
        self.assertFalse(torre_controle.classificar_rota_torre(ontem, HOJE, set(), set())["fica"])


class TestEncerrarNaoEscondeInsucesso(unittest.TestCase):
    """28/09: "Encerrar" tira a rota da tela, mas o insucesso ainda não
    tratado dela continua na Fila de ação."""

    def test_insucesso_de_rota_encerrada_segue_no_agregado(self):
        card = _rota("concluida", ONTEM, [{"codigo": "#PS-9", "situacao": "insucesso"},
                                           {"codigo": "#PS-8", "situacao": "insucesso"}], rota_id=77)
        contrib = torre_controle._contrib_vazia()
        contrib["insucessos_lista"] = [{"codigo": "#PS-9"}, {"codigo": "#PS-8"}]
        with mock.patch.object(torre_controle, "listar_rotas", return_value=[{"id": 77}]), \
             mock.patch.object(torre_controle, "_data_local_da_rota", return_value=ONTEM), \
             mock.patch.object(torre_controle, "_montar_card_rota", return_value=(card, contrib)), \
             mock.patch.object(torre_controle, "_rota_ids_com_exclusao_no_dia", return_value=set()), \
             mock.patch.object(torre_controle, "_buscar_tratadas", return_value={"insucesso:#PS-8": {}}):
            rotas, agregado, _ = torre_controle._coletar_rotas_abertas("t", HOJE, {}, {77})
        self.assertEqual(rotas, [])
        self.assertEqual([i["codigo"] for i in agregado["insucessos_lista"]], ["#PS-9"])


if __name__ == "__main__":
    unittest.main()
