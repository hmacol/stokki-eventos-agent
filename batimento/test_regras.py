# -*- coding: utf-8 -*-
import unittest

from batimento import regras as r


def fato(**kw):
    base = {"status_stokki_bruto": "Sent", "transportadora_tipo": "ENTREGA",
            "nucleo_status": "ENTREGUE", "nucleo_fluxo": "ENTREGA",
            "vuupt_sucesso": True, "vuupt_insucesso": False, "servico_vivo": False,
            "canhoto_fonte": "vuupt_foto", "canhoto_validado": False,
            "dedicado": False, "lalamove": False, "embarcador_recusou": False}
    base.update(kw)
    return base


class SituacaoStokki(unittest.TestCase):
    def test_textos(self):
        self.assertEqual(r.situacao_stokki("Waiting for Carrier"), r.STOKKI_ABERTO)
        self.assertEqual(r.situacao_stokki("Aguardando Transportador"), r.STOKKI_ABERTO)
        self.assertEqual(r.situacao_stokki("On hold"), r.STOKKI_ABERTO)
        self.assertEqual(r.situacao_stokki("Em Conferência"), r.STOKKI_ABERTO)
        self.assertEqual(r.situacao_stokki("Sent"), r.STOKKI_EXPEDIDO)
        self.assertEqual(r.situacao_stokki("Enviado"), r.STOKKI_EXPEDIDO)
        self.assertEqual(r.situacao_stokki("Cancelado"), r.STOKKI_CANCELADO)
        self.assertEqual(r.situacao_stokki("Envio cancelado"), r.STOKKI_CANCELADO)
        self.assertEqual(r.situacao_stokki(""), r.STOKKI_DESCONHECIDO)
        self.assertEqual(r.situacao_stokki("Xyz"), r.STOKKI_DESCONHECIDO)


class Classificar(unittest.TestCase):
    def test_entregue_com_canhoto_sem_validar_vale(self):
        caixa, destino, ev = r.classificar(fato())
        self.assertEqual((caixa, destino), (r.DESTINO, r.ENTREGUE))
        self.assertIn("canhoto=vuupt_foto sem validar", ev)

    def test_entregue_sem_canhoto_e_divergencia(self):
        self.assertEqual(r.classificar(fato(canhoto_fonte=None))[:2],
                         (r.DIVERGENCIA, r.EXPEDIDO_SEM_DOCUMENTO))

    def test_canhoto_do_app_vale(self):
        self.assertEqual(r.classificar(fato(vuupt_sucesso=False, canhoto_fonte="app"))[:2],
                         (r.DESTINO, r.ENTREGUE))

    def test_dedicado(self):
        self.assertEqual(r.classificar(fato(dedicado=True))[:2], (r.DESTINO, r.DEDICADO))

    def test_expedido_sem_entrega(self):
        self.assertEqual(r.classificar(fato(vuupt_sucesso=False, nucleo_status="ABERTO"))[:2],
                         (r.DIVERGENCIA, r.EXPEDIDO_SEM_ENTREGA))
        self.assertEqual(r.classificar(fato(vuupt_sucesso=False, nucleo_status=None))[:2],
                         (r.DIVERGENCIA, r.EXPEDIDO_SEM_ENTREGA))

    def test_expedido_com_insucesso_aberto(self):
        self.assertEqual(r.classificar(fato(vuupt_sucesso=False, vuupt_insucesso=True,
                                            nucleo_status="INSUCESSO"))[:2],
                         (r.DIVERGENCIA, r.EXPEDIDO_COM_INSUCESSO_ABERTO))

    def test_redespacho_sem_comprovante_hoje(self):
        self.assertEqual(r.classificar(fato(transportadora_tipo="TERCEIROS", vuupt_sucesso=False,
                                            nucleo_status=None))[:2],
                         (r.DIVERGENCIA, r.REDESPACHO_SEM_COMPROVANTE))
        self.assertEqual(r.classificar(fato(transportadora_tipo="TERCEIROS",
                                            comprovante_redespacho=True))[:2],
                         (r.DESTINO, r.REDESPACHADO))

    def test_retirada(self):
        self.assertEqual(r.classificar(fato(transportadora_tipo="RETIRADA"))[:2],
                         (r.DIVERGENCIA, r.RETIRADA_SEM_COMPROVANTE))
        self.assertEqual(r.classificar(fato(nucleo_fluxo="RETIRADA", comprovante_retirada=True))[:2],
                         (r.DESTINO, r.RETIRADO))

    def test_lalamove(self):
        self.assertEqual(r.classificar(fato(lalamove=True))[:2],
                         (r.DIVERGENCIA, r.LALAMOVE_SEM_COMPROVANTE))
        self.assertEqual(r.classificar(fato(lalamove=True, comprovante_lalamove=True))[:2],
                         (r.DESTINO, r.LALAMOVE))
        self.assertEqual(r.classificar(fato(lalamove=True, vuupt_sucesso=False, nucleo_status="EM_ROTA"))[:2],
                         (r.DIVERGENCIA, r.EXPEDIDO_SEM_ENTREGA))

    def test_cancelado_na_stokki(self):
        self.assertEqual(r.classificar(fato(status_stokki_bruto="Cancelado", nucleo_status="CANCELADO"))[:2],
                         (r.DESTINO, r.CANCELADO))
        self.assertEqual(r.classificar(fato(status_stokki_bruto="Cancelado", nucleo_status="CANCELADO",
                                            embarcador_recusou=True))[:2],
                         (r.DESTINO, r.DEVOLVIDO))
        self.assertEqual(r.classificar(fato(status_stokki_bruto="Cancelado", servico_vivo=True,
                                            nucleo_status="EM_ROTA"))[:2],
                         (r.DIVERGENCIA, r.CANCELADO_STOKKI_SERVICO_VIVO))

    def test_aberto_na_stokki_e_do_vigia(self):
        caixa, estado, _ = r.classificar(fato(status_stokki_bruto="Waiting for Carrier", vuupt_sucesso=False,
                                              nucleo_status="ABERTO", servico_vivo=True, vigia_estado="NO_POOL"))
        self.assertEqual((caixa, estado), (r.EM_ANDAMENTO, "NO_POOL"))

    def test_aberto_na_stokki_mas_entregue(self):
        self.assertEqual(r.classificar(fato(status_stokki_bruto="Waiting for Carrier"))[:2],
                         (r.DIVERGENCIA, r.ENTREGUE_NAO_EXPEDIDO))

    def test_aberto_entregue_com_reentrega_em_rota_segue_em_andamento(self):
        f = fato(status_stokki_bruto="Waiting for Carrier", servico_vivo=True, nucleo_status="EM_ROTA",
                 vigia_estado="EM_ROTA")
        self.assertEqual(r.classificar(f)[0], r.EM_ANDAMENTO)

    def test_aberto_com_servico_cancelado_na_vuupt(self):
        self.assertEqual(r.classificar(fato(status_stokki_bruto="Open", vuupt_sucesso=False,
                                            nucleo_status="CANCELADO"))[:2],
                         (r.DIVERGENCIA, r.CANCELADO_VUUPT_STOKKI_ABERTO))

    def test_status_desconhecido(self):
        self.assertEqual(r.classificar(fato(status_stokki_bruto="Returned"))[:2],
                         (r.DIVERGENCIA, r.STATUS_STOKKI_DESCONHECIDO))


if __name__ == "__main__":
    unittest.main()
