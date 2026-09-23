# -*- coding: utf-8 -*-
"""
test_sincronizar_recebimentos_wms.py

Testes da rotina que le os recebimentos do embarcador piloto na Stokki e
cria a entrada esperada no WMS (sincronizar_recebimentos_wms.py). Sessao
Stokki e sempre uma duble (SessaoFalsa) -- nenhum teste aqui toca a rede
nem a Stokki de verdade (login concorrente derruba a sessao da VPS e do
outro projeto, agente-importacao-stokki).

Rodar (da raiz):
    py -3.11 -m unittest test_sincronizar_recebimentos_wms -v
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_RAIZ = Path(__file__).parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(_RAIZ / "painel_agentes"))

import wms  # noqa: E402
import wms_pedidos  # noqa: E402
import sincronizar_recebimentos_wms as mod  # noqa: E402

PILOTO_ID = "48"
PILOTO_NOME = "MARIA DOLORES"

# HTML minimo com as duas tabelas do detalhe: a de lote (Produto | Lote |
# Entrada | Fabricacao | Validade | Quantidade), que o parser IGNORA de
# proposito (decisao do Hugo, 23/09/2026: sem pre-preenchimento vindo da
# Stokki), e a de itens (NR | ID | SKU | Nome | ... | Quantidade total
# recebida | Obs), a unica que extrair_itens_do_recebimento le.
_HTML_ITENS = """
<table class="table">
  <thead>
    <tr><th>Produto</th><th>Lote</th><th>Entrada</th><th>Fabricação</th><th>Validade</th><th>Quantidade</th></tr>
  </thead>
  <tbody>
    <tr>
      <td>SKU1 - PRODUTO 1</td>
      <td>L-A</td>
      <td>18/09/2026</td>
      <td></td>
      <td>2026-10-15</td>
      <td>4</td>
    </tr>
  </tbody>
</table>
<table class="table">
  <thead>
    <tr>
      <th>NR.</th><th>ID</th><th>SKU</th><th>Nome</th><th>Localização</th>
      <th>Quantidade</th><th>unidade</th><th>Valor Unitário</th>
      <th>Quantidade total recebida</th><th>Obs</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td>1</td><td>#ITM-948</td><td>SKU1</td><td>PRODUTO 1</td>
      <td>Recebimento</td><td>4</td><td>UN</td><td>R$ 10,00</td>
      <td>4</td><td></td>
    </tr>
  </tbody>
</table>
"""

# HTML minimo do caso PE-2440 (achado da revisao, 22/09/2026): sem a
# tabela de lote na pagina, so a tabela de itens -- mesmo assim registrado
# normalmente, porque e a unica tabela que o parser sempre le.
_HTML_ITENS_SEM_LOTE = """
<table class="table">
  <thead>
    <tr>
      <th>NR.</th><th>ID</th><th>SKU</th><th>Nome</th><th>Localização</th>
      <th>Quantidade</th><th>unidade</th><th>Valor Unitário</th>
      <th>Quantidade total recebida</th><th>Obs</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td>1</td><td>#ITM-948</td><td>SKU1</td><td>PRODUTO 1</td>
      <td>Recebimento</td><td>4</td><td>UN</td><td>R$ 10,00</td>
      <td>4</td><td></td>
    </tr>
  </tbody>
</table>
"""


class _RespostaFalsa:
    """Imita o suficiente de requests.Response pro codigo da rotina."""

    def __init__(self, json_data=None, text=""):
        self._json = json_data
        self.text = text

    def raise_for_status(self):
        pass

    def json(self):
        return self._json


class SessaoFalsa:
    """
    Duble de StokkiSession -- nunca toca a rede. .get() devolve o que o
    teste montar, conforme a URL: a listagem (.../table) e o detalhe
    (.../show/{id}).
    """

    def __init__(self, linhas, html_por_id):
        self.linhas = linhas
        self.html_por_id = html_por_id
        self.chamadas = []  # [(url, params)], pra conferir o que foi pedido

    def get(self, url, params=None, headers=None):
        self.chamadas.append((url, params))
        if url.endswith("/table"):
            return _RespostaFalsa(json_data={"aaData": self.linhas, "iTotalRecords": len(self.linhas)})
        if "/show/" in url:
            id_recebimento = url.rstrip("/").split("/")[-1]
            return _RespostaFalsa(text=self.html_por_id.get(id_recebimento, ""))
        raise AssertionError(f"URL inesperada na sessao falsa: {url}")


def _linha(id_recebimento, stkkc_id, nome_truncado="MARIA DOLORES IND ...", situacao="Recebido"):
    """Uma linha do aaData de incoming/table no formato real (dict) -- o
    campo 'id' reproduz a armadilha real: href com o id certo + um numero
    solto que NAO serve (41099 no exemplo do Hugo)."""
    return {
        "checkbox": f'<input type="checkbox" value="{id_recebimento}">',
        "id": (f'<a href="https://freshlog.stokki.com.br/pt-br/administrator/inventory/incoming/'
               f'show/{id_recebimento}">#PE-{id_recebimento}</a><br>'
               f'<span class="text-muted">{id_recebimento}99</span>'),
        "arrival_date": "22/09/2026",
        "type": "Entrada",
        "motion": "Normal",
        "client": f'{nome_truncado} <span class="text-muted">#stkkc-{stkkc_id}</span>',
        "origin": "Fornecedor",
        "destination": "Freshlog",
        "receipt_date": "",
        "marker": "",
        "state": situacao,
        "action": "",
    }


class BaseRotina(unittest.TestCase):
    """Banco temporario com area, posicoes e um produto (SKU1) -- o mesmo
    catalogo que o _HTML_ITENS espera resolver."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "dados.db"
        self.conn = wms_pedidos.conectar(self.db)
        wms.criar_area(self.conn, "C9", "CONTAINER", "Container 9")
        wms.gerar_posicoes(self.conn, "C9", estantes=2, niveis=2)
        self.conn.execute(
            "INSERT INTO wms_produtos (id, stokki_id, sku, descricao, embarcador, qtd_por_caixa, "
            "unidade, atualizado_em) VALUES (1, 900, 'SKU1', 'PRODUTO 1', 'MARIA DOLORES', 1, "
            "'UN', '2026-09-21 10:00:00')")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()


class TestRodar(BaseRotina):
    def test_recebimento_do_piloto_e_registrado_com_a_quantidade_da_stokki(self):
        linhas = [_linha(2478, PILOTO_ID)]
        sess = SessaoFalsa(linhas, {"2478": _HTML_ITENS})

        res = mod.rodar(self.conn, sess, PILOTO_NOME, PILOTO_ID, limite=50, modo_teste=False)

        self.assertEqual(res["lidos"], 1)
        self.assertEqual(res["do_piloto"], 1)
        self.assertEqual(res["gravados"], 1)
        self.assertEqual(res["erros"], 0)

        recebimento = self.conn.execute("SELECT * FROM wms_recebimentos WHERE id_stokki = 2478").fetchone()
        self.assertIsNotNone(recebimento)
        self.assertEqual(recebimento["codigo"], "#PE-2478")
        self.assertEqual(recebimento["estado"], "ESPERADO")
        item = self.conn.execute("SELECT * FROM wms_recebimento_itens").fetchone()
        self.assertEqual(item["produto_id"], 1)
        self.assertEqual(item["qtd_un"], 4)  # so a quantidade vem da Stokki -- sem lote/validade

        # o filtro do lado do servidor tem que ter sido usado (correcao 1)
        url_tabela, params_tabela = next(c for c in sess.chamadas if c[0].endswith("/table"))
        self.assertEqual(params_tabela["client"], PILOTO_ID)
        # o id usado no /show/ tem que ser o do href (2478), nunca o numero
        # solto (247899) -- a armadilha descrita pelo Hugo
        chamada_show = next(c for c in sess.chamadas if "/show/" in c[0])
        self.assertTrue(chamada_show[0].endswith("/show/2478"))

    def test_linha_de_outro_embarcador_e_ignorada_mesmo_se_vier_na_listagem(self):
        linhas = [_linha(2479, "99", "OUTRO EMBARCADOR ...")]
        sess = SessaoFalsa(linhas, {"2479": _HTML_ITENS})

        res = mod.rodar(self.conn, sess, PILOTO_NOME, PILOTO_ID, limite=50, modo_teste=False)

        self.assertEqual(res["do_piloto"], 0)
        self.assertEqual(res["ignorados_outro_embarcador"], 1)
        recebimento = self.conn.execute("SELECT * FROM wms_recebimentos WHERE id_stokki = 2479").fetchone()
        self.assertIsNone(recebimento)
        self.assertFalse(any("/show/" in url for url, _ in sess.chamadas))

    def test_recebimento_sem_stkkc_na_linha_tambem_e_barrado(self):
        linha_sem_stkkc = _linha(2480, PILOTO_ID)
        linha_sem_stkkc["client"] = "SEM MARCACAO NENHUMA"
        sess = SessaoFalsa([linha_sem_stkkc], {"2480": _HTML_ITENS})

        res = mod.rodar(self.conn, sess, PILOTO_NOME, PILOTO_ID, limite=50, modo_teste=False)

        self.assertEqual(res["do_piloto"], 0)
        self.assertEqual(res["ignorados_outro_embarcador"], 1)

    def test_modo_teste_nao_grava_nada(self):
        linhas = [_linha(2481, PILOTO_ID)]
        sess = SessaoFalsa(linhas, {"2481": _HTML_ITENS})

        res = mod.rodar(self.conn, sess, PILOTO_NOME, PILOTO_ID, limite=50, modo_teste=True)

        self.assertEqual(res["do_piloto"], 1)
        n = self.conn.execute("SELECT COUNT(*) n FROM wms_recebimentos").fetchone()["n"]
        self.assertEqual(n, 0)
        self.assertFalse(any("/show/" in url for url, _ in sess.chamadas))

    def test_rodada_repetida_sobre_recebimento_inalterado_nao_duplica_item(self):
        linhas = [_linha(2482, PILOTO_ID)]
        sess = SessaoFalsa(linhas, {"2482": _HTML_ITENS})

        mod.rodar(self.conn, sess, PILOTO_NOME, PILOTO_ID, limite=50, modo_teste=False)
        mod.rodar(self.conn, sess, PILOTO_NOME, PILOTO_ID, limite=50, modo_teste=False)

        n_receb = self.conn.execute("SELECT COUNT(*) n FROM wms_recebimentos").fetchone()["n"]
        n_itens = self.conn.execute("SELECT COUNT(*) n FROM wms_recebimento_itens").fetchone()["n"]
        self.assertEqual(n_receb, 1)
        self.assertEqual(n_itens, 1)

    def test_recebimento_em_transito_sem_itens_nao_e_erro(self):
        linhas = [_linha(2483, PILOTO_ID, situacao="Em transito")]
        sess = SessaoFalsa(linhas, {"2483": "<html>ainda sem a tabela de itens</html>"})

        res = mod.rodar(self.conn, sess, PILOTO_NOME, PILOTO_ID, limite=50, modo_teste=False)

        self.assertEqual(res["em_transito"], 1)
        self.assertEqual(res["erros"], 0)
        self.assertEqual(res["gravados"], 0)
        recebimento = self.conn.execute("SELECT * FROM wms_recebimentos WHERE id_stokki = 2483").fetchone()
        self.assertIsNone(recebimento)

    def test_recebido_sem_tabela_de_itens_conta_como_erro_mas_nao_derruba_a_rodada(self):
        # sem a tabela de itens na pagina (formato mudou/pagina quebrada
        # de verdade) -- vira erro, e mesmo assim tem que aguentar sem
        # quebrar a rodada.
        linhas = [_linha(2484, PILOTO_ID, situacao="Recebido"), _linha(2485, PILOTO_ID, situacao="Recebido")]
        sess = SessaoFalsa(linhas, {"2484": "<html>sem tabela nenhuma</html>", "2485": _HTML_ITENS})

        res = mod.rodar(self.conn, sess, PILOTO_NOME, PILOTO_ID, limite=50, modo_teste=False)

        self.assertEqual(res["erros"], 1)
        self.assertEqual(res["gravados"], 1)
        ok = self.conn.execute("SELECT * FROM wms_recebimentos WHERE id_stokki = 2485").fetchone()
        self.assertIsNotNone(ok)
        ruim = self.conn.execute("SELECT * FROM wms_recebimentos WHERE id_stokki = 2484").fetchone()
        self.assertIsNone(ruim)

    def test_recebido_sem_tabela_de_lote_na_pagina_e_registrado_normalmente(self):
        # achado da revisao (Hugo, 22/09/2026, caso real PE-2440): sem a
        # tabela de lote na pagina, so a de itens -- a mercadoria existe e
        # e enderecavel. Tem que gravar do mesmo jeito: a tabela de lote
        # nunca foi lida mesmo quando presente (decisao do Hugo, 23/09/2026).
        linhas = [_linha(2487, PILOTO_ID, situacao="Recebido")]
        sess = SessaoFalsa(linhas, {"2487": _HTML_ITENS_SEM_LOTE})

        res = mod.rodar(self.conn, sess, PILOTO_NOME, PILOTO_ID, limite=50, modo_teste=False)

        self.assertEqual(res["gravados"], 1)
        self.assertEqual(res["erros"], 0)
        recebimento = self.conn.execute("SELECT * FROM wms_recebimentos WHERE id_stokki = 2487").fetchone()
        self.assertIsNotNone(recebimento)
        item = self.conn.execute(
            "SELECT * FROM wms_recebimento_itens WHERE recebimento_id = ?", (recebimento["id"],)).fetchone()
        self.assertEqual(item["sku"], "SKU1")
        self.assertEqual(item["produto_id"], 1)
        self.assertEqual(item["qtd_un"], 4)

    def test_recebimento_reenderecado_nao_volta_a_esperado(self):
        # o operador (tela do celular, tarefa seguinte) marca ENDERECADO;
        # uma rodada seguinte que so atualiza cabecalho nao pode reverter.
        linhas = [_linha(2486, PILOTO_ID)]
        sess = SessaoFalsa(linhas, {"2486": _HTML_ITENS})
        mod.rodar(self.conn, sess, PILOTO_NOME, PILOTO_ID, limite=50, modo_teste=False)
        rid = self.conn.execute("SELECT id FROM wms_recebimentos WHERE id_stokki = 2486").fetchone()["id"]
        self.conn.execute("UPDATE wms_recebimentos SET estado = 'ENDERECADO' WHERE id = ?", (rid,))
        self.conn.commit()

        mod.rodar(self.conn, sess, PILOTO_NOME, PILOTO_ID, limite=50, modo_teste=False)

        estado = self.conn.execute("SELECT estado FROM wms_recebimentos WHERE id = ?", (rid,)).fetchone()["estado"]
        self.assertEqual(estado, "ENDERECADO")

    def test_recebimento_ja_enderecado_nao_e_rebuscado_na_stokki(self):
        # I10 da revisao final: a rotina relia o detalhe dos ~50 mais
        # recentes a cada 30 min, pra sempre, inclusive os ja fechados --
        # ~50 GETs por rodada contra um sistema de sessao unica e fragil.
        linhas = [_linha(2488, PILOTO_ID)]
        sess = SessaoFalsa(linhas, {"2488": _HTML_ITENS})
        mod.rodar(self.conn, sess, PILOTO_NOME, PILOTO_ID, limite=50, modo_teste=False)
        self.conn.execute("UPDATE wms_recebimentos SET estado = 'ENDERECADO' WHERE id_stokki = 2488")
        self.conn.commit()

        sess2 = SessaoFalsa(linhas, {"2488": _HTML_ITENS})
        res = mod.rodar(self.conn, sess2, PILOTO_NOME, PILOTO_ID, limite=50, modo_teste=False)

        self.assertEqual(res["ja_fechados"], 1)
        self.assertEqual(res["gravados"], 0)
        self.assertFalse(any("/show/" in url for url, _ in sess2.chamadas))


class TestTravaStokki(unittest.TestCase):
    """Mesma trava cooperativa de sincronizar_pedidos_wms.py -- a sessao
    Stokki e unica. Duble/monkeypatch do modulo sessao_uso -- nenhum
    teste aqui toca a Stokki nem o banco de producao (wms_pedidos.conectar
    e StokkiSession tambem duplados, pra provar que nem chegam a ser
    chamados)."""

    def test_trava_ocupada_desiste_sem_gravar_e_sem_chamar_a_stokki(self):
        # Sai com 0 (I9 da revisao final): trava ocupada e operacao normal,
        # nao falha -- o .service tem OnFailure=stokki-alerta-falha@%n e
        # alertava a cada rodada.
        with mock.patch("stokki.sessao_uso.adquirir", return_value=False) as adquirir, \
             mock.patch("stokki.sessao_uso.em_uso", return_value="outro-processo"), \
             mock.patch("stokki.sessao_uso.liberar") as liberar, \
             mock.patch("sincronizar_recebimentos_wms.wms_pedidos.conectar") as conectar, \
             mock.patch("sincronizar_recebimentos_wms.StokkiSession") as sessao_cls:
            codigo = mod.main(["--limite", "5"])

        self.assertEqual(codigo, 0)
        adquirir.assert_called_once_with(mod.DONO_TRAVA, ttl_segundos=mod.TRAVA_TTL_SEGUNDOS,
                                          esperar_segundos=mod.TRAVA_ESPERA_SEGUNDOS)
        conectar.assert_not_called()
        sessao_cls.assert_not_called()
        liberar.assert_not_called()  # nunca adquiriu -- nao ha o que liberar

    def test_trava_ocupada_tambem_desiste_em_modo_teste(self):
        with mock.patch("stokki.sessao_uso.adquirir", return_value=False), \
             mock.patch("stokki.sessao_uso.em_uso", return_value="outro-processo"), \
             mock.patch("stokki.sessao_uso.liberar"), \
             mock.patch("sincronizar_recebimentos_wms.wms_pedidos.conectar") as conectar, \
             mock.patch("sincronizar_recebimentos_wms.StokkiSession") as sessao_cls:
            codigo = mod.main(["--modo-teste"])

        self.assertEqual(codigo, 0)  # desistir por trava ocupada nao e falha
        conectar.assert_not_called()
        sessao_cls.assert_not_called()

    def test_trava_livre_adquire_roda_e_libera_no_final(self):
        with mock.patch("stokki.sessao_uso.adquirir", return_value=True) as adquirir, \
             mock.patch("stokki.sessao_uso.liberar") as liberar, \
             mock.patch("sincronizar_recebimentos_wms.wms_pedidos.conectar") as conectar, \
             mock.patch("sincronizar_recebimentos_wms.StokkiSession"), \
             mock.patch("sincronizar_recebimentos_wms.rodar", return_value={"lidos": 0}) as rodar_mock:
            conn_falso = mock.Mock()
            conectar.return_value = conn_falso
            codigo = mod.main(["--limite", "5"])

        self.assertEqual(codigo, 0)
        adquirir.assert_called_once()
        rodar_mock.assert_called_once()
        conn_falso.close.assert_called_once()
        liberar.assert_called_once_with(mod.DONO_TRAVA)

    def test_liberar_roda_mesmo_se_a_rodada_estourar(self):
        with mock.patch("stokki.sessao_uso.adquirir", return_value=True), \
             mock.patch("stokki.sessao_uso.liberar") as liberar, \
             mock.patch("sincronizar_recebimentos_wms.wms_pedidos.conectar") as conectar, \
             mock.patch("sincronizar_recebimentos_wms.StokkiSession"), \
             mock.patch("sincronizar_recebimentos_wms.rodar", side_effect=RuntimeError("bug")):
            conectar.return_value = mock.Mock()
            with self.assertRaises(RuntimeError):
                mod.main(["--limite", "5"])

        liberar.assert_called_once_with(mod.DONO_TRAVA)


if __name__ == "__main__":
    unittest.main()
