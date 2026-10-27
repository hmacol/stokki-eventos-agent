# -*- coding: utf-8 -*-
"""
"Ultima entrega" do destinatario e o caso de cada NF na previa do envio
(spec 2026-10-27, secao 3): contam todas as alteracoes -- Vuupt (equipe) e
portal (cliente) --, por embarcador; a Vuupt vale quando existe.

    py -3.11 -m unittest portal_cliente.test_endereco_entrega_memoria -v
"""
import json
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import endereco_entrega as ee  # noqa: E402
import envio_pedidos as ep  # noqa: E402
from nucleo.banco import garantir_esquema  # noqa: E402
from test_endereco_entrega_xml import _xml  # noqa: E402

EMB, SENDER = "22135070000190", 22812827
OUTRO_EMB, OUTRO_SENDER = "11111111000111", 999
DOC = "51322677000187"                    # destinatario do XML sintetico (fiscal CEP 03080-010, nro 207)
HOJE = date.today()
ONTEM, ANTEONTEM = HOJE - timedelta(days=1), HOJE - timedelta(days=2)
END_HADDOCK = "Rua Haddock Lobo, 595, Cerqueira César, São Paulo - SP, 01414-001, Brasil"
END_FISCAL = "Rua Ivai, 207, Tatuapé, São Paulo - SP, 03080-010, Brasil"
GALPAO = "Est. Francisco Hengles, 591, Potuvera, Itapecerica da Serra - SP, 06885-160, Brasil"


class _Catalogo:
    def resolver_por_endereco(self, endereco):
        return object() if "Hengles" in endereco else None


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        raiz = Path(self.tmp.name)
        for nome, valor in (("DB_PATH", raiz / "d.db"), ("PASTA_XMLS", raiz / "x"),
                            ("PASTA_TEMP", raiz / "x" / "_temporarios")):
            p = mock.patch.object(ep, nome, valor)
            p.start()
            self.addCleanup(p.stop)
        self.conn = ep.conectar()
        self.addCleanup(self.conn.close)
        garantir_esquema(self.conn)
        self.nfe = ep.ler_nfe(_xml(""))     # sem <entrega>: fiscal Rua Ivai, 207, CEP 03080010
        self._n = 0
        p = mock.patch.object(ep, "_tipo_transportadora", return_value=None)
        p.start()
        self.addCleanup(p.stop)

    def parada(self, data: date, endereco: str, situacao="ENTREGUE", sender=SENDER, doc="51.322.677/0001-87",
               codigo=None):
        self._n += 1
        codigo = codigo or f"PS-{1000 + self._n}"
        rota = self.conn.execute("INSERT INTO nucleo_rotas (data_rota) VALUES (?)", (data.isoformat(),)).lastrowid
        self.conn.execute("INSERT OR IGNORE INTO nucleo_pedidos (codigo, destinatario_codigo, sender_id, endereco) "
                          "VALUES (?, ?, ?, ?)", (codigo, doc, sender, endereco))
        self.conn.execute("INSERT INTO nucleo_paradas (rota_id, ordem, codigo, endereco, situacao, sender_id) "
                          "VALUES (?, 1, ?, ?, ?, ?)", (rota, f"#{codigo}", endereco, situacao, sender))
        self.conn.commit()
        return codigo

    def envio(self, quando: date, origem="cliente", cep="01414001", numero="595", status="CRIADO",
              codigo_pedido=None, emb=EMB):
        self._n += 1
        end = {"cep": cep, "logradouro": "Rua Haddock Lobo", "numero": numero, "complemento": "", "bairro": "Cerqueira César",
               "municipio": "São Paulo", "uf": "SP", "cmun": "3550308", "documento": DOC, "nome": "DEST"}
        agora = f"{quando.isoformat()} 10:00:00"
        self.conn.execute(
            "INSERT INTO portal_envios (cnpj_embarcador, chave_nfe, xml_path, status, criado_em, atualizado_em, "
            "destinatario_doc, destinatario_endereco, destinatario_bairro, destinatario_municipio, destinatario_uf, "
            "destinatario_cep, codigo_pedido, endereco_entrega_origem, endereco_entrega_json) "
            "VALUES (?, ?, 'x.xml', ?, ?, ?, ?, ?, 'Cerqueira César', 'São Paulo', 'SP', ?, ?, ?, ?)",
            (emb, f"chave{self._n}", status, agora, agora, DOC, f"Rua Haddock Lobo, {numero}", cep, codigo_pedido,
             origem, json.dumps(end) if origem in ("cliente", "ultima_entrega") else None))
        self.conn.commit()

    def caso(self, sender=SENDER, emb=EMB):
        return ee.classificar_entrega(self.conn, self.nfe, emb, sender, catalogo=_Catalogo())


class UltimaEntrega(Base):
    def test_vuupt_corrigida_pela_equipe_vira_sugestao(self):
        self.parada(ONTEM, END_HADDOCK)
        c = self.caso()
        self.assertEqual(c["modo"], "sugestao")
        self.assertEqual(c["sugestao"]["data_br"], ONTEM.strftime("%d/%m"))
        self.assertEqual(c["sugestao"]["texto"], END_HADDOCK)
        self.assertEqual(c["sugestao"]["campos"], {"cep": "01414001", "numero": "595"})

    def test_ultima_no_fiscal_nao_sugere(self):
        self.parada(ANTEONTEM, END_HADDOCK)
        self.parada(ONTEM, END_FISCAL)
        self.assertEqual(self.caso()["modo"], "livre")

    def test_correcao_so_de_numero_nao_sugere(self):
        self.parada(ONTEM, END_FISCAL.replace("207", "300"))
        self.assertEqual(self.caso()["modo"], "livre")

    def test_pedido_aberto_sem_escolha_nao_apaga_memoria(self):
        self.parada(ONTEM, END_HADDOCK)
        self.parada(HOJE, END_FISCAL, situacao="PENDENTE")
        self.envio(HOJE, origem="fiscal", cep="03080010", numero="207")
        self.assertEqual(self.caso()["modo"], "sugestao")

    def test_escolha_no_portal_ainda_aberta_conta(self):
        self.envio(HOJE, origem="cliente")
        c = self.caso()
        self.assertEqual(c["modo"], "sugestao")
        self.assertEqual(c["sugestao"]["campos"]["logradouro"], "Rua Haddock Lobo")
        self.assertEqual(c["sugestao"]["campos"]["cmun"], "3550308")

    def test_vuupt_vence_escolha_do_portal(self):
        self.envio(ANTEONTEM, origem="cliente", codigo_pedido="PS-77")
        self.parada(ONTEM, "Rua Bela Cintra, 1200, Consolação, São Paulo - SP, 01415-000, Brasil", codigo="PS-77")
        c = self.caso()
        self.assertEqual(c["sugestao"]["campos"], {"cep": "01415000", "numero": "1200"})

    def test_reentrega_conta_como_o_mesmo_pedido(self):
        self.envio(ANTEONTEM, origem="cliente", codigo_pedido="PS-78")
        self.parada(ONTEM, END_FISCAL, codigo="PS-78-R1")
        self.assertEqual(self.caso()["modo"], "livre")

    def test_cancelado_e_redespacho_nao_contam(self):
        self.envio(ONTEM, origem="cliente", status="CANCELADO")
        self.parada(ONTEM, GALPAO)
        self.assertEqual(self.caso()["modo"], "livre")

    def test_sem_cep_no_texto_nao_conta(self):
        self.parada(ONTEM, "Rua Haddock Lobo, 595, São Paulo - SP, Brasil")
        self.assertEqual(self.caso()["modo"], "livre")

    def test_outro_embarcador_nao_aparece(self):
        self.parada(ONTEM, END_HADDOCK, sender=OUTRO_SENDER)
        self.envio(ONTEM, origem="cliente", emb=OUTRO_EMB)
        self.assertEqual(self.caso()["modo"], "livre")

    def test_sem_sender_so_portal(self):
        self.parada(ONTEM, END_HADDOCK)
        self.assertEqual(self.caso(sender=None)["modo"], "livre")
        self.envio(ONTEM, origem="cliente")
        self.assertEqual(self.caso(sender=None)["modo"], "sugestao")

    def test_empate_vuupt_ganha(self):
        self.envio(ONTEM, origem="cliente", cep="01415000", numero="1200")
        self.parada(ONTEM, END_HADDOCK)
        self.assertEqual(ee.ultima_entrega(self.conn, EMB, SENDER, DOC, catalogo=_Catalogo())["fonte"], "vuupt")


class ModosSemMemoria(Base):
    def test_nota_com_entrega_e_terceiros(self):
        self.nfe = ep.ler_nfe(_xml())
        self.assertEqual(self.caso()["modo"], "nota")
        self.nfe = ep.ler_nfe(_xml(""))
        with mock.patch.object(ep, "_tipo_transportadora", return_value="TERCEIROS"):
            c = self.caso()
        self.assertEqual(c["modo"], "terceiros")
        with mock.patch.object(ep, "_tipo_transportadora", return_value="RETIRADA"):
            self.assertEqual(self.caso()["modo"], "retirada")

    def test_terceiros_ganha_de_sugestao(self):
        self.parada(ONTEM, END_HADDOCK)
        with mock.patch.object(ep, "_tipo_transportadora", return_value="TERCEIROS"):
            self.assertEqual(self.caso()["modo"], "terceiros")

    def test_sem_historico_e_livre(self):
        self.assertEqual(self.caso(), {"modo": "livre", "transportadora": "", "sugestao": None})


if __name__ == "__main__":
    unittest.main()
