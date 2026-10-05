# -*- coding: utf-8 -*-
"""Testes das regras puras do agente. Rodar: py -3.11 -m unittest agente.test_regras"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agente import regras as r  # noqa: E402


def fato_vigia(estado, codigo="PS-100", vencido=False, **extra):
    f = {"fonte": "vigia", "codigo": codigo, "estado": estado, "vencido": vencido,
         "desde": "2026-10-05 08:00:00", "vence_em": None, "motivo": "m", "detalhe": "d", "service_id": 1}
    f.update(extra)
    return f


class TestTentativa(unittest.TestCase):
    def test_codigos(self):
        self.assertEqual(r.tentativa("PS-1"), 1)
        self.assertEqual(r.tentativa("#PS-1"), 1)
        self.assertEqual(r.tentativa("PS-1-R1"), 2)
        self.assertEqual(r.tentativa("PS-1-R2"), 3)
        self.assertEqual(r.tentativa("ps-1-r2"), 3)
        self.assertEqual(r.tentativa("PS-1-R1-R1"), 3)  # bug antigo de sufixo repetido
        self.assertEqual(r.tentativa(""), 1)
        self.assertEqual(r.tentativa(None), 1)


class TestVigia(unittest.TestCase):
    def test_aguardando_cliente_so_quando_vencido(self):
        self.assertEqual(r.decidir(fato_vigia("AGUARDANDO_CLIENTE", vencido=False)), [])
        acoes = r.decidir(fato_vigia("AGUARDANDO_CLIENTE", vencido=True))
        self.assertEqual([(a.tipo, a.template) for a in acoes], [(r.AVISAR, r.COBRAR_DATA_AGENDAMENTO)])
        self.assertFalse(acoes[0].precisa_aprovacao)
        self.assertIn("PS-100", acoes[0].fato_origem)

    def test_insucesso_avisa_na_hora(self):
        acoes = r.decidir(fato_vigia("INSUCESSO", vencido=False))
        self.assertEqual([a.template for a in acoes], [r.AVISO_FALHA_ENTREGA])
        self.assertEqual(acoes[0].dados["tentativa"], 1)

    def test_insucesso_na_terceira_tentativa_pede_devolucao(self):
        acoes = r.decidir(fato_vigia("INSUCESSO", codigo="PS-100-R2"))
        self.assertEqual([a.template for a in acoes], [r.AVISO_FALHA_ENTREGA, r.PEDIR_CONFIRMACAO_DEVOLUCAO])
        self.assertEqual(acoes[1].fato_origem, "devolucao:PS-100-R2")
        self.assertTrue(all(a.tipo == r.AVISAR for a in acoes))

    def test_segunda_tentativa_nao_pede_devolucao(self):
        acoes = r.decidir(fato_vigia("INSUCESSO", codigo="PS-100-R1"))
        self.assertEqual([a.template for a in acoes], [r.AVISO_FALHA_ENTREGA])

    def test_recusado_pede_devolucao(self):
        acoes = r.decidir(fato_vigia("RECUSADO"))
        self.assertEqual([a.template for a in acoes], [r.PEDIR_CONFIRMACAO_DEVOLUCAO])

    def test_estados_que_ja_sao_excecao_da_torre_nao_geram_acao(self):
        for estado in ("ROTA_PASSADA", "SEM_SERVICO", "RASCUNHO_COM_ERRO", "EM_RASCUNHO",
                       "NO_POOL", "AGENDADO", "EM_ROTA"):
            self.assertEqual(r.decidir(fato_vigia(estado, vencido=True)), [], estado)

    def test_origem_muda_quando_desde_muda(self):
        a = r.decidir(fato_vigia("INSUCESSO", desde="2026-10-05 08:00:00"))[0]
        b = r.decidir(fato_vigia("INSUCESSO", desde="2026-10-06 08:00:00"))[0]
        self.assertNotEqual(a.fato_origem, b.fato_origem)

    def test_nenhuma_acao_fala_com_destinatario(self):
        # garantia estrutural: todo AVISAR e pro embarcador (nao ha outro destino na regra)
        for estado in ("AGUARDANDO_CLIENTE", "INSUCESSO", "RECUSADO"):
            for a in r.decidir(fato_vigia(estado, vencido=True)):
                self.assertIn(a.tipo, (r.AVISAR, r.PROPOR))


class TestBatimento(unittest.TestCase):
    def test_divergencia_vira_proposta_com_aprovacao(self):
        acoes = r.decidir({"fonte": "batimento", "codigo": "PS-7", "motivo": "ENTREGUE_NAO_EXPEDIDO",
                           "evidencias": {"vuupt": "done"}})
        self.assertEqual(len(acoes), 1)
        self.assertEqual(acoes[0].tipo, r.PROPOR)
        self.assertEqual(acoes[0].template, r.PROPOSTA_EXPEDIR_STOKKI)
        self.assertTrue(acoes[0].precisa_aprovacao)
        self.assertEqual(acoes[0].dados["evidencias"], {"vuupt": "done"})

    def test_todos_os_motivos_da_lista_fechada(self):
        for motivo, template in r.PROPOSTA_POR_DIVERGENCIA.items():
            acoes = r.decidir({"fonte": "batimento", "codigo": "PS-7", "motivo": motivo})
            self.assertEqual(acoes[0].template, template, motivo)

    def test_status_desconhecido_e_motivo_fora_da_lista_nao_geram_acao(self):
        self.assertEqual(r.decidir({"fonte": "batimento", "codigo": "PS-7", "motivo": "STATUS_STOKKI_DESCONHECIDO"}), [])
        self.assertEqual(r.decidir({"fonte": "batimento", "codigo": "PS-7", "motivo": "QUALQUER"}), [])

    def test_fonte_desconhecida(self):
        self.assertEqual(r.decidir({"fonte": "outra"}), [])
        self.assertEqual(r.decidir({}), [])


if __name__ == "__main__":
    unittest.main()
