# -*- coding: utf-8 -*-
"""
Worker da fila de Pedidos de Entrada (cria o #PE na Stokki). Nada aqui
abre navegador nem toca a rede: o wizard é dublê (mock) e a listagem da
Stokki é uma SessaoFalsa.

    py -3.11 -m unittest portal_cliente.test_enviar_entradas_stokki -v
"""
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
import entradas  # noqa: E402
import enviar_entradas_stokki as worker  # noqa: E402
from test_entradas import CNPJ, AMANHA, xml_remessa  # noqa: E402

CHAVE = "35260912345678000195550010000412211000000017"
HTML_DETALHE = (_RAIZ / "stokki" / "fixtures" / "recebimento_detalhe_cabecalho.html").read_text(encoding="utf-8")


class _Resp:
    def __init__(self, json_data=None, text=""):
        self._json, self.text, self.status_code = json_data, text, 200

    def raise_for_status(self):
        pass

    def json(self):
        return self._json


class SessaoFalsa:
    def __init__(self, linhas, html_por_id):
        self.linhas, self.html_por_id, self.chamadas = linhas, html_por_id, []

    def get(self, url, params=None, headers=None):
        self.chamadas.append((url, params))
        if url.endswith("/table"):
            return _Resp(json_data={"aaData": self.linhas})
        if "/show/" in url:
            return _Resp(text=self.html_por_id.get(url.rstrip("/").split("/")[-1], ""))
        raise AssertionError(url)


def _linha(id_stokki, ref):
    return {"id": f'<a href="https://freshlog.stokki.com.br/pt-br/administrator/inventory/incoming/show/{id_stokki}">#PE-{id_stokki}</a>'
                  f'<br><span class="text-muted">{ref}</span>', "client": "X <span>#stkkc-48</span>", "state": "Em transito", "arrival_date": ""}


def _linha_cancelada(id_stokki, ref):
    d = _linha(id_stokki, ref)
    d["state"] = "Cancelado"
    return d


class SessaoQuebrada:
    """procurar_pe sobre isso tem que levantar, nunca devolver (None, '')
    como se não tivesse achado (achado 2: falha de CONSULTA != não achar)."""

    def get(self, url, params=None, headers=None):
        raise RuntimeError("rede caiu")


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        raiz = Path(self._tmp.name)
        self._patches = [mock.patch.object(ep, "DB_PATH", raiz / "dados.db"), mock.patch.object(ep, "_RAIZ", raiz),
                         mock.patch.object(ep, "PASTA_XMLS", raiz / "portal_envios"),
                         mock.patch.object(ep, "PASTA_TEMP", raiz / "portal_envios" / "_temporarios"),
                         mock.patch.object(worker, "_avisar_erros", lambda *a, **k: None)]
        for p in self._patches:
            p.start()
        self.conn = entradas.conectar()
        self.conn.execute("CREATE TABLE interno (cnpj_embarcador TEXT, apelido TEXT, nome_remetente TEXT, stkkc_id INTEGER, sender_id INTEGER, email TEXT, notificar_email INTEGER)")
        self.conn.execute("INSERT INTO interno VALUES (?, 'CLIENTE TESTE', 'CLIENTE TESTE', 48, 1, 'c@t.com', 1)", (CNPJ,))
        self.conn.commit()
        entradas.definir_entradas_ativo(self.conn, CNPJ, True)
        self.config = {"portal_cliente": {}}

    def tearDown(self):
        self.conn.close()
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def _anunciar(self, **kw):
        conteudo = xml_remessa(chave=CHAVE, **kw)
        nfe = entradas.ler_nfe_entrada(conteudo, "nota.xml")
        token = ep.guardar_temporario(conteudo)
        entradas.confirmar_entradas(self.conn, CNPJ, [{"token": token, "data_prevista": AMANHA}], "cliente", {})
        return dict(self.conn.execute("SELECT * FROM portal_entradas ORDER BY id DESC LIMIT 1").fetchone())

    def _fila(self):
        return [dict(r) for r in self.conn.execute("SELECT * FROM portal_entradas WHERE stokki_status = 'NA_FILA' AND status = 'ANUNCIADO'")]


class TestSimulado(Base):
    def test_simular_marca_criado_sem_id(self):
        self._anunciar()
        r = worker.processar_lote(self.conn, CNPJ, self._fila(), self.config, simular=True)
        self.assertEqual(r["criados"], 1)
        e = dict(self.conn.execute("SELECT * FROM portal_entradas").fetchone())
        self.assertEqual((e["stokki_status"], e["stokki_id"], e["status"]), ("CRIADO", None, "ANUNCIADO"))


class TestWizardDublado(Base):
    def _rodar(self, resultado_xml):
        with mock.patch.object(worker, "carregar_importador", return_value=(mock.Mock(), mock.Mock())), \
             mock.patch.object(worker, "_credenciais", return_value=("u", "s")), \
             mock.patch.object(worker.sessao_uso, "adquirir", return_value=True), \
             mock.patch.object(worker.sessao_uso, "liberar"), \
             mock.patch.object(worker, "executar_wizard_xml", return_value=resultado_xml) as wiz:
            r = worker.processar_lote(self.conn, CNPJ, self._fila(), self.config)
        return r, wiz

    def test_criado_grava_pe(self):
        self._anunciar()
        r, wiz = self._rodar({CHAVE: {"criado": True, "ja_existia": False, "erro": "", "stokki_id": 2497, "codigo": "#PE-2497", "resposta": "{}"}})
        self.assertEqual(r["criados"], 1)
        e = dict(self.conn.execute("SELECT * FROM portal_entradas").fetchone())
        self.assertEqual((e["stokki_status"], e["stokki_id"], e["stokki_codigo"]), ("CRIADO", 2497, "#PE-2497"))
        # o wizard recebeu a data prevista no formato da Stokki e o arquivo por chave
        _cfg, _u, _s, lote, arquivos = wiz.call_args.args[:5]
        self.assertEqual(lote[0]["chave_nfe"], CHAVE)
        self.assertTrue(arquivos[CHAVE].name.endswith(f"{CHAVE}.xml"))

    def test_ja_existe_na_stokki_vira_criado_sem_criar(self):
        self._anunciar()
        r, _ = self._rodar({CHAVE: {"criado": False, "ja_existia": True, "erro": "", "stokki_id": 2400, "codigo": "#PE-2400", "resposta": ""}})
        self.assertEqual(r["ja_existiam"], 1)
        e = dict(self.conn.execute("SELECT * FROM portal_entradas").fetchone())
        self.assertEqual((e["stokki_status"], e["stokki_id"]), ("CRIADO", 2400))

    def test_recusa_da_stokki_vira_erro_com_mensagem(self):
        self._anunciar()
        r, _ = self._rodar({CHAVE: {"criado": False, "ja_existia": False, "erro": "CNPJ inválido", "stokki_id": None, "codigo": "", "resposta": '{"errors":["CNPJ inválido"]}'}})
        self.assertEqual(r["erros"], 1)
        e = dict(self.conn.execute("SELECT * FROM portal_entradas").fetchone())
        self.assertEqual((e["stokki_status"], e["stokki_erro"], e["status"]), ("ERRO", "CNPJ inválido", "ANUNCIADO"))

    def test_falha_tecnica_volta_pra_fila_e_na_terceira_vira_erro(self):
        self._anunciar()
        for tentativa in (1, 2, 3):
            with mock.patch.object(worker, "carregar_importador", return_value=(mock.Mock(), mock.Mock())), \
                 mock.patch.object(worker, "_credenciais", return_value=("u", "s")), \
                 mock.patch.object(worker.sessao_uso, "adquirir", return_value=True), \
                 mock.patch.object(worker.sessao_uso, "liberar"), \
                 mock.patch.object(worker, "executar_wizard_xml", side_effect=RuntimeError("playwright caiu")):
                worker.processar_lote(self.conn, CNPJ, self._fila(), self.config)
            e = dict(self.conn.execute("SELECT * FROM portal_entradas").fetchone())
            self.assertEqual(e["stokki_tentativas"], tentativa)
            self.assertEqual(e["stokki_status"], "ERRO" if tentativa == 3 else "NA_FILA")

    def test_trava_ocupada_adia_sem_tocar(self):
        self._anunciar()
        with mock.patch.object(worker, "carregar_importador", return_value=(mock.Mock(), mock.Mock())), \
             mock.patch.object(worker.sessao_uso, "adquirir", return_value=False), \
             mock.patch.object(worker.sessao_uso, "em_uso", return_value="outro"), \
             mock.patch.object(worker, "executar_wizard_xml") as wiz:
            r = worker.processar_lote(self.conn, CNPJ, self._fila(), {"portal_cliente": {"espera_stokki_minutos": 0}})
        self.assertEqual(r["adiados"], 1)
        wiz.assert_not_called()
        self.assertEqual(self.conn.execute("SELECT stokki_status FROM portal_entradas").fetchone()[0], "NA_FILA")

    def test_indeterminado_volta_pra_fila_sem_email(self):
        # achado 1: sem resposta/tela claras a Stokki pode ter criado mesmo
        # assim -- nunca ERRO na hora, nunca e-mail de recusa por isso.
        self._anunciar()
        resultado = {CHAVE: {"criado": False, "ja_existia": False, "erro": "", "stokki_id": None, "codigo": "",
                             "resposta": "", "indeterminado": True}}
        with mock.patch.object(worker, "carregar_importador", return_value=(mock.Mock(), mock.Mock())), \
             mock.patch.object(worker, "_credenciais", return_value=("u", "s")), \
             mock.patch.object(worker.sessao_uso, "adquirir", return_value=True), \
             mock.patch.object(worker.sessao_uso, "liberar"), \
             mock.patch.object(worker, "_avisar_erros") as avisar, \
             mock.patch.object(worker, "executar_wizard_xml", return_value=resultado):
            r = worker.processar_lote(self.conn, CNPJ, self._fila(), self.config)
        avisar.assert_not_called()
        self.assertEqual(r["adiados"], 1)
        e = dict(self.conn.execute("SELECT * FROM portal_entradas").fetchone())
        self.assertEqual((e["stokki_status"], e["stokki_tentativas"], e["status"]), ("NA_FILA", 1, "ANUNCIADO"))

    def test_tres_indeterminados_seguidos_vira_erro(self):
        self._anunciar()
        resultado = {CHAVE: {"criado": False, "ja_existia": False, "erro": "", "stokki_id": None, "codigo": "",
                             "resposta": "", "indeterminado": True}}
        for tentativa in (1, 2, 3):
            with mock.patch.object(worker, "carregar_importador", return_value=(mock.Mock(), mock.Mock())), \
                 mock.patch.object(worker, "_credenciais", return_value=("u", "s")), \
                 mock.patch.object(worker.sessao_uso, "adquirir", return_value=True), \
                 mock.patch.object(worker.sessao_uso, "liberar"), \
                 mock.patch.object(worker, "_avisar_erros") as avisar, \
                 mock.patch.object(worker, "executar_wizard_xml", return_value=resultado):
                worker.processar_lote(self.conn, CNPJ, self._fila(), self.config)
            avisar.assert_not_called()
            e = dict(self.conn.execute("SELECT * FROM portal_entradas").fetchone())
            self.assertEqual(e["stokki_tentativas"], tentativa)
            self.assertEqual(e["stokki_status"], "ERRO" if tentativa == 3 else "NA_FILA")

    def test_cancelado_entre_ciclo_e_lote_fica_de_fora(self):
        # achado 3: o snapshot que o ciclo() leu pode estar velho -- se o
        # cliente cancelou nesse meio-tempo, a entrada não pode virar
        # ENVIANDO nem entrar no wizard.
        self._anunciar()
        fila_velha = self._fila()   # snapshot de antes do cancelamento
        self.conn.execute("UPDATE portal_entradas SET status = 'CANCELADO'")
        self.conn.commit()
        with mock.patch.object(worker, "carregar_importador") as ci, \
             mock.patch.object(worker, "executar_wizard_xml") as wiz:
            r = worker.processar_lote(self.conn, CNPJ, fila_velha, self.config)
        ci.assert_not_called()
        wiz.assert_not_called()
        self.assertEqual((r["criados"], r["ja_existiam"], r["erros"], r["adiados"]), (0, 0, 0, 0))
        e = dict(self.conn.execute("SELECT * FROM portal_entradas").fetchone())
        self.assertEqual((e["status"], e["stokki_status"]), ("CANCELADO", "NA_FILA"))


class TestCiclo(Base):
    def test_ciclo_pega_so_anunciadas_na_fila(self):
        self._anunciar()
        self.conn.execute("UPDATE portal_entradas SET status = 'CANCELADO'")
        self.conn.commit()
        with mock.patch.object(worker, "processar_lote") as pl:
            r = worker.ciclo(self.config, simular=True)
        pl.assert_not_called()
        self.assertEqual(r["lotes"], 0)

    def test_enviando_orfao_volta_pra_fila(self):
        self._anunciar()
        self.conn.execute("UPDATE portal_entradas SET stokki_status = 'ENVIANDO', atualizado_em = '2026-01-01 00:00:00'")
        self.conn.commit()
        with mock.patch.object(worker, "processar_lote", return_value={"criados": 0, "ja_existiam": 0, "erros": 0, "adiados": 0}) as pl:
            worker.ciclo(self.config, simular=True)
        self.assertEqual(pl.call_args.args[2][0]["stokki_status"], "NA_FILA")


class TestPuros(unittest.TestCase):
    def test_arrival_date_nunca_passada(self):
        hoje = date.today()
        self.assertEqual(worker._arrival_date((hoje - timedelta(days=1)).isoformat()), hoje.strftime("%d/%m/%Y"))
        self.assertEqual(worker._arrival_date((hoje + timedelta(days=2)).isoformat()), (hoje + timedelta(days=2)).strftime("%d/%m/%Y"))
        self.assertEqual(worker._arrival_date(""), hoje.strftime("%d/%m/%Y"))

    def test_resultados_do_lote_resposta_http_manda(self):
        # 422 com errors explicito: ERRO definitivo (indeterminado=False),
        # mesmo com a tela mostrando "Pedido criado".
        linhas = [{"nome": f"{CHAVE}.xml (12.3 KB)", "invoice": CHAVE, "barra_texto": "Pedido criado", "barra_classe": "progress-bar bg-success", "erros": []}]
        respostas = {CHAVE: {"status": 422, "body": '{"errors": {"po": ["Chave da NFe já utilizada"]}}'}}
        r = worker.resultados_do_lote(linhas, [CHAVE], respostas)
        self.assertEqual(r[CHAVE]["criado"], False)
        self.assertEqual(r[CHAVE]["indeterminado"], False)
        self.assertIn("já utilizada", r[CHAVE]["erro"])
        # 2xx com a tela confirmando: criado, sem duvida
        respostas = {CHAVE: {"status": 200, "body": '{"success":true}'}}
        r2 = worker.resultados_do_lote(linhas, [CHAVE], respostas)
        self.assertEqual((r2[CHAVE]["criado"], r2[CHAVE]["indeterminado"]), (True, False))
        # achado 1: sem resposta capturada e sem linha na tela -- indeterminado,
        # nunca ERRO na hora (nao dá pra saber se criou ou nao)
        r3 = worker.resultados_do_lote([], [CHAVE], {})
        self.assertEqual(r3[CHAVE], {"criado": False, "erro": "", "indeterminado": True})
        # achado 1: 2xx no store mas a tela mostra vermelho SEM detalhar por
        # que -- tambem indeterminado (nao é uma recusa explicita)
        linhas_vermelhas = [{"nome": f"{CHAVE}.xml", "invoice": CHAVE, "barra_texto": "", "barra_classe": "progress-bar bg-danger", "erros": []}]
        r4 = worker.resultados_do_lote(linhas_vermelhas, [CHAVE], {CHAVE: {"status": 200, "body": '{"success":true}'}})
        self.assertEqual(r4[CHAVE], {"criado": False, "erro": "", "indeterminado": True})
        # barra vermelha COM mensagem, sem resposta HTTP capturada: recusa
        # explicita pela tela, tambem conta (indeterminado=False)
        linhas_com_erro = [{"nome": f"{CHAVE}.xml", "invoice": CHAVE, "barra_texto": "", "barra_classe": "progress-bar bg-danger", "erros": ["SKU não cadastrado"]}]
        r5 = worker.resultados_do_lote(linhas_com_erro, [CHAVE], {})
        self.assertEqual(r5[CHAVE], {"criado": False, "erro": "SKU não cadastrado", "indeterminado": False})

    def test_procurar_pe_pela_chave_so_abre_o_detalhe_com_a_mesma_ref(self):
        sess = SessaoFalsa([_linha(2400, "41000"), _linha(2497, "41221")], {"2497": HTML_DETALHE, "2400": "<html>outra</html>"})
        id_stokki, codigo = worker.procurar_pe(sess, "48", "41221", chave_nfe=CHAVE)
        self.assertEqual((id_stokki, codigo), (2497, "#PE-2497"))
        self.assertEqual([u for u, _ in sess.chamadas if "/show/" in u], ["https://freshlog.stokki.com.br/pt-br/administrator/inventory/incoming/show/2497"])
        self.assertEqual(sess.chamadas[0][1]["client"], "48")

    def test_procurar_pe_por_referencia_nao_abre_detalhe(self):
        sess = SessaoFalsa([_linha(2500, "REM-9")], {})
        self.assertEqual(worker.procurar_pe(sess, "48", "REM-9", referencia="REM-9"), (2500, "#PE-2500"))
        self.assertFalse(any("/show/" in u for u, _ in sess.chamadas))

    def test_procurar_pe_sem_par(self):
        sess = SessaoFalsa([_linha(2400, "41000")], {"2400": "<html></html>"})
        self.assertEqual(worker.procurar_pe(sess, "48", "41221", chave_nfe=CHAVE), (None, ""))

    def test_procurar_pe_ignora_pe_cancelado_mesmo_com_a_chave_batendo(self):
        # achado 4: um #PE cancelado nao pode contar como "ja existe" pro
        # dedupe -- mesmo que a chave da NF-e bata no detalhe dele.
        sess = SessaoFalsa([_linha_cancelada(2497, "41221")], {"2497": HTML_DETALHE})
        self.assertEqual(worker.procurar_pe(sess, "48", "41221", chave_nfe=CHAVE), (None, ""))
        self.assertFalse(any("/show/" in u for u, _ in sess.chamadas))

    def test_procurar_pe_consulta_quebrada_levanta_em_vez_de_fingir_que_nao_achou(self):
        # achado 2: falha de CONSULTA (rede, sessao caida) tem que levantar
        # -- nunca virar (None, "") como se so nao tivesse achado, senao o
        # worker sobe o #PE de novo por engano.
        with self.assertRaises(worker.ErroConsultaStokki):
            worker.procurar_pe(SessaoQuebrada(), "48", "41221", chave_nfe=CHAVE)


if __name__ == "__main__":
    unittest.main()
