"""Testes de pipeline._deve_segurar_por_transportadora (sem rede, sem banco)."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pipeline


def _res(fonte="destino"):
    return SimpleNamespace(fonte=fonte)


def _catalogo(desconhecida=True):
    chamadas = []

    class _Cat:
        def resolver(self, nome, cnpj=""):
            chamadas.append((nome, cnpj))
            return SimpleNamespace(desconhecida=desconhecida)

    cat = _Cat()
    cat.chamadas = chamadas
    return cat


NOVA = {"transportadora": {"nome": "Rapido Novo", "documento": "12.345.678/0001-90"}}


class DeveSegurarPorTransportadoraTest(unittest.TestCase):
    def test_transportadora_vazia_ou_nao_informado_nao_segura(self):
        for transp in (None, {}, {"nome": ""}, {"nome": "Não informado"}, {"nome": "NAO INFORMADO"}):
            det = {"transportadora": transp}
            self.assertIsNone(pipeline._deve_segurar_por_transportadora(det, _res(), None, _catalogo()), transp)

    def test_sem_chave_transportadora_nao_segura(self):
        self.assertIsNone(pipeline._deve_segurar_por_transportadora({}, _res(), None, _catalogo()))

    def test_desconhecida_com_destino_segura(self):
        cat = _catalogo()
        self.assertEqual(pipeline._deve_segurar_por_transportadora(NOVA, _res("destino"), None, cat),
                         ("Rapido Novo", "12345678000190"))
        self.assertEqual(cat.chamadas, [("Rapido Novo", "12345678000190")])

    def test_desconhecida_fora_da_area_segura(self):
        self.assertIsNotNone(pipeline._deve_segurar_por_transportadora(
            NOVA, _res("aguardando_redespacho"), None, _catalogo()))

    def test_desconhecida_com_local_entrega_ou_llm_nao_segura(self):
        for fonte in ("local_entrega", "mensagem_llm"):
            self.assertIsNone(pipeline._deve_segurar_por_transportadora(NOVA, _res(fonte), None, _catalogo()), fonte)

    def test_ja_na_vuupt_nao_segura(self):
        self.assertIsNone(pipeline._deve_segurar_por_transportadora(
            NOVA, _res(), {"status": "not_assigned"}, _catalogo()))

    def test_conhecida_nao_segura(self):
        self.assertIsNone(pipeline._deve_segurar_por_transportadora(NOVA, _res(), None, _catalogo(False)))


class RegistrarEAvisarTest(unittest.TestCase):
    def _rodar(self, db, aviso):
        det = {"destino": {"nome": "Loja", "cidade": "Campinas", "uf": "SP"}}
        with mock.patch.object(pipeline, "DB_PATH", db),                 mock.patch.object(pipeline, "_buscar_dados_embarcador_banco", return_value={}),                 mock.patch.object(pipeline.consulta_transportadora, "enviar_aviso", aviso):
            pipeline._registrar_e_avisar_transportadora("PS-1", det, "Rapido Novo", "", 7, {})

    def test_falha_no_envio_nao_levanta_e_pendencia_fica_gravada(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            self._rodar(db, mock.Mock(side_effect=RuntimeError("smtp")))
            conn = pipeline.consulta_transportadora.conectar_consultas(db)
            rows = conn.execute("SELECT pedido, embarcador_cnpj FROM transportadora_pedidos_parados").fetchall()
            conn.close()
            self.assertEqual([(r["pedido"], r["embarcador_cnpj"]) for r in rows], [("PS-1", "stkkc:7")])

    def test_primeiro_aviso_sai_uma_vez(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            aviso = mock.Mock(return_value={})
            self._rodar(db, aviso)
            self.assertEqual(aviso.call_count, 1)
            self.assertFalse(aviso.call_args.kwargs["lembrete"])

    def test_ja_avisado_ontem_nao_reenvia(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            self._rodar(db, mock.Mock(return_value={}))
            conn = pipeline.consulta_transportadora.conectar_consultas(db)
            conn.execute("UPDATE transportadora_consultas SET enviado_em='2000-01-01T08:00:00'")
            conn.commit()
            conn.close()
            aviso = mock.Mock(return_value={})
            self._rodar(db, aviso)
            aviso.assert_not_called()

    def test_falha_ao_abrir_banco_nao_levanta(self):
        with mock.patch.object(pipeline.consulta_transportadora, "conectar_consultas", side_effect=OSError("x")),                 mock.patch.object(pipeline, "_buscar_dados_embarcador_banco", return_value={}):
            pipeline._registrar_e_avisar_transportadora("PS-2", {}, "X", "", 7, {})


class CnpjDocumentoTest(unittest.TestCase):
    def test_13_digitos_vira_14_e_lixo_cai_pra_nome(self):
        cat = _catalogo()
        det = {"transportadora": {"nome": "Nova", "documento": "2345678000190"}}
        self.assertEqual(pipeline._deve_segurar_por_transportadora(det, _res(), None, cat), ("Nova", "02345678000190"))
        det = {"transportadora": {"nome": "Nova", "documento": "123"}}
        self.assertEqual(pipeline._deve_segurar_por_transportadora(det, _res(), None, cat), ("Nova", ""))


if __name__ == "__main__":
    unittest.main()
