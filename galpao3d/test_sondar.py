import unittest

from galpao3d.sondar import CHAVE_NFE_FALSA, limpar_captura


class LimparCapturaTest(unittest.TestCase):
    def test_chave_nfe(self):
        h = limpar_captura('<a href="x?nfe=' + "1" * 44 + '">' + "2" * 44 + "</a>")
        self.assertNotIn("1" * 44, h)
        self.assertEqual(h.count(CHAVE_NFE_FALSA), 2)

    def test_csrf_e_token(self):
        h = limpar_captura('<meta name="csrf-token" content="abcdefghijklmnopqrstuvwxyz0123">'
                           '<input type="hidden" name="_token" value="segredo">')
        self.assertIn('<meta name="csrf-token" content="XXX">', h)
        self.assertIn('<input type="hidden" name="_token" value="XXX">', h)

    def test_link_download(self):
        h = limpar_captura('<a class="b" href="https://x/pt-br/document/download/document/QUJDREVGR0hJSktMTU5PUFFSU1RVVldY">t</a>')
        self.assertEqual(h, '<a class="b" href="#">t</a>')

    def test_data_url_e_rastreio(self):
        h = limpar_captura('<i data-url="https://x/a?t=1"></i><input id="link_public_outbound" value="https://freshlog.stokki.com.br/order/tracking/sale/abc">')
        self.assertIn('<i data-url="#">', h)
        self.assertIn('id="link_public_outbound" value="#"', h)

    def test_sem_byte_de_controle(self):
        h = limpar_captura('<meta name="csrf-token" content="a"><a href="/download/x">')
        self.assertFalse(any(ord(c) < 9 for c in h))


if __name__ == "__main__":
    unittest.main()
