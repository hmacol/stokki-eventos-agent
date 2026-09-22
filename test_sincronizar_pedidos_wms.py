# -*- coding: utf-8 -*-
"""
test_sincronizar_pedidos_wms.py

Testes da rotina de 15 min que le os pedidos do embarcador piloto na
Stokki e reserva estoque (sincronizar_pedidos_wms.py). Sessao Stokki e
sempre uma duble (SessaoFalsa) -- nenhum teste aqui toca a rede nem a
Stokki de verdade (login concorrente derruba a sessao da VPS e do outro
projeto, agente-importacao-stokki).

Rodar (da raiz):
    py -3.11 -m unittest test_sincronizar_pedidos_wms -v
"""
import sys
import tempfile
import unittest
from pathlib import Path

_RAIZ = Path(__file__).parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(_RAIZ / "painel_agentes"))

import wms  # noqa: E402
import wms_pedidos  # noqa: E402
import sincronizar_pedidos_wms as mod  # noqa: E402

PILOTO_ID = "48"
PILOTO_NOME = "MARIA DOLORES"

# HTML minimo com a aba "Itens do pedido" -- so o que extrair_itens_do_pedido
# precisa (tabela com Produto | EAN | Quantidade).
_HTML_ITENS = """
<table class="table">
  <thead><tr><th>Produto</th><th>EAN</th><th>Quantidade</th></tr></thead>
  <tbody>
    <tr>
      <td>SKU1 - PRODUTO 1</td>
      <td>111111111111</td>
      <td>4</td>
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

    def __init__(self, linhas_por_status, html_por_id):
        self.linhas_por_status = linhas_por_status
        self.html_por_id = html_por_id
        self.chamadas = []  # [(url, params)], pra conferir o que foi pedido

    def get(self, url, params=None, headers=None):
        self.chamadas.append((url, params))
        if url.endswith("/table"):
            status = (params or {}).get("state", "")
            linhas = self.linhas_por_status.get(status, [])
            return _RespostaFalsa(json_data={"aaData": linhas, "iTotalRecords": 385})
        if "/show/" in url:
            id_pedido = url.rstrip("/").split("/")[-1]
            return _RespostaFalsa(text=self.html_por_id.get(id_pedido, ""))
        raise AssertionError(f"URL inesperada na sessao falsa: {url}")


def _linha(id_pedido, stkkc_id, nome_truncado="MARIA DOLORES IND ...", status="Waiting for Carrier"):
    """Uma linha do aaData no formato real (dict, nao lista) -- as chaves
    sao exatamente as da Stokki: checkbox, id, expedition_date, type,
    motion, client, origin, destination, carrier, marker, state, action."""
    return {
        "checkbox": f'<input type="checkbox" value="{id_pedido}">',
        "id": f'<a href="/pt-br/administrator/inventory/outbound/show/{id_pedido}">#PS-{id_pedido}</a>',
        "expedition_date": "22/09/2026",
        "type": "Saida",
        "motion": "Normal",
        "client": f'{nome_truncado} <span class="text-muted">#stkkc-{stkkc_id}</span>',
        "origin": "Freshlog",
        "destination": "Cliente Final",
        "carrier": "Transportadora X",
        "marker": "",
        "state": status,
        "action": "",
    }


def _linhas_vazias():
    return {status: [] for status in mod.STATUS_INTERESSANTES}


class BaseRotina(unittest.TestCase):
    """Banco temporario com area, posicoes e um produto (SKU1) com saldo
    -- o mesmo catalogo que o _HTML_ITENS espera resolver."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "dados.db"
        self.conn = wms_pedidos.conectar(self.db)
        wms.criar_area(self.conn, "C9", "CONTAINER", "Container 9")
        wms.gerar_posicoes(self.conn, "C9", estantes=2, niveis=2)
        self.conn.execute(
            "INSERT INTO wms_produtos (id, stokki_id, sku, descricao, embarcador, ean, dun, qtd_por_caixa, "
            "unidade, atualizado_em) VALUES (1, 900, 'SKU1', 'PRODUTO 1', 'MARIA DOLORES', "
            "'111111111111', '222222222222', 6, 'UN', '2026-09-21 10:00:00')")
        self.conn.commit()
        wms.registrar_movimento(self.conn, tipo="ENTRADA", produto_id=1, quantidade=10,
                                lote="L-A", validade="2026-10-15", destino="C9-E1-N1")

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()


class TestRodar(BaseRotina):
    def test_pedido_do_piloto_e_registrado_e_reservado(self):
        linhas = _linhas_vazias()
        linhas["Waiting for Carrier"] = [_linha(40001, PILOTO_ID)]
        sess = SessaoFalsa(linhas, {"40001": _HTML_ITENS})

        res = mod.rodar(self.conn, sess, PILOTO_NOME, PILOTO_ID, limite=50, modo_teste=False)

        self.assertEqual(res["lidos"], 1)
        self.assertEqual(res["do_piloto"], 1)
        self.assertEqual(res["ignorados_outro_embarcador"], 0)
        self.assertEqual(res["reservados"], 1)
        self.assertEqual(res["erros"], 0)

        pedido = self.conn.execute("SELECT * FROM wms_pedidos WHERE id_stokki = 40001").fetchone()
        self.assertIsNotNone(pedido)
        self.assertEqual(pedido["codigo_ps"], "PS-40001")
        self.assertEqual(pedido["estado_reserva"], "RESERVADO")
        reserva = self.conn.execute("SELECT * FROM wms_reservas").fetchone()
        self.assertEqual(reserva["quantidade_un"], 4)
        self.assertEqual(reserva["estado"], "ATIVA")

        # o filtro do lado do servidor tem que ter sido usado (correcao 1)
        url_tabela, params_tabela = next(c for c in sess.chamadas if c[0].endswith("/table"))
        self.assertEqual(params_tabela["client"], PILOTO_ID)
        # uma busca so da pagina do pedido (correcao 3) -- nao duas
        chamadas_show = [c for c in sess.chamadas if "/show/" in c[0]]
        self.assertEqual(len(chamadas_show), 1)

    def test_linha_de_outro_embarcador_e_ignorada_mesmo_se_vier_na_listagem(self):
        # Rede de seguranca (correcao 2): simula uma Stokki que, apesar do
        # filtro client= no request, devolvesse tambem uma linha de outro
        # embarcador (#stkkc-99) -- tem que ser barrada antes de qualquer
        # gravacao ou reserva.
        linhas = _linhas_vazias()
        linhas["Waiting for Carrier"] = [_linha(40002, "99", "OUTRO EMBARCADOR ...")]
        sess = SessaoFalsa(linhas, {"40002": _HTML_ITENS})

        res = mod.rodar(self.conn, sess, PILOTO_NOME, PILOTO_ID, limite=50, modo_teste=False)

        self.assertEqual(res["do_piloto"], 0)
        self.assertEqual(res["ignorados_outro_embarcador"], 1)
        pedido = self.conn.execute("SELECT * FROM wms_pedidos WHERE id_stokki = 40002").fetchone()
        self.assertIsNone(pedido)
        # nunca deveria ter buscado a pagina de um pedido de outro embarcador
        self.assertFalse(any("/show/" in url for url, _ in sess.chamadas))

    def test_pedido_sem_stkkc_na_linha_tambem_e_barrado(self):
        # sem #stkkc- nenhum no campo client (formato mudou / veio vazio):
        # a rede de seguranca tem que tratar como "nao e o piloto", nunca
        # deixar passar por omissao.
        linhas = _linhas_vazias()
        linha_sem_stkkc = _linha(40004, PILOTO_ID)
        linha_sem_stkkc["client"] = "SEM MARCACAO NENHUMA"
        linhas["Waiting for Carrier"] = [linha_sem_stkkc]
        sess = SessaoFalsa(linhas, {"40004": _HTML_ITENS})

        res = mod.rodar(self.conn, sess, PILOTO_NOME, PILOTO_ID, limite=50, modo_teste=False)

        self.assertEqual(res["do_piloto"], 0)
        self.assertEqual(res["ignorados_outro_embarcador"], 1)

    def test_modo_teste_nao_grava_nada(self):
        linhas = _linhas_vazias()
        linhas["Waiting for Carrier"] = [_linha(40003, PILOTO_ID)]
        sess = SessaoFalsa(linhas, {"40003": _HTML_ITENS})

        res = mod.rodar(self.conn, sess, PILOTO_NOME, PILOTO_ID, limite=50, modo_teste=True)

        self.assertEqual(res["do_piloto"], 1)
        n_pedidos = self.conn.execute("SELECT COUNT(*) n FROM wms_pedidos").fetchone()["n"]
        self.assertEqual(n_pedidos, 0)
        n_reservas = self.conn.execute("SELECT COUNT(*) n FROM wms_reservas").fetchone()["n"]
        self.assertEqual(n_reservas, 0)
        # em modo teste nem precisa buscar a pagina de detalhe do pedido
        self.assertFalse(any("/show/" in url for url, _ in sess.chamadas))

    def test_rodada_repetida_sobre_pedido_inalterado_nao_duplica_reserva(self):
        # prova que a rotina pode rodar a cada 15 min sem medo: pedido que
        # nao mudou entre rodadas nao ganha reserva nova (reservar_pedido
        # ja e idempotente -- aqui provamos que a rotina inteira tambem e).
        linhas = _linhas_vazias()
        linhas["Waiting for Carrier"] = [_linha(40005, PILOTO_ID)]
        sess = SessaoFalsa(linhas, {"40005": _HTML_ITENS})

        mod.rodar(self.conn, sess, PILOTO_NOME, PILOTO_ID, limite=50, modo_teste=False)
        mod.rodar(self.conn, sess, PILOTO_NOME, PILOTO_ID, limite=50, modo_teste=False)

        n = self.conn.execute("SELECT COUNT(*) n FROM wms_reservas WHERE estado = 'ATIVA'").fetchone()["n"]
        self.assertEqual(n, 1)

    def test_pedido_com_erro_nao_derruba_a_rodada(self):
        # HTML sem a tabela de itens (pedido ruim) -- vira erro, mas o
        # pedido do piloto que vem certo na mesma rodada tem que passar.
        linhas = _linhas_vazias()
        linhas["Waiting for Carrier"] = [_linha(40006, PILOTO_ID), _linha(40007, PILOTO_ID)]
        sess = SessaoFalsa(linhas, {"40006": "<html>sem tabela de itens</html>", "40007": _HTML_ITENS})

        res = mod.rodar(self.conn, sess, PILOTO_NOME, PILOTO_ID, limite=50, modo_teste=False)

        self.assertEqual(res["erros"], 1)
        self.assertEqual(res["reservados"], 1)
        pedido_ok = self.conn.execute("SELECT * FROM wms_pedidos WHERE id_stokki = 40007").fetchone()
        self.assertIsNotNone(pedido_ok)
        pedido_ruim = self.conn.execute("SELECT * FROM wms_pedidos WHERE id_stokki = 40006").fetchone()
        self.assertIsNone(pedido_ruim)


if __name__ == "__main__":
    unittest.main()
