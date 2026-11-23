# -*- coding: utf-8 -*-
"""pipeline.py --so-status (botao "Status Stokki" do planejamento, Hugo 09/11): rele na Stokki so a
situacao dos pedidos abertos (as mesmas 4 fontes da importacao, coletar_abertos) e regrava o retrato
vigia_stokki_abertos sem apagar a ultima acao da importacao; nao toca na Vuupt.

    py -3.11 -m unittest test_pipeline_so_status -v
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pipeline
from vigia import banco as vigia_banco


def _linha(id_, client="EMB QUALQUER #stkkc-500"):
    return {"id": f'<a href="/show/{id_}">{id_}</a>', "client": client, "_codigo_ps": f"PS-{id_}"}


def _iterar_fake(por_chamada):
    """iterar_todos_pedidos(sess, status=..., cliente=...) -> linhas da chave (status, cliente)."""
    def iterar(_sess, status=None, cliente=None, **_kw):
        valor = por_chamada.get((status, cliente), [])
        if isinstance(valor, Exception):
            raise valor
        return iter([dict(x) for x in valor])
    return iterar


class ColetarAbertosTest(unittest.TestCase):
    def _coletar(self, por_chamada, em_espera=()):
        espera = (mock.Mock(side_effect=em_espera) if isinstance(em_espera, Exception)
                  else mock.Mock(return_value=list(em_espera)))
        with mock.patch.object(pipeline, "EMBARCADORES_IMPORTAR_ABERTOS", {"18": "DOURADO"}), \
             mock.patch.object(pipeline.stokki_pedidos, "iterar_todos_pedidos", _iterar_fake(por_chamada)), \
             mock.patch.object(pipeline, "listar_pedidos_em_espera", espera), \
             mock.patch.object(pipeline, "_embarcador_prioritario", side_effect=lambda l: "#stkkc-18" in l["client"]):
            return pipeline.coletar_abertos(object(), {})

    def test_as_quatro_fontes_com_fonte_e_status(self):
        linhas, completa = self._coletar({
            ("Separating", "18"): [_linha(1, "DOURADO #stkkc-18")],
            ("Waiting for Carrier", None): [_linha(2), _linha(1, "DOURADO #stkkc-18")],
            ("Ready to Pack", None): [_linha(3)],
        }, em_espera=[{"id_stokki": 4, "cliente": "EMB Y", "codigo_ps": "#PS-4"}])
        self.assertTrue(completa)
        por_codigo = {pipeline.stokki_pedidos.extrair_codigo_ps_da_linha(l): (l["_fonte"], l["_status_stokki"])
                      for l in linhas}
        self.assertEqual(por_codigo, {
            "PS-1": ("prioritario", "Separating"),
            "PS-2": ("aguardando_transportador", "Waiting for Carrier"),
            "PS-3": ("em_separacao", "Ready to Pack"),
            "PS-4": ("estacao_impressao", "Em espera"),
        })

    def test_fonte_que_falha_marca_incompleta(self):
        _, completa = self._coletar({("Open", None): RuntimeError("500")})
        self.assertFalse(completa)
        _, completa = self._coletar({}, em_espera=RuntimeError("captura falhou"))
        self.assertFalse(completa)


class MainStatusTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name) / "dados.db"
        p = mock.patch.object(vigia_banco, "DB_PATH", self.db)
        p.start()
        self.addCleanup(p.stop)
        conn = vigia_banco.conectar()
        vigia_banco.registrar_listagem_stokki(
            [{"codigo": "PS-1", "status_stokki": "Open", "acao": "criado", "observacao": "ok"}],
            completa=True, conn=conn)
        conn.close()
        self.sessao = mock.patch("stokki.sessao_uso.adquirir", return_value="principal")
        self.adquirir = self.sessao.start()
        self.addCleanup(self.sessao.stop)
        p = mock.patch("stokki.sessao_uso.liberar")
        self.liberar = p.start()
        self.addCleanup(p.stop)
        for nome in ("_carregar_config", "StokkiSession"):
            p = mock.patch.object(pipeline, nome)
            p.start()
            self.addCleanup(p.stop)

    def _retrato(self):
        conn = vigia_banco.conectar()
        try:
            return {r["codigo"]: (r["status_stokki"], r["ultima_acao"])
                    for r in conn.execute("SELECT * FROM vigia_stokki_abertos")}
        finally:
            conn.close()

    def _linhas(self):
        a = _linha(1)
        a["_status_stokki"], a["_fonte"] = "Waiting for Carrier", "aguardando_transportador"
        b = _linha(2)
        b["_status_stokki"], b["_fonte"] = "Separating", "em_separacao"
        return [a, b]

    def test_regrava_status_e_mantem_a_acao_da_importacao(self):
        with mock.patch.object(pipeline, "coletar_abertos", return_value=(self._linhas(), True)):
            pipeline.main_status()
        self.assertEqual(self._retrato(), {"PS-1": ("Waiting for Carrier", "criado"),
                                           "PS-2": ("Separating", None)})
        self.liberar.assert_called_once_with(pipeline.DONO_TRAVA_STATUS)

    def test_modo_teste_nao_grava(self):
        with mock.patch.object(pipeline, "coletar_abertos", return_value=(self._linhas(), True)):
            pipeline.main_status(modo_teste=True)
        self.assertEqual(self._retrato(), {"PS-1": ("Open", "criado")})

    def test_stokki_ocupada_desiste_sem_listar(self):
        self.adquirir.return_value = False
        with mock.patch.object(pipeline, "coletar_abertos") as coletar, \
             mock.patch("stokki.sessao_uso.em_uso", return_value="pipeline-horario"):
            with self.assertRaises(RuntimeError):
                pipeline.main_status()
        coletar.assert_not_called()

    def test_falha_ao_gravar_vira_erro_e_solta_a_trava(self):
        with mock.patch.object(pipeline, "coletar_abertos", return_value=(self._linhas(), True)), \
             mock.patch.object(vigia_banco, "registrar_listagem_stokki", side_effect=RuntimeError("travado")):
            with self.assertRaises(RuntimeError):
                pipeline.main_status()
        self.liberar.assert_called_once_with(pipeline.DONO_TRAVA_STATUS)


if __name__ == "__main__":
    unittest.main()
