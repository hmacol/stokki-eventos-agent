# -*- coding: utf-8 -*-
"""py -3.11 -m unittest stokki.test_trocar_nf -v"""
import base64
import json
import tempfile
import unittest
from pathlib import Path

from stokki import trocar_nf as tn

CHAVE = "35" + "2" * 42
OUTRA = "35" + "7" * 42


def _xml(chave: str, codigos: list[str]) -> bytes:
    dets = "".join(f'<det nItem="{i}"><prod><cProd>{c}</cProd></prod></det>' for i, c in enumerate(codigos, 1))
    return (f'<?xml version="1.0"?><nfeProc xmlns="http://www.portalfiscal.inf.br/nfe"><NFe>'
            f'<infNFe Id="NFe{chave}"><ide><nNF>152754</nNF></ide>{dets}'
            f'<pag><detPag><tPag>01</tPag></detPag></pag></infNFe></NFe></nfeProc>').encode()


ORIGINAL = _xml(CHAVE, ["A1", "B2", "C3"])
CONVERTIDO = _xml(CHAVE, ["XDDOURADO"])

# Trecho REAL da celula "action" da listagem (sonda de 24/10, PS-42542).
def _linha(id_stokki: int, caminho: str, nf: str = "152754") -> dict:
    return {
        "id": f'<a href="https://freshlog.stokki.com.br/pt-br/administrator/inventory/outbound/show/{id_stokki}">'
              f'#PS-{id_stokki}</a><br>{nf}<br>Referências REF {nf}',
        "state": "Aguardando Transportador",
        "action": f'<a class="dropdown-item btn_danfe" href="#" data-url="{caminho}">Gerar DANFE</a>'
                  f'<a class="dropdown-item modal_action" href="#" data-target="#modal_action_invoice" '
                  f'data-id="{id_stokki}" data-action="invoice">Informar Nota Fiscal</a>',
    }


PAGINA_LISTAGEM = ('<form id="form_excel"><input type="hidden" name="_token" value="ERRADO"></form>'
                   '<form id="form_invoice_standardized" method="post" enctype="multipart/form-data" class="w-100">'
                   '<input type="hidden" name="_token" value="TOKEN123"><input type="hidden" id="modal_invoice_id" '
                   'name="order_id_nfe"></form>')


class _Resp:
    def __init__(self, status=200, texto="", conteudo=None):
        self.status_code, self.text = status, texto
        self.content = conteudo if conteudo is not None else texto.encode()

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return json.loads(self.text)


class _Sessao:
    """Roteia por URL. `linhas` = respostas da listagem em ordem (antes, depois
    da troca); `arquivos` = {caminho: bytes} servidos por /xml/nfe/<b64>."""
    def __init__(self, linhas, arquivos, post=None, csrf=""):
        self.linhas, self.arquivos, self.post_resp = list(linhas), dict(arquivos), post
        self.posts, self.gets = [], []
        self._csrf_token = csrf

    def get(self, url, params=None, headers=None, **kw):
        self.gets.append((url, params))
        if url.endswith("/outbound/table"):
            atual = self.linhas.pop(0) if len(self.linhas) > 1 else self.linhas[0]
            return _Resp(200, json.dumps({"aaData": atual}))
        if "/xml/nfe/" in url:
            caminho = base64.b64decode(url.rsplit("/", 1)[-1]).decode()
            if caminho in self.arquivos:
                return _Resp(200, conteudo=self.arquivos[caminho])
            return _Resp(404, "nao achou")
        if url == tn.URL_LISTAGEM:
            return _Resp(200, PAGINA_LISTAGEM)
        raise AssertionError(f"GET inesperado {url}")

    def post(self, url, **kw):
        self.posts.append((url, kw))
        return self.post_resp


class _ComArquivo(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.original = Path(self.tmp.name) / f"{CHAVE}.xml"
        self.original.write_bytes(ORIGINAL)

    def _trocar(self, s):
        return tn.trocar_nf(s, 42542, self.original, "152754", "18")


class Leitura(unittest.TestCase):
    def test_chave_contagem_e_codigos_do_xml(self):
        self.assertEqual(tn.chave_do_xml(ORIGINAL), CHAVE)
        self.assertIsNone(tn.chave_do_xml(b"<nada/>"))
        self.assertEqual(tn.contar_itens(ORIGINAL), 3)   # <detPag> nao conta
        self.assertEqual(tn.codigos_dos_itens(CONVERTIDO), ["XDDOURADO"])

    def test_token_csrf_da_listagem(self):
        self.assertEqual(tn.token_csrf(_Sessao([[]], {})), "TOKEN123")

    def test_localizar_pedido_por_nf_devolve_id_codigo_e_caminho(self):
        s = _Sessao([[_linha(42542, "users/a.xml", nf="1527540"), _linha(42541, "users/b.xml")]], {})
        self.assertEqual(tn.localizar_pedido_por_nf(s, "152754", "18"),
                         {"id_stokki": 42541, "codigo": "PS-42541", "caminho_nf": "users/b.xml"})
        params = s.gets[0][1]
        self.assertEqual((params["client"], params["input_search"]), ("18", "152754"))

    def test_localizar_sem_resultado_devolve_none(self):
        self.assertIsNone(tn.localizar_pedido_por_nf(_Sessao([[]], {}), "152754", "18"))


class Trocar(_ComArquivo):
    def test_ja_estava_quando_pedido_tem_mesma_chave_e_itens(self):
        s = _Sessao([[_linha(42542, "users/inv/x.xml")]], {"users/inv/x.xml": ORIGINAL})
        r = self._trocar(s)
        self.assertEqual((r["ok"], r["ja_estava"]), (True, True))
        self.assertEqual(s.posts, [])

    def test_xml_convertido_com_mesma_chave_nao_conta_como_trocado(self):
        s = _Sessao([[_linha(42542, "users/inv/x.xml")], [_linha(42542, "users/inv/y.xml")]],
                    {"users/inv/x.xml": CONVERTIDO, "users/inv/y.xml": ORIGINAL},
                    post=_Resp(200, '{"success":true}'))
        r = self._trocar(s)
        self.assertEqual((r["ok"], r["ja_estava"], r["conferencia"]), (True, False, "itens"))
        self.assertEqual(len(s.posts), 1)

    def test_sucesso_posta_campos_exatos_e_confere_itens(self):
        s = _Sessao([[_linha(42542, "users/inv/x.xml")], [_linha(42542, "users/inv/y.xml")]],
                    {"users/inv/x.xml": CONVERTIDO, "users/inv/y.xml": ORIGINAL},
                    post=_Resp(200, '{"success":true}'))
        self._trocar(s)
        url, kw = s.posts[0]
        self.assertEqual(url, tn.URL_NFE_STORE)
        f = kw["files"]
        self.assertEqual(f["_token"], (None, "TOKEN123"))
        self.assertEqual(f["order_id_nfe"], (None, "42542"))
        self.assertEqual(f["action"], (None, "invoice"))
        self.assertEqual(f["page"], (None, "index"))
        self.assertEqual(f["nfe"], (None, CHAVE))
        self.assertEqual(f["file_nfe"][0], f"{CHAVE}.xml")
        self.assertEqual(f["file_nfe"][1], ORIGINAL)

    def test_erro_json_da_stokki_vira_ok_false_com_motivo(self):
        s = _Sessao([[_linha(42542, "users/inv/x.xml")]], {"users/inv/x.xml": CONVERTIDO},
                    post=_Resp(422, json.dumps({"errors": {"nfe": ["Chave inválida"]}})))
        r = self._trocar(s)
        self.assertFalse(r["ok"])
        self.assertIn("Chave inválida", r["erro"])

    def test_aceitou_mas_itens_nao_bateram(self):
        s = _Sessao([[_linha(42542, "users/inv/x.xml")], [_linha(42542, "users/inv/y.xml")]],
                    {"users/inv/x.xml": CONVERTIDO, "users/inv/y.xml": CONVERTIDO},
                    post=_Resp(200, '{"success":true}'))
        r = self._trocar(s)
        self.assertFalse(r["ok"])
        self.assertTrue(r["erro"].startswith("Stokki aceitou mas"), r["erro"])

    def test_sem_download_confere_por_caminho(self):
        s = _Sessao([[_linha(42542, "users/inv/x.xml")], [_linha(42542, "users/inv/y.xml")]], {},
                    post=_Resp(200, '{"success":true}'))
        r = self._trocar(s)
        self.assertEqual((r["ok"], r["conferencia"]), (True, "caminho"))

    def test_sem_download_e_caminho_igual_falha(self):
        s = _Sessao([[_linha(42542, "users/inv/x.xml")]], {}, post=_Resp(200, '{"success":true}'))
        r = self._trocar(s)
        self.assertFalse(r["ok"])
        self.assertTrue(r["erro"].startswith("Stokki aceitou mas"), r["erro"])

    def test_original_sem_chave_nao_posta(self):
        self.original.write_bytes(b"<nfeProc/>")
        s = _Sessao([[_linha(42542, "users/inv/x.xml")]], {})
        r = self._trocar(s)
        self.assertEqual((r["ok"], r["erro"]), (False, "XML original sem chave de acesso"))
        self.assertEqual(s.posts, [])

    def test_pedido_fora_da_listagem_nao_posta(self):
        s = _Sessao([[_linha(99999, "users/inv/x.xml")]], {})
        r = self._trocar(s)
        self.assertFalse(r["ok"])
        self.assertIn("nao encontrado", r["erro"])
        self.assertEqual(s.posts, [])


if __name__ == "__main__":
    unittest.main()
