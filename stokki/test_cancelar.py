# -*- coding: utf-8 -*-
"""py -3.11 -m unittest stokki.test_cancelar"""
import unittest

from stokki import cancelar


# Trecho REAL da pagina show/39959 (sonda de 06/10): linha "Situação" e o form_cancel.
def _pagina(situacao: str) -> str:
    return (
        '<tr><th style="width:280px">Situação:</th><td>\n <span class="badge badge-secondary">'
        f'<i class="fa fa-x"></i>{situacao}</span>\n</td></tr>'
        '<!-- Modal Cancel --> <div class="modal fade" id="modal_cancel"><form id="form_cancel" method="post" enctype="multipart/form-data" class="w-100">'
        '<div class="form-group" id="validation_errors_cancel"></div> <input type="hidden" name="_token" value="TOKEN123">'
        '<input class="form-control" id="cancel_reason" name="reason" placeholder="Mínimo de 15 caracteres">'
        '<input type="number" id="cancel_id" name="provider_outbound_id" value="39959" hidden> <input type="text" name="page" value="show" hidden>'
        '</form></div>'
    )


class _Resp:
    def __init__(self, status, texto="", json_=None):
        self.status_code, self.text, self._json = status, texto, json_

    def json(self):
        if self._json is None:
            raise ValueError("sem json")
        return self._json


class _Sessao:
    """Sessao falsa: `gets` = respostas do GET em ordem; `posts` guarda o que foi enviado."""
    def __init__(self, gets, post=None):
        self.gets, self.post_resp, self.posts, self.urls = list(gets), post, [], []

    def get(self, url, **kw):
        self.urls.append(url)
        return self.gets.pop(0)

    def post(self, url, **kw):
        self.posts.append((url, kw))
        return self.post_resp


class CancelarPedido(unittest.TestCase):
    def test_situacao_e_token(self):
        s = _Sessao([_Resp(200, _pagina("Aguardando Transportador"))])
        self.assertEqual(cancelar.situacao_e_token(s, 39959), ("Aguardando Transportador", "TOKEN123"))

    def test_sucesso_com_poll(self):
        s = _Sessao([_Resp(200, _pagina("Aguardando Transportador")),
                     _Resp(200, '{"success":true,"state":"Processing"}', {"success": True, "state": "Processing"}),
                     _Resp(200, '{"success":true,"state":"Canceled"}', {"success": True, "state": "Canceled"}),
                     _Resp(200, _pagina("Cancelado"))],
                    post=_Resp(200, "", {"success": True, "processing": True}))
        r = cancelar.cancelar_pedido(s, 39959, "Portal Fresh Hub: cliente desistiu", dormir=lambda n: None)
        self.assertEqual((r["ok"], r["situacao"], r["ja_estava"]), (True, "Cancelado", False))
        url, kw = s.posts[0]
        self.assertTrue(url.endswith("/inventory/outbound/cancel"))
        self.assertEqual(kw["files"]["_token"], (None, "TOKEN123"))
        self.assertEqual(kw["files"]["provider_outbound_id"], (None, "39959"))
        self.assertEqual(kw["files"]["page"], (None, "show"))
        self.assertEqual(kw["files"]["reason"], (None, "Portal Fresh Hub: cliente desistiu"))

    def test_ja_cancelado_nao_posta(self):
        s = _Sessao([_Resp(200, _pagina("Cancelado"))])
        r = cancelar.cancelar_pedido(s, 39959, "Portal Fresh Hub: cliente desistiu")
        self.assertEqual((r["ok"], r["ja_estava"], s.posts), (True, True, []))

    def test_expedido_nao_cancela(self):
        s = _Sessao([_Resp(200, _pagina("Enviado"))])
        r = cancelar.cancelar_pedido(s, 39959, "Portal Fresh Hub: cliente desistiu")
        self.assertFalse(r["ok"])
        self.assertIn("expedido", r["erro"])
        self.assertEqual(s.posts, [])

    def test_motivo_curto_e_completado(self):
        s = _Sessao([_Resp(200, _pagina("Aguardando Transportador")),
                     _Resp(200, "", {"success": True, "state": "Canceled"}),
                     _Resp(200, _pagina("Cancelado"))],
                    post=_Resp(200, "", {"success": True, "processing": True}))
        cancelar.cancelar_pedido(s, 39959, "", dormir=lambda n: None)
        motivo = s.posts[0][1]["files"]["reason"][1]
        self.assertGreaterEqual(len(motivo), 15)
        self.assertTrue(motivo.startswith("Portal Fresh Hub"))

    def test_erro_422_vira_erro(self):
        s = _Sessao([_Resp(200, _pagina("Aguardando Transportador"))],
                    post=_Resp(422, '{"errors":{"reason":["minimo 15"]}}', {"errors": {"reason": ["minimo 15"]}}))
        r = cancelar.cancelar_pedido(s, 39959, "Portal Fresh Hub: cliente desistiu")
        self.assertFalse(r["ok"])
        self.assertIn("minimo 15", r["erro"])

    def test_poll_que_nao_conclui_e_falha(self):
        gets = [_Resp(200, _pagina("Aguardando Transportador"))]
        gets += [_Resp(200, "", {"success": True, "state": "Processing"})] * 15
        gets += [_Resp(200, _pagina("Aguardando Transportador"))]
        s = _Sessao(gets, post=_Resp(200, "", {"success": True, "processing": True}))
        r = cancelar.cancelar_pedido(s, 39959, "Portal Fresh Hub: cliente desistiu", dormir=lambda n: None)
        self.assertFalse(r["ok"])
        self.assertIn("processamento", r["erro"])


if __name__ == "__main__":
    unittest.main()
