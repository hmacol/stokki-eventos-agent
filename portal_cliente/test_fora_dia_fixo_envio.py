# -*- coding: utf-8 -*-
"""
Portal: aviso no envio quando a data escolhida está fora do dia de visita
da região, e chip "Envio dedicado" com o motivo (dias fixos v2, Hugo 03/10).
    py -3.11 -m unittest portal_cliente.test_fora_dia_fixo_envio -v
"""
import sqlite3
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

import envio_pedidos as ep  # noqa: E402

HOJE = date.today()
QUARTA = HOJE + timedelta(days=(2 - HOJE.weekday()) % 7 or 7)   # próxima quarta (nunca hoje)
QUINTA = QUARTA + timedelta(days=1)


def _pedido(cidade="Campinas", endereco="Rua Barão de Jaguara, 900"):
    return {"origem": ep.ORIGEM_PLANILHA, "referencia": "PED-1", "numero_nf": "", "destinatario_nome": "Mercado X",
            "destinatario_endereco": endereco, "destinatario_municipio": cidade, "destinatario_uf": "SP",
            "destinatario_cep": "13015001"}


class AvisoNoEnvio(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(ep, "PASTA_TEMP", Path(self.tmp.name) / "_temporarios")
        p.start()
        self.addCleanup(p.stop)

    def _item(self, data, **kw):
        token = ep.guardar_temporario_pedido(kw.pop("pedido", _pedido()), "tokenplanilha0001")
        return {"token": token, "requer_agendamento": True, "agendamento_data": data.isoformat(), **kw}

    def test_data_fora_do_dia_avisa(self):
        avisos = ep.avisos_fora_dia_fixo([self._item(QUINTA)])
        self.assertEqual(len(avisos), 1)
        a = avisos[0]
        self.assertEqual((a["regiao"], a["dias"], a["data_br"], a["rotulo"], a["destinatario_nome"]),
                         ("Campinas", "Quartas", QUINTA.strftime("%d/%m/%Y"), "Pedido PED-1", "Mercado X"))

    def test_data_no_dia_de_visita_nao_avisa(self):
        self.assertEqual(ep.avisos_fora_dia_fixo([self._item(QUARTA)]), [])

    def test_confirmado_assim_mesmo_nao_avisa_de_novo(self):
        self.assertEqual(ep.avisos_fora_dia_fixo([self._item(QUINTA, aceita_fora_dia_fixo=True)]), [])

    def test_agendamento_pendente_ou_sem_data_nao_avisa(self):
        item = self._item(QUINTA, agendamento_pendente=True)
        self.assertEqual(ep.avisos_fora_dia_fixo([item]), [])
        sem = self._item(QUINTA)
        sem["agendamento_data"] = ""
        self.assertEqual(ep.avisos_fora_dia_fixo([sem]), [])

    def test_data_invalida_ou_passada_nao_avisa(self):
        ruim = self._item(QUINTA)
        ruim["agendamento_data"] = "31/02"
        self.assertEqual(ep.avisos_fora_dia_fixo([ruim, self._item(HOJE - timedelta(days=1))]), [])

    def test_destino_sem_regiao_nao_avisa(self):
        self.assertEqual(ep.avisos_fora_dia_fixo([self._item(QUINTA, pedido=_pedido(cidade="São Paulo"))]), [])

    def test_sem_logradouro_reconhece_pela_cidade(self):
        self.assertEqual(len(ep.avisos_fora_dia_fixo([self._item(QUINTA, pedido=_pedido(endereco=""))])), 1)

    def test_token_expirado_levanta_erro_de_envio(self):
        with self.assertRaises(ep.ErroEnvio):
            ep.avisos_fora_dia_fixo([{"token": "naoexiste000000", "agendamento_data": QUINTA.isoformat()}])


class ChipNoPortal(unittest.TestCase):
    def test_dedicado_por_codigo_com_motivo(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""CREATE TABLE portal_envios (id INTEGER PRIMARY KEY, cnpj_embarcador TEXT, status TEXT, criado_em TEXT,
            criado_stokki_em, emitida_em, destinatario_doc, destinatario_endereco, destinatario_bairro, destinatario_municipio,
            destinatario_uf, agendamento_data, numero_nf, referencia, data_expedicao, xml_path, origem, agendamento_pendente,
            bloqueio_motivo, bloqueio_chamado_id, codigo_pedido);
            CREATE TABLE portal_solicitacoes (id INTEGER PRIMARY KEY, envio_id, cnpj_embarcador, tipo, detalhes, status, solicitado_por, criado_em, concluido_em, resposta);
            CREATE TABLE pedidos_dedicados (id INTEGER PRIMARY KEY, codigo_pedido TEXT, envio_id INTEGER, valor REAL,
                marcado_por TEXT, removido_em TEXT);
            INSERT INTO portal_envios (id, cnpj_embarcador, status, criado_em, numero_nf, xml_path, origem, agendamento_pendente, codigo_pedido)
              VALUES (1, '1', 'CRIADO', '2099-01-01 00:00:00', '1', 'a.xml', 'xml', 0, 'PS-77'),
                     (2, '1', 'NA_FILA', '2099-01-01 00:00:00', '2', 'b.xml', 'xml', 0, NULL);
            INSERT INTO pedidos_dedicados (codigo_pedido, envio_id, valor, marcado_por, removido_em) VALUES
              ('PS-77', NULL, 784.09, 'automatico: fora do dia fixo', NULL),
              (NULL, 2, 10.0, 'hugo', NULL);""")
        lista = {l["id"]: l for l in ep.listar_envios(conn, "1")}
        self.assertEqual(lista[1]["dedicado"], {"valor": 784.09, "motivo": "data fora do dia de visita da região"})
        self.assertEqual(lista[2]["dedicado"], {"valor": 10.0})


if __name__ == "__main__":
    unittest.main()
