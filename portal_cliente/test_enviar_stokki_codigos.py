# -*- coding: utf-8 -*-
"""Código PS logo depois de criar: busca pelo número da NF do embarcador
(não pela chave do arquivo do lote).

    py -3.11 -m unittest portal_cliente.test_enviar_stokki_codigos -v
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import enviar_stokki as es  # noqa: E402
import envio_pedidos as ep  # noqa: E402

CNPJ = "22135070000190"
CHAVE = "31261022135070000190550010000419901257812765"


class _PaginaFalsa:
    """page.context.request.get respondendo como a listagem outbound/table."""
    def __init__(self, linhas):
        self.chamadas = []
        pagina = self

        class _Resp:
            status, ok = 200, True

            def text(self):
                return ""

            def json(self):
                return {"aaData": linhas}

        class _Request:
            def get(self, url, params=None, headers=None):
                pagina.chamadas.append(params or {})
                return _Resp()

        self.context = type("Ctx", (), {"request": _Request()})()


class BuscarCodigos(unittest.TestCase):
    def test_busca_pela_nf_filtrando_o_cliente(self):
        linha = {"id": '<a href="https://x/pt-br/administrator/inventory/outbound/show/42840">#PS-42840</a>',
                 "reference": "41990 Referências REF 41990"}
        page = _PaginaFalsa([linha])
        self.assertEqual(es._buscar_codigos_na_listagem(page, ["41990"], "48"), {"41990": "PS-42840"})
        self.assertEqual((page.chamadas[0]["input_search"], page.chamadas[0]["client"]), ("41990", "48"))

    def test_linha_de_outra_nf_nao_casa(self):
        linha = {"id": '<a href="/show/42841">#PS-42841</a>', "reference": "419901 Referências REF 419901"}
        self.assertEqual(es._buscar_codigos_na_listagem(_PaginaFalsa([linha]), ["41990"], "48"), {})


class ProcessarLoteMandaNfPorChave(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        raiz = Path(self.tmp.name)
        self.wizard = mock.Mock()
        self._patches = [mock.patch.object(ep, "DB_PATH", raiz / "d.db"), mock.patch.object(ep, "_RAIZ", raiz),
                         mock.patch.object(ep, "PASTA_XMLS", raiz / "x"),
                         mock.patch.object(es, "PASTA_LOTES", raiz / "lotes"),
                         mock.patch.object(es, "carregar_importador", return_value=(object(), object())),
                         mock.patch.object(es, "transformar_xml"),
                         mock.patch.object(es, "executar_wizard", self.wizard),
                         mock.patch.object(es, "_credenciais", return_value=("u", "s")),
                         mock.patch.object(es, "_avisar_erros"),
                         mock.patch.object(es.sessao_uso, "adquirir", return_value="principal"),
                         mock.patch.object(es.sessao_uso, "liberar")]
        for p in self._patches:
            p.start()
        self.conn = ep.conectar()
        self.conn.executescript(f"""
            CREATE TABLE interno (cnpj_embarcador TEXT PRIMARY KEY, apelido TEXT, nome_remetente TEXT, stkkc_id INTEGER,
                                  sender_id INTEGER, email TEXT, atualizado_em TEXT, fluxos TEXT);
            INSERT INTO interno VALUES ('{CNPJ}', 'MARIA DOLORES', 'MARIA DOLORES', 48, 1, '', NULL, 'armazenagem');
        """)
        rel = Path("x") / CNPJ / f"{CHAVE}.xml"
        (raiz / rel).parent.mkdir(parents=True, exist_ok=True)
        (raiz / rel).write_bytes(b"<nfeProc/>")
        self.conn.execute("""INSERT INTO portal_envios (cnpj_embarcador, chave_nfe, numero_nf, xml_path, status, fluxo,
                             criado_em, atualizado_em) VALUES (?, ?, '41990', ?, 'NA_FILA', 'armazenagem', 'a', 'a')""",
                          (CNPJ, CHAVE, str(rel)))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        for p in self._patches:
            p.stop()
        self.tmp.cleanup()

    def test_wizard_recebe_o_numero_da_nf_de_cada_chave(self):
        self.wizard.return_value = ([{"arquivo": f"{CHAVE}.xml", "criado": True, "erro": ""}], {}, {"41990": "PS-42840"})
        e = dict(self.conn.execute("SELECT * FROM portal_envios").fetchone())
        es.processar_lote(self.conn, CNPJ, [e], {})
        self.assertEqual(self.wizard.call_args.kwargs.get("nf_por_chave"), {CHAVE: "41990"})
        self.assertEqual(self.conn.execute("SELECT codigo_pedido FROM portal_envios").fetchone()[0], "PS-42840")


if __name__ == "__main__":
    unittest.main()
