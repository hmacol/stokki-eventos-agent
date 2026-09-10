# -*- coding: utf-8 -*-
"""
Testes dos modos de processar_documentos.main() (10/09):
  - ESCOPADO (pedidos_stokki=[...]): só a etapa da Stokki -- sem e-mail,
    sem e-mail de embarcadores, sem retentativa.
  - INCREMENTAL: e-mail sim, Stokki só pros pedidos ainda sem NF (trava
    cooperativa adquirida e liberada), sem retentativa, notifica só com erro.
  - COMPLETO: tudo, como antes.

Rodar:  py -3.11 -m unittest documentos_pedido.test_processar_documentos_modos
Tudo pesado (IMAP, Playwright, Vuupt, GCS, banco) é mockado.
"""
import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

_RAIZ = Path(__file__).parent.parent
for _p in (_RAIZ, _RAIZ / "documentos_pedido"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import processar_documentos as pdoc  # noqa: E402


class _Base(unittest.TestCase):
    def _rodar(self, adquirir_ok: bool = True, login_erro: Exception | None = None, **kwargs):
        """Roda main() com todas as bordas mockadas; devolve os mocks."""
        m = {}
        with ExitStack() as stack:
            m["config"] = stack.enter_context(patch.object(pdoc, "_carregar_config", return_value={}))
            m["vuupt"] = stack.enter_context(patch.object(pdoc, "VuuptClient"))
            m["indexador"] = stack.enter_context(patch.object(pdoc, "IndexadorNF"))
            stack.enter_context(patch.object(pdoc, "_backfill_nf_danfes_locais"))
            m["email"] = stack.enter_context(patch.object(pdoc, "buscar_pdfs_por_email", return_value=[]))
            m["embarcadores"] = stack.enter_context(patch.object(pdoc, "_etapas_email_embarcadores"))
            m["retentar"] = stack.enter_context(patch.object(
                pdoc, "retentar_revisao_manual",
                return_value={"resolvidos": 0, "sem_arquivo": 0, "ainda_pendente": 0}))
            m["notificar"] = stack.enter_context(patch.object(pdoc, "notificar_execucao"))
            m["descobrir"] = stack.enter_context(patch(
                "selecionar_pedidos.descobrir_pedidos",
                return_value=(["PS-1", "PS-2", "PS-3", "PS-4"], {"PS-3"}, {"PS-4"})))
            m["com_nf"] = stack.enter_context(patch.object(
                pdoc, "pedidos_com_documento_enviado", return_value={"PS-2"}))
            m["adquirir"] = stack.enter_context(patch("stokki.sessao_uso.adquirir", return_value=adquirir_ok))
            stack.enter_context(patch("stokki.sessao_uso.em_uso", return_value="painel: Pipeline"))
            m["liberar"] = stack.enter_context(patch("stokki.sessao_uso.liberar"))
            m["buscar"] = stack.enter_context(patch(
                "stokki_documentos.buscar_documentos_do_pedido", return_value=[]))
            stack.enter_context(patch("stokki_documentos._login", side_effect=login_erro))
            stack.enter_context(patch("stokki_documentos.nova_pagina"))
            m["pw"] = stack.enter_context(patch("playwright.sync_api.sync_playwright"))
            m["contadores"] = pdoc.main(**kwargs)
        return m

    def _visitados(self, m):
        return [c.args[2] for c in m["buscar"].call_args_list]


class TestModoEscopado(_Base):
    def test_so_stokki_dos_pedidos_informados(self):
        m = self._rodar(pedidos_stokki=["PS-9", "PS-8"], notificar=False)
        self.assertEqual(self._visitados(m), ["PS-9", "PS-8"])
        m["email"].assert_not_called()
        m["embarcadores"].assert_not_called()
        m["retentar"].assert_not_called()
        m["descobrir"].assert_not_called()
        m["adquirir"].assert_not_called()  # a trava é de quem chamou (documentacao_rota)
        m["notificar"].assert_not_called()

    def test_lista_vazia_nao_abre_navegador(self):
        m = self._rodar(pedidos_stokki=[], notificar=False)
        m["pw"].assert_not_called()
        m["retentar"].assert_not_called()


class TestModoIncremental(_Base):
    def test_visita_so_quem_ainda_nao_tem_nf(self):
        m = self._rodar(incremental=True)
        # PS-2 já tem NF; PS-3 é embarcador sem NF; PS-4 é DANFE só por e-mail
        self.assertEqual(self._visitados(m), ["PS-1"])
        m["email"].assert_called_once()
        m["embarcadores"].assert_called_once()
        m["retentar"].assert_not_called()

    def test_segura_e_libera_a_trava_da_stokki(self):
        m = self._rodar(incremental=True)
        m["adquirir"].assert_called_once()
        self.assertEqual(m["adquirir"].call_args.args[0], pdoc.DONO_TRAVA_INCREMENTAL)
        m["liberar"].assert_called_once_with(pdoc.DONO_TRAVA_INCREMENTAL)

    def test_stokki_ocupada_pula_etapa_e_nao_libera_trava_alheia(self):
        m = self._rodar(adquirir_ok=False, incremental=True)
        self.assertEqual(self._visitados(m), [])
        m["pw"].assert_not_called()
        m["liberar"].assert_not_called()
        m["embarcadores"].assert_called_once()  # e-mail segue normal

    def test_libera_trava_mesmo_com_erro_no_meio(self):
        m = self._rodar(login_erro=RuntimeError("stokki caiu"), incremental=True)
        m["liberar"].assert_called_once_with(pdoc.DONO_TRAVA_INCREMENTAL)
        # erro geral -> incremental notifica
        m["notificar"].assert_called_once()

    def test_sem_erro_nao_notifica(self):
        m = self._rodar(incremental=True)
        m["notificar"].assert_not_called()


class TestModoCompleto(_Base):
    def test_tudo_como_antes(self):
        m = self._rodar()
        self.assertEqual(self._visitados(m), ["PS-1", "PS-2", "PS-3", "PS-4"])
        m["email"].assert_called_once()
        m["embarcadores"].assert_called_once()
        m["retentar"].assert_called_once()
        m["adquirir"].assert_not_called()
        m["notificar"].assert_called_once()
        self.assertIn("ENVIADO", m["contadores"])


if __name__ == "__main__":
    unittest.main()
