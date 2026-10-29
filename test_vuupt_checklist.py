# -*- coding: utf-8 -*-
"""
Testes do vuupt_checklist (checklist de entrega com foto por API) e do
VuuptClient.concluir_como_agente com fotos. Não bate na Vuupt: a sessão é
falsa e registra as chamadas.
Rodar: py -3.11 -m unittest test_vuupt_checklist
"""
import io
import unittest

from PIL import Image

import vuupt_checklist as vc
from vuupt_client import VuuptClient


class Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload
        self.text = "x" if payload is not None else ""
        self.ok = status_code < 400

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class SessaoFalsa:
    """Registra (metodo, url, kwargs) e responde com respostas montadas."""

    def __init__(self, listagem=None, finish_status=200, gravar_arquivo=True):
        self.chamadas = []
        self.listagem = listagem          # lista de checklists que o GET devolve
        self.finish_status = finish_status
        self.gravar_arquivo = gravar_arquivo
        self.enviados = []                # campo_id enviados
        self.headers = {}

    def get(self, url, **kw):
        self.chamadas.append(("GET", url, kw))
        if url.endswith("/checklists"):
            if self.listagem is not None:
                return Resp(200, {"data": self.listagem})
            campos = [{"checklist_form_field_id": c,
                       "file_url": ("http://x/%d.jpg" % c) if self.gravar_arquivo else None}
                      for c in self.enviados]
            return Resp(200, {"data": [{"id": 777, "checklist_form_id": 1531,
                                        "images_quantity": len(self.enviados),
                                        "fields": {"data": campos}}]})
        return Resp(200, {"service": {"id": 5, "type": "delivery"}})

    def post(self, url, **kw):
        self.chamadas.append(("POST", url, kw))
        if url.endswith("/checklists"):
            return Resp(200, {"checklist_answer": {"id": 777}})
        self.enviados.append(int(kw["data"]["fields[0][id]"]))
        return Resp(200, {})

    def put(self, url, **kw):
        self.chamadas.append(("PUT", url, kw))
        if url.endswith("/finish"):
            return Resp(self.finish_status, {"message": "images_quantity (1) <> processed images (2)"})
        return Resp(200, {})


def vuupt_falso(sessao):
    v = VuuptClient(token="token-fake")
    v.session = sessao
    return v


SERV = {"id": 5, "type": "delivery", "latitude": "-23.5", "longitude": "-46.6"}


def _img_bytes(fmt="JPEG"):
    buf = io.BytesIO()
    Image.new("RGB", (20, 20), "red").save(buf, fmt)
    return buf.getvalue()


class ChecklistTest(unittest.TestCase):
    def test_cria_cabecalho_com_valores_fixos_e_utc(self):
        s = SessaoFalsa()
        vc.preencher_checklist_entrega(vuupt_falso(s), SERV, {15086: b"jpg"})
        post = next(c for c in s.chamadas if c[0] == "POST" and c[1].endswith("/checklists"))
        d = post[2]["data"]
        self.assertEqual(d["checklist_form_id"], 1531)
        self.assertEqual(d["checklist_rule_id"], 1526)
        self.assertEqual(d["entity_id"], 5)
        self.assertEqual(d["trigger"], "finished_service")
        self.assertEqual(d["images_quantity"], 1)
        self.assertEqual(d["latitude"], "-23.5")
        self.assertRegex(d["gps_date_time"], r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$")
        self.assertRegex(d["filled_at"], r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$")
        self.assertIsNone(post[2]["headers"]["Content-Type"])

    def test_foto_vai_na_parte_fields_value(self):
        s = SessaoFalsa()
        vc.preencher_checklist_entrega(vuupt_falso(s), SERV, {15086: b"jpg", 15349: b"jpg2"})
        envios = [c for c in s.chamadas if c[0] == "POST" and c[1].endswith("/checklists/777")]
        self.assertEqual(len(envios), 2)
        for e in envios:
            self.assertIn("fields[0][value]", e[2]["files"])
            self.assertEqual(e[2]["data"]["fields[0][file_source]"], "gallery")
            self.assertEqual(e[2]["data"]["checklist_form_id"], 1531)
            self.assertIsNone(e[2]["headers"]["Content-Type"])
            self.assertTrue(e[2]["data"]["fields[0][content_uuid]"])
        self.assertEqual(s.enviados, [15086, 15349])

    def test_finish_e_put(self):
        s = SessaoFalsa()
        cid = vc.preencher_checklist_entrega(vuupt_falso(s), SERV, {15086: b"jpg"})
        self.assertEqual(cid, 777)
        self.assertTrue(any(c[0] == "PUT" and c[1].endswith("/checklists/777/finish") for c in s.chamadas))

    def test_finish_409_vira_checklist_nao_gravado(self):
        s = SessaoFalsa(finish_status=409)
        with self.assertRaises(vc.ChecklistNaoGravado):
            vc.preencher_checklist_entrega(vuupt_falso(s), SERV, {15086: b"jpg"})

    def test_foto_sem_file_url_na_releitura_falha(self):
        s = SessaoFalsa(gravar_arquivo=False)
        with self.assertRaises(vc.ChecklistNaoGravado):
            vc.preencher_checklist_entrega(vuupt_falso(s), SERV, {15086: b"jpg"})

    def test_reaproveita_checklist_existente_do_servico(self):
        existente = {"id": 900, "checklist_form_id": 1531, "images_quantity": 1,
                     "fields": [{"checklist_form_field_id": "15086", "file_url": None}]}
        s = SessaoFalsa(listagem=[existente])
        orig_get = s.get

        def get(url, **kw):
            r = orig_get(url, **kw)
            if len([c for c in s.chamadas if c[0] == "GET"]) > 1:
                existente["fields"][0]["file_url"] = "http://x/a.jpg"   # a releitura ja mostra a foto
            return r
        s.get = get
        cid = vc.preencher_checklist_entrega(vuupt_falso(s), SERV, {15086: b"jpg"})
        self.assertEqual(cid, 900)
        self.assertFalse(any(c[0] == "POST" and c[1].endswith("/checklists") for c in s.chamadas))
        self.assertTrue(any(c[1].endswith("/checklists/900") for c in s.chamadas))

    def test_existente_com_quantidade_diferente_cria_novo(self):
        existente = {"id": 900, "checklist_form_id": 1531, "images_quantity": 2, "fields": []}
        s = SessaoFalsa(listagem=[existente])
        with self.assertRaises(vc.ChecklistNaoGravado):   # a releitura (fake) so devolve o 900, sem arquivo
            vc.preencher_checklist_entrega(vuupt_falso(s), SERV, {15086: b"jpg"})
        self.assertTrue(any(c[0] == "POST" and c[1].endswith("/checklists") for c in s.chamadas))

    def test_existente_incompativel_cria_novo_e_conclui(self):
        # velho: qtd 1, 15086 ja enviado (finish falhou); retentativa manda so 15349
        velho = {"id": 900, "checklist_form_id": 1531, "images_quantity": 1,
                 "fields": [{"checklist_form_field_id": 15086, "file_url": "http://x/a.jpg"}]}
        s = SessaoFalsa(listagem=[velho])
        orig_post = s.post

        def post(url, **kw):
            r = orig_post(url, **kw)
            if url.endswith("/checklists/777"):
                s.listagem.append({"id": 777, "checklist_form_id": 1531, "images_quantity": 1,
                                   "fields": {"data": [{"checklist_form_field_id": 15349,
                                                        "file_url": "http://x/b.jpg"}]}})
            return r
        s.post = post
        cid = vc.preencher_checklist_entrega(vuupt_falso(s), SERV, {15349: b"jpg"})
        self.assertEqual(cid, 777)
        self.assertTrue(any(c[0] == "POST" and c[1].endswith("/checklists") for c in s.chamadas))
        self.assertFalse(any(c[1].endswith("/checklists/900") for c in s.chamadas))

    def _completo(self, finish_status):
        ck = {"id": 900, "checklist_form_id": 1531, "images_quantity": 1,
              "fields": [{"checklist_form_field_id": 15086, "file_url": "http://x/a.jpg"}]}
        return SessaoFalsa(listagem=[ck], finish_status=finish_status)

    def test_finish_repetido_recusado_em_checklist_completo_nao_trava(self):
        s = self._completo(409)
        cid = vc.preencher_checklist_entrega(vuupt_falso(s), SERV, {15086: b"jpg"})
        self.assertEqual(cid, 900)
        self.assertFalse(any(c[0] == "POST" for c in s.chamadas))

    def test_finish_repetido_recusado_mas_releitura_sem_foto_falha(self):
        s = self._completo(409)
        s.listagem[0]["fields"][0]["file_url"] = None
        with self.assertRaises(vc.ChecklistNaoGravado):
            vc.preencher_checklist_entrega(vuupt_falso(s), SERV, {15086: b"jpg"})

    def test_finish_recusado_depois_de_upload_continua_fatal(self):
        ck = {"id": 900, "checklist_form_id": 1531, "images_quantity": 2,
              "fields": [{"checklist_form_field_id": 15086, "file_url": "http://x/a.jpg"}]}
        s = SessaoFalsa(listagem=[ck], finish_status=409)
        with self.assertRaises(vc.ChecklistNaoGravado):
            vc.preencher_checklist_entrega(vuupt_falso(s), SERV, {15086: b"j", 15349: b"j"})

    def test_sem_coordenada_usa_base(self):
        s = SessaoFalsa()
        vc.preencher_checklist_entrega(vuupt_falso(s), {"id": 5, "type": "delivery"}, {15086: b"jpg"})
        d = next(c for c in s.chamadas if c[0] == "POST" and c[1].endswith("/checklists"))[2]["data"]
        self.assertEqual(float(d["latitude"]), vc.BASE_LAT)
        self.assertEqual(float(d["longitude"]), vc.BASE_LNG)

    def test_filtro_do_get(self):
        s = SessaoFalsa()
        vc.checklist_entrega_do_servico(vuupt_falso(s), 5)
        p = s.chamadas[0][2]["params"]
        self.assertEqual(p["filter[0][field]"], "entity_id")
        self.assertEqual(p["filter[0][value]"], 5)
        self.assertEqual(p["include"], "fields")

    def test_para_jpeg_png_e_heic(self):
        self.assertEqual(vc.para_jpeg(_img_bytes("PNG"))[:2], b"\xff\xd8")
        self.assertEqual(vc.para_jpeg(_img_bytes("JPEG"))[:2], b"\xff\xd8")
        try:
            import pillow_heif
        except ImportError:
            self.skipTest("pillow_heif nao instalado")
        pillow_heif.register_heif_opener()
        buf = io.BytesIO()
        Image.new("RGB", (20, 20)).save(buf, "HEIF")
        self.assertEqual(vc.para_jpeg(buf.getvalue())[:2], b"\xff\xd8")

    def test_pdf_nao_e_foto(self):
        with self.assertRaisesRegex(ValueError, "não é foto"):
            vc.para_jpeg(b"%PDF-1.4 abc")
        with self.assertRaises(ValueError):
            vc.para_jpeg(b"texto qualquer")


class ConcluirTest(unittest.TestCase):
    def _ordem(self, s):
        out = []
        for m, u, _ in s.chamadas:
            if "/services/5/" in u:
                out.append(u.rsplit("/", 1)[1])
            elif u.endswith("/finish"):
                out.append("finish")
            elif u.endswith("/checklists") and m == "POST":
                out.append("cria_checklist")
        return out

    def test_concluir_com_fotos_checklist_entre_checkin_e_checkout(self):
        s = SessaoFalsa()
        cid = vuupt_falso(s).concluir_como_agente(5, status_atual="assigned",
                                                  fotos_checklist={15086: b"jpg"}, servico=SERV)
        self.assertEqual(cid, 777)
        o = self._ordem(s)
        self.assertLess(o.index("check-in"), o.index("cria_checklist"))
        self.assertLess(o.index("finish"), o.index("check-out"))

    def test_concluir_falha_no_checklist_nao_faz_checkout(self):
        s = SessaoFalsa(finish_status=409)
        with self.assertRaises(vc.ChecklistNaoGravado):
            vuupt_falso(s).concluir_como_agente(5, fotos_checklist={15086: b"jpg"}, servico=SERV)
        self.assertNotIn("check-out", self._ordem(s))

    def test_concluir_sem_fotos_igual_a_hoje(self):
        s = SessaoFalsa()
        r = vuupt_falso(s).concluir_como_agente(5, status_atual="assigned")
        self.assertIsNone(r)
        self.assertEqual(self._ordem(s), ["accept", "start", "check-in", "check-out"])

    def test_concluir_busca_servico_se_nao_veio(self):
        s = SessaoFalsa()
        vuupt_falso(s).concluir_como_agente(5, fotos_checklist={15086: b"jpg"})
        self.assertTrue(any(m == "GET" and u.endswith("/services/5") for m, u, _ in s.chamadas))

    def test_servico_que_nao_e_entrega_recusa(self):
        s = SessaoFalsa()
        with self.assertRaises(vc.ChecklistNaoGravado):
            vuupt_falso(s).concluir_como_agente(5, fotos_checklist={15086: b"jpg"},
                                                servico={"id": 5, "type": "pickup"})
        self.assertNotIn("check-out", self._ordem(s))


if __name__ == "__main__":
    unittest.main()
