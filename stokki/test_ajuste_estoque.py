# -*- coding: utf-8 -*-
"""py -3.11 -m unittest stokki.test_ajuste_estoque"""
import unittest

from stokki import ajuste_estoque as ae


class _Resp:
    def __init__(self, texto="", json_=None, status=200):
        self.status_code, self.text, self._json = status, texto, json_

    def json(self):
        if self._json is None:
            raise ValueError("sem json")
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class _Sessao:
    """GETs respondidos por trecho da URL (na ordem, pra repeticoes); posts guardados."""
    def __init__(self, gets: list):
        self.gets, self.posts = list(gets), []

    def get(self, url, **kw):
        for i, (trecho, resp) in enumerate(self.gets):
            if trecho in url:
                return self.gets.pop(i)[1]
        raise AssertionError(f"GET inesperado: {url}")

    def post(self, url, **kw):
        self.posts.append((url, kw))
        return _Resp('{"success":true}', {"success": True})


_FORM = ('<form id="form_order"><input name="_token" value="TOK"><input name="user_id" value="7"></form>')


def _ajustes(*ids):
    return _Resp(json_={"aaData": [{"id": f"#stkkadj-{i}", "type": "Saída", "state": "Em análise",
                                    "action": f'<a data-type="Outbound" data-id="{i}"></a>'} for i in ids]})


class Numero(unittest.TestCase):
    def test_formatos(self):
        self.assertEqual(ae.numero("1.234,5"), 1234.5)
        self.assertEqual(ae.numero("14 (Em andamento)"), 14)
        self.assertEqual(ae.numero("<b>-2</b>"), -2)
        self.assertEqual(ae.numero(None), 0)


class Planos(unittest.TestCase):
    # Caso real de 27/10: Strogonoff da Padrao Puro (produto 1400)
    LINHAS = [
        {"endereco": "Ajuste", "product_id": 1400, "estoque": 1, "comprometido": 1, "disponivel": 0},
        {"endereco": "CONG1-1-1", "product_id": 1400, "estoque": 10, "comprometido": 0, "disponivel": 10},
        {"endereco": "CONG1-1-1", "product_id": 1401, "estoque": 5, "comprometido": 0, "disponivel": 5},
        {"endereco": "Virtual", "product_id": 1400, "estoque": 14, "comprometido": 14, "disponivel": 0},
    ]
    AJUSTAVEL = {"Ajuste": {1400: 1}, "CONG1-1-1": {1400: 10, 1401: 5}, "Virtual": {}}

    def test_zerar_so_o_produto_pedido_e_virtual_fica_preso(self):
        saidas, presos = ae.plano_zerar(self.LINHAS, self.AJUSTAVEL, {1400})
        self.assertEqual(saidas, {"Ajuste": [(1400, 1)], "CONG1-1-1": [(1400, 10)]})
        self.assertEqual(presos, [("Virtual", 1400, 14, 14)])

    def test_zerar_todos(self):
        saidas, _ = ae.plano_zerar(self.LINHAS, self.AJUSTAVEL, None)
        self.assertEqual(saidas["CONG1-1-1"], [(1400, 10), (1401, 5)])

    def test_contagem_entrada_saida_em_ordem_e_pendente(self):
        saldo = {1: {"disponivel": 10}, 2: {"disponivel": 10}, 3: {"disponivel": 3}}
        ajust = {"A-1": {2: 2}, "B-1": {2: 5, 3: 1}}
        entradas, saidas, pendentes = ae.plano_contagem(saldo, {1: 12, 2: 4, 3: 0}, ["A-1", "B-1"], ajust)
        self.assertEqual(entradas, [(1, 2)])
        self.assertEqual(saidas, {"A-1": [(2, 2)], "B-1": [(2, 4), (3, 1)]})
        self.assertEqual(pendentes, [(3, 2)])

    def test_contagem_produto_fora_do_saldo(self):
        with self.assertRaises(ValueError):
            ae.plano_contagem({}, {9: 1}, ["A-1"], {})

    def test_ler_alvos(self):
        texto = "product_id;alvo\n2279;28\n2282, 192  # moscato\n\n# comentario\n2278\t43\n"
        self.assertEqual(ae.ler_alvos(texto), {2279: 28, 2282: 192, 2278: 43})


class Escrita(unittest.TestCase):
    def test_criar_ajuste_envia_form_e_acha_o_novo(self):
        s = _Sessao([("create/outbound", _Resp(_FORM)), ("adjustment/table", _ajustes(1159)),
                     ("adjustment/table", _ajustes(1160, 1159))])
        aj = ae.criar_ajuste(s, 23, "outbound", 144, [(1400, 10.0)], "teste", dormir=lambda n: None)
        self.assertEqual(aj["id"], 1160)
        url, kw = s.posts[0]
        self.assertTrue(url.endswith("/inventory/adjustment/store"))
        dados = dict(kw["data"])
        self.assertEqual((dados["_token"], dados["user_id"], dados["client_id"], dados["location_id"]),
                         ("TOK", "7", "23", "144"))
        self.assertEqual((dados["product_id[0]"], dados["quantity[0]"], dados["observation"]), ("1400", "10", "teste"))

    def test_criar_ajuste_sem_ajuste_novo_levanta(self):
        s = _Sessao([("create/incoming", _Resp(_FORM)), ("adjustment/table", _ajustes(1159)),
                     ("adjustment/table", _ajustes(1159))])
        with self.assertRaises(RuntimeError):
            ae.criar_ajuste(s, 23, "incoming", 144, [(1400, 1)], "teste", dormir=lambda n: None)

    def test_confirmar_espera_regularizar(self):
        s = _Sessao([("create/outbound", _Resp(_FORM)),
                     ("confirm/check/1160", _Resp(json_={"state": "Processing"})),
                     ("confirm/check/1160", _Resp(json_={"state": "Regularized stock"}))])
        self.assertTrue(ae.confirmar(s, {"id": 1160, "tipo": "Saída", "confirm_type": "Outbound"}, dormir=lambda n: None))
        self.assertEqual(s.posts[0][1]["data"]["type"], "Outbound")

    def test_confirmar_desiste(self):
        s = _Sessao([("create/outbound", _Resp(_FORM))] +
                    [("confirm/check/1", _Resp(json_={"state": "Processing"}))] * 3)
        self.assertFalse(ae.confirmar(s, {"id": 1, "tipo": "Saída"}, dormir=lambda n: None, tentativas=3))


class AcharCliente(unittest.TestCase):
    def test_id_numerico_nao_casa_com_cnpj(self):
        lista = {"clients": [{"id": 23, "select_label": "PADRAO PURO (50.478.796/0001-60)"},
                             {"id": 1, "select_label": "OUTRO (11.111.111/0001-11)"}]}
        orig = ae.listar_embarcadores
        ae.listar_embarcadores = lambda sess: lista
        try:
            self.assertEqual(ae.achar_cliente(None, "1"), [(1, "OUTRO (11.111.111/0001-11)")])
            self.assertEqual([c for c, _ in ae.achar_cliente(None, "padrao")], [23])
        finally:
            ae.listar_embarcadores = orig


if __name__ == "__main__":
    unittest.main()
