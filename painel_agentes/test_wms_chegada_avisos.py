# -*- coding: utf-8 -*-
"""
Testes dos e-mails da etapa Receber (wms_chegada_avisos.py).
Nenhum teste manda e-mail de verdade: o envio e sempre um duble que so guarda a chamada.
Rodar (de dentro de painel_agentes/): py -3.11 -m unittest test_wms_chegada_avisos -v
"""
import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import controle_notificacoes as cn  # noqa: E402
import test_wms_chegada as base  # noqa: E402
import wms_chegada  # noqa: E402
import wms_chegada_avisos as avisos  # noqa: E402

CNPJ = "11111111000111"
EMAILS_CLIENTE = ["compras@cliente.com.br", "fiscal@cliente.com.br"]


class EnvioFalso:
    """Duble de email_utils.enviar_email -- guarda, nao envia."""

    def __init__(self, ok=True):
        self.ok = ok
        self.chamadas = []

    def __call__(self, destinatarios, assunto, corpo, config_email, cc=None, anexos=None):
        self.chamadas.append({"para": list(destinatarios), "assunto": assunto, "corpo": corpo,
                              "cc": list(cc or []), "anexos": list(anexos or []),
                              "anexo_existia": [Path(a[0]).exists() for a in (anexos or [])]})
        return self.ok


class _Avisos(base._Base):
    OP = {"id": 7, "nome": "Ana"}

    def setUp(self):
        super().setUp()
        self.pasta = Path(self._tmp.name) / "fotos"
        self._pasta_original, wms_chegada.PASTA_FOTOS = wms_chegada.PASTA_FOTOS, self.pasta
        self.addCleanup(setattr, wms_chegada, "PASTA_FOTOS", self._pasta_original)
        self.conn.execute(
            "CREATE TABLE interno (cnpj_embarcador TEXT PRIMARY KEY, nome_remetente TEXT, apelido TEXT, "
            "email TEXT, notificar_email INTEGER NOT NULL DEFAULT 1, sender_id INTEGER, stkkc_id INTEGER)")
        self.conn.execute("INSERT INTO interno VALUES (?,?,?,?,?,?,?)",
                          (CNPJ, "CLIENTE LTDA", "Cliente", "; ".join(EMAILS_CLIENTE), 1, 101, 48))
        for rid, codigo in ((self.rec, "#PE-1"), (self.rec_seco, "#PE-2")):
            self.conn.execute("UPDATE wms_recebimentos SET codigo = ?, embarcador = 'CLIENTE', stkkc_id = '48' "
                              "WHERE id = ?", (codigo, rid))
        self.conn.commit()
        self.envio = EnvioFalso()

    def _png(self):
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (40, 30), (200, 10, 10)).save(buf, format="PNG")
        return buf.getvalue()

    def _chegada(self, ids, uuid, fotos=("CAMINHAO_ABERTO", "CARGA", "TERMOMETRO", "AVARIA"), **mudancas):
        c = wms_chegada.abrir(self.conn, uuid=uuid, recebimento_ids=ids, operador=self.OP)
        for tipo in fotos:
            wms_chegada.salvar_foto(self.conn, c["id"], tipo, self._png(), pasta=self.pasta)
        dados = {"motorista": "Jose Silva", "placa": "abc-1d23", "transportadora": "TransX", "volumes": "12",
                 "lacre": "L99", "integra": True, "observacao": "", "temperaturas": {}}
        dados.update(mudancas)
        wms_chegada.concluir(self.conn, c["id"], dados, {}, self.OP)
        return c["id"]

    def liberada(self, ids=None, uuid="u1", **mudancas):
        ids = ids or [self.rec_seco]
        return self._chegada(ids, uuid, **mudancas)

    def aguardando(self, uuid="u2", **mudancas):
        return self._chegada([self.rec], uuid, temperaturas={"REFRIGERADO": "5", "CONGELADO": "-9"}, **mudancas)

    def estado(self, cid):
        return wms_chegada.detalhe(self.conn, cid)["estado"]

    def envios(self):
        return [r[0] for r in self.conn.execute("SELECT chave FROM wms_chegada_envios ORDER BY chave")]

    def cliente(self, cid, config=None, **kw):
        return avisos.avisar_cliente_chegada(self.conn, cid, config if config is not None else {},
                                             enviar=self.envio, **kw)

    def _tela(self, fn, *args):
        c = cn.conectar(self.db)
        try:
            fn(c, *args, "teste")
        finally:
            c.close()


class TestEscritorio(_Avisos):
    def test_escritorio_recebe_temperatura_fora(self):
        cid = self.aguardando()
        r = avisos.avisar_escritorio(self.conn, cid, {}, "TEMPERATURA", enviar=self.envio)
        self.assertTrue(r["enviado"])
        self.assertEqual(r["destinos"], ["entregas@freshlogbr.com"])
        c = self.envio.chamadas[0]
        self.assertEqual(c["para"], ["entregas@freshlogbr.com"])
        self.assertEqual(c["assunto"], "Temperatura fora da faixa no recebimento: #PE-1")
        for trecho in ("-9", "Jose Silva", "ABC1D23", "/wms"):
            self.assertIn(trecho, c["corpo"])
        self.assertEqual(self.envios(), [f"escritorio-TEMPERATURA-{cid}"])

    def test_escritorio_email_vem_do_config(self):
        cid = self.aguardando()
        avisos.avisar_escritorio(self.conn, cid, {"notificacao_chegada_recebimento": {"email_escritorio": "x@y.com"}},
                                 "TEMPERATURA", enviar=self.envio)
        self.assertEqual(self.envio.chamadas[0]["para"], ["x@y.com"])

    def test_escritorio_nao_repete(self):
        cid = self.aguardando()
        avisos.avisar_escritorio(self.conn, cid, {}, "TEMPERATURA", enviar=self.envio)
        r = avisos.avisar_escritorio(self.conn, cid, {}, "TEMPERATURA", enviar=self.envio)
        self.assertEqual((r["enviado"], r["motivo"]), (False, "ja_enviado"))
        self.assertEqual(len(self.envio.chamadas), 1)

    def test_escritorio_avaria(self):
        cid = self.liberada(integra=False, observacao="caixas amassadas")
        r = avisos.avisar_escritorio(self.conn, cid, {}, "AVARIA", enviar=self.envio)
        self.assertTrue(r["enviado"])
        self.assertEqual(self.envio.chamadas[0]["assunto"], "Avaria no recebimento: #PE-2")
        self.assertIn("caixas amassadas", self.envio.chamadas[0]["corpo"])

    def test_escritorio_resposta_do_cliente(self):
        cid = self.aguardando()
        self.assertEqual(avisos.avisar_escritorio(self.conn, cid, {}, "RESPOSTA_CLIENTE", enviar=self.envio)["motivo"],
                         "sem_resposta")
        wms_chegada.responder_cliente(self.conn, cid, "Maria Souza", False)
        avisos.avisar_escritorio(self.conn, cid, {}, "RESPOSTA_CLIENTE", enviar=self.envio)
        self.assertEqual(self.envio.chamadas[0]["assunto"], "Cliente não autorizou o recebimento: #PE-1")

    def test_escritorio_motivo_invalido(self):
        with self.assertRaises(ValueError):
            avisos.avisar_escritorio(self.conn, self.aguardando(), {}, "OUTRO", enviar=self.envio)

    def test_falha_de_envio_nao_registra(self):
        cid = self.aguardando()
        r = avisos.avisar_escritorio(self.conn, cid, {}, "TEMPERATURA", enviar=EnvioFalso(ok=False))
        self.assertEqual((r["enviado"], r["motivo"]), (False, "falha_no_envio"))
        self.assertEqual(self.envios(), [])


class TestCliente(_Avisos):
    def test_cliente_chegou_sai_preso_por_padrao(self):
        cid = self.liberada()
        r = self.cliente(cid)
        self.assertTrue(r[0]["enviado"])
        self.assertEqual(self.envio.chamadas[0]["para"], [avisos.EMAIL_TESTE])
        self.assertEqual(self.envio.chamadas[0]["cc"], [])

    def test_tipo_sem_decisao_na_tela_nao_vai_para_o_cliente_real(self):
        # config diz envio real (forcar_destino ""), mas ninguem soltou o tipo na tela: continua preso
        cid = self.liberada()
        self.cliente(cid, {"notificacao_chegada_recebimento": {"forcar_destino": ""}})
        self.assertEqual(self.envio.chamadas[0]["para"], [avisos.EMAIL_TESTE])

    def test_tipo_ligado_na_tela_vai_para_o_cliente(self):
        self._tela(cn.definir_estado, "chegada_recebimento", "LIGADO")
        cid = self.liberada()
        self.cliente(cid, {"notificacao_chegada_recebimento": {"forcar_destino": ""}})
        self.assertEqual(self.envio.chamadas[0]["para"], EMAILS_CLIENTE)
        self.assertEqual(self.envio.chamadas[0]["cc"], [avisos.COPIA_INTERNA])
        self.assertIn(f"chegou-{self.rec_seco}", self.envios())

    def test_tipo_desligado_na_tela_nao_envia(self):
        self._tela(cn.definir_estado, "chegada_recebimento", "DESLIGADO")
        r = self.cliente(self.liberada())
        self.assertEqual((r[0]["enviado"], r[0]["motivo"]), (False, "bloqueado_pela_tela"))
        self.assertEqual(self.envio.chamadas, [])

    def test_preferencia_do_cliente_desligada_nao_envia(self):
        self.conn.execute("UPDATE interno SET notificar_email = 0")
        self.conn.commit()
        r = self.cliente(self.liberada())
        self.assertEqual(r[0]["motivo"], "desligado_pelo_cliente")
        self.assertEqual(self.envio.chamadas, [])

    def test_cliente_chegou_leva_pdf_anexo(self):
        self.cliente(self.liberada())
        anexos = self.envio.chamadas[0]["anexos"]
        self.assertEqual(len(anexos), 1)
        self.assertTrue(anexos[0][1].endswith(".pdf"))
        self.assertEqual(anexos[0][1], "Relatorio-de-Recebimento-#PE-2.pdf")
        self.assertEqual(self.envio.chamadas[0]["anexo_existia"], [True])
        self.assertFalse(Path(anexos[0][0]).exists())  # temporario apagado depois do envio
        self.assertEqual(self.envio.chamadas[0]["assunto"], "Sua carga chegou: #PE-2")

    def test_cliente_chegou_um_por_recebimento(self):
        cid = self.liberada(ids=[self.rec_seco, self.rec], temperaturas={"REFRIGERADO": "5", "CONGELADO": "-15"})
        r = self.cliente(cid)
        self.assertEqual(len(r), 2)
        self.assertEqual(sorted(c["assunto"] for c in self.envio.chamadas),
                         ["Sua carga chegou: #PE-1", "Sua carga chegou: #PE-2"])

    def test_cliente_chegou_nao_sai_aguardando_escritorio(self):
        cid = self.aguardando()
        self.assertEqual(self.estado(cid), "AGUARDANDO_ESCRITORIO")
        r = self.cliente(cid)
        self.assertEqual((r[0]["enviado"], r[0]["motivo"]), (False, "fora_do_estado"))
        self.assertEqual(self.envio.chamadas, [])

    def test_cliente_chegou_nao_repete(self):
        cid = self.liberada()
        self.cliente(cid)
        r = self.cliente(cid)
        self.assertEqual((r[0]["enviado"], r[0]["motivo"]), (False, "ja_enviado"))
        self.assertEqual(len(self.envio.chamadas), 1)

    def test_redirecionado_tem_chave_propria(self):
        self.cliente(self.liberada())
        self.assertEqual(self.envios(), [f"chegou-{self.rec_seco}-teste"])

    def test_falha_de_envio_nao_registra(self):
        cid = self.liberada()
        self.envio.ok = False
        r = self.cliente(cid)
        self.assertEqual(r[0]["motivo"], "falha_no_envio")
        self.assertEqual(self.envios(), [])
        self.envio.ok = True
        self.assertEqual(avisos.enviar_pendentes(self.conn, {}, enviar=self.envio), {"chegou": 1, "escritorio": 0})


class TestPedirLiberacao(_Avisos):
    LINK = "https://app.freshhub.com.br/cliente/recebimento/abc123"

    def test_pedir_liberacao_leva_o_link(self):
        cid = self.aguardando()
        r = avisos.pedir_liberacao(self.conn, cid, {}, self.LINK, enviar=self.envio)
        self.assertTrue(r["enviado"])
        c = self.envio.chamadas[0]
        self.assertEqual(c["assunto"], "Liberação de recebimento fora da temperatura: #PE-1")
        self.assertEqual(c["para"], [avisos.EMAIL_TESTE])  # preso por padrao
        for trecho in (self.LINK, "-9", "-12"):
            self.assertIn(trecho, c["corpo"])
        self.assertEqual(self.envios(), [])  # nao e idempotente

    def test_pedir_liberacao_pode_repetir(self):
        cid = self.aguardando()
        avisos.pedir_liberacao(self.conn, cid, {}, self.LINK, enviar=self.envio)
        avisos.pedir_liberacao(self.conn, cid, {}, self.LINK, enviar=self.envio)
        self.assertEqual(len(self.envio.chamadas), 2)

    def test_pedir_liberacao_ignora_preferencia_desligada(self):
        self.conn.execute("UPDATE interno SET notificar_email = 0")
        self.conn.commit()
        r = avisos.pedir_liberacao(self.conn, self.aguardando(), {}, self.LINK, enviar=self.envio)
        self.assertTrue(r["enviado"])

    def test_pedir_liberacao_fora_do_estado_recusa(self):
        r = avisos.pedir_liberacao(self.conn, self.liberada(), {}, self.LINK, enviar=self.envio)
        self.assertEqual((r["enviado"], r["motivo"]), (False, "fora_do_estado"))
        self.assertEqual(self.envio.chamadas, [])


class TestPendentes(_Avisos):
    def test_pendentes_manda_o_que_faltou(self):
        liberada = self.liberada()
        aguard = self.aguardando()
        r = avisos.enviar_pendentes(self.conn, {}, enviar=self.envio)
        self.assertEqual(r, {"chegou": 1, "escritorio": 1})
        assuntos = sorted(c["assunto"] for c in self.envio.chamadas)
        self.assertEqual(assuntos, ["Sua carga chegou: #PE-2", "Temperatura fora da faixa no recebimento: #PE-1"])
        self.assertIn(f"escritorio-TEMPERATURA-{aguard}", self.envios())
        self.assertIsNotNone(liberada)
        # segunda varredura nao repete nada
        self.assertEqual(avisos.enviar_pendentes(self.conn, {}, enviar=self.envio), {"chegou": 0, "escritorio": 0})
        self.assertEqual(len(self.envio.chamadas), 2)

    def _envelhecer(self, cid, horas):
        self.conn.execute("UPDATE wms_chegadas SET concluida_em = datetime('now', 'localtime', ?) WHERE id = ?",
                          (f"-{horas} hours", cid))
        self.conn.commit()

    def test_pendentes_nao_e_retroativo_ao_ligar_o_tipo(self):
        cid = self.liberada()
        self.cliente(cid)  # preso: grava so a chave -teste
        self.assertEqual(self.envios(), [f"chegou-{self.rec_seco}-teste"])
        self._tela(cn.definir_estado, "chegada_recebimento", "LIGADO")
        r = avisos.enviar_pendentes(self.conn, {"notificacao_chegada_recebimento": {"forcar_destino": ""}},
                                    enviar=self.envio)
        self.assertEqual(r["chegou"], 0)
        self.assertEqual(len(self.envio.chamadas), 1)  # so o primeiro, preso

    def test_pendentes_ignora_chegada_com_mais_de_48_horas(self):
        cid = self.liberada()
        self._envelhecer(cid, 72)
        self.assertEqual(avisos.enviar_pendentes(self.conn, {}, enviar=self.envio), {"chegou": 0, "escritorio": 0})
        self.assertEqual(self.envio.chamadas, [])

    def test_pendentes_pega_chegada_de_1_hora(self):
        cid = self.liberada()
        self._envelhecer(cid, 1)
        self.assertEqual(avisos.enviar_pendentes(self.conn, {}, enviar=self.envio)["chegou"], 1)

    def test_pendentes_nao_avisa_escritorio_de_chegada_antiga(self):
        cid = self.aguardando()
        self._envelhecer(cid, 72)
        self.assertEqual(avisos.enviar_pendentes(self.conn, {}, enviar=self.envio), {"chegou": 0, "escritorio": 0})
        self.assertEqual(self.envio.chamadas, [])

    def test_cliente_chegou_com_dois_recebimentos_cita_os_dois_codigos(self):
        cid = self.liberada(ids=[self.rec_seco, self.rec], temperaturas={"REFRIGERADO": "5", "CONGELADO": "-15"})
        self.cliente(cid)
        self.assertEqual(len(self.envio.chamadas), 2)
        for c in self.envio.chamadas:
            self.assertIn("#PE-1", c["assunto"] + c["corpo"])
            self.assertIn("#PE-2", c["assunto"] + c["corpo"])
            self.assertEqual(c["para"], [avisos.EMAIL_TESTE])

    def test_pendentes_cobre_avaria_e_resposta_do_cliente(self):
        avaria = self.liberada(integra=False, observacao="caixas amassadas")
        aguard = self.aguardando(uuid="u9")
        wms_chegada.responder_cliente(self.conn, aguard, "Maria Souza", True)
        avisos.enviar_pendentes(self.conn, {}, enviar=self.envio)
        chaves = self.envios()
        self.assertIn(f"escritorio-AVARIA-{avaria}", chaves)
        self.assertIn(f"escritorio-RESPOSTA_CLIENTE-{aguard}", chaves)


if __name__ == "__main__":
    unittest.main()
