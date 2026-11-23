# -*- coding: utf-8 -*-
"""
Confirmar envio com o endereco de entrega escolhido na previa (spec
2026-10-27): area, dia fixo e gravacao usam o escolhido; caso 4 exige
escolha; nota com <entrega> e TERCEIROS/RETIRADA nao aceitam troca.

    py -3.11 -m unittest portal_cliente.test_endereco_entrega_envio -v
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

import cotacao  # noqa: E402
import endereco_entrega as ee  # noqa: E402
import envio_pedidos as ep  # noqa: E402
from test_endereco_entrega_xml import CHAVE, _xml  # noqa: E402

EMB = "22135070000190"
LIGADA = {"portal_cliente": {"envios": {"endereco_entrega_ativo": True}}}
CEP_HADDOCK = {"cep": "01414-001", "logradouro": "Rua Haddock Lobo", "bairro": "Cerqueira César",
               "cidade": "São Paulo", "uf": "SP", "ibge": "3550308"}
ESCOLHA = {"entrega_escolha": "cliente", "entrega_endereco": {"cep": "01414001", "numero": "595", "complemento": "loja 2"}}
LIVRE = {"modo": "livre", "transportadora": "", "sugestao": None}
SUGESTAO = {"modo": "sugestao", "transportadora": "",
            "sugestao": {"data_br": "26/10", "texto": "Rua Haddock Lobo, 595", "campos": {"cep": "01414001", "numero": "595"}}}


def _xml_nf(n: int, entrega: str = "") -> bytes:
    """XML sintetico com chave/numero proprios (varias NFs no mesmo lote)."""
    chave = CHAVE[:-6] + f"{n:06d}"
    return _xml(entrega).replace(CHAVE.encode(), chave.encode()).replace(b"<nNF>41240</nNF>", f"<nNF>{n}</nNF>".encode())


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        raiz = Path(self.tmp.name)
        patches = [mock.patch.object(ep, "DB_PATH", raiz / "d.db"), mock.patch.object(ep, "PASTA_XMLS", raiz / "x"),
                   mock.patch.object(ep, "PASTA_TEMP", raiz / "x" / "_temporarios"), mock.patch.object(ep, "_RAIZ", raiz),
                   mock.patch.object(ep, "validar_item", return_value={"ok": True, "erros": [], "avisos": [], "envio_existente": None}),
                   mock.patch.object(ep, "info_destinatarios", return_value={}),
                   mock.patch.object(ep, "gravar_destinatario"),
                   mock.patch.object(cotacao, "consultar_cep", side_effect=lambda cep: CEP_HADDOCK if cep == "01414001" else None)]
        self.area = mock.patch.object(ep, "classificar_area_envio", return_value=None)
        patches.append(self.area)
        self.caso = mock.patch.object(ee, "classificar_entrega", return_value=LIVRE)
        patches.append(self.caso)
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        (raiz / "x" / "_temporarios").mkdir(parents=True)
        self.conn = ep.conectar()
        self.addCleanup(self.conn.close)

    def item(self, n=41240, entrega="", **kw):
        token = f"tokenteste{n}"
        (ep.PASTA_TEMP / f"{token}.xml").write_bytes(_xml_nf(n, entrega))
        return {"token": token, "horario_inicio": "09:00", "horario_fim": "17:30", **kw}

    def confirmar(self, itens, config=LIGADA):
        return ep.confirmar_envios(self.conn, EMB, itens, "cliente", config, "nenhuma", sender_id=22812827)

    def linhas(self):
        return self.conn.execute("SELECT * FROM portal_envios ORDER BY id").fetchall()


class Confirmar(Base):
    def test_cliente_altera_grava_entrega_e_fiscal(self):
        self.confirmar([self.item(**ESCOLHA)])
        r = self.linhas()[0]
        self.assertEqual((r["destinatario_endereco"], r["destinatario_cep"]), ("Rua Haddock Lobo, 595 - loja 2", "01414001"))
        self.assertEqual(r["endereco_fiscal"], "RUA IVAI, 207, TATUAPE, SAO PAULO/SP, CEP 03080-010")
        self.assertEqual(r["endereco_entrega_origem"], "cliente")
        end = json.loads(r["endereco_entrega_json"])
        self.assertEqual((end["cmun"], end["documento"], end["nome"]), ("3550308", "51322677000187", "DESTINATARIO TESTE"))
        self.assertEqual(ep._linha(r, {})["endereco_entrega_origem"], "cliente")

    def test_area_usa_endereco_escolhido(self):
        with mock.patch.object(ep, "classificar_area_envio", return_value=None) as area:
            self.confirmar([self.item(**ESCOLHA)])
        self.assertEqual(area.call_args[0][0]["destinatario_cep"], "01414001")

    def test_sem_escolha_grava_fiscal(self):
        self.confirmar([self.item()])
        r = self.linhas()[0]
        self.assertEqual((r["destinatario_cep"], r["endereco_entrega_origem"], r["endereco_entrega_json"]),
                         ("03080010", "fiscal", None))
        self.assertEqual(ep._linha(r, {})["endereco_entrega_origem"], "fiscal")

    def test_sugestao_sem_escolha_recusa(self):
        with mock.patch.object(ee, "classificar_entrega", return_value=SUGESTAO):
            with self.assertRaisesRegex(ep.ErroEnvio, r"^NF 41240: escolha onde entregar"):
                self.confirmar([self.item()])

    def test_sugestao_duas_nfs_mesmo_destinatario(self):
        with mock.patch.object(ee, "classificar_entrega", return_value=SUGESTAO):
            with self.assertRaisesRegex(ep.ErroEnvio, r"^NF 41241: escolha onde entregar"):
                self.confirmar([self.item(41240, entrega_escolha="ultima_entrega",
                                          entrega_endereco={"cep": "01414001", "numero": "595"}),
                                self.item(41241)])
        self.assertEqual(self.linhas(), [])

    def test_sugestao_escolha_fiscal_aceita(self):
        with mock.patch.object(ee, "classificar_entrega", return_value=SUGESTAO):
            self.confirmar([self.item(entrega_escolha="fiscal")])
        self.assertEqual(self.linhas()[0]["endereco_entrega_origem"], "fiscal")

    def test_sugestao_repetir_grava_ultima_entrega(self):
        with mock.patch.object(ee, "classificar_entrega", return_value=SUGESTAO):
            self.confirmar([self.item(entrega_escolha="ultima_entrega", entrega_endereco={"cep": "01414001", "numero": "595"})])
        r = self.linhas()[0]
        self.assertEqual((r["endereco_entrega_origem"], r["destinatario_cep"]), ("ultima_entrega", "01414001"))

    def test_repetir_sem_sugestao_recusa(self):
        with self.assertRaisesRegex(ep.ErroEnvio, r"^NF 41240: "):
            self.confirmar([self.item(entrega_escolha="ultima_entrega", entrega_endereco={"cep": "01414001", "numero": "1"})])

    def test_cep_invalido_vira_erro_envio(self):
        with self.assertRaisesRegex(ep.ErroEnvio, r"^NF 41240: CEP não encontrado$"):
            self.confirmar([self.item(entrega_escolha="cliente", entrega_endereco={"cep": "99999999", "numero": "1"})])
        self.assertEqual(self.linhas(), [])

    def test_nota_com_entrega_recusa_troca(self):
        from test_endereco_entrega_xml import ENTREGA
        with mock.patch.object(ee, "classificar_entrega", return_value={**LIVRE, "modo": "nota"}):
            with self.assertRaisesRegex(ep.ErroEnvio, "não aceita troca de endereço"):
                self.confirmar([self.item(entrega=ENTREGA, **ESCOLHA)])
            self.confirmar([self.item(entrega=ENTREGA)])
        self.assertEqual(self.linhas()[0]["endereco_entrega_origem"], "nota")

    def test_terceiros_recusa_troca(self):
        with mock.patch.object(ee, "classificar_entrega", return_value={**LIVRE, "modo": "terceiros", "transportadora": "X"}):
            with self.assertRaisesRegex(ep.ErroEnvio, "não aceita troca de endereço"):
                self.confirmar([self.item(**ESCOLHA)])

    def test_chave_desligada_ignora_campos(self):
        with mock.patch.object(ee, "classificar_entrega", return_value=SUGESTAO) as caso:
            self.confirmar([self.item(**ESCOLHA)], config={})
        caso.assert_not_called()
        r = self.linhas()[0]
        self.assertEqual((r["destinatario_cep"], r["endereco_entrega_origem"]), ("03080010", "fiscal"))


class ForaDoDiaFixo(Base):
    class _Regioes:
        """Regiao de 'quarta' so pro CEP fiscal; nenhuma data e valida nela."""
        def regra_dia_fixo_do_servico(self, servico):
            return {"nome": "Leste", "regiao": "Leste"} if "03080010" in servico["address"] else None

        def data_valida_na_regiao(self, regra, data):
            return False

        def descricao_dias(self, regra):
            return "quarta"

    def setUp(self):
        super().setUp()
        p = mock.patch.object(ep, "_regioes_dia_fixo", return_value=self._Regioes())
        p.start()
        self.addCleanup(p.stop)
        self.data = (date.today() + timedelta(days=3)).isoformat()

    def test_fora_dia_fixo_olha_endereco_escolhido(self):
        sem = self.item(agendamento_data=self.data)
        self.assertEqual(len(ep.avisos_fora_dia_fixo([sem], LIGADA)), 1)
        com = self.item(agendamento_data=self.data, **ESCOLHA)
        self.assertEqual(ep.avisos_fora_dia_fixo([com], LIGADA), [])

    def test_fora_dia_fixo_chave_desligada_olha_fiscal(self):
        com = self.item(agendamento_data=self.data, **ESCOLHA)
        self.assertEqual(len(ep.avisos_fora_dia_fixo([com], {})), 1)

    def test_fora_dia_fixo_cep_invalido_pula_aviso(self):
        ruim = self.item(agendamento_data=self.data, entrega_escolha="cliente", entrega_endereco={"cep": "1", "numero": "1"})
        self.assertEqual(ep.avisos_fora_dia_fixo([ruim], LIGADA), [])


if __name__ == "__main__":
    unittest.main()
