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
import re
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


def _pagina_com_situacao(situacao, com_itens=True):
    """
    Pagina de detalhe do pedido como a Stokki devolve: o status e o
    PRIMEIRO badge-status depois de "Situação:" (os seguintes sao
    marcadores tipo "Remessa Expressa"). Mesmo formato que
    retiradas/acompanhar_retiradas.py e pedidos_parados_triagem.py leem.
    """
    return f"""
<table class="table">
  <tr><th>Situação:</th><td>
      <span class="badge badge-status badge-info">{situacao}</span>
      <span class="badge badge-status badge-warning">Remessa Expressa</span>
  </td></tr>
</table>
{_HTML_ITENS if com_itens else ""}
"""


class _RespostaFalsa:
    """Imita o suficiente de requests.Response pro codigo da rotina."""

    def __init__(self, json_data=None, text="", status_code=200):
        self._json = json_data
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._json


class SessaoFalsa:
    """
    Duble de StokkiSession -- nunca toca a rede. Responde como a Stokki de
    PRODUCAO responde, medido pelo Hugo em 23/09 com o piloto
    (cliente='48'):

        status="all"        pedi 200 -> voltaram   0  (iTotalDisplayRecords=0)
        status=""           pedi 100 -> voltaram  30  (iTotalDisplayRecords=30)
        status="Sent"       pedi 200 -> voltaram 200  (iTotalDisplayRecords=3465)
        status="Delivered"  pedi 200 -> voltaram   0  (iTotalDisplayRecords=0)

    Duas fidelidades que o duble antigo NAO tinha, e por isso escondeu o
    critico da rodada 2:
      1. `state="all"` devolve LISTA VAZIA, sempre. O duble antigo
         entregava linhas pra qualquer status, entao a suite inteira (e
         duas revisoes) passou por cima de uma consulta que em producao
         voltaria vazia e faria TODO candidato ser cancelado.
      2. `/show/<id>` de pedido que nao existe responde **500**, nao 404
         (medido pelo Hugo; a mesma nota esta no
         pedidos_parados_triagem desde 28/08). O duble da rodada 2 dizia
         404 e um teste consagrava esse comportamento que producao nao
         tem -- a mesma dobra de duble complacente, um nivel abaixo.

    A listagem respeita start/length, como a Stokki (que honra o tamanho
    de pagina pedido: pedi 200, vieram 200).
    """

    def __init__(self, linhas_por_status, html_por_id):
        self.linhas_por_status = linhas_por_status
        self.html_por_id = html_por_id
        self.chamadas = []  # [(url, params)], pra conferir o que foi pedido

    def get(self, url, params=None, headers=None):
        self.chamadas.append((url, params))
        if url.endswith("/table"):
            status = (params or {}).get("state", "")
            # producao: "all" nao e um filtro valido -- devolve vazio
            todas = [] if status == "all" else self.linhas_por_status.get(status, [])
            inicio = int((params or {}).get("start", 0) or 0)
            tamanho = int((params or {}).get("length", 100) or 100)
            linhas = todas[inicio:inicio + tamanho]
            return _RespostaFalsa(json_data={"aaData": linhas, "iTotalRecords": 385,
                                             "iTotalDisplayRecords": len(todas)})
        if "/show/" in url:
            id_pedido = url.rstrip("/").split("/")[-1]
            if id_pedido not in self.html_por_id:
                # producao: id inexistente da 500, nao 404
                return _RespostaFalsa(text="Server Error", status_code=500)
            return _RespostaFalsa(text=self.html_por_id[id_pedido])
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


class TestLiberarPedidoCancelado(BaseRotina):
    """
    I2 da revisao final: cancelar_reservas era codigo morto -- pedido
    cancelado na Stokki ficava com reserva ATIVA pra sempre, travando o
    disponivel (spec 7.3 item 6).

    Rodada 3: o rotulo de CANCELADO na pagina do pedido virou a unica via
    automatica de liberacao. Pedido que SUMIU nao e mais detectado (ver
    TestSumicoNaoEMaisDetectado) -- decisao do Hugo, 23/09.

    Cuidado que o teste tambem fixa: pedido que saiu dos status varridos
    porque foi EXPEDIDO nao pode ter a reserva liberada -- ela ainda vai
    virar a SAIDA da baixa. Liberar ali baixaria estoque nenhum e o saldo
    ficaria mentindo pra sempre.
    """

    def _reservar_o_pedido(self, id_pedido=40100):
        linhas = _linhas_vazias()
        linhas["Waiting for Carrier"] = [_linha(id_pedido, PILOTO_ID)]
        sess = SessaoFalsa(linhas, {str(id_pedido): _HTML_ITENS})
        mod.rodar(self.conn, sess, PILOTO_NOME, PILOTO_ID, limite=50, modo_teste=False)
        ativas = self.conn.execute(
            "SELECT COUNT(*) n FROM wms_reservas WHERE estado = 'ATIVA'").fetchone()["n"]
        self.assertEqual(ativas, 1)

    def _estado(self, id_stokki=40100):
        return self.conn.execute("SELECT * FROM wms_pedidos WHERE id_stokki = ?",
                                 (id_stokki,)).fetchone()

    def test_pedido_cancelado_na_stokki_tem_as_reservas_liberadas(self):
        self._reservar_o_pedido()
        # a pagina do pedido existe e diz "Cancelado"
        sess = SessaoFalsa(_linhas_vazias(), {"40100": _pagina_com_situacao("Cancelado")})

        res = mod.rodar(self.conn, sess, PILOTO_NOME, PILOTO_ID, limite=50, modo_teste=False)

        self.assertEqual(res["liberados"], 1)
        pedido = self._estado()
        self.assertEqual(pedido["estado_reserva"], "CANCELADO")
        self.assertIn("cancelado", pedido["motivo_cancelamento"].lower())
        # o disponivel voltou pro galpao
        self.assertEqual(wms_pedidos.disponivel_por_lote(self.conn, 1)[0]["disponivel"], 10)

    def test_rotulo_parecido_com_cancelado_nao_libera(self):
        # "Cancelamento solicitado" nao e "Cancelado": liberar aqui
        # marcaria o pedido CANCELADO pra sempre (reservar_pedido pula
        # CANCELADO) e, se ele embarcasse depois, sairia sem SAIDA --
        # mercadoria fantasma na prateleira, em silencio.
        self._reservar_o_pedido()
        sess = SessaoFalsa(_linhas_vazias(),
                           {"40100": _pagina_com_situacao("Cancelamento solicitado")})

        res = mod.rodar(self.conn, sess, PILOTO_NOME, PILOTO_ID, limite=50, modo_teste=False)

        self.assertEqual(res["liberados"], 0)
        self.assertEqual(self._estado()["estado_reserva"], "RESERVADO")
        self.assertEqual(self.conn.execute(
            "SELECT estado FROM wms_reservas").fetchone()["estado"], "ATIVA")

    def test_pedido_expedido_nunca_e_cancelado_por_esta_varredura(self):
        # A varredura nao pode cancelar quem foi EXPEDIDO: a mercadoria
        # saiu, entao a reserva vira SAIDA (ver TestBaixarPedidoJaExpedido).
        # Cancelar aqui baixaria estoque nenhum e o saldo mentiria pra sempre.
        self._reservar_o_pedido()
        sess = SessaoFalsa(_linhas_vazias(), {"40100": _pagina_com_situacao("Enviado")})

        res = mod.rodar(self.conn, sess, PILOTO_NOME, PILOTO_ID, limite=50, modo_teste=False)

        self.assertEqual(res["liberados"], 0)
        self.assertEqual(self._estado()["estado_reserva"], "BAIXADO")
        n_cancelada = self.conn.execute(
            "SELECT COUNT(*) n FROM wms_reservas WHERE estado = 'CANCELADA'").fetchone()["n"]
        self.assertEqual(n_cancelada, 0)

    def test_modo_teste_nunca_libera_reserva(self):
        self._reservar_o_pedido()
        sess = SessaoFalsa(_linhas_vazias(), {})

        res = mod.rodar(self.conn, sess, PILOTO_NOME, PILOTO_ID, limite=50, modo_teste=True)

        self.assertEqual(res["liberados"], 0)
        self.assertNotEqual(self._estado()["estado_reserva"], "CANCELADO")


class TestBaixarPedidoJaExpedido(BaseRotina):
    """
    Achado da revisao final: baixar_por_expedicao tinha UM chamador so
    (expedir_pedidos.py, dentro do laco dos servicos entregues da Vuupt).
    Pedido que sai pela Stokki sem passar por rota (retirada no galpao,
    redespacho, transportadora propria) nunca chegava la: a reserva ficava
    ATIVA pra sempre, sumia do disponivel e fazia pedido novo nascer
    PARCIAL por falta que nao existe.

    A varredura do fim da rodada da a BAIXA (o estoque saiu de verdade) --
    nunca cancela a reserva, que e coisa diferente e falsificaria o saldo.
    """

    def _reservar_o_pedido(self, id_pedido=40200):
        linhas = _linhas_vazias()
        linhas["Waiting for Carrier"] = [_linha(id_pedido, PILOTO_ID)]
        sess = SessaoFalsa(linhas, {str(id_pedido): _HTML_ITENS})
        mod.rodar(self.conn, sess, PILOTO_NOME, PILOTO_ID, limite=50, modo_teste=False)

    def _rodada_com_situacao(self, situacao, id_pedido=40200):
        """A pagina do pedido (e so ela) diz a situacao -- nenhuma listagem
        participa da decisao."""
        sess = SessaoFalsa(_linhas_vazias(), {str(id_pedido): _pagina_com_situacao(situacao)})
        return mod.rodar(self.conn, sess, PILOTO_NOME, PILOTO_ID, limite=50, modo_teste=False)

    def _saidas(self):
        return self.conn.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(quantidade), 0) q FROM wms_movimentos "
            "WHERE tipo = 'SAIDA'").fetchone()

    def test_pedido_expedido_na_stokki_recebe_a_baixa(self):
        self._reservar_o_pedido()
        self.assertEqual(wms_pedidos.disponivel_por_lote(self.conn, 1)[0]["disponivel"], 6)

        res = self._rodada_com_situacao("Sent")

        self.assertEqual(res["baixados"], 1)
        self.assertEqual(res["liberados"], 0)
        pedido = self.conn.execute("SELECT * FROM wms_pedidos WHERE id_stokki = 40200").fetchone()
        self.assertEqual(pedido["estado_reserva"], "BAIXADO")
        reserva = self.conn.execute("SELECT * FROM wms_reservas").fetchone()
        self.assertEqual(reserva["estado"], "CONSUMIDA")
        # saiu de verdade do estoque fisico: 10 - 4 = 6
        saidas = self._saidas()
        self.assertEqual(saidas["n"], 1)
        self.assertEqual(saidas["q"], 4)
        self.assertEqual(self.conn.execute(
            "SELECT quantidade FROM wms_saldos WHERE produto_id = 1").fetchone()["quantidade"], 6)

    def test_situacao_em_portugues_tambem_conta_como_expedido(self):
        # a Stokki em pt-br mostra "Enviado" no badge-status
        self._reservar_o_pedido()

        res = self._rodada_com_situacao("Enviado")

        self.assertEqual(res["baixados"], 1)
        self.assertEqual(self._saidas()["n"], 1)

    def test_rodar_de_novo_nao_baixa_em_dobro(self):
        self._reservar_o_pedido()
        self._rodada_com_situacao("Sent")

        res = self._rodada_com_situacao("Sent")

        # sem reserva ATIVA o pedido nem e candidato -- e mesmo que fosse, o
        # uuid deterministico do movimento barraria a segunda SAIDA.
        self.assertEqual(res["baixados"], 0)
        saidas = self._saidas()
        self.assertEqual(saidas["n"], 1)
        self.assertEqual(saidas["q"], 4)

    def test_pedido_nao_expedido_mantem_a_reserva_intacta(self):
        # pedido que saiu dos status varridos sem ter sido expedido nem
        # cancelado (ex.: voltou pra "Em espera" na Stokki): nada de baixa,
        # nada de cancelamento -- a reserva fica como estava.
        self._reservar_o_pedido()

        res = self._rodada_com_situacao("Em espera")

        self.assertEqual(res["baixados"], 0)
        self.assertEqual(res["liberados"], 0)
        self.assertEqual(self._saidas()["n"], 0)
        reserva = self.conn.execute("SELECT * FROM wms_reservas").fetchone()
        self.assertEqual(reserva["estado"], "ATIVA")
        self.assertEqual(reserva["quantidade_un"], 4)
        pedido = self.conn.execute("SELECT * FROM wms_pedidos WHERE id_stokki = 40200").fetchone()
        self.assertEqual(pedido["estado_reserva"], "RESERVADO")

    def test_pedido_ainda_nos_status_varridos_nao_e_candidato(self):
        # o pedido apareceu na varredura normal desta rodada: nao ha nada a
        # concluir sobre ele no fim, e nenhuma consulta extra e feita.
        self._reservar_o_pedido()
        linhas = _linhas_vazias()
        linhas["Waiting for Carrier"] = [_linha(40200, PILOTO_ID)]
        sess = SessaoFalsa(linhas, {"40200": _pagina_com_situacao("Enviado")})

        res = mod.rodar(self.conn, sess, PILOTO_NOME, PILOTO_ID, limite=50, modo_teste=False)

        self.assertEqual(res["baixados"], 0)
        self.assertEqual(self._saidas()["n"], 0)
        # so o GET da reserva (1 por status varrido em que ele aparece)
        self.assertEqual(len([c for c in sess.chamadas if "/show/" in c[0]]), 1)

    def test_modo_teste_nunca_baixa(self):
        self._reservar_o_pedido()
        sess = SessaoFalsa(_linhas_vazias(), {"40200": _pagina_com_situacao("Enviado")})

        res = mod.rodar(self.conn, sess, PILOTO_NOME, PILOTO_ID, limite=50, modo_teste=True)

        self.assertEqual(res["baixados"], 0)
        self.assertEqual(self._saidas()["n"], 0)

    def test_consulta_que_falhou_nao_baixa_nem_libera(self):
        # sessao derrubada / Stokki fora do ar: ausencia de resposta nunca
        # e evidencia de nada.
        self._reservar_o_pedido()

        class SessaoQueFalhaNoDetalhe(SessaoFalsa):
            def get(self, url, params=None, headers=None):
                if "/show/40200" in url:
                    raise RuntimeError("401 da Stokki (sessao derrubada)")
                return super().get(url, params, headers)

        res = mod.rodar(self.conn, SessaoQueFalhaNoDetalhe(_linhas_vazias(), {}),
                        PILOTO_NOME, PILOTO_ID, limite=50, modo_teste=False)

        self.assertEqual(res["baixados"], 0)
        self.assertEqual(res["liberados"], 0)
        self.assertEqual(self._saidas()["n"], 0)
        self.assertEqual(self.conn.execute(
            "SELECT estado FROM wms_reservas").fetchone()["estado"], "ATIVA")

    def test_uma_consulta_por_candidato_e_nenhuma_listagem_geral(self):
        # a Stokki e de terceiros e de sessao unica: a varredura custa 1
        # GET por candidato (e no dia a dia nao ha candidato nenhum).
        self._reservar_o_pedido()
        sess = SessaoFalsa(_linhas_vazias(), {"40200": _pagina_com_situacao("Em espera")})

        mod.rodar(self.conn, sess, PILOTO_NOME, PILOTO_ID, limite=50, modo_teste=False)

        detalhes = [c for c in sess.chamadas if "/show/40200" in c[0]]
        self.assertEqual(len(detalhes), 1)
        listagens_gerais = [c for c in sess.chamadas
                            if c[0].endswith("/table") and (c[1] or {}).get("state") == "all"]
        self.assertEqual(listagens_gerais, [])


class TestEvidenciaDiretaNaPaginaDoPedido(BaseRotina):
    """
    CRITICAL da rodada 2 (23/09), medido pelo Hugo na Stokki de producao:
    `status="all"` -- o valor que a varredura usava pra montar a lista de
    situacoes -- devolve LISTA VAZIA (iTotalDisplayRecords=0). Com zero
    linhas, o criterio de fim de lista concluia "lista completa", TODO
    candidato ficava sem situacao e era lido como "sumiu": CANCELADO em
    vez de baixado, toda rodada, desde a primeira.

    O duble antigo devolvia linhas pra qualquer status, e por isso a
    suite inteira e duas revisoes passaram por cima disso. A rede que
    faltava e o primeiro teste desta classe.

    A varredura nao usa listagem nenhuma: le a pagina do proprio pedido.
    """

    ID_PRESO = 40300

    def setUp(self):
        super().setUp()
        linhas = _linhas_vazias()
        linhas["Waiting for Carrier"] = [_linha(self.ID_PRESO, PILOTO_ID)]
        mod.rodar(self.conn, SessaoFalsa(linhas, {str(self.ID_PRESO): _HTML_ITENS}),
                  PILOTO_NOME, PILOTO_ID, limite=50, modo_teste=False)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) n FROM wms_reservas WHERE estado = 'ATIVA'").fetchone()["n"], 1)

    def _estado(self):
        return self.conn.execute("SELECT * FROM wms_pedidos WHERE id_stokki = ?",
                                 (self.ID_PRESO,)).fetchone()

    def _saidas(self):
        return self.conn.execute(
            "SELECT COUNT(*) n FROM wms_movimentos WHERE tipo = 'SAIDA'").fetchone()["n"]

    def test_status_que_nao_devolve_nada_nao_faz_ninguem_ser_cancelado(self):
        # A REDE QUE FALTOU. Nenhuma listagem devolve linha nenhuma (e o
        # "all" nunca devolve, como em producao); a pagina do pedido diz
        # "Enviado". Antes: liberados=1, CANCELADO, zero SAIDA, disponivel
        # de volta a 10 com a mercadoria ja no caminhao.
        sess = SessaoFalsa(_linhas_vazias(), {str(self.ID_PRESO): _pagina_com_situacao("Enviado")})

        res = mod.rodar(self.conn, sess, PILOTO_NOME, PILOTO_ID, limite=50, modo_teste=False)

        self.assertEqual(res["liberados"], 0)
        self.assertEqual(res["baixados"], 1)
        self.assertEqual(self._estado()["estado_reserva"], "BAIXADO")
        self.assertEqual(self._saidas(), 1)
        self.assertEqual(self.conn.execute(
            "SELECT estado FROM wms_reservas").fetchone()["estado"], "CONSUMIDA")

    def test_a_varredura_nao_consulta_listagem_nenhuma(self):
        sess = SessaoFalsa(_linhas_vazias(), {str(self.ID_PRESO): _pagina_com_situacao("Enviado")})

        mod.rodar(self.conn, sess, PILOTO_NOME, PILOTO_ID, limite=50, modo_teste=False)

        # so as listagens dos status varridos (3), nenhuma "all" nem "Sent"
        estados_pedidos = [(c[1] or {}).get("state") for c in sess.chamadas if c[0].endswith("/table")]
        self.assertEqual(sorted(estados_pedidos), sorted(mod.STATUS_INTERESSANTES))

    def test_o_all_da_stokki_realmente_volta_vazio_no_duble(self):
        # guarda do proprio duble: se alguem voltar a montar decisao em
        # cima de listagem geral, o teste acima passa a falhar por este
        # motivo -- e nao por acidente do duble.
        sess = SessaoFalsa(_linhas_vazias(), {})
        resposta = sess.get("http://x/pt-br/administrator/inventory/outbound/table",
                            params={"state": "all", "start": 0, "length": 200})
        self.assertEqual(resposta.json()["aaData"], [])
        self.assertEqual(resposta.json()["iTotalDisplayRecords"], 0)

    def test_pagina_ilegivel_nao_conclui_nada(self):
        # HTML 200 mas sem o bloco "Situação:" (layout mudou, pagina de
        # erro amigavel): nao da pra concluir, entao nao se mexe em nada.
        sess = SessaoFalsa(_linhas_vazias(), {str(self.ID_PRESO): "<html>oi</html>"})

        res = mod.rodar(self.conn, sess, PILOTO_NOME, PILOTO_ID, limite=50, modo_teste=False)

        self.assertEqual(res["liberados"], 0)
        self.assertEqual(res["baixados"], 0)
        self.assertEqual(self.conn.execute(
            "SELECT estado FROM wms_reservas").fetchone()["estado"], "ATIVA")

    def test_500_nao_cancela(self):
        # o que producao devolve pra id inexistente -- e tambem o que ela
        # devolve quando esta quebrada. Nao da pra distinguir, entao nao
        # se conclui nada.
        res = mod.rodar(self.conn, SessaoFalsa(_linhas_vazias(), {}),
                        PILOTO_NOME, PILOTO_ID, limite=50, modo_teste=False)

        self.assertEqual(res["liberados"], 0)
        self.assertEqual(self.conn.execute(
            "SELECT estado FROM wms_reservas").fetchone()["estado"], "ATIVA")


class TestSumicoNaoEMaisDetectado(BaseRotina):
    """
    Important 1 da rodada 3. O ramo do 404 foi removido: pedido
    inexistente devolve 500 (medido em producao), entao o 404 so podia
    acontecer se a ROTA /…/outbound/show/<id> parasse de resolver
    (mudanca de path, proxy, WAF). Nesse cenario TODOS os candidatos
    devolveriam 404 e a rotina cancelaria em massa -- 20 por rodada, 4
    rodadas por hora: a catastrofe original entrando por outra porta.

    Um ramo que so pode abrir pelo motivo errado e pior que ramo nenhum.
    O preco esta escrito: sumico nao e mais detectado automaticamente; a
    saida e o botao "Liberar reserva" da tela /wms/estoque.
    """

    def setUp(self):
        super().setUp()
        linhas = _linhas_vazias()
        linhas["Waiting for Carrier"] = [_linha(40700, PILOTO_ID)]
        mod.rodar(self.conn, SessaoFalsa(linhas, {"40700": _HTML_ITENS}),
                  PILOTO_NOME, PILOTO_ID, limite=50, modo_teste=False)

    def _reserva(self):
        return self.conn.execute("SELECT estado FROM wms_reservas").fetchone()["estado"]

    def test_404_nao_cancela_ninguem(self):
        class SessaoCom404(SessaoFalsa):
            def get(self, url, params=None, headers=None):
                if "/show/" in url:
                    self.chamadas.append((url, params))
                    return _RespostaFalsa(text="Not Found", status_code=404)
                return super().get(url, params, headers)

        res = mod.rodar(self.conn, SessaoCom404(_linhas_vazias(), {}),
                        PILOTO_NOME, PILOTO_ID, limite=50, modo_teste=False)

        self.assertEqual(res["liberados"], 0)
        self.assertEqual(res["baixados"], 0)
        self.assertEqual(self._reserva(), "ATIVA")
        self.assertEqual(self.conn.execute(
            "SELECT estado_reserva FROM wms_pedidos").fetchone()["estado_reserva"], "RESERVADO")

    def test_rota_quebrada_nao_cancela_a_carteira_inteira(self):
        # o cenario que motivou a decisao: a rota some e TODOS os
        # candidatos passam a dar 404 ao mesmo tempo.
        html_1un = _HTML_ITENS.replace("<td>4</td>", "<td>1</td>")
        linhas = _linhas_vazias()
        linhas["Waiting for Carrier"] = [_linha(40710 + i, PILOTO_ID) for i in range(3)]
        mod.rodar(self.conn, SessaoFalsa(linhas, {str(40710 + i): html_1un for i in range(3)}),
                  PILOTO_NOME, PILOTO_ID, limite=50, modo_teste=False)

        class SessaoRotaQuebrada(SessaoFalsa):
            def get(self, url, params=None, headers=None):
                if "/show/" in url:
                    self.chamadas.append((url, params))
                    return _RespostaFalsa(text="Not Found", status_code=404)
                return super().get(url, params, headers)

        res = mod.rodar(self.conn, SessaoRotaQuebrada(_linhas_vazias(), {}),
                        PILOTO_NOME, PILOTO_ID, limite=50, modo_teste=False)

        self.assertEqual(res["liberados"], 0)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) n FROM wms_reservas WHERE estado = 'ATIVA'").fetchone()["n"], 4)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) n FROM wms_pedidos WHERE estado_reserva = 'CANCELADO'").fetchone()["n"], 0)


class TestRotulosDeExpedicao(unittest.TestCase):
    """M2: _esta_expedido decide dar BAIXA em estoque -- casar por pedaco
    de palavra transformava rotulo negado em saida de mercadoria."""

    def test_rotulos_que_significam_saiu_do_galpao(self):
        for rotulo in ("Sent", "sent", "Enviado", "ENVIADO", "Entregue", "Delivered",
                       '<span class="badge">Enviado</span>'):
            limpo = re.sub(r"<[^>]+>", " ", rotulo).strip()
            self.assertTrue(mod._esta_expedido(limpo), rotulo)

    def test_rotulo_negado_nunca_vira_baixa(self):
        for rotulo in ("Nao entregue", "Não entregue", "Nao enviado", "Not sent",
                       "Reenviado", "Aguardando Transportador", "Em espera", "", None):
            self.assertFalse(mod._esta_expedido(rotulo), rotulo)


class TestEnsaioDoModoTeste(BaseRotina):
    """I1: --modo-teste e a mitigacao recomendada antes da primeira rodada
    de verdade. Um modo teste que nao imprime nada nao mitiga nada."""

    def setUp(self):
        super().setUp()
        linhas = _linhas_vazias()
        linhas["Waiting for Carrier"] = [_linha(40500, PILOTO_ID)]
        mod.rodar(self.conn, SessaoFalsa(linhas, {"40500": _HTML_ITENS}),
                  PILOTO_NOME, PILOTO_ID, limite=50, modo_teste=False)

    def _ensaio(self, situacao):
        """situacao=None -> a pagina do pedido responde 500 (o que producao
        devolve pra id inexistente): nao da pra concluir nada."""
        html = {} if situacao is None else {"40500": _pagina_com_situacao(situacao)}
        return mod.rodar(self.conn, SessaoFalsa(_linhas_vazias(), html), PILOTO_NOME, PILOTO_ID,
                         limite=50, modo_teste=True)

    def test_modo_teste_diz_que_baixaria_sem_baixar(self):
        res = self._ensaio("Enviado")

        self.assertEqual(res["ensaio"], {"baixaria": 1, "liberaria": 0, "manteria": 0})
        self.assertEqual(res["baixados"], 0)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) n FROM wms_movimentos WHERE tipo = 'SAIDA'").fetchone()["n"], 0)
        self.assertEqual(self.conn.execute(
            "SELECT estado FROM wms_reservas").fetchone()["estado"], "ATIVA")

    def test_modo_teste_diz_que_liberaria_sem_liberar(self):
        res = self._ensaio("Cancelado")

        self.assertEqual(res["ensaio"], {"baixaria": 0, "liberaria": 1, "manteria": 0})
        self.assertEqual(res["liberados"], 0)
        self.assertNotEqual(self.conn.execute(
            "SELECT estado_reserva FROM wms_pedidos").fetchone()["estado_reserva"], "CANCELADO")

    def test_modo_teste_conta_como_mantido_o_que_nao_deu_pra_ler(self):
        res = self._ensaio(None)   # 500

        self.assertEqual(res["ensaio"], {"baixaria": 0, "liberaria": 0, "manteria": 1})

    def test_modo_teste_relata_o_que_manteria(self):
        res = self._ensaio("Em espera")

        self.assertEqual(res["ensaio"], {"baixaria": 0, "liberaria": 0, "manteria": 1})


class TestTetoDeCandidatosPorRodada(BaseRotina):
    """I2: a primeira rodada encontra o passivo inteiro. Cada candidato e
    um GET na Stokki (sessao unica) e, quando baixa, um commit no dados.db
    compartilhado. O que sobra vai pra proxima rodada, 15 min depois."""

    def setUp(self):
        super().setUp()
        # 3 pedidos reservados (1 UN cada, pra caber no saldo de 10)
        html_1un = _HTML_ITENS.replace("<td>4</td>", "<td>1</td>")
        linhas = _linhas_vazias()
        linhas["Waiting for Carrier"] = [_linha(40600 + i, PILOTO_ID) for i in range(3)]
        sess = SessaoFalsa(linhas, {str(40600 + i): html_1un for i in range(3)})
        mod.rodar(self.conn, sess, PILOTO_NOME, PILOTO_ID, limite=50, modo_teste=False)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) n FROM wms_reservas WHERE estado = 'ATIVA'").fetchone()["n"], 3)

    def test_teto_corta_a_rodada_e_a_proxima_termina(self):
        paginas = {str(40600 + i): _pagina_com_situacao("Enviado") for i in range(3)}
        teto_original = mod.CANDIDATOS_POR_RODADA
        mod.CANDIDATOS_POR_RODADA = 2
        try:
            sess1 = SessaoFalsa(_linhas_vazias(), paginas)
            primeira = mod.rodar(self.conn, sess1, PILOTO_NOME, PILOTO_ID,
                                 limite=50, modo_teste=False)
            self.assertEqual(primeira["baixados"], 2)
            # o teto tambem limita as CONSULTAS: 2 GETs, nao 3
            self.assertEqual(len([c for c in sess1.chamadas if "/show/" in c[0]]), 2)
            self.assertEqual(self.conn.execute(
                "SELECT COUNT(*) n FROM wms_reservas WHERE estado = 'ATIVA'").fetchone()["n"], 1)

            segunda = mod.rodar(self.conn, SessaoFalsa(_linhas_vazias(), paginas),
                                PILOTO_NOME, PILOTO_ID, limite=50, modo_teste=False)
        finally:
            mod.CANDIDATOS_POR_RODADA = teto_original

        self.assertEqual(segunda["baixados"], 1)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) n FROM wms_reservas WHERE estado = 'ATIVA'").fetchone()["n"], 0)
        # 3 SAIDAs no total, uma por pedido -- nada baixado duas vezes
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) n FROM wms_movimentos WHERE tipo = 'SAIDA'").fetchone()["n"], 3)


class TestListagemCheiaNaoDesligaAVarredura(BaseRotina):
    """
    Important 3 da rodada 3. Ate a rodada 2, listagem que enchia a pagina
    fazia `rodar` voltar ANTES de montar candidatos -- desligando a
    varredura inteira, inclusive a BAIXA. O .service roda sem --limite
    (default 50) e o piloto ja bate 30 num status: o desligamento
    aconteceria justamente nos dias de mais movimento, com um WARNING num
    log que ninguem le.

    A premissa do guarda morreu junto com a listagem: hoje a decisao vem
    da pagina de cada candidato, entao completude de listagem nao importa.
    """

    def setUp(self):
        super().setUp()
        linhas = _linhas_vazias()
        linhas["Waiting for Carrier"] = [_linha(40800, PILOTO_ID)]
        mod.rodar(self.conn, SessaoFalsa(linhas, {"40800": _HTML_ITENS}),
                  PILOTO_NOME, PILOTO_ID, limite=50, modo_teste=False)

    def test_com_listagem_cheia_a_baixa_continua_acontecendo(self):
        # limite=1 e a listagem devolve 1 linha (de outro pedido): pagina
        # cheia. O 40800 continua sendo verificado e baixado.
        linhas = _linhas_vazias()
        linhas["Waiting for Carrier"] = [_linha(40801, PILOTO_ID)]
        sess = SessaoFalsa(linhas, {"40801": _HTML_ITENS,
                                    "40800": _pagina_com_situacao("Enviado")})

        res = mod.rodar(self.conn, sess, PILOTO_NOME, PILOTO_ID, limite=1, modo_teste=False)

        self.assertEqual(res["baixados"], 1)
        self.assertEqual(self.conn.execute(
            "SELECT estado_reserva FROM wms_pedidos WHERE id_stokki = 40800"
        ).fetchone()["estado_reserva"], "BAIXADO")
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) n FROM wms_movimentos WHERE tipo = 'SAIDA'").fetchone()["n"], 1)

    def test_pedido_nao_lido_por_pagina_cheia_so_e_logado(self):
        # o pedido nao foi lido nesta rodada (pagina cheia com outro),
        # entao vira candidato -- mas a pagina dele diz que ainda esta em
        # status normal, entao nada acontece.
        linhas = _linhas_vazias()
        linhas["Waiting for Carrier"] = [_linha(40801, PILOTO_ID)]
        sess = SessaoFalsa(linhas, {"40801": _HTML_ITENS,
                                    "40800": _pagina_com_situacao("Aguardando Transportador")})

        res = mod.rodar(self.conn, sess, PILOTO_NOME, PILOTO_ID, limite=1, modo_teste=False)

        self.assertEqual(res["baixados"], 0)
        self.assertEqual(res["liberados"], 0)
        self.assertEqual(self.conn.execute(
            "SELECT estado FROM wms_reservas WHERE pedido_id = "
            "(SELECT id FROM wms_pedidos WHERE id_stokki = 40800)").fetchone()["estado"], "ATIVA")


class TestRotacaoDaFilaDeCandidatos(BaseRotina):
    """
    Important 4 da rodada 3: sem rotacao, a fila sai sempre na mesma
    ordem e um candidato permanentemente inconclusivo (pagina que nao
    responde, rotulo fora das listas brancas, pedido parado em status nao
    varrido) fica pra sempre na cabeca, escondendo quem esta atras
    enquanto o teto nao alcanca todo mundo.
    """

    def setUp(self):
        super().setUp()
        html_1un = _HTML_ITENS.replace("<td>4</td>", "<td>1</td>")
        linhas = _linhas_vazias()
        linhas["Waiting for Carrier"] = [_linha(40900 + i, PILOTO_ID) for i in range(3)]
        mod.rodar(self.conn, SessaoFalsa(linhas, {str(40900 + i): html_1un for i in range(3)}),
                  PILOTO_NOME, PILOTO_ID, limite=50, modo_teste=False)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) n FROM wms_reservas WHERE estado = 'ATIVA'").fetchone()["n"], 3)
        # os tres ficam inconclusivos pra sempre: rotulo que nao esta em
        # lista branca nenhuma (o caso do candidato "travado")
        self.paginas = {str(40900 + i): _pagina_com_situacao("Em analise") for i in range(3)}

    def _consultados_na_rodada(self, numero_da_rodada):
        sess = SessaoFalsa(_linhas_vazias(), self.paginas)
        original = mod._rodada_atual
        mod._rodada_atual = lambda: numero_da_rodada
        try:
            mod.rodar(self.conn, sess, PILOTO_NOME, PILOTO_ID, limite=50, modo_teste=False)
        finally:
            mod._rodada_atual = original
        return [c[0].rsplit("/", 1)[-1] for c in sess.chamadas if "/show/" in c[0]]

    def test_candidato_travado_na_cabeca_nao_esconde_os_outros(self):
        teto_original = mod.CANDIDATOS_POR_RODADA
        mod.CANDIDATOS_POR_RODADA = 1
        try:
            rodadas = [self._consultados_na_rodada(n) for n in (0, 1, 2)]
        finally:
            mod.CANDIDATOS_POR_RODADA = teto_original

        # uma consulta por rodada, e cada rodada olha um pedido diferente
        self.assertEqual([len(r) for r in rodadas], [1, 1, 1])
        self.assertEqual({r[0] for r in rodadas}, {"40900", "40901", "40902"})

    def test_sem_estouro_de_teto_a_ordem_nao_e_mexida(self):
        # 3 candidatos e teto 20: rotacionar seria ruido
        vistos = self._consultados_na_rodada(7)
        self.assertEqual(vistos, ["40900", "40901", "40902"])


class TestTravaStokki(unittest.TestCase):
    """
    Correcao 1 da revisao (Important, 22/09): a rotina tem que respeitar a
    trava cooperativa de stokki/sessao_uso.py -- a sessao Stokki e unica,
    e um login concorrente no meio da rodada (stokki-wms-sincronizar-
    produtos, agente-importacao-stokki, o painel) derruba os cookies com
    401. Duble/monkeypatch do modulo sessao_uso -- nenhum teste aqui toca
    a Stokki nem o banco de producao (wms_pedidos.conectar e StokkiSession
    tambem duplados, pra provar que nem chegam a ser chamados).
    """

    def test_trava_ocupada_desiste_sem_gravar_e_sem_chamar_a_stokki(self):
        # Sai com 0 (I9 da revisao final): o .service tem
        # OnFailure=stokki-alerta-falha@%n -- sair com 1 alertava a cada
        # rodada em que outro processo estava usando a Stokki. Desistir por
        # trava ocupada e operacao normal, nao falha (mesmo precedente de
        # notificar_transportadoras.py e roteirizacao/documentacao_rota.py).
        with mock.patch("stokki.sessao_uso.adquirir", return_value=False) as adquirir, \
             mock.patch("stokki.sessao_uso.em_uso", return_value="outro-processo"), \
             mock.patch("stokki.sessao_uso.liberar") as liberar, \
             mock.patch("sincronizar_pedidos_wms.wms_pedidos.conectar") as conectar, \
             mock.patch("sincronizar_pedidos_wms.StokkiSession") as sessao_cls:
            codigo = mod.main(["--limite", "5"])

        self.assertEqual(codigo, 0)
        adquirir.assert_called_once_with(mod.DONO_TRAVA, ttl_segundos=mod.TRAVA_TTL_SEGUNDOS,
                                          esperar_segundos=mod.TRAVA_ESPERA_SEGUNDOS)
        conectar.assert_not_called()
        sessao_cls.assert_not_called()
        liberar.assert_not_called()  # nunca adquiriu -- nao ha o que liberar

    def test_trava_ocupada_tambem_desiste_em_modo_teste(self):
        # decisao (Hugo/revisao, 22/09): --modo-teste tambem respeita a
        # trava -- ele so pula a ESCRITA, mas ainda faz leitura de verdade
        # (listar_pedidos) na mesma sessao unica, e um login concorrente
        # derrubaria essas leituras com 401 do mesmo jeito.
        with mock.patch("stokki.sessao_uso.adquirir", return_value=False) as adquirir, \
             mock.patch("stokki.sessao_uso.em_uso", return_value="outro-processo"), \
             mock.patch("stokki.sessao_uso.liberar"), \
             mock.patch("sincronizar_pedidos_wms.wms_pedidos.conectar") as conectar, \
             mock.patch("sincronizar_pedidos_wms.StokkiSession") as sessao_cls:
            codigo = mod.main(["--modo-teste"])

        self.assertEqual(codigo, 0)  # desistir por trava ocupada nao e falha
        adquirir.assert_called_once()
        conectar.assert_not_called()
        sessao_cls.assert_not_called()

    def test_trava_livre_adquire_roda_e_libera_no_final(self):
        with mock.patch("stokki.sessao_uso.adquirir", return_value=True) as adquirir, \
             mock.patch("stokki.sessao_uso.liberar") as liberar, \
             mock.patch("sincronizar_pedidos_wms.wms_pedidos.conectar") as conectar, \
             mock.patch("sincronizar_pedidos_wms.StokkiSession") as sessao_cls, \
             mock.patch("sincronizar_pedidos_wms.rodar", return_value={"lidos": 0}) as rodar_mock:
            conn_falso = mock.Mock()
            conectar.return_value = conn_falso
            codigo = mod.main(["--limite", "5"])

        self.assertEqual(codigo, 0)
        adquirir.assert_called_once()
        rodar_mock.assert_called_once()
        conn_falso.close.assert_called_once()
        liberar.assert_called_once_with(mod.DONO_TRAVA)

    def test_liberar_roda_mesmo_se_a_rodada_estourar(self):
        # a trava nao pode ficar presa se rodar() explodir no meio.
        with mock.patch("stokki.sessao_uso.adquirir", return_value=True), \
             mock.patch("stokki.sessao_uso.liberar") as liberar, \
             mock.patch("sincronizar_pedidos_wms.wms_pedidos.conectar") as conectar, \
             mock.patch("sincronizar_pedidos_wms.StokkiSession"), \
             mock.patch("sincronizar_pedidos_wms.rodar", side_effect=RuntimeError("bug")):
            conectar.return_value = mock.Mock()
            with self.assertRaises(RuntimeError):
                mod.main(["--limite", "5"])

        liberar.assert_called_once_with(mod.DONO_TRAVA)


if __name__ == "__main__":
    unittest.main()
