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
        self.assertIn("g.nivel_acesso in ('total', 'operador')", _ESTOQUE)

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


class TestCaixasNaTela(unittest.TestCase):
    """Task 8: caixa com serial na tela do galpao (inventario, receber, consultar)."""

    def test_aba_inventario_existe(self):
        self.assertIn('data-aba="inventario"', _WMS)

    def test_ativacao_chama_a_rota_da_caixa(self):
        self.assertIn("'/caixas/' + ", _WMS)
        self.assertIn("/ativar'", _WMS)

    def test_receber_respeita_usa_caixas(self):
        self.assertIn("it.usa_caixas", _WMS)

    def test_fila_offline_reenvia_ativacao(self):
        self.assertIn("ativar_caixa", _WMS)

    def test_consultar_trata_caixa(self):
        self.assertIn("r.tipo === 'caixa'", _WMS)
        self.assertIn("caixa_inexistente", _WMS)

    def test_fila_nao_reenvia_recusado_pra_sempre(self):
        # recusa do servidor tira da fila; so falha de rede mantem
        self.assertIn("filaRecusados", _WMS)

    def test_separar_aceita_etiqueta_de_caixa(self):
        self.assertIn("function caixaComoLote", _WMS)
        self.assertIn("r = caixaComoLote(r);", _WMS)

    def test_mover_reusa_o_uuid_no_retry(self):
        self.assertIn("ctx.mover.uuid", _WMS)

    def test_produto_fora_do_piloto_nao_pede_etiqueta(self):
        self.assertIn("Produto fora do piloto: use Movimentos.", _WMS)

    def test_inventario_respeita_a_chave_caixas_ativo(self):
        self.assertIn("BOOT.caixas_ativo", _WMS)
        self.assertIn("Caixas desligadas", _WMS)


class TestTelaEtiquetar(unittest.TestCase):
    """Etiqueta de volume de todo pedido da Stokki (28/11): a tela Etiquetar entra pelo Inicio
    (escolha do Hugo: botao no Inicio, tudo numa tela) e fala com /api/wms/etiquetar/*."""

    def test_tela_etiquetar_usa_as_rotas_novas(self):
        self.assertIn("TELAS.etiquetar", _WMS)
        self.assertIn('data-ir="etiquetar"', _WMS)
        for rota in ("/etiquetar/pedido", "/etiquetar/reimprimir", "/etiquetar/prontos"):
            self.assertIn(rota, _WMS)
        self.assertIn("confirmado: true", _WMS)

    def test_nao_virou_aba_na_barra_de_baixo(self):
        self.assertNotIn('data-aba="etiquetar"', _WMS)

    def test_pedido_desconhecido_avisa_mas_deixa_imprimir(self):
        self.assertIn("Não achei esse pedido no nosso sistema", _WMS)

    def test_pdf_abre_direto_depois_de_imprimir(self):
        """Hugo, 28/11: a tela e usada no computador; o PDF abre na hora e, se o navegador
        bloquear a aba, o botao 'Abrir PDF' fica na tela. Abrir marca o trabalho como impresso."""
        self.assertIn("window.open(API + '/etiquetar/pdf/'", _WMS)
        self.assertIn("/etiquetar/impresso", _WMS)
        self.assertIn("Abrir PDF", _WMS)

    def test_bipe_sem_foco_no_campo_continua_na_tela_etiquetar(self):
        """Revisao final: no celular o campo nao ganha foco sozinho e o leitor cai no handler
        global (lerCodigo), que levava o bipe pra Consultar ou pro cadastro de produto."""
        self.assertIn("S.tela === 'etiquetar') && S.ctx.bipar) return S.ctx.bipar(codigo);", _WMS)
        self.assertIn("S.ctx.bipar = buscar;", _WMS)

    def test_imprimir_guarda_o_pedido_antes_de_esperar_o_servidor(self):
        """Revisao final: com rede lenta o operador bipava o pedido B enquanto o POST do A estava
        no ar, e o reenvio confirmado ia com o codigo do B e o total do A."""
        self.assertIn("const codigo = atual.codigo", _WMS)
        self.assertNotIn("codigo: atual.codigo, total: total", _WMS)
        self.assertIn("{ codigo: codigo, total: total, confirmado: true }", _WMS)


if __name__ == "__main__":
    unittest.main()
