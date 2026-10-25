# -*- coding: utf-8 -*-
"""
test_comparar_pool_freshhub.py -- comparador do pool em sombra Vuupt x freshhub (plano 1.2 do freshhub,
Task 5): o mesmo comparador roda com `--fonte freshhub`; as rodadas das duas fontes convivem na tabela
(chave rodado_em + fonte) e o placar e por fonte.

    py -3.11 -m unittest nucleo.test_comparar_pool_freshhub -v
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_RAIZ))

from nucleo import banco, comparar_pool as cp  # noqa: E402
from nucleo.test_comparar_pool import _s  # noqa: E402

_DDL_ANTIGA = (
    "CREATE TABLE nucleo_reconciliacoes_pool (rodado_em TEXT PRIMARY KEY, total_vuupt INTEGER NOT NULL DEFAULT 0,"
    " total_nucleo INTEGER NOT NULL DEFAULT 0, total_divergencias INTEGER NOT NULL DEFAULT 0,"
    " divergencias_proprias INTEGER NOT NULL DEFAULT 0, duplicados INTEGER NOT NULL DEFAULT 0, detalhes_json TEXT)"
)


class TestFonteFreshhub(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = Path(self._tmp.name) / "t.db"
        self._patch = mock.patch.object(banco, "DB_PATH", self.db)
        self._patch.start()
        self.conn = banco.conectar()

    def tearDown(self):
        self.conn.close()
        self._patch.stop()
        self._tmp.cleanup()

    def _fontes(self):
        return {r[0] for r in self.conn.execute("SELECT fonte FROM nucleo_reconciliacoes_pool")}

    def test_salvar_com_fonte_e_placar_por_fonte(self):
        limpo, sujo = cp.comparar([_s()], [_s()]), cp.comparar([_s()], [])
        cp.salvar(limpo, self.conn, rodado_em="2026-10-27 08:25:00")
        cp.salvar(sujo, self.conn, rodado_em="2026-10-27 08:25:00", fonte="freshhub")  # mesma hora, outra fonte
        cp.salvar(limpo, self.conn, rodado_em="2026-10-28 08:25:00", fonte="freshhub")
        self.assertEqual(cp.dias_limpos_seguidos(self.conn), 1)
        self.assertEqual(cp.dias_limpos_seguidos(self.conn, fonte="freshhub"), 1)
        self.assertEqual([h["dia"] for h in cp.historico(self.conn, 10, fonte="freshhub")],
                         ["2026-10-28", "2026-10-27"])
        self.assertEqual([h["dia"] for h in cp.historico(self.conn, 10)], ["2026-10-27"])
        self.assertEqual(self._fontes(), {"nucleo", "freshhub"})

    def test_tabela_antiga_ganha_a_coluna_fonte_sem_perder_as_rodadas(self):
        self.conn.execute(_DDL_ANTIGA)
        self.conn.execute("INSERT INTO nucleo_reconciliacoes_pool VALUES ('2026-10-24 20:25:06', 243, 242, 1, 1, 0, '{}')")
        self.conn.commit()
        cp._garantir_tabela(self.conn)
        linhas = self.conn.execute("SELECT rodado_em, total_vuupt, fonte FROM nucleo_reconciliacoes_pool").fetchall()
        self.assertEqual([tuple(x) for x in linhas], [("2026-10-24 20:25:06", 243, "nucleo")])
        # a chave passou a (rodado_em, fonte): a rodada freshhub da mesma hora cabe
        cp.salvar(cp.comparar([_s()], [_s()]), self.conn, rodado_em="2026-10-24 20:25:06", fonte="freshhub")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM nucleo_reconciliacoes_pool").fetchone()[0], 2)
        self.assertEqual(cp.dias_limpos_seguidos(self.conn), 0)  # nucleo: 1 divergencia no dia 24
        self.assertEqual(cp.dias_limpos_seguidos(self.conn, fonte="freshhub"), 1)
        cp._garantir_tabela(self.conn)  # de novo: nada muda
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM nucleo_reconciliacoes_pool").fetchone()[0], 2)

    def test_main_com_fonte_freshhub_compara_a_vuupt_com_a_api(self):
        cliente = mock.Mock()
        cliente.obter_pool.return_value = {"itens": [_s()], "dados_de": "2026-10-27T11:10:00Z"}
        vuupt = mock.Mock()
        vuupt.listar_servicos.return_value = [_s()]
        config = {"vuupt_api": {"token": "t"}, "freshhub_api": {"url": "http://127.0.0.1:8100", "token": "fhs_x"}}
        with mock.patch.object(cp, "_carregar_config", return_value=config), \
                mock.patch("vuupt_client.VuuptClient", return_value=vuupt), \
                mock.patch.object(cp.freshhub_client, "de_config", return_value=cliente), \
                mock.patch("sys.stdout"):
            codigo = cp.main(["--salvar", "--fonte", "freshhub", "--db", str(self.db)])
        self.assertEqual(codigo, 0)
        linha = self.conn.execute("SELECT fonte, total_vuupt, total_nucleo FROM nucleo_reconciliacoes_pool").fetchone()
        self.assertEqual(tuple(linha), ("freshhub", 1, 1))
        cliente.obter_pool.assert_called_once()

    def test_main_com_fonte_freshhub_sem_config_da_api_so_avisa(self):
        config = {"vuupt_api": {"token": "t"}}
        with mock.patch.object(cp, "_carregar_config", return_value=config), mock.patch("sys.stdout"), \
                self.assertLogs("nucleo.comparar_pool", level="WARNING") as logs:
            codigo = cp.main(["--salvar", "--fonte", "freshhub", "--db", str(self.db)])
        self.assertEqual(codigo, 0)
        self.assertIn("freshhub_api", logs.output[0])
        self.assertEqual(cp.historico(self.conn, 10, fonte="freshhub"), [])  # nada gravado

    def test_resumo_nomeia_a_fonte(self):
        r = cp.comparar([_s()], [])
        self.assertIn("freshhub", cp.resumir(r, fonte="freshhub"))
        self.assertIn("núcleo", cp.resumir(r))


class TestAtraso(unittest.TestCase):
    """Revisao (25/10, I5) / plano Focus 4: o pool do novo e do snapshot da ultima carga (ate 30 min atras).
    O que so difere por esse atraso e classe propria 'atraso', fora do placar dos 3 dias (como duplicados)."""

    DADOS_DE = "2026-10-27T11:10:00+00:00"

    def test_so_na_vuupt_criado_depois_do_snapshot_e_atraso(self):
        novo = _s(code="#PS-20", id=2, created_at="2026-10-27 11:40:00")  # UTC, depois de dados_de
        velho = _s(code="#PS-30", id=3, created_at="2026-10-27 09:00:00")
        r = cp.comparar([_s(), novo, velho], [_s()], dados_de=self.DADOS_DE)
        self.assertEqual(r["atraso"], ["PS-20"])
        self.assertEqual(r["so_na_vuupt"], ["PS-30"])
        self.assertEqual(r["divergencias_proprias"], 1)
        self.assertEqual(r["total_divergencias"], 2)

    def test_so_no_freshhub_que_a_vuupt_ja_atribuiu_ou_apagou_e_atraso(self):
        por_id = {7: {"id": 7, "status": "assigned"}, 8: None, 9: {"id": 9, "status": "not_assigned"}}
        fonte = [_s(), _s(code="#PS-70", id=7), _s(code="#PS-80", id=8), _s(code="#PS-90", id=9)]
        r = cp.comparar([_s()], fonte, dados_de=self.DADOS_DE, buscar_servico=lambda sid: por_id.get(sid))
        self.assertEqual(r["atraso"], ["PS-70", "PS-80"])
        self.assertEqual(r["so_no_nucleo"], ["PS-90"])  # continua not_assigned la: divergencia de verdade
        self.assertEqual(r["divergencias_proprias"], 1)

    def test_busca_que_falha_mantem_a_divergencia(self):
        def quebra(sid):
            raise RuntimeError("Vuupt fora")
        r = cp.comparar([_s()], [_s(), _s(code="#PS-70", id=7)], dados_de=self.DADOS_DE, buscar_servico=quebra)
        self.assertEqual((r["atraso"], r["so_no_nucleo"]), ([], ["PS-70"]))

    def test_sem_dados_de_nem_busca_nada_muda(self):
        r = cp.comparar([_s(), _s(code="#PS-20", id=2, created_at="2026-10-27 11:40:00")], [_s()])
        self.assertEqual((r["atraso"], r["so_na_vuupt"], r["divergencias_proprias"]), ([], ["PS-20"], 1))

    def test_resumo_e_banco_mostram_o_atraso(self):
        r = cp.comparar([_s(), _s(code="#PS-20", id=2, created_at="2026-10-27 11:40:00")], [_s()],
                        dados_de=self.DADOS_DE)
        self.assertIn("atraso", cp.resumir(r, fonte="freshhub"))
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d, \
                mock.patch.object(banco, "DB_PATH", Path(d) / "t.db"):
            conn = banco.conectar()
            try:
                cp.salvar(r, conn, rodado_em="2026-10-27 08:25:00", fonte="freshhub")
                detalhes = conn.execute("SELECT detalhes_json, divergencias_proprias FROM nucleo_reconciliacoes_pool").fetchone()
            finally:
                conn.close()
        self.assertEqual(detalhes[1], 0)
        self.assertIn('"atraso": ["PS-20"]', detalhes[0])

    def test_main_passa_dados_de_e_a_busca_na_vuupt(self):
        cliente = mock.Mock()
        cliente.obter_pool.return_value = {"itens": [_s(), _s(code="#PS-70", id=7)], "dados_de": self.DADOS_DE}
        vuupt = mock.Mock()
        vuupt.listar_servicos.return_value = [_s()]
        vuupt.buscar_servico_por_id.return_value = {"id": 7, "status": "assigned"}
        config = {"vuupt_api": {"token": "t"}, "freshhub_api": {"url": "http://127.0.0.1:8100", "token": "fhs_x"}}
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d, \
                mock.patch.object(banco, "DB_PATH", Path(d) / "t.db"), \
                mock.patch.object(cp, "_carregar_config", return_value=config), \
                mock.patch("vuupt_client.VuuptClient", return_value=vuupt), \
                mock.patch.object(cp.freshhub_client, "de_config", return_value=cliente), \
                mock.patch("sys.stdout"):
            codigo = cp.main(["--salvar", "--fonte", "freshhub", "--db", str(Path(d) / "t.db")])
            conn = banco.conectar()
            try:
                linha = conn.execute("SELECT divergencias_proprias, total_divergencias FROM nucleo_reconciliacoes_pool").fetchone()
            finally:
                conn.close()
        self.assertEqual(codigo, 0)  # atraso nao e divergencia propria: saida 0, sem e-mail
        self.assertEqual(tuple(linha), (0, 1))
        vuupt.buscar_servico_por_id.assert_called_once_with(7)


if __name__ == "__main__":
    unittest.main()
