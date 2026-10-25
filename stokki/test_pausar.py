# -*- coding: utf-8 -*-
"""py -3.11 -m unittest stokki.test_pausar"""
import unittest

from stokki import pausar


# Pagina falsa no formato da show/{id} (linha "Situação" igual a do
# test_cancelar; form_pause com os campos vistos pela sonda de 25/10:
# _token, provider_outbound_id, page). O valor de `page` nao foi observado
# na sonda -- aqui "show" so como exemplo; o modulo le o que vier no input.
def _pagina(situacao: str, com_form: bool = True, page: str = "show") -> str:
    form = (
        '<!-- Modal Pause --> <div class="modal fade" id="modal_pause"><form id="form_pause" method="post" enctype="multipart/form-data">'
        '<div class="form-group" id="validation_errors_pause"></div> <input type="hidden" name="_token" value="TOKPAUSE">'
        f'<input type="number" name="provider_outbound_id" value="39959" hidden> <input type="text" name="page" value="{page}" hidden>'
        '</form></div>'
    ) if com_form else ""
    return (
        '<tr><th style="width:280px">Situação:</th><td>\n <span class="badge badge-secondary">'
        f'<i class="fa fa-x"></i>{situacao}</span>\n</td></tr>'
        # form_cancel antes, com outro token: o _token tem que vir do form_pause
        '<form id="form_cancel" method="post"><input type="hidden" name="_token" value="TOKCANCEL">'
        '<input type="text" name="page" value="cancelpage" hidden></form>'
        + form
    )


class _Resp:
    def __init__(self, status, texto="", json_=None):
        self.status_code, self.text, self._json = status, texto, json_

    def json(self):
        if self._json is None:
            raise ValueError("sem json")
        return self._json


class _Sessao:
    def __init__(self, gets, post=None):
        self.gets, self.post_resp, self.posts, self.urls = list(gets), post, [], []

    def get(self, url, **kw):
        self.urls.append(url)
        return self.gets.pop(0)

    def post(self, url, **kw):
        self.posts.append((url, kw))
        return self.post_resp


class PausarPedido(unittest.TestCase):
    def test_pausa_ok_e_confirma_situacao(self):
        s = _Sessao([_Resp(200, _pagina("Aguardando Transportador", page="detalhe")),
                     _Resp(200, _pagina("Em Espera"))],
                    post=_Resp(200, '{"success":true}', {"success": True}))
        r = pausar.pausar_pedido(s, 39959)
        self.assertEqual((r["ok"], r["situacao"], r["ja_estava"], r["erro"]), (True, "Em Espera", False, ""))
        url, kw = s.posts[0]
        self.assertTrue(url.endswith("/administrator/inventory/outbound/pause"))
        self.assertEqual(kw["files"]["_token"], (None, "TOKPAUSE"))
        self.assertEqual(kw["files"]["provider_outbound_id"], (None, "39959"))
        self.assertEqual(kw["files"]["page"], (None, "detalhe"))
        self.assertTrue(s.urls[0].endswith("/outbound/show/39959"))
        self.assertTrue(s.urls[1].endswith("/outbound/show/39959"))

    def test_sem_form_pause_nao_posta(self):
        s = _Sessao([_Resp(200, _pagina("Aguardando Transportador", com_form=False))])
        r = pausar.pausar_pedido(s, 39959)
        self.assertFalse(r["ok"])
        self.assertIn("pausa", r["erro"])
        self.assertEqual(s.posts, [])

    def test_situacao_nao_mudou_e_falha(self):
        s = _Sessao([_Resp(200, _pagina("Aguardando Transportador")),
                     _Resp(200, _pagina("Aguardando Transportador"))],
                    post=_Resp(200, '{"success":true}', {"success": True}))
        r = pausar.pausar_pedido(s, 39959)
        self.assertFalse(r["ok"])
        self.assertEqual(r["situacao"], "Aguardando Transportador")
        self.assertIn("Aguardando Transportador", r["erro"])

    def test_ja_em_espera_nao_posta(self):
        s = _Sessao([_Resp(200, _pagina("Em espera"))])
        r = pausar.pausar_pedido(s, 39959)
        self.assertEqual((r["ok"], r["ja_estava"], s.posts), (True, True, []))

    def test_cancelado_ou_expedido_nao_pausa(self):
        for sit in ("Cancelado", "Enviado"):
            s = _Sessao([_Resp(200, _pagina(sit))])
            r = pausar.pausar_pedido(s, 39959)
            self.assertFalse(r["ok"], sit)
            self.assertEqual(s.posts, [])

    def test_erro_422_vira_erro(self):
        s = _Sessao([_Resp(200, _pagina("Aguardando Transportador"))],
                    post=_Resp(422, '{"errors":{"page":["invalido"]}}', {"errors": {"page": ["invalido"]}}))
        r = pausar.pausar_pedido(s, 39959)
        self.assertFalse(r["ok"])
        self.assertIn("invalido", r["erro"])
        self.assertEqual(len(s.urls), 1)


if __name__ == "__main__":
    unittest.main()
