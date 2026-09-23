# -*- coding: utf-8 -*-
"""
test_wms_telas.py

Guardas das correcoes que moram no JS da tela do galpao (wms.html) e da
tela da equipe (wms_estoque.html), da revisao final da fase 2.

Nao ha harness de JS neste projeto -- estes testes conferem a LIGACAO
(qual dado a tela manda e com que criterio ela casa a linha), nao o
comportamento renderizado. E pouco, mas e o que impede o bug de voltar
sem ninguem notar: as tres correcoes abaixo eram exatamente "a tela
manda o campo errado".

Rodar (da raiz):
    py -3.11 -m unittest painel_agentes.test_wms_telas -v
"""
import sys
import tempfile
import unittest
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(Path(__file__).parent))

import wms_pedidos  # noqa: E402

_TEMPLATES = Path(__file__).parent / "templates"
_WMS = (_TEMPLATES / "wms.html").read_text(encoding="utf-8")
_ESTOQUE = (_TEMPLATES / "wms_estoque.html").read_text(encoding="utf-8")


class TestSeparacaoCasaPelaLinha(unittest.TestCase):
    """I11: `sep.find(s => produtos.some(p => p.id === s.produto_id))` casava
    por PRODUTO. O mesmo SKU aparece em varias linhas do pedido (a linha e
    por embalagem, spec secao 11) -- bipar trocava o lote da linha errada."""

    def test_nao_casa_mais_pela_primeira_reserva_do_produto(self):
        self.assertNotIn("sep.find((s) => r.produtos.some", _WMS)

    def test_usa_a_linha_escolhida_pelo_operador_pra_desempatar(self):
        self.assertIn("ctx.reservaSel", _WMS)
        self.assertIn("candidatos.find((s) => s.id === ctx.reservaSel)", _WMS)


class TestConfirmacaoDoLoteNaTela(unittest.TestCase):
    """I4: bipar o lote sugerido e confirmacao -- a tela precisa saber
    diferenciar (a API devolve reserva.confirmada)."""

    def test_tela_mostra_confirmado_em_vez_de_trocado(self):
        self.assertIn("resp.reserva && resp.reserva.confirmada", _WMS)
        self.assertIn("Lote confirmado", _WMS)


class TestEnderecarMandaOUuid(unittest.TestCase):
    """I6: a contabilizacao e uma segunda chamada HTTP; sem o uuid do
    movimento ela nao tem como ser idempotente."""

    def test_o_mesmo_uuid_vai_na_entrada_e_na_contabilizacao(self):
        self.assertIn("uuid: idMov", _WMS)
        self.assertIn("body: { qtd, uuid: idMov }", _WMS)


class TestTelaDaEquipeMostraOsDoisTiposDePendencia(unittest.TestCase):
    """I7 e I12 na tela da equipe."""

    def test_pendencia_mostra_tipo_e_motivo(self):
        self.assertIn("p.tipo_pendencia", _ESTOQUE)
        self.assertIn("p.motivo", _ESTOQUE)

    def test_pedido_com_saldo_negativo_ganha_aviso(self):
        self.assertIn("p.saldo_negativo", _ESTOQUE)


class TestTelaDaEquipeMostraReservaAntiga(unittest.TestCase):
    """Frente 2: reserva ATIVA parada ha dias e o sintoma visivel de baixa
    que nunca veio -- a tela tem que buscar a lista e dizer a idade."""

    def test_tela_busca_as_reservas_antigas(self):
        self.assertIn("api_wms_reservas_antigas", _ESTOQUE)
        self.assertIn("tab-antigas", _ESTOQUE)

    def test_mostra_idade_e_o_que_esta_preso(self):
        self.assertIn("p.dias", _ESTOQUE)
        self.assertIn("i.posicao", _ESTOQUE)


class TestBotaoLiberarReserva(unittest.TestCase):
    """Frente 3: a acao e de escrita -- so nivel de equipe interna ve o
    botao, o motivo e obrigatorio e o POST vai pra rota certa."""

    def test_botao_so_aparece_pra_equipe_interna(self):
        self.assertIn("session.get('nivel_acesso') in ('total', 'operador')", _ESTOQUE)

    def test_pede_motivo_e_nao_manda_vazio(self):
        self.assertIn("prompt(", _ESTOQUE)
        self.assertIn("O motivo é obrigatório", _ESTOQUE)

    def test_posta_na_rota_de_liberar(self):
        self.assertIn("api_wms_liberar_reservas", _ESTOQUE)
        self.assertIn("JSON.stringify({ motivo: motivo.trim() })", _ESTOQUE)


class TestUnidadeDoItemDoRecebimento(unittest.TestCase):
    """M7: `it.unidade` era sempre undefined -- wms_recebimento_itens nao
    tem essa coluna. A unidade vem do produto do catalogo."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = wms_pedidos.conectar(Path(self._tmp.name) / "dados.db")
        self.conn.execute(
            "INSERT INTO wms_produtos (id, stokki_id, sku, descricao, embarcador, unidade, "
            "qtd_por_caixa, atualizado_em) VALUES (1, 900, 'SKU1', 'PRODUTO 1', 'MARIA DOLORES', "
            "'CX', 1, '2026-09-21 10:00:00')")
        self.conn.commit()
        self.rid = wms_pedidos.registrar_recebimento(
            self.conn, {"id_stokki": 2490, "codigo": "#PE-2490", "embarcador": "MARIA DOLORES",
                        "situacao": "Recebido", "chegada": "22/09/2026"},
            [{"linha": 1, "sku": "SKU1", "ean_linha": "", "descricao": "PRODUTO 1",
              "qtd_embalagem": 4, "lote": "L-A", "validade": "18/02/2027"},
             {"linha": 2, "sku": "FANTASMA", "ean_linha": "", "descricao": "SEM CATALOGO",
              "qtd_embalagem": 1, "lote": "", "validade": ""}])
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def test_unidade_vem_do_produto(self):
        itens = wms_pedidos.itens_do_recebimento(self.conn, self.rid)
        self.assertEqual(itens[0]["unidade"], "CX")

    def test_item_sem_produto_no_catalogo_cai_pra_un(self):
        itens = wms_pedidos.itens_do_recebimento(self.conn, self.rid)
        self.assertIsNone(itens[1]["produto_id"])
        self.assertEqual(itens[1]["unidade"], "UN")


if __name__ == "__main__":
    unittest.main()
