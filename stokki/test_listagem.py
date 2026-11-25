# -*- coding: utf-8 -*-
"""Listagem nova da Stokki (24/11/2026): a linha em JSON estruturado tem que
chegar nos consumidores no formato de antes. As linhas abaixo sao copia do
que a producao devolveu em 24/11 (so os itens de 'actions' foram encurtados).

    py -3.11 -m unittest stokki.test_listagem
"""
import re
import unittest

from stokki import listagem, pedidos, recebimentos, trocar_nf

XML = "users/clients/inventories/outbounds/invoices/ABS672LJB9V2p5fR6XwGtvMqlTRHUyDw3fURHrWd.xml"


def linha_saida(**troca) -> dict:
    linha = {
        "id": 45080, "reference": "#PS-45080",
        "show_url": "https://freshlog.stokki.com.br/pt-br/administrator/inventory/outbound/show/45080",
        "references": [{"label": "REF", "value": "390512"}],
        "order_date": None, "created_at": {"date": "24/11/2026", "time": "15:49"},
        "expedition_date": {"date": "25/11/2026", "time": None},
        "type": "Saída", "motion": "Venda",
        "client": {"name": "MAGAZZINO DISTRIB. DE AL IMENTOS LTDA - Grupo Trigo", "code": "#stkkc-86"},
        "origin": {"name": "Freshlog", "place": "São Paulo, SP"},
        "destination": {"name": "J K RESTAURANTE ARACATUBA LTDA", "place": "ARACATUBA, SP"},
        "carrier": {"name": "FRESHLOG", "document": "54.039.899/0001-85"},
        "markers": [{"label": "Entrega Expressa", "color": "express", "icon": "bolt", "title": None},
                    {"label": "Aguardando Faturamento", "color": "danger", "icon": "file-invoice-dollar", "title": None}],
        "state": {"key": "On hold", "label": "Em espera", "color": "danger", "icon": "hand-paper", "on_hold": True},
        "actions": {"button": {"label": "Opções", "tone": "default", "opens": None}, "items": [
            {"key": "view", "label": "Ver Pedido", "icon": "eye", "class": None, "blank": False, "data": [],
             "url": "https://freshlog.stokki.com.br/pt-br/administrator/inventory/outbound/show/45080"},
            {"key": "expenses", "label": "Adicionar Despesas Extras", "icon": "file-import", "class": "btn_expense",
             "url": None, "blank": False, "data": {"id": 45080, "warehouse_id": 1, "client_id": 86}},
            {"key": "danfe", "label": "Gerar DANFE", "icon": "file-download", "class": "btn_danfe", "url": None,
             "blank": False, "data": {"url": XML}}]},
        "selection": {"cancel": True},
    }
    linha.update(troca)
    return linha


def linha_entrada(**troca) -> dict:
    linha = {
        "id": 2661, "reference": "#PE-2661",
        "show_url": "https://freshlog.stokki.com.br/pt-br/administrator/inventory/incoming/show/2661",
        "references": [{"label": "REF", "value": "42509"}],
        "created_at": {"date": "23/11/2026", "time": "09:52"},
        "arrival_date": {"date": "23/11/2026", "time": None}, "receipt_date": None,
        "type": "Entrada", "motion": "Remessa",
        "client": {"name": "MARIA DOLORES INDUSTRIA E COMERCIO DE ALIMENTOS LTDA", "code": "#stkkc-48"},
        "origin": {"name": "Maria Dolores Comercio de Alimentos Ltda", "place": "Patos de Minas, MG"},
        "destination": {"name": "Freshlog", "place": "São Paulo, SP"},
        "markers": [],
        "state": {"key": "In transit", "label": "Em trânsito", "color": "secondary", "icon": "truck", "on_hold": False},
        "actions": {"button": {"label": "Opções"}, "items": []},
        "selection": {"archive": False, "cancel": True},
    }
    linha.update(troca)
    return linha


def _sem_tags(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


class TestLinhaDeSaida(unittest.TestCase):
    def setUp(self):
        self.linha = listagem.converter_linha(linha_saida())

    def test_id_codigo_e_referencia_saem_pelos_extratores_de_sempre(self):
        self.assertEqual(pedidos.extrair_id_da_linha(self.linha), 45080)
        self.assertEqual(pedidos.extrair_codigo_ps_da_linha(self.linha), "PS-45080")
        self.assertEqual(pedidos.extrair_referencia_da_linha(self.linha), "390512")
        self.assertIn("#PS-45080", self.linha["id"])

    def test_cliente_traz_o_stkkc_e_o_nome(self):
        self.assertEqual(re.search(r"#stkkc-(\d+)", self.linha["client"]).group(1), "86")
        self.assertIn("MAGAZZINO", _sem_tags(self.linha["client"]))

    def test_situacao_e_o_rotulo_em_portugues(self):
        self.assertEqual(self.linha["state"], "Em espera")

    def test_marcadores_viram_texto_que_o_aviso_de_espera_reconhece(self):
        self.assertIn("aguardando faturamento", _sem_tags(self.linha["marker"]).lower())

    def test_transportadora_destino_e_data(self):
        self.assertTrue(pedidos.extrair_transportadora(self.linha).startswith("FRESHLOG"))
        self.assertEqual(re.sub(r"<[^>]+>", "", self.linha["destination"]).split("\n")[0],
                         "J K RESTAURANTE ARACATUBA LTDA")
        self.assertEqual(self.linha["expedition_date"], "25/11/2026")

    def test_sem_transportadora_fica_vazio(self):
        self.assertEqual(pedidos.extrair_transportadora(listagem.converter_linha(linha_saida(carrier=None))), "")

    def test_troca_de_nf_acha_o_caminho_do_xml_e_a_nf_no_texto(self):
        self.assertEqual(trocar_nf._caminho_da_linha(self.linha), XML)
        self.assertRegex(trocar_nf._texto_linha(self.linha), r"(?<!\d)390512(?!\d)")

    def test_ids_soltos_das_acoes_nao_entram_no_texto(self):
        # warehouse_id/client_id do JSON novo nao podem casar com numero de NF
        self.assertNotIn("warehouse", self.linha["action"])
        self.assertNotIn("client_id", self.linha["action"])

    def test_so_as_chaves_antigas(self):
        for nova in ("reference", "references", "show_url", "markers", "actions", "selection"):
            self.assertNotIn(nova, self.linha)
        for antiga in ("checkbox", "id", "expedition_date", "type", "motion", "client", "origin",
                       "destination", "carrier", "marker", "state", "action"):
            self.assertIsInstance(self.linha[antiga], str)


class TestLinhaDeEntrada(unittest.TestCase):
    def setUp(self):
        self.linha = listagem.converter_linha(linha_entrada())

    def test_extratores_do_recebimento(self):
        self.assertEqual(recebimentos.extrair_id_da_linha(self.linha), 2661)
        self.assertEqual(recebimentos.extrair_codigo_da_linha(self.linha), "#PE-2661")
        self.assertEqual(recebimentos.extrair_ref_da_linha(self.linha), "42509")

    def test_cabecalho(self):
        cab = recebimentos.extrair_cabecalho_da_linha(self.linha)
        self.assertEqual((cab["id_stokki"], cab["codigo"], cab["situacao"], cab["chegada"]),
                         (2661, "#PE-2661", "Em trânsito", "23/11/2026"))
        self.assertIn("#stkkc-48", self.linha["client"])

    def test_sem_referencia(self):
        self.assertEqual(recebimentos.extrair_ref_da_linha(listagem.converter_linha(linha_entrada(references=[]))), "")


class TestFormatoAntigoPassaDireto(unittest.TestCase):
    def test_linha_antiga_nao_e_tocada(self):
        antiga = {"id": '<a href="/pt-br/administrator/inventory/outbound/show/7">#PS-7</a>', "state": "Aberto"}
        self.assertIs(listagem.converter_linha(antiga), antiga)
        self.assertEqual(listagem.converter_linha(["a", "b"]), ["a", "b"])


class _Resp:
    def __init__(self, status=200, corpo=None):
        self.status_code, self._corpo, self.text = status, corpo, ""

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._corpo


class _Sessao:
    def __init__(self, resp):
        self.resp, self.urls = resp, []

    def get(self, url, params=None, headers=None, **kw):
        self.urls.append(url)
        return self.resp


class TestChamada(unittest.TestCase):
    def test_pedidos_usa_o_endereco_novo_e_devolve_no_formato_antigo(self):
        sess = _Sessao(_Resp(corpo={"iTotalRecords": 1, "iTotalDisplayRecords": 1, "aaData": [linha_saida()]}))
        dados = pedidos.listar_pedidos(sess)
        self.assertTrue(sess.urls[0].endswith("/inventory/outbound/listing/table"))
        self.assertEqual(pedidos.extrair_id_da_linha(dados["aaData"][0]), 45080)
        self.assertEqual(dados["iTotalDisplayRecords"], 1)

    def test_recebimentos_usa_o_endereco_novo(self):
        sess = _Sessao(_Resp(corpo={"aaData": [linha_entrada()]}))
        dados = recebimentos.listar_recebimentos(sess, cliente="48")
        self.assertTrue(sess.urls[0].endswith("/inventory/incoming/listing/table"))
        self.assertEqual(recebimentos.extrair_codigo_da_linha(dados["aaData"][0]), "#PE-2661")

    def test_recusa_da_stokki_vira_erro_com_o_motivo(self):
        corpo = {"message": "The given data was invalid.",
                 "errors": {"state": ["O campo Estado selecionado é inválido."]}}
        with self.assertRaises(listagem.ErroListagemStokki) as ctx:
            pedidos.listar_pedidos(_Sessao(_Resp(422, corpo)), status="Sent")
        self.assertIn("Estado selecionado é inválido", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
