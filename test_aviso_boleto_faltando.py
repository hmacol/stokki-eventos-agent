# -*- coding: utf-8 -*-
"""
Rodar:  py -3.11 -m unittest test_aviso_boleto_faltando
"""
import unittest
from unittest.mock import MagicMock, patch

import aviso_boleto_faltando as abf

PEDIDOS = [
    {"codigo": "PS-1", "codigo_servico": "PS-1", "sender_id": 11, "documento": "1", "cliente": "A", "embarcador": "NUU"},
    {"codigo": "PS-2", "codigo_servico": "PS-2", "sender_id": 11, "documento": "1", "cliente": "A", "embarcador": "NUU"},
    {"codigo": "PS-3", "codigo_servico": "PS-3", "sender_id": 22, "documento": "2", "cliente": "B", "embarcador": "SOT"},
]
EMBS = {11: {"nome": "NUU", "emails": ["fin@nuu.com"], "cnpj": "111", "desligado": False},
        22: {"nome": "SOTILLE", "emails": [], "cnpj": "222", "desligado": True}}
CFG = {"aviso_boleto_faltando": {"ativo": True, "forcar_destino": "hugo@freshlogbr.com"}}


class Executar(unittest.TestCase):
    def _rodar(self, faltando_antes, faltando_depois, ja=frozenset(), modo_teste=False, atualizar=True,
               email_ok=True, zap="enviado", chamado_ok=True):
        conn = MagicMock()
        rodar_docs = MagicMock()
        enviar = MagicMock(return_value=email_ok)
        seq = iter([faltando_antes, faltando_depois])
        with patch.object(abf.bf, "pedidos_da_data", return_value=PEDIDOS), \
             patch.object(abf.bf, "faltando", side_effect=lambda c, p, s: [x for x in next(seq)]), \
             patch.object(abf.bf, "ja_avisados", return_value=set(ja)), \
             patch.object(abf.bf, "registrar_avisados") as reg, \
             patch.object(abf.bf, "registrar_no_chamado", return_value=5 if chamado_ok else None) as cham, \
             patch.object(abf, "_telefone", return_value="5511911112222"), \
             patch.object(abf.notificar_whatsapp, "avisar_cliente_boleto_faltando", return_value=zap) as wz:
            r = abf.executar(CFG, "2026-10-07", modo_teste=modo_teste, atualizar=atualizar, conn=conn,
                             enviar=enviar, situacoes={"PS-1": "TEM_BOLETO"}, embarcadores=EMBS,
                             rodar_documentos=rodar_docs)
        return r, rodar_docs, enviar, wz, cham, reg

    def test_atualiza_so_os_candidatos_e_avisa_quem_continua_faltando(self):
        r, docs, enviar, wz, cham, reg = self._rodar([PEDIDOS[0], PEDIDOS[1]], [PEDIDOS[0]])
        docs.assert_called_once()
        self.assertEqual(sorted(docs.call_args.args[0]), ["PS-1", "PS-2"])
        self.assertEqual(r["avisados"], 1)
        self.assertEqual(enviar.call_args.args[0], ["hugo@freshlogbr.com"])   # forcar_destino
        wz.assert_called_once()
        cham.assert_called_once()
        reg.assert_called_once()
        self.assertEqual([i["codigo"] for i in reg.call_args.args[1]], ["PS-1"])

    def test_ja_avisado_nao_entra(self):
        r, docs, enviar, *_ = self._rodar([PEDIDOS[0]], [PEDIDOS[0]], ja={"PS-1"})
        docs.assert_not_called()
        enviar.assert_not_called()
        self.assertEqual(r["avisados"], 0)

    def test_nada_faltando_nao_atualiza_nem_avisa(self):
        r, docs, enviar, *_ = self._rodar([], [])
        docs.assert_not_called()
        enviar.assert_not_called()

    def test_embarcador_desligado_so_chamado(self):
        r, docs, enviar, wz, cham, reg = self._rodar([PEDIDOS[2]], [PEDIDOS[2]])
        enviar.assert_not_called()
        wz.assert_not_called()
        cham.assert_called_once()
        reg.assert_called_once()                 # chamado conta como canal

    def test_todos_os_canais_falham_nao_registra(self):
        r, docs, enviar, wz, cham, reg = self._rodar([PEDIDOS[0]], [PEDIDOS[0]], email_ok=False,
                                                     zap="falhou", chamado_ok=False)
        reg.assert_not_called()
        self.assertTrue(r["falhas"])

    def test_modo_teste_nao_grava_nem_abre_chamado(self):
        r, docs, enviar, wz, cham, reg = self._rodar([PEDIDOS[0]], [PEDIDOS[0]], modo_teste=True)
        reg.assert_not_called()
        cham.assert_not_called()
        self.assertEqual(enviar.call_args.args[0], ["hugo@freshlogbr.com"])

    def test_sem_atualizar(self):
        r, docs, *_ = self._rodar([PEDIDOS[0]], [PEDIDOS[0]], atualizar=False)
        docs.assert_not_called()

    def test_falha_na_atualizacao_segue_e_marca_falha(self):
        conn = MagicMock()
        with patch.object(abf.bf, "pedidos_da_data", return_value=PEDIDOS), \
             patch.object(abf.bf, "faltando", return_value=[PEDIDOS[0]]), \
             patch.object(abf.bf, "ja_avisados", return_value=set()), \
             patch.object(abf.bf, "registrar_avisados"), \
             patch.object(abf.bf, "registrar_no_chamado", return_value=5), \
             patch.object(abf, "_telefone", return_value=None):
            r = abf.executar(CFG, "2026-10-07", conn=conn, enviar=MagicMock(return_value=True),
                             situacoes={}, embarcadores=EMBS, rodar_documentos=MagicMock(side_effect=RuntimeError("stokki")))
        self.assertEqual(r["avisados"], 1)
        self.assertTrue(any("stokki" in f for f in r["falhas"]))


if __name__ == "__main__":
    unittest.main()
