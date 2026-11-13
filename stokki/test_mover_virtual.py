# -*- coding: utf-8 -*-
"""py -3.11 -m unittest stokki.test_mover_virtual  (nada aqui fala com a Stokki: a tela e falsa)"""
import unittest

from stokki import mover_virtual as mv

MOTIVO = "Retorno ao estoque do PS-43101 cancelado"


class ApiFalsa:
    """Responde pelos mesmos caminhos que a tela do provider usa."""
    token = "tok"

    def __init__(self, oferecido=12, destinos=None, poll=None, post=None, check=200, falhas=None, produto=None):
        self.falhas = falhas or {}      # prefixo do caminho -> (status, corpo)
        self.produto = produto          # corpo da busca do produto, se for pra mudar
        self.oferecido = oferecido
        self.destinos = {"FAB-1": 223, "FAB-2": 224} if destinos is None else destinos
        self.poll = poll          # lista de respostas do poll; None = concluido de primeira
        self.post_resp = post
        self.check = check
        self.posts, self.gets, self.n_poll = [], [], 0

    def get(self, caminho):
        self.gets.append(caminho)
        for prefixo, resp in self.falhas.items():
            if caminho.startswith(prefixo):
                return resp
        if caminho.startswith("adjust/location/search/"):
            return 200, {"location": {"id": 3, "code": "VIRTUAL"}}
        if caminho.startswith("adjust/destination/search/0/"):
            cod = caminho.rsplit("/", 1)[1]
            if cod in self.destinos:
                return 200, {"location": {"id": self.destinos[cod], "code": cod}}
            return 404, {}
        if caminho.startswith("adjust/product/search/"):
            if self.produto is not None:
                return 200, self.produto
            return 200, {"products": [{"id": 2278, "quantity_available": self.oferecido}]}
        if caminho.startswith("adjust/check/location/"):
            return self.check, {}
        if caminho.startswith("adjust/processed/"):
            resp = self.poll[min(self.n_poll, len(self.poll) - 1)] if self.poll else {"finished": True}
            self.n_poll += 1
            return 200, resp
        raise AssertionError(caminho)

    def post(self, campos):
        self.posts.append(campos)
        if self.post_resp is not None:
            return self.post_resp
        return 200, '{"adjust_id": %d}' % (110000 + len(self.posts))


def mov(qtd=4, destinos=(("FAB-1", 4),)):
    return {"product_id": 2278, "sku": "FABMLT", "qtd": qtd, "destinos": list(destinos)}


def rodar(api, movimentos, ao_concluir=None):
    return mv._executar(api, 100, movimentos, MOTIVO, ao_concluir, dormir=lambda s: None)


class Campos(unittest.TestCase):
    def test_montar_campos(self):
        self.assertEqual(
            mv.montar_campos(100, 3, 2278, 12, 4, 223, "Retorno ao estoque do PS-43101 cancelado"),
            {"client_id": 100, "origin_location_id": 3, "product_id": 2278, "quantity_available": 12, "quantity": 4,
             "destination_location_id": 223, "damaged": 0, "reason": "Retorno ao estoque do PS-43101 cancelado"})


class Executar(unittest.TestCase):
    def test_move_e_confirma_no_poll(self):
        api = ApiFalsa()
        (r,) = rodar(api, [mov()])
        self.assertEqual((r["product_id"], r["destino"], r["pedido"], r["movido"], r["erro"]), (2278, "FAB-1", 4, 4, ""))
        self.assertEqual(r["adjust_id"], 110001)
        (p,) = api.posts
        self.assertEqual((p["quantity"], p["quantity_available"], p["destination_location_id"], p["origin_location_id"]),
                         ("4", "12", "223", "3"))
        self.assertEqual((p["reason"], p["damaged"], p["_token"]), (MOTIVO, "0", "tok"))

    def test_nunca_move_mais_que_a_tela_oferece(self):
        api = ApiFalsa(oferecido=3)
        (r,) = rodar(api, [mov(4, [("FAB-1", 4)])])
        self.assertEqual((r["pedido"], r["movido"]), (4, 3))
        self.assertEqual(api.posts[0]["quantity"], "3")

    def test_destino_nao_achado_tenta_o_proximo(self):
        api = ApiFalsa(destinos={"FAB-2": 224})
        r = rodar(api, [mov(4, [("FAB-1", 4), ("FAB-2", 4)])])
        self.assertEqual([x["destino"] for x in r], ["FAB-1", "FAB-2"])
        self.assertEqual((r[0]["movido"], r[0]["adjust_id"]), (0, None))
        self.assertTrue(r[0]["erro"])
        self.assertEqual(r[1]["movido"], 4)

    def test_destino_com_outro_codigo_e_recusado(self):
        api = ApiFalsa()
        api.destinos = {"FAB-1": 223}
        antigo = api.get

        def get(c):
            if c.startswith("adjust/destination/search/0/"):
                return 200, {"location": {"id": 9, "code": "FAB-10"}}   # busca por aproximacao
            return antigo(c)
        api.get = get
        (r,) = rodar(api, [mov(4, [("FAB-1", 4)])])
        self.assertEqual(r["movido"], 0)
        self.assertEqual(api.posts, [])

    def test_partes_da_origem_somam_e_alternativas_so_levam_o_que_falta(self):
        api = ApiFalsa()
        r = rodar(api, [mov(4, [("FAB-1", 1), ("FAB-2", 3), ("FAB-9", 4)])])
        self.assertEqual([(x["destino"], x["movido"]) for x in r], [("FAB-1", 1), ("FAB-2", 3)])
        self.assertEqual(sum(float(p["quantity"]) for p in api.posts), 4)

    def test_alternativa_so_leva_o_que_falta_depois_de_recusa(self):
        api = ApiFalsa(destinos={"FAB-2": 224, "FAB-9": 229})
        r = rodar(api, [mov(4, [("FAB-1", 1), ("FAB-2", 3), ("FAB-9", 4)])])
        self.assertEqual([(x["destino"], x["movido"]) for x in r], [("FAB-1", 0), ("FAB-2", 3), ("FAB-9", 1)])

    def test_total_nunca_passa_de_qtd(self):
        api = ApiFalsa()
        rodar(api, [mov(4, [("FAB-1", 4), ("FAB-2", 4)])])
        self.assertEqual(len(api.posts), 1)

    def test_poll_que_nao_conclui_guarda_o_adjust_id_e_para_tudo(self):
        api = ApiFalsa(poll=[{"finished": False}])
        r = rodar(api, [mov(4, [("FAB-1", 4), ("FAB-2", 4)]), {**mov(), "product_id": 2282, "sku": "FABMO1"}])
        self.assertEqual(r[0]["movido"], 0)
        self.assertEqual(r[0]["adjust_id"], 110001)
        self.assertIn("ainda em processamento", r[0]["erro"])
        self.assertTrue(r[0]["incerto"])
        self.assertEqual(len(api.posts), 1)          # nem outro destino nem outro produto
        self.assertEqual(len(r), 1)

    def test_post_recusado_com_4xx_tenta_o_proximo_destino(self):
        api = ApiFalsa(post=(422, '{"message": "x"}'))
        r = rodar(api, [mov(4, [("FAB-1", 4), ("FAB-2", 4)])])
        self.assertEqual(len(api.posts), 2)
        self.assertEqual([x["movido"] for x in r], [0, 0])
        self.assertTrue(all(x["adjust_id"] is None for x in r))

    def test_post_200_sem_adjust_id_nao_tenta_outro(self):
        api = ApiFalsa(post=(200, "{}"))
        r = rodar(api, [mov(4, [("FAB-1", 4), ("FAB-2", 4)])])
        self.assertEqual(len(api.posts), 1)
        self.assertEqual(r[0]["movido"], 0)
        self.assertTrue(r[0]["incerto"])

    def test_check_do_destino_recusado_nao_move(self):
        api = ApiFalsa(check=422)
        (r,) = rodar(api, [mov()])
        self.assertEqual((r["movido"], api.posts), (0, []))

    def test_callback_a_cada_resultado(self):
        vistos = []
        r = rodar(ApiFalsa(), [mov(4, [("FAB-1", 1), ("FAB-2", 3)])], ao_concluir=vistos.append)
        self.assertEqual(vistos, r)


class Classificacao(unittest.TestCase):
    def test_virtual_com_401_e_tecnico_e_para(self):
        api = ApiFalsa(falhas={"adjust/location/search/": (401, {})})
        r = rodar(api, [mov(), {**mov(), "product_id": 2282}])
        self.assertEqual(len(r), 1)
        self.assertTrue(r[0]["tecnico"])
        self.assertEqual((r[0]["movido"], api.posts), (0, []))

    def test_busca_do_produto_que_falha_e_tecnica_com_mensagem_verdadeira(self):
        api = ApiFalsa(falhas={"adjust/product/search/": (500, {})})
        (r,) = rodar(api, [mov(4, [("FAB-1", 4), ("FAB-2", 4)])])
        self.assertTrue(r["tecnico"])
        self.assertIn("busca do produto", r["erro"])
        self.assertNotIn("nao oferece saldo", r["erro"])

    def test_destino_404_e_recusa_nao_tecnica(self):
        api = ApiFalsa(destinos={}, falhas={})
        (r,) = rodar(api, [mov()])
        self.assertFalse(r.get("tecnico"))
        self.assertFalse(r.get("incerto"))

    def test_destino_com_401_e_tecnico_e_nao_tenta_o_proximo(self):
        api = ApiFalsa(falhas={"adjust/destination/search/": (401, {})})
        r = rodar(api, [mov(4, [("FAB-1", 4), ("FAB-2", 4)])])
        self.assertEqual(len(r), 1)
        self.assertTrue(r[0]["tecnico"])

    def test_post_com_401_e_tecnico(self):
        r = rodar(ApiFalsa(post=(401, "x")), [mov(4, [("FAB-1", 4), ("FAB-2", 4)])])
        self.assertEqual(len(r), 1)
        self.assertTrue(r[0]["tecnico"])

    def test_post_com_5xx_e_incerto_e_para(self):
        api = ApiFalsa(post=(500, "erro"))
        r = rodar(api, [mov(4, [("FAB-1", 4), ("FAB-2", 4)]), {**mov(), "product_id": 2282}])
        self.assertEqual(len(r), 1)
        self.assertTrue(r[0]["incerto"])
        self.assertEqual((r[0]["movido"], r[0]["adjust_id"], len(api.posts)), (0, None, 1))

    def test_adjust_id_em_texto_vira_int(self):
        (r,) = rodar(ApiFalsa(post=(200, '{"adjust_id": "110832"}')), [mov()])
        self.assertEqual((r["adjust_id"], r["movido"]), (110832, 4))

    def test_adjust_id_ilegivel_e_incerto(self):
        (r,) = rodar(ApiFalsa(post=(200, '{"adjust_id": "abc"}')), [mov()])
        self.assertTrue(r["incerto"])
        self.assertEqual((r["movido"], r["adjust_id"]), (0, None))

    def test_tela_oferecendo_menos_move_o_oferecido_e_encerra_o_item(self):
        api = ApiFalsa(oferecido=3)
        r = rodar(api, [mov(4, [("FAB-1", 4), ("FAB-2", 4)])])
        self.assertEqual(len(api.posts), 1)
        self.assertEqual(api.posts[0]["quantity"], "3")
        self.assertEqual([(x["destino"], x["movido"]) for x in r], [("FAB-1", 3)])

    def test_destino_sem_nenhuma_chave_de_texto_e_recusado_e_loga_as_chaves(self):
        api = ApiFalsa()
        antigo = api.get

        def get(c):
            if c.startswith("adjust/destination/search/0/"):
                return 200, {"location": {"id": 9, "xyz": "FAB-1"}}
            return antigo(c)
        api.get = get
        with self.assertLogs("stokki.mover_virtual", level="WARNING") as cap:
            (r,) = rodar(api, [mov()])
        self.assertEqual((r["movido"], api.posts), (0, []))
        self.assertIn("xyz", "".join(cap.output))

    def test_quantity_available_zero_nao_cai_para_quantity(self):
        api = ApiFalsa(produto={"products": [{"id": 2278, "quantity_available": 0, "quantity": 12}]})
        (r,) = rodar(api, [mov()])
        self.assertEqual((r["movido"], api.posts), (0, []))

    def test_sem_quantity_available_usa_quantity(self):
        api = ApiFalsa(produto={"products": [{"id": 2278, "quantity": 12}]})
        (r,) = rodar(api, [mov()])
        self.assertEqual(r["movido"], 4)


if __name__ == "__main__":
    unittest.main()
