# -*- coding: utf-8 -*-
"""py -3.11 -m unittest stokki.test_informar_nf_portal -v"""
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from stokki import informar_nf_portal as inp

CHAVE = "35" + "2" * 42
EMB = "48654566000163"
DEST = "11222333000181"
AGORA = datetime(2026, 11, 22, 10, 0, 0)
ATIVO = {"estacao_impressao": {"informar_nf_portal": {"ativo": True}}}


def _xml(emit=EMB, dest=DEST, chave=CHAVE) -> bytes:
    return (f'<nfeProc xmlns="http://www.portalfiscal.inf.br/nfe"><NFe><infNFe Id="NFe{chave}">'
            f'<emit><CNPJ>{emit}</CNPJ><xNome>E</xNome></emit><dest><CNPJ>{dest}</CNPJ></dest>'
            f'<det nItem="1"><prod><cProd>A</cProd></prod></det></infNFe></NFe></nfeProc>').encode()


class _Resp:
    def __init__(self, status=200, texto='{"success": true}'):
        self.status_code, self.text = status, texto


class _Sessao:
    _csrf_token = "TOKEN"

    def __init__(self, resposta=None):
        self.posts, self.resposta = [], resposta or _Resp()

    def get(self, url, **kw):
        return _Resp(200, "")

    def post(self, url, **kw):
        self.posts.append(kw["files"])
        return self.resposta


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.pasta = Path(self.tmp.name)
        self.db = self.pasta / "dados.db"
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE portal_envios (id INTEGER PRIMARY KEY, cnpj_embarcador TEXT, destinatario_doc TEXT, "
                     "status TEXT, codigo_pedido TEXT)")
        conn.execute("CREATE TABLE portal_envios_anexos (id INTEGER PRIMARY KEY, envio_id INTEGER, arquivo TEXT, "
                     "tipo TEXT, numero_nf TEXT, criado_em TEXT)")
        conn.commit()
        conn.close()
        self.sessao = _Sessao()
        self.aguardando = [{100}, set()]          # antes do envio, depois do envio
        self.leituras = 0
        for alvo, valor in (("conta_do_processo", None), ("adquirir", "principal"), ("liberar", None)):
            p = mock.patch(f"stokki.sessao_uso.{alvo}", return_value=valor)
            setattr(self, f"m_{alvo}", p.start())
            self.addCleanup(p.stop)
        p = mock.patch.object(inp, "avisar_equipe")
        self.m_aviso = p.start()
        self.addCleanup(p.stop)

    def _pedido(self, envio_id=1, codigo="PS-100", xml=None, dest=DEST, status="CRIADO", tipo=inp.TIPO_XML,
                criado="2026-11-20 10:00:00"):
        arquivo = f"x_{envio_id}.xml"
        (self.pasta / arquivo).write_bytes(xml if xml is not None else _xml())
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO portal_envios VALUES (?,?,?,?,?)", (envio_id, EMB, dest, status, codigo))
        conn.execute("INSERT INTO portal_envios_anexos VALUES (?,?,?,?,?,?)",
                     (envio_id, envio_id, arquivo, tipo, "9421", criado))
        conn.commit()
        conn.close()

    def _ler(self, _config):
        self.leituras += 1
        return self.aguardando[min(self.leituras, len(self.aguardando)) - 1]

    def _rodar(self, config=ATIVO, dry_run=False):
        return inp.informar_nfs_do_portal(config, dry_run=dry_run, agora=AGORA, db_path=self.db,
                                          pasta_anexos=self.pasta, ler_aguardando=self._ler,
                                          fabrica_sessao=lambda: self.sessao)

    def _estado(self):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute("SELECT status, tentativas, erro FROM portal_xml_stokki").fetchall()
        except sqlite3.OperationalError:
            return []
        finally:
            conn.close()


class TestFluxo(Base):
    def test_informa_e_confere_que_saiu_do_aguardando(self):
        self._pedido()
        r = self._rodar()
        self.assertEqual(r["informadas"], ["PS-100"])
        campos = self.sessao.posts[0]
        self.assertEqual(campos["order_id_nfe"], (None, "100"))
        self.assertEqual(campos["nfe"], (None, CHAVE))
        self.assertEqual(campos["file_nfe"][1], _xml())
        self.assertEqual(self._estado(), [("INFORMADA", 0, None)])
        self.m_liberar.assert_called_once_with(inp.DONO_TRAVA)
        # rodada seguinte nao reenvia nem le a Stokki
        self.leituras = 0
        self.assertEqual(self._rodar()["candidatos"], 0)
        self.assertEqual((len(self.sessao.posts), self.leituras), (1, 0))

    def test_sem_candidato_nao_toca_na_stokki(self):
        self._pedido(tipo="Boleto")
        r = self._rodar()
        self.assertEqual((r["candidatos"], self.leituras, self.sessao.posts), (0, 0, []))

    def test_desligada_nao_faz_nada_mas_modo_teste_lista(self):
        self._pedido()
        r = self._rodar(config={})
        self.assertIn("desligada", r["motivo"])
        self.assertEqual(self.leituras, 0)
        r = self._rodar(config={}, dry_run=True)
        self.assertEqual(r["simulados"], ["PS-100"])
        self.assertEqual((self.sessao.posts, self._estado()), ([], []))
        self.m_adquirir.assert_not_called()

    def test_pedido_fora_do_aguardando_faturamento_espera(self):
        self._pedido(codigo="PS-200")
        r = self._rodar()
        self.assertEqual((r["candidatos"], r["aguardando"], self.sessao.posts, self._estado()), (1, 0, [], []))

    def test_destinatario_diferente_nao_envia_e_avisa_uma_vez(self):
        self._pedido(xml=_xml(dest="99999999000199"))
        r = self._rodar()
        self.assertEqual(self.sessao.posts, [])
        self.assertIn("destinatário", r["falhas"][0])
        self.assertEqual(self._estado()[0][0], "ERRO")
        self.m_aviso.assert_called_once()
        self.assertEqual(self._rodar()["candidatos"], 0)
        self.m_aviso.assert_called_once()

    def test_emitente_de_outra_empresa_nao_envia_mas_filial_vale(self):
        self._pedido(envio_id=1, codigo="PS-100", xml=_xml(emit="11111111000111"))
        self._pedido(envio_id=2, codigo="PS-101", xml=_xml(emit="48654566000325"))
        self.aguardando = [{100, 101}, set()]
        r = self._rodar()
        self.assertEqual(r["informadas"], ["PS-101"])
        self.assertIn("emitente", r["falhas"][0])

    def test_pedido_sem_documento_do_destinatario_envia(self):
        self._pedido(dest="")
        self.assertEqual(self._rodar()["informadas"], ["PS-100"])

    def test_stokki_recusa_tenta_3_rodadas_e_avisa(self):
        self._pedido()
        self.sessao.resposta = _Resp(422, json.dumps({"errors": {"file_nfe": ["XML inválido"]}}))
        for esperado in ("PENDENTE", "PENDENTE", "ERRO"):
            self.leituras = 0
            r = self._rodar()
            self.assertIn("XML inválido", r["falhas"][0])
            self.assertEqual(self._estado()[0][0], esperado)
        self.m_aviso.assert_called_once()
        self.assertEqual(self._rodar()["candidatos"], 0)

    def test_aceitou_mas_continua_aguardando_conta_como_falha(self):
        self._pedido()
        self.aguardando = [{100}, {100}]
        r = self._rodar()
        self.assertEqual(r["informadas"], [])
        self.assertEqual(self._estado()[0][:2], ("PENDENTE", 1))

    def test_stokki_ocupada_nao_gasta_tentativa(self):
        self._pedido()
        self.m_adquirir.return_value = False
        with mock.patch("stokki.sessao_uso.em_uso", return_value="pipeline"):
            r = self._rodar()
        self.assertIn("ocupada", r["motivo"])
        self.assertEqual((self.sessao.posts, self._estado()), ([], []))

    def test_processo_que_ja_tem_a_conta_admin_nao_pega_nem_solta_trava(self):
        self._pedido()
        self.m_conta_do_processo.return_value = "principal"
        self.assertEqual(self._rodar()["informadas"], ["PS-100"])
        self.m_adquirir.assert_not_called()
        self.m_liberar.assert_not_called()

    def test_so_codigos_restringe_o_piloto(self):
        self._pedido(envio_id=1, codigo="PS-100")
        self._pedido(envio_id=2, codigo="PS-101")
        self.aguardando = [{100, 101}, set()]
        cfg = {"estacao_impressao": {"informar_nf_portal": {"ativo": True, "so_codigos": ["PS-101"]}}}
        self.assertEqual(self._rodar(config=cfg)["informadas"], ["PS-101"])

    def test_envio_cancelado_ou_anexo_antigo_ficam_fora(self):
        self._pedido(envio_id=1, status="CANCELADO")
        self._pedido(envio_id=2, codigo="PS-101", criado="2026-09-01 10:00:00")
        self.assertEqual(self._rodar()["candidatos"], 0)


if __name__ == "__main__":
    unittest.main()
