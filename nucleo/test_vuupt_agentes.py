"""Testes do cliente Vuupt de agentes/veiculos (sem rede: sessao falsa)."""
import unittest

import requests

from nucleo import vuupt_agentes as va


class Resp:
    def __init__(self, status=200, corpo=None, texto=None):
        self.status_code = status
        self._corpo = corpo if corpo is not None else {}
        import json
        self.text = texto if texto is not None else json.dumps(self._corpo)

    def json(self):
        return self._corpo


class Sessao:
    def __init__(self, respostas):
        self.respostas = list(respostas)
        self.chamadas = []

    def _resp(self, metodo, url, **kw):
        self.chamadas.append((metodo, url, kw))
        r = self.respostas.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def get(self, url, **kw):
        return self._resp("GET", url, **kw)

    def post(self, url, **kw):
        return self._resp("POST", url, **kw)

    def put(self, url, **kw):
        return self._resp("PUT", url, **kw)

    def delete(self, url, **kw):
        return self._resp("DELETE", url, **kw)


def cliente(*respostas):
    s = Sessao(respostas)
    return va.VuuptAgentes("tok", sessao=s), s


class TestVuuptAgentes(unittest.TestCase):
    def test_gerar_email(self):
        d = "@freshhub.com.br"
        self.assertEqual(va.gerar_email("João Pedro da Silva", set()), "jps" + d)
        self.assertEqual(va.gerar_email("João Pedro da Silva", {"jps" + d}), "jps2" + d)
        self.assertEqual(va.gerar_email("João Pedro da Silva", {"jps" + d, "jps2" + d}), "jps3" + d)
        self.assertEqual(va.gerar_email("MARIA  DE   SOUZA e LIMA", set()), "msl" + d)
        self.assertEqual(va.gerar_email("Ângela Íris", set()), "ai" + d)

    def test_telefone(self):
        self.assertEqual(va.telefone_e164("11998765432"), "+5511998765432")
        self.assertTrue(va.mesmo_telefone("+55 11 99876-5432", "11998765432"))
        self.assertFalse(va.mesmo_telefone("", ""))

    def test_criar_agente_payload(self):
        c, s = cliente(Resp(201, {"user": {"id": 777}}), Resp(201, {"data": {"id": 778}}))
        self.assertEqual(c.criar_agente("Joao", "j@freshhub.com.br", "123456", "11998765432"), 777)
        metodo, url, kw = s.chamadas[0]
        self.assertEqual(metodo, "POST")
        self.assertTrue(url.endswith("/users"))
        j = kw["json"]
        self.assertEqual(j["is_driver"], "Y")
        self.assertEqual(j["is_admin"], "N")
        self.assertEqual(j["is_user"], "N")
        self.assertEqual(j["is_user_readonly"], "N")
        self.assertEqual(j["phone_number"], "+5511998765432")
        self.assertEqual(j["password"], "123456")
        self.assertEqual(kw["headers"]["Authorization"], "Bearer tok")
        self.assertEqual(c.criar_agente("Joao", "j@freshhub.com.br", "123456", "11998765432"), 778)

    def test_criar_veiculo(self):
        c, s = cliente(Resp(201, {"vehicle": {"id": 55}}), Resp(201, {"id": 56}))
        self.assertEqual(c.criar_veiculo("ABC1D23", 5, 777), 55)
        j = s.chamadas[0][2]["json"]
        self.assertEqual(j["license_plate"], "ABC1D23")
        self.assertEqual(j["code"], "ABC1D23")
        self.assertEqual(j["operational_base_id"], 5)
        self.assertEqual(j["agentsAllowed"], [777])
        self.assertEqual(c.criar_veiculo("ABC1D23", None, 777), 56)
        self.assertNotIn("operational_base_id", s.chamadas[1][2]["json"])

    def test_ligar_e_apagar(self):
        c, s = cliente(Resp(200), Resp(204), Resp(204))
        c.ligar_veiculo(777, 55)
        self.assertEqual(s.chamadas[0][0], "PUT")
        self.assertTrue(s.chamadas[0][1].endswith("/users/777"))
        self.assertEqual(s.chamadas[0][2]["json"], {"vehicle_id": 55})
        c.apagar_agente(777)
        c.apagar_veiculo(55)
        self.assertEqual(s.chamadas[1][0], "DELETE")
        self.assertTrue(s.chamadas[2][1].endswith("/vehicles/55"))

    def test_erro_vira_ErroVuupt(self):
        c, _ = cliente(Resp(422, {"errors": {"email": ["taken"]}}))
        with self.assertRaises(va.ErroVuupt) as ctx:
            c.criar_agente("J", "j@x", "123456", "11998765432")
        self.assertEqual(ctx.exception.status, 422)
        self.assertIn("email", ctx.exception.corpo)

    def test_erro_de_rede(self):
        c, _ = cliente(requests.ConnectionError("caiu"))
        with self.assertRaises(va.ErroVuupt) as ctx:
            c.listar_agentes()
        self.assertIsNone(ctx.exception.status)
        self.assertIn("caiu", ctx.exception.corpo)

    def test_paginacao(self):
        meta = {"pagination": {"total_pages": 2}}
        c, s = cliente(Resp(200, {"data": [{"id": 1}], "meta": meta}),
                       Resp(200, {"data": [{"id": 2}], "meta": meta}))
        self.assertEqual(c.listar_agentes(), [{"id": 1}, {"id": 2}])
        self.assertEqual(s.chamadas[1][2]["params"]["page"], 2)

    def test_base_operacional(self):
        v = [{"operational_base_id": 5}, {"operational_base_id": 5},
             {"operational_base_id": 9}, {}]
        self.assertEqual(va.base_operacional_mais_comum(v), 5)
        self.assertIsNone(va.base_operacional_mais_comum([]))


if __name__ == "__main__":
    unittest.main()
