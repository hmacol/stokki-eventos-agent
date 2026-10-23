# -*- coding: utf-8 -*-
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from galpao3d import banco, coletar
from stokki import sessao_uso

PASTA = Path(__file__).parent / "fixtures"
AGORA = datetime(2026, 10, 23, 10, 0, 0)
CONFIG = {"stokki": {"usuario": "a@x", "senha": "1"}, "vuupt_api": {"token": "t"}}


def _linha(id_, estado="Open"):
    return {"id": f'<a href="/pt-br/administrator/inventory/outbound/show/{id_}">PS-{id_}</a>',
            "client": f"<b>CLIENTE {id_}</b>", "state": estado}


class SessaoFalsa:
    """Serve as fixtures no lugar da página show; conta as aberturas."""

    def __init__(self, html="show_separando.html", ao_abrir=None):
        self.ao_abrir = ao_abrir
        self.html = (PASTA / html).read_text(encoding="utf-8")
        self.shows = []
        self.status = 200   # 500/404 = show falha

    def get(self, url, **kw):
        self.shows.append(url)
        if self.ao_abrir:
            self.ao_abrir(len(self.shows))
        status = self.status

        def raise_for_status():
            if status >= 400:
                raise RuntimeError(f"HTTP {status}")
        return mock.Mock(text=self.html, status_code=status, raise_for_status=raise_for_status)

    def ids(self):
        return [int(u.rsplit("/", 1)[1]) for u in self.shows]


class ColetarStokkiTest(unittest.TestCase):
    def setUp(self):
        pasta = tempfile.TemporaryDirectory()
        self.addCleanup(pasta.cleanup)
        db = Path(pasta.name) / "dados.db"
        self.conn = banco.conectar(db)
        self.addCleanup(self.conn.close)
        self.fila = []
        self.relogio_t = 0
        self.abertos = {}   # estado -> [linhas]
        for p in (mock.patch.object(sessao_uso, "DB_PATH", db),
                  mock.patch.dict(sessao_uso._DONOS_DESTE_PROCESSO, clear=True),
                  mock.patch.object(coletar.sessao_uso.contas, "_ler_config", return_value=CONFIG),
                  mock.patch.object(coletar.sessao_uso, "fila", side_effect=lambda conta="principal": self.fila),
                  mock.patch.object(coletar, "contar_pedidos", return_value={"count_open": 1, "success": True}),
                  mock.patch.object(coletar, "iterar_todos_pedidos", side_effect=self._iterar)):
            p.start()
            self.addCleanup(p.stop)
        self.sessao = SessaoFalsa()
        self.db = db

    def _iterar(self, sessao, status="", **kw):
        linhas = self.abertos.get(status, [])
        yield from linhas   # contrato real: UMA linha (dict) por vez

    def _rodar(self):
        return coletar.coletar_stokki(self.conn, CONFIG, sessao_fabrica=lambda cfg, conta: self.sessao, agora=AGORA,
                                      relogio=lambda: self.relogio_t)

    def test_ocupada_sai_zero_e_registra(self):
        import sqlite3
        sessao_uso._conectar().close()
        c = sqlite3.connect(self.db)
        c.execute("INSERT OR REPLACE INTO stokki_sessao_uso VALUES ('principal', 'outro', '2026-01-01 00:00:00', '2999-01-01 00:00:00')")
        c.commit()
        c.close()
        fabrica = mock.Mock()
        r = coletar.coletar_stokki(self.conn, CONFIG, sessao_fabrica=fabrica, agora=AGORA)
        self.assertEqual(r, "ocupada")
        fabrica.assert_not_called()
        row = self.conn.execute("SELECT fonte, resultado FROM galpao_coletas").fetchone()
        self.assertEqual((row["fonte"], row["resultado"]), ("stokki", "ocupada"))

    def test_mudanca_abre_show_uma_vez(self):
        self.abertos = {"Separating": [_linha(42355)]}
        self.assertEqual(self._rodar(), "ok")
        self.assertEqual(len(self.sessao.shows), 1)
        self.abertos = {"Ready to Pack": [_linha(42355)]}
        self._rodar()
        self.assertEqual(len(self.sessao.shows), 2)
        self._rodar()
        self.assertEqual(len(self.sessao.shows), 2)
        sit = self.conn.execute("SELECT situacao FROM galpao_pedidos WHERE id_stokki=42355").fetchone()[0]
        self.assertEqual(sit, "Em Conferência")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM galpao_contagens").fetchone()[0], 1)

    def test_pulo_de_etapas_grava_toda_linha_do_tempo(self):
        self.abertos = {"Open": [_linha(7)]}
        hist = {"linha_do_tempo": [
            {"quando": datetime(2026, 10, 23, 8, 0), "usuario": "a", "situacao": "Aberto"},
            {"quando": datetime(2026, 10, 23, 8, 5), "usuario": "b", "situacao": "Em separação"},
            {"quando": datetime(2026, 10, 23, 8, 20), "usuario": "c", "situacao": "Em Conferência"},
            {"quando": datetime(2026, 10, 23, 8, 30), "usuario": "d", "situacao": "Aguardando Transportador"},
        ], "operador_tarefa": "b", "tarefa": "", "codigo_separacao": "S1", "conferencia": None,
            "unidades": 3, "itens": 1}
        with mock.patch.object(coletar, "ler_historico", return_value=hist):
            self._rodar()
            self.abertos = {}   # sumiu dos abertos
            self._rodar()
            self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM galpao_eventos WHERE id_stokki=7").fetchone()[0], 4)
            sit = self.conn.execute("SELECT situacao FROM galpao_pedidos WHERE id_stokki=7").fetchone()[0]
            self.assertEqual(sit, "Aguardando Transportador")
            self.assertEqual(banco.gravar_historico(self.conn, 7, "PS-7", "X", sit, hist), 0)
            n_shows = len(self.sessao.shows)
            self._rodar()   # já saiu dos abertos: não reabre
            self.assertEqual(len(self.sessao.shows), n_shows)

    def test_limite_de_40_shows(self):
        self.abertos = {"Separating":[_linha(i) for i in range(1000, 1060)]}
        self._rodar()
        self.assertEqual(len(self.sessao.shows), 40)
        self._rodar()   # sobra vai pra rodada seguinte
        self.assertEqual(len(self.sessao.shows), 60)

    def test_cede_a_vez_quando_aparece_fila(self):
        self.abertos = {"Separating":[_linha(i) for i in range(1, 6)]}
        self.sessao.ao_abrir = lambda n: setattr(self, "fila", ["pipeline"])
        self.assertEqual(self._rodar(), "ok")
        self.assertEqual(len(self.sessao.shows), 1)
        obs = self.conn.execute("SELECT obs FROM galpao_coletas ORDER BY id DESC").fetchone()[0]
        self.assertIn("cedeu a vez", obs)

    def test_orcamento_de_tempo_estoura(self):
        self.abertos = {"Separating":[_linha(i) for i in range(1, 6)]}
        self.sessao.ao_abrir = lambda n: setattr(self, "relogio_t", coletar.ORCAMENTO_SEGUNDOS + 1)
        self.assertEqual(self._rodar(), "ok")
        self.assertEqual(len(self.sessao.shows), 1)

    def test_fila_antes_de_listar_nao_abre_show(self):
        self.abertos = {"Separating": [_linha(1)]}
        self.fila = ["pipeline"]
        self.assertEqual(self._rodar(), "parcial")
        self.assertEqual(self.sessao.shows, [])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM galpao_contagens").fetchone()[0], 1)
        # "parcial" nao conta como coleta ok (idade dos dados usa so "ok")
        self.assertEqual(self.conn.execute("SELECT resultado FROM galpao_coletas").fetchone()[0], "parcial")
        self.assertIsNone(banco.ultima_coleta(self.conn, "stokki", "ok"))

    def test_open_novo_grava_sem_abrir_show(self):
        self.abertos = {"Open": [_linha(i) for i in range(1000, 1060)]}
        self.assertEqual(self._rodar(), "ok")
        self.assertEqual(self.sessao.shows, [])
        rows = self.conn.execute("SELECT codigo, embarcador, situacao FROM galpao_pedidos ORDER BY id_stokki").fetchall()
        self.assertEqual(len(rows), 60)
        self.assertEqual(tuple(rows[0]), ("PS-1000", "CLIENTE 1000", "Aberto"))
        self._rodar()   # continua Open: nada a abrir
        self.assertEqual(self.sessao.shows, [])

    def _conhecido(self, id_, situacao):
        self.conn.execute("INSERT INTO galpao_pedidos (id_stokki, codigo, embarcador, situacao) VALUES (?,?,?,?)",
                          (id_, f"PS-{id_}", "X", situacao))
        self.conn.commit()

    def test_ordem_da_fila_de_shows(self):
        self._conhecido(1, "Aberto")          # -> Separating
        self._conhecido(2, "Aberto")          # -> Ready to Pack
        self._conhecido(3, "Em separação")    # sumiu dos abertos
        self._conhecido(4, "Em espera")       # -> Open (mudou: abre show)
        self.abertos = {"Open": [_linha(4), _linha(9)],        # 9: novo em Open, sem show
                        "Separating": [_linha(1)],
                        "Ready to Pack": [_linha(2)],
                        "On hold": [_linha(5)]}                # 5: novo fora de Open
        self._rodar()
        ids = self.sessao.ids()
        self.assertEqual(ids[:3], [3, 2, 1])
        self.assertEqual(sorted(ids[3:]), [4, 5])

    def test_situacao_vem_da_listagem_mesmo_sem_show(self):
        self._conhecido(2, "Em separação")
        self.abertos = {"Ready to Pack": [_linha(2)]}
        self.sessao.status = 500
        self._rodar()
        sit = self.conn.execute("SELECT situacao FROM galpao_pedidos WHERE id_stokki=2").fetchone()[0]
        self.assertEqual(sit, "Em Conferência")
        self.sessao.status = 200
        self._rodar()   # show que falhou volta na rodada seguinte
        self.assertEqual(self.sessao.ids(), [2, 2])
        self._rodar()
        self.assertEqual(self.sessao.ids(), [2, 2])

    def test_show_que_falha_3_vezes_desiste(self):
        self.abertos = {"Separating": [_linha(8)]}
        self.sessao.status = 500
        for _ in range(5):
            self._rodar()
        self.assertEqual(self.sessao.ids(), [8, 8, 8])
        sit = self.conn.execute("SELECT situacao FROM galpao_pedidos WHERE id_stokki=8").fetchone()[0]
        self.assertEqual(sit, "Em separação")   # continua na listagem: situacao da listagem
        self.sessao.status = 200
        self.abertos = {"Ready to Pack": [_linha(8)]}   # nova mudanca: tenta de novo
        self._rodar()
        self.assertEqual(self.sessao.ids(), [8, 8, 8, 8])

    def test_sumido_com_show_falhando_vira_fora_dos_abertos(self):
        self._conhecido(3, "Em separação")
        self.sessao.status = 500
        for _ in range(5):
            self._rodar()
        self.assertEqual(self.sessao.ids(), [3, 3, 3])
        sit = self.conn.execute("SELECT situacao FROM galpao_pedidos WHERE id_stokki=3").fetchone()[0]
        self.assertEqual(sit, coletar.SITUACAO_FORA)

    def test_show_404_desiste_na_hora(self):
        self._conhecido(3, "Em separação")
        self.sessao.status = 404
        self._rodar()
        self._rodar()
        self.assertEqual(self.sessao.ids(), [3])
        sit = self.conn.execute("SELECT situacao FROM galpao_pedidos WHERE id_stokki=3").fetchone()[0]
        self.assertEqual(sit, coletar.SITUACAO_FORA)

    def test_dono_de_teste_nao_libera_trava_do_timer(self):
        import sqlite3
        sessao_uso._conectar().close()
        c = sqlite3.connect(self.db)
        c.execute("INSERT OR REPLACE INTO stokki_sessao_uso VALUES ('principal', ?, '2026-01-01 00:00:00', "
                  "'2999-01-01 00:00:00')", (coletar.DONO,))
        c.commit()
        r = coletar.coletar_stokki(self.conn, CONFIG, sessao_fabrica=lambda cfg, conta: self.sessao, agora=AGORA,
                                   dono=coletar.DONO_TESTE)
        self.assertEqual(r, "ocupada")
        self.assertEqual(c.execute("SELECT dono FROM stokki_sessao_uso").fetchone()[0], coletar.DONO)
        c.close()

    def test_listagem_falha_nao_fecha_ninguem(self):
        self.abertos = {"Separating": [_linha(5)]}
        self._rodar()
        with mock.patch.object(coletar, "iterar_todos_pedidos", side_effect=RuntimeError("500")):
            self.assertEqual(self._rodar(), "erro")
        self.assertEqual(len(self.sessao.shows), 1)
        self.assertEqual(self.conn.execute("SELECT resultado FROM galpao_coletas ORDER BY id DESC").fetchone()[0], "erro")


class ColetarVuuptTest(unittest.TestCase):
    def test_vuupt_resumo(self):
        conn = banco.conectar(Path(tempfile.mkdtemp()) / "d.db")
        self.addCleanup(conn.close)
        rotas = [
            {"name": "R1", "agent_name": "Joao", "status": "on_route", "services": {"data": [
                {"status": "done", "status_done": "success"},
                {"status": "done", "status_done": "failed"},
                {"status": "pending"}]}},
            {"name": "R2", "agent_name": "Ana", "status": "finished", "services": []},
        ]
        r = coletar.coletar_vuupt(conn, CONFIG, listar=lambda token, dia: rotas, agora=AGORA)
        self.assertEqual(r, "ok")
        import json
        res = json.loads(conn.execute("SELECT json FROM galpao_rua").fetchone()[0])
        self.assertEqual((res["carros_na_rua"], res["entregues"], res["insucessos"]), (1, 1, 1))
        r1 = res["rotas"][0]
        self.assertEqual((r1["total"], r1["pendentes"], r1["motorista"]), (3, 1, "Joao"))


class MigracaoTest(unittest.TestCase):
    def test_banco_antigo_ganha_colunas_da_show(self):
        import sqlite3
        db = Path(tempfile.mkdtemp()) / "d.db"
        c = sqlite3.connect(db)
        c.execute("CREATE TABLE galpao_pedidos (id_stokki INTEGER PRIMARY KEY, codigo TEXT, embarcador TEXT,"
                  " situacao TEXT, unidades INTEGER, itens INTEGER, operador_tarefa TEXT,"
                  " codigo_separacao TEXT, atualizado_em TEXT)")
        c.execute("INSERT INTO galpao_pedidos (id_stokki, situacao) VALUES (1, 'Aberto')")
        c.commit()
        c.close()
        conn = banco.conectar(db)
        self.addCleanup(conn.close)
        row = conn.execute("SELECT falhas_show, show_pendente FROM galpao_pedidos").fetchone()
        self.assertEqual(tuple(row), (0, 0))


class RetencaoTest(unittest.TestCase):
    def test_limpa_so_tabelas_por_minuto(self):
        conn = banco.conectar(Path(tempfile.mkdtemp()) / "d.db")
        self.addCleanup(conn.close)
        velho, novo = "2026-10-10 10:00:00", "2026-10-22 10:00:00"
        for q in (velho, novo):
            conn.execute("INSERT INTO galpao_rua VALUES (?, '{}')", (q,))
            conn.execute("INSERT INTO galpao_contagens VALUES (?, '{}')", (q,))
            conn.execute("INSERT INTO galpao_coletas (fonte, quando, resultado) VALUES ('stokki', ?, 'ok')", (q,))
            conn.execute("INSERT INTO galpao_eventos (id_stokki, situacao, quando) VALUES (1, 'Aberto', ?)", (q,))
        coletar.limpar_antigos(conn, AGORA)
        for t in ("galpao_rua", "galpao_contagens", "galpao_coletas"):
            self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0], 1, t)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM galpao_eventos").fetchone()[0], 2)


class MainTest(unittest.TestCase):
    def test_fora_da_janela_nao_coleta(self):
        with mock.patch.object(coletar, "coletar_stokki") as s, mock.patch.object(coletar, "coletar_vuupt") as v, \
                mock.patch.object(coletar, "_carregar_config") as c, mock.patch.object(coletar.banco, "conectar") as b:
            self.assertEqual(coletar.main([], agora=datetime(2026, 10, 23, 22, 7)), 0)
        for m in (s, v, c, b):
            m.assert_not_called()

    def _dono_usado(self, argv):
        with mock.patch.object(coletar, "coletar_stokki", return_value="ok") as s, \
                mock.patch.object(coletar, "coletar_vuupt", return_value="ok"), \
                mock.patch.object(coletar, "_carregar_config", return_value=CONFIG), \
                mock.patch.object(coletar.banco, "conectar"), mock.patch.object(coletar, "limpar_antigos"), \
                mock.patch("builtins.print"):
            coletar.main(argv, agora=AGORA)
        return s.call_args.kwargs["dono"]

    def test_dono_do_timer_e_da_execucao_manual(self):
        self.assertEqual(self._dono_usado([]), "galpao-3d")
        self.assertEqual(self._dono_usado(["--forcar"]), "galpao-3d-teste")
        self.assertEqual(self._dono_usado(["--modo-teste"]), "galpao-3d-teste")


if __name__ == "__main__":
    unittest.main()
