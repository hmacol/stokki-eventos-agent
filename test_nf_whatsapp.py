import unittest
from datetime import date, datetime

import notificar_whatsapp as nw

AGORA = datetime(2026, 11, 23, 10, 0)


class NfNoWhatsappTest(unittest.TestCase):
    def test_insucesso_com_sem_e_varias(self):
        base = {"destinatario": "D", "remetente": "R", "motivo": "m"}
        linhas = nw.texto_insucessos([dict(base, codigo="#PS-1", nf="100"),
                                      dict(base, codigo="#PS-2", nf=""),
                                      dict(base, codigo="#PS-3", nf="1, 2")], agora=AGORA).split("\n")
        self.assertTrue(linhas[2].startswith("PS-1 · NF 100 · D"))
        self.assertTrue(linhas[3].startswith("PS-2 · NF — · D"))
        self.assertTrue(linhas[4].startswith("PS-3 · NF 1, 2 · D"))

    def test_sem_campo_nf_texto_antigo(self):
        t = nw.texto_insucessos([{"codigo": "#PS-1", "destinatario": "D", "remetente": "R", "motivo": "m"}], agora=AGORA)
        self.assertNotIn("NF", t)

    def test_alteracao_entrega(self):
        t = nw.texto_alteracao_entrega({"codigo": "PS-9", "nf": "55", "empresa": "Emp"}, agora=AGORA)
        self.assertIn("Pedido PS-9 · NF 55 · Emp", t)
        t = nw.texto_alteracao_entrega({"codigo": "PS-9", "nf": "", "empresa": "Emp"}, agora=AGORA)
        self.assertIn("Pedido PS-9 · NF — · Emp", t)

    def test_ciencia_dedicado(self):
        t = nw.texto_ciencia_dedicado("Emb", [{"codigo": "PS-1", "nf": "10"}, {"codigo": "PS-2", "nf": ""}],
                                      "https://x/l", False)
        self.assertIn("PS-1 · NF 10, PS-2 · NF —", t)
        self.assertLessEqual(len(t), nw.MAX_MENSAGEM)

    def test_fora_dia_fixo(self):
        t = nw.texto_fora_dia_fixo("PS-4", date(2026, 11, 25), "Campinas", "Quartas", 10.0, nf="77, 78")
        self.assertIn("pedido PS-4 · NF 77, 78 está", t)
        self.assertLessEqual(len(t), nw.MAX_MENSAGEM)
        self.assertNotIn("NF", nw.texto_fora_dia_fixo("PS-4", date(2026, 11, 25), "C", "Q", 10.0))


if __name__ == "__main__":
    unittest.main()
