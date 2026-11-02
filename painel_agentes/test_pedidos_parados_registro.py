# -*- coding: utf-8 -*-
"""
test_pedidos_parados_registro.py

Registro de pedidos parados direto no painel (aba Registrar, 24/10/2026):
tabela pedidos_parados_registros, apagar em 15 min, listas de hoje/ontem
e a triagem lendo da tabela local em vez do Lovable.

Rodar (de dentro de painel_agentes/):
    py -3.11 -m unittest test_pedidos_parados_registro -v
"""
import io
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(Path(__file__).parent))

import pedidos_parados_triagem as ppt  # noqa: E402


def _fmt(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "t.db"
        self.fotos = Path(self.tmp.name) / "fotos"
        self._p = [mock.patch.object(ppt, "DB_PATH", self.db),
                   mock.patch.object(ppt, "PASTA_FOTOS", self.fotos)]
        for p in self._p:
            p.start()

    def tearDown(self):
        for p in self._p:
            p.stop()
        self.tmp.cleanup()

    def _um(self, sql, *args):
        c = ppt._conectar()
        try:
            return c.execute(sql, args).fetchone()
        finally:
            c.close()  # no Windows, conexao aberta trava o rmtree do tearDown

    def _inserir(self, numero, quando, por="x", volumes=2):
        c = ppt._conectar()
        c.execute("INSERT INTO pedidos_parados_registros (order_number, order_number_bruto, volumes, registrado_por, criado_em) "
                  "VALUES (?,?,?,?,?)", (numero, numero, volumes, por, _fmt(quando)))
        c.commit()
        c.close()


class TestRegistrar(_Base):
    def test_normaliza_e_guarda_bruto(self):
        r = ppt.registrar("PS-40525", "3", "quecia")
        row = self._um("SELECT * FROM pedidos_parados_registros WHERE id=?", r["id"])
        self.assertEqual((r["order_number"], row["order_number_bruto"], row["volumes"], row["registrado_por"]),
                         ("40525", "PS-40525", 3, "quecia"))
        self.assertEqual(r["ja_hoje"], 0)

    def test_pedido_vazio_ou_so_prefixo(self):
        for bruto in ("", "   ", "PS-"):
            with self.assertRaises(ppt.ErroRegistro):
                ppt.registrar(bruto, 1, "x")

    def test_pedido_com_caractere_fora_do_formato(self):
        # o numero cai em onclick="duplicar('...')" na triagem: nada de aspas, <, >, &, `
        for bruto in ("40525'", "<b>1</b>", "1&2", "x`y", "1" * 41, "1');alert(1)//", "1234/2", "../.."):
            with self.assertRaises(ppt.ErroRegistro, msg=bruto):
                ppt.registrar(bruto, 1, "x")
        for bruto in ("NF 777", "ABC-12_3", "#PS 40525"):
            ppt.registrar(bruto, 1, "x")

    def test_compacta_pontos_e_espacos_quando_sobra_numero(self):
        # a ajuda da tela promete que "PS-", "#" e pontos sao limpos
        for bruto, esperado in (("PS 40.525", "40525"), ("40.525", "40525"), ("ps-40 525", "40525"), ("ABC-12.3", "ABC-12.3")):
            self.assertEqual(ppt.registrar(bruto, 1, "x")["order_number"], esperado, bruto)

    def test_volumes_invalidos(self):
        for v in (0, -1, "abc", "", "3.0", None):
            with self.assertRaises(ppt.ErroRegistro):
                ppt.registrar("40525", v, "x")

    def test_foto_invalida_e_grande(self):
        with self.assertRaises(ppt.ErroRegistro):
            ppt.registrar("1", 1, "x", foto=b"nao e imagem")
        with self.assertRaises(ppt.ErroRegistro):
            ppt.registrar("1", 1, "x", foto=b"\xff\xd8" + b"0" * ppt.MAX_FOTO_BYTES)
        self.assertEqual(self._um("SELECT COUNT(*) FROM pedidos_parados_registros")[0], 0)

    def test_foto_jpg_vai_pra_pasta(self):
        r = ppt.registrar("1", 1, "x", foto=b"\xff\xd8\xff\xe0teste")
        self.assertTrue((self.fotos / str(r["id"]) / "foto.jpg").is_file())
        self.assertEqual(ppt.caminho_foto(r["id"]).name, "foto.jpg")
        self.assertIsNone(ppt.caminho_foto(r["id"] + 99))

    def test_segundo_registro_no_dia_conta_ja_hoje(self):
        ppt.registrar("40525", 3, "quecia")
        self.assertEqual(ppt.registrar("40525", 3, "werlly")["ja_hoje"], 1)


class TestApagar(_Base):
    def test_so_autor_e_so_15_min(self):
        rid = ppt.registrar("1", 1, "quecia", foto=b"\x89PNGxx")["id"]
        with self.assertRaises(ppt.ErroRegistro):
            ppt.apagar_registro(rid, "werlly")
        ppt.apagar_registro(rid, "quecia")
        self.assertFalse((self.fotos / str(rid)).exists())
        with self.assertRaises(ppt.ErroRegistro):
            ppt.apagar_registro(rid, "quecia")  # ja apagado -> nao encontrado

    def test_passou_do_prazo(self):
        rid = ppt.registrar("1", 1, "quecia")["id"]
        c = ppt._conectar()
        c.execute("UPDATE pedidos_parados_registros SET criado_em=? WHERE id=?",
                  (_fmt(datetime.now() - timedelta(minutes=16)), rid))
        c.commit()
        c.close()
        with self.assertRaises(ppt.ErroRegistro):
            ppt.apagar_registro(rid, "quecia")


class TestRegistrosHoje(_Base):
    def test_ontem_sem_hoje_e_dias_parado(self):
        hoje = datetime.now().replace(hour=10)
        ontem = hoje - timedelta(days=1)
        self._inserir("A", ontem - timedelta(days=4))
        self._inserir("A", ontem)                       # parado ha 6 dias, nao registrado hoje
        self._inserir("B", ontem)
        self._inserir("B", datetime.now(), por="quecia")  # registrado hoje, agora (dentro dos 15 min)
        r = ppt.registros_hoje("quecia")
        self.assertEqual([x["order_number"] for x in r["hoje"]], ["B"])
        self.assertEqual([(x["order_number"], x["dias_parado"], x["volumes"]) for x in r["ontem_sem_hoje"]],
                         [("A", 6, 2)])
        self.assertTrue(r["hoje"][0]["pode_apagar"])
        self.assertFalse(ppt.registros_hoje("werlly")["hoje"][0]["pode_apagar"])

    def test_ainda_parado_tira_de_ontem(self):
        self._inserir("A", datetime.now() - timedelta(days=1))
        ppt.registrar("A", 2, "x")
        r = ppt.registros_hoje("x")
        self.assertEqual(r["ontem_sem_hoje"], [])
        self.assertEqual(r["hoje"][0]["dias_parado"], 2)

    def test_traz_classificacao(self):
        rid = ppt.registrar("A", 1, "x")["id"]
        with mock.patch.object(ppt.tratativas, "registrar_evento"):  # nao escreve no dados.db real
            ppt.classificar("A", str(rid), "Reenvio", "x")
        self.assertEqual(ppt.registros_hoje("x")["hoje"][0]["classificacao"], "Reenvio")


class TestListagemLocal(_Base):
    """Triagem lendo pedidos_parados_registros (sem Lovable); Vuupt mockada."""

    def setUp(self):
        super().setUp()
        self._p2 = [mock.patch.object(ppt, "_entregues_com_cache", return_value=set()),
                    mock.patch.object(ppt, "_resolver_ids_stokki", return_value={}),
                    mock.patch.object(ppt, "_vuupt", return_value=mock.Mock()),
                    mock.patch.object(ppt, "_embarcadores_por_pedido", return_value={}),
                    mock.patch.object(ppt.tratativas, "registrar_evento")]
        for p in self._p2:
            p.start()
        ppt._cache_base.update(quando=0.0, dados=None)

    def tearDown(self):
        for p in self._p2:
            p.stop()
        ppt._cache_base.update(quando=0.0, dados=None)
        super().tearDown()

    def test_so_dia_mais_recente_e_dias_parado(self):
        agora = datetime.now().replace(hour=10, microsecond=0)
        self._inserir("A", agora - timedelta(days=3))
        self._inserir("A", agora - timedelta(days=1))
        self._inserir("A", agora)
        self._inserir("B", agora - timedelta(days=1))
        lista = ppt.listar_com_classificacao()
        self.assertEqual([(p["order_number"], p["dias_parado"], p["vezes_registrado"], p["prioridade"]) for p in lista],
                         [("A", 4, 3, True)])
        self.assertEqual(lista[0]["registrado_por"], "x")
        self.assertFalse(lista[0]["foto"])
        self.assertEqual(lista[0]["created_at"], agora.strftime("%Y-%m-%dT%H:%M:%S"))
        self.assertEqual(lista[0]["freshhub_id"],
                         str(self._um("SELECT MAX(id) FROM pedidos_parados_registros WHERE order_number='A'")[0]))

    def test_data_local_sem_fuso_e_local(self):
        self.assertEqual(ppt._data_local("2026-10-24 00:30:00"), date(2026, 10, 24))
        self.assertEqual(ppt._data_local("2026-10-24T02:30:00+00:00"), date(2026, 10, 23))

    def test_revisao_urgente_com_hora_local(self):
        rid = ppt.registrar("A", 1, "x")["id"]
        ppt.classificar("A", str(rid), "Entregue", "x")
        ppt._cache_base.update(quando=0.0, dados=None)
        self.assertFalse(ppt.listar_com_classificacao()[0]["revisao_urgente"])
        self._inserir("A", datetime.now() + timedelta(minutes=2))
        ppt._cache_base.update(quando=0.0, dados=None)
        self.assertTrue(ppt.listar_com_classificacao()[0]["revisao_urgente"])

    def test_registrar_invalida_cache(self):
        self.assertEqual(ppt.listar_com_classificacao(), [])
        ppt.registrar("Z", 1, "x")
        self.assertEqual([p["order_number"] for p in ppt.listar_com_classificacao()], ["Z"])

    def test_encaminhar_pausado_nao_toca_nada(self):
        with mock.patch.object(ppt, "ENCAMINHAMENTO_DEMANDAS_ATIVO", False), \
             mock.patch.object(ppt, "_vuupt") as v:
            with self.assertRaises(ValueError):
                ppt.encaminhar_operacao("A", "Cancelados", "x", equipe_id=1)
        v.assert_not_called()

    def test_encaminhar_cria_demanda_no_painel(self):
        # 26/10: a demanda nasce na tela Demandas, pra equipe escolhida; nao duplica
        import acesso_telas
        import demandas
        import grupos_equipe
        for alvo in (demandas, acesso_telas, grupos_equipe):
            p = mock.patch.object(alvo, "DB_PATH", self.db)
            p.start()
            self.addCleanup(p.stop)
        c = demandas.conectar()
        estoque = next(e["id"] for e in demandas.equipes_ativas(c) if e["nome"] == "Estoque")
        c.close()
        vuupt = mock.Mock()
        vuupt.buscar_customer_por_id.return_value = {"name": "Mercado X"}
        with mock.patch.object(ppt, "_vuupt", return_value=vuupt), \
             mock.patch.object(ppt, "_resolver_pedido", return_value=({"customer_id": 9}, "PS-77")), \
             mock.patch.object(ppt.tratativas, "registrar_evento") as evento:
            with self.assertRaises(ValueError):
                ppt.encaminhar_operacao("77", "Cancelados", "hugo")          # sem equipe
            r = ppt.encaminhar_operacao("77", "Cancelados", "hugo", equipe_id=estoque, autor="Hugo")
            de_novo = ppt.encaminhar_operacao("77", "Cancelados", "hugo", equipe_id=estoque)
        self.assertFalse(r["ja_existia"])
        self.assertEqual((de_novo["demanda_id"], de_novo["ja_existia"]), (r["demanda_id"], True))
        self.assertEqual(evento.call_count, 1)
        c = demandas.conectar()
        d = c.execute("SELECT * FROM demandas WHERE id = ?", (r["demanda_id"],)).fetchone()
        c.close()
        self.assertEqual((d["titulo"], d["prioridade"], d["pedido_codigo"], d["origem"], d["criado_por_nome"]),
                         ("Cancelamento de Pedido e Retorno ao Estoque — Mercado X", "ALTA", "PS-77",
                          "pedidos_parados", "Hugo"))

    def test_retornar_para_estoque_classifica_e_cria_demanda(self):
        # Hugo, 29/10: classificacao nova, mesmo fluxo do Descartar (Encaminhar cria Demanda)
        import acesso_telas
        import demandas
        import grupos_equipe
        for alvo in (demandas, acesso_telas, grupos_equipe):
            p = mock.patch.object(alvo, "DB_PATH", self.db)
            p.start()
            self.addCleanup(p.stop)
        rid = ppt.registrar("78", 1, "x")["id"]
        ppt.classificar("78", str(rid), "Retornar para Estoque", "x")
        self.assertEqual(ppt.registros_hoje("x")["hoje"][0]["classificacao"], "Retornar para Estoque")
        c = demandas.conectar()
        estoque = next(e["id"] for e in demandas.equipes_ativas(c) if e["nome"] == "Estoque")
        c.close()
        vuupt = mock.Mock()
        vuupt.buscar_customer_por_id.return_value = {"name": "Mercado X"}
        with mock.patch.object(ppt, "_vuupt", return_value=vuupt), \
             mock.patch.object(ppt, "_resolver_pedido", return_value=({"customer_id": 9}, "PS-78")), \
             mock.patch.object(ppt.tratativas, "registrar_evento"):
            r = ppt.encaminhar_operacao("78", "Retornar para Estoque", "hugo", equipe_id=estoque)
        c = demandas.conectar()
        d = c.execute("SELECT titulo FROM demandas WHERE id = ?", (r["demanda_id"],)).fetchone()
        c.close()
        self.assertEqual(d["titulo"], "Retorno ao Estoque — Mercado X")

    def test_classificar_pausado_nao_conclui(self):
        rid = ppt.registrar("A", 1, "x")["id"]
        # classificar nunca conclui sozinho (so a acao do botao conclui)
        ppt.classificar("A", str(rid), "Cancelados", "x")
        self.assertNotEqual(ppt.registros_hoje("x")["hoje"][0]["acao_status"], "concluida")

    def test_foto_mais_recente_vale_para_o_pedido(self):
        # "Ainda parado" re-registra sem foto: a triagem continua mostrando a foto do registro anterior
        com_foto = ppt.registrar("A", 1, "x", foto=b"\xff\xd8\xff\xe0x")["id"]
        sem_foto = ppt.registrar("A", 1, "x")["id"]
        p = ppt.listar_com_classificacao()[0]
        self.assertEqual((p["foto"], p["foto_id"], p["freshhub_id"]), (True, com_foto, str(sem_foto)))

    def test_flag_ligada_desde_26_10(self):
        # religado pelo Hugo: o Encaminhar cria a demanda na tela Demandas do painel
        self.assertTrue(ppt.ENCAMINHAMENTO_DEMANDAS_ATIVO)


class TestRotas(_Base):
    """Rotas do painel (Flask test_client). O app e carregado como em
    test_batimento_tela.py; requer_auth le config.yaml real (so
    painel_agentes.usuario/senha) e acesso_telas no dados.db real (leitura)."""

    @classmethod
    def setUpClass(cls):
        import importlib.util
        import acesso_telas
        aqui = Path(__file__).parent
        spec = importlib.util.spec_from_file_location("painel_agentes_app", aqui / "painel_agentes.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        mod.app.config["TESTING"] = True
        cls.app = mod.app
        cls.rodada = acesso_telas.rodada_sessoes()["rodada"]

    def _cliente(self, nivel="galpao", usuario="galpao"):
        c = self.app.test_client()
        with c.session_transaction() as sess:
            sess["nivel_acesso"] = nivel
            sess["usuario"] = usuario
            sess["rodada_sessoes"] = self.rodada
        c.environ_base["HTTP_ORIGIN"] = "http://localhost"  # exige_mesma_origem
        return c

    def test_registrar_e_listar(self):
        c = self._cliente()
        r = c.post("/api/pedidos-parados/registrar", data={"order_number": "PS-1", "volumes": "2"})
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.get_json()["order_number"], "1")
        self.assertEqual(c.get("/api/pedidos-parados/registros-hoje").get_json()["hoje"][0]["volumes"], 2)

    def test_volumes_invalido_400(self):
        r = self._cliente().post("/api/pedidos-parados/registrar", data={"order_number": "1", "volumes": "3.0"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("Volumes", r.get_json()["erro"])

    def test_leitura_nao_registra(self):
        r = self._cliente("leitura", "leitor").post("/api/pedidos-parados/registrar",
                                                     data={"order_number": "1", "volumes": "1"})
        self.assertEqual(r.status_code, 403)

    def test_apagar_de_outro_403_e_inexistente_404(self):
        rid = self._cliente().post("/api/pedidos-parados/registrar",
                                   data={"order_number": "1", "volumes": "1"}).get_json()["id"]
        self.assertEqual(self._cliente("operador", "quecia").post("/api/pedidos-parados/registro/apagar",
                                                                  json={"id": rid}).status_code, 403)
        self.assertEqual(self._cliente().post("/api/pedidos-parados/registro/apagar",
                                              json={"id": rid + 9}).status_code, 404)
        self.assertEqual(self._cliente().post("/api/pedidos-parados/registro/apagar",
                                              json={"id": rid}).status_code, 200)

    def test_foto_servida_e_404(self):
        rid = self._cliente().post("/api/pedidos-parados/registrar", data={
            "order_number": "1", "volumes": "1", "foto": (io.BytesIO(b"\xff\xd8\xff\xe0x"), "f.jpg")}).get_json()["id"]
        self.assertEqual(self._cliente().get(f"/pedidos-parados/foto/{rid}").status_code, 200)
        self.assertEqual(self._cliente().get(f"/pedidos-parados/foto/{rid + 9}").status_code, 404)

    def test_encaminhar_pausado_409(self):
        with mock.patch.object(ppt, "ENCAMINHAMENTO_DEMANDAS_ATIVO", False):
            r = self._cliente("total", "hugo").post("/api/pedidos-parados/encaminhar",
                                                    json={"order_number": "1", "classificacao": "Cancelados"})
        self.assertEqual(r.status_code, 409)

    def test_galpao_abre_tela_na_aba_registrar(self):
        r = self._cliente().get("/pedidos-parados")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'ABA_INICIAL = "registrar"', r.data)
        r2 = self._cliente("total", "hugo").get("/pedidos-parados")
        self.assertIn(b'ABA_INICIAL = "triagem"', r2.data)


if __name__ == "__main__":
    unittest.main()
