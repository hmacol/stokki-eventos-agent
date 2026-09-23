# -*- coding: utf-8 -*-
"""
Bloqueio de area nao atendida no portal (Hugo, 23/09/2026): pedido pra
cidade que nao atendemos fica AGUARDANDO_LIBERACAO em vez de ir pra Stokki.

    py -3.11 -m unittest portal_cliente.test_bloqueio_area
"""
import sqlite3
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

import envio_pedidos as ep  # noqa: E402


def _nfe_fake(nf: str, cidade: str, uf: str) -> dict:
    return {"chave_nfe": f"352609681469760001005500100000000{nf.zfill(2)}1000000001"[:44], "numero_nf": nf, "serie": "1",
            "emitida_em": "2026-09-23 10:00:00", "destinatario_doc": "09014480000467", "destinatario_nome": "Cliente X",
            "destinatario_endereco": "Rua A, 1", "destinatario_bairro": "Centro", "destinatario_municipio": cidade,
            "destinatario_uf": uf, "destinatario_cep": "01001000", "destinatario_telefone": "", "volumes": 1, "peso_kg": 1.0,
            "valor_nf": 10.0, "itens": "1x Produto"}


class ClassificarAreaEnvio(unittest.TestCase):
    def _nfe(self, cidade="Sao Paulo", uf="SP"):
        return {"destinatario_endereco": "Rua A, 1", "destinatario_municipio": cidade, "destinatario_uf": uf,
                "destinatario_cep": "01001000"}

    def test_monta_servico_sintetico_e_devolve_tipo(self):
        with mock.patch.object(ep, "_identificar_area", return_value=[({"id": 0}, "fora_sp")]) as ident:
            self.assertEqual(ep.classificar_area_envio(self._nfe("Curitiba", "PR"), {"google_maps": {"api_key": "k"}}), "fora_sp")
        servicos, key = ident.call_args[0]
        self.assertEqual(servicos[0]["address"], "Rua A, 1, Curitiba - PR, 01001000")
        self.assertEqual(key, "k")

    def test_atendida(self):
        with mock.patch.object(ep, "_identificar_area", return_value=[]):
            self.assertIsNone(ep.classificar_area_envio(self._nfe(), {}))

    def test_falha_deixa_passar(self):
        with mock.patch.object(ep, "_identificar_area", side_effect=RuntimeError("geocode")):
            with self.assertLogs(ep.logger, level="WARNING"):
                self.assertIsNone(ep.classificar_area_envio(self._nfe(), {}))

    def test_desligado_no_config(self):
        with mock.patch.object(ep, "_identificar_area", return_value=[({"id": 0}, "fora_sp")]):
            self.assertIsNone(ep.classificar_area_envio(self._nfe(), {"portal_cliente": {"envios": {"bloqueio_area_ativo": False}}}))


class ConfirmarComBloqueio(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        raiz = Path(self.tmp.name)
        self._patches = [mock.patch.object(ep, "DB_PATH", raiz / "d.db"),
                         mock.patch.object(ep, "PASTA_XMLS", raiz / "x"),
                         mock.patch.object(ep, "PASTA_TEMP", raiz / "x" / "_temporarios"),
                         mock.patch.object(ep, "_RAIZ", raiz)]
        for p in self._patches:
            p.start()
        (raiz / "x" / "_temporarios").mkdir(parents=True)
        self.conn = ep.conectar()

    def tearDown(self):
        self.conn.close()
        for p in self._patches:
            p.stop()
        self.tmp.cleanup()

    def _token(self, nf):
        tk = f"tokenteste{nf}"  # _caminho_temporario exige 10-40 chars
        (ep.PASTA_TEMP / f"{tk}.xml").write_bytes(f"<nf>{nf}</nf>".encode())
        return tk

    def test_bloqueado_e_normal_no_mesmo_lote(self):
        nfes = {b"<nf>1</nf>": _nfe_fake("1", "Curitiba", "PR"), b"<nf>2</nf>": _nfe_fake("2", "Sao Paulo", "SP")}
        itens = [{"token": self._token(1), "horario_inicio": "08:00", "horario_fim": "17:00"},
                 {"token": self._token(2), "horario_inicio": "08:00", "horario_fim": "17:00"}]
        with mock.patch.object(ep, "ler_nfe", side_effect=lambda conteudo, *a, **k: dict(nfes[conteudo])), \
             mock.patch.object(ep, "validar_item", return_value={"ok": True, "erros": [], "envio_existente": None}), \
             mock.patch.object(ep, "info_destinatarios", return_value={}), \
             mock.patch.object(ep, "gravar_destinatario"), \
             mock.patch.object(ep, "classificar_area_envio", side_effect=lambda nfe, cfg: "fora_sp" if nfe["destinatario_uf"] == "PR" else None):
            criados = ep.confirmar_envios(self.conn, "68146976000100", itens, "cliente", {}, "nenhuma")
        por_nf = {c["numero_nf"]: c for c in criados}
        self.assertEqual(por_nf["1"]["status"], ep.STATUS_AGUARDANDO_LIBERACAO)
        self.assertEqual(por_nf["1"]["bloqueio_motivo"], "fora_sp")
        self.assertEqual((por_nf["1"]["destinatario_municipio"], por_nf["1"]["destinatario_uf"]), ("Curitiba", "PR"))
        self.assertEqual(por_nf["2"]["status"], ep.STATUS_NA_FILA)
        self.assertIsNone(por_nf["2"]["bloqueio_motivo"])
        rows = {r["numero_nf"]: r for r in self.conn.execute("SELECT numero_nf, status, bloqueio_motivo FROM portal_envios")}
        self.assertEqual(rows["1"]["status"], "AGUARDANDO_LIBERACAO")
        self.assertEqual(rows["1"]["bloqueio_motivo"], "fora_sp")
        self.assertEqual(rows["2"]["status"], "NA_FILA")

    def test_classifica_antes_de_qualquer_escrita(self):
        """Revisao 23/09: geocodificar com transacao de escrita aberta trava o
        cache do geocache (outra conexao) por 5 s por NF. A classificacao
        precisa acontecer antes do 1o INSERT."""
        nfes = {b"<nf>1</nf>": _nfe_fake("1", "Curitiba", "PR"), b"<nf>2</nf>": _nfe_fake("2", "Sao Paulo", "SP")}
        itens = [{"token": self._token(1), "horario_inicio": "08:00", "horario_fim": "17:00"},
                 {"token": self._token(2), "horario_inicio": "08:00", "horario_fim": "17:00"}]
        linhas_ao_classificar = []

        def classificar(nfe, cfg):
            linhas_ao_classificar.append(self.conn.execute("SELECT COUNT(*) FROM portal_envios").fetchone()[0])
            linhas_ao_classificar.append(self.conn.in_transaction)
            return None
        with mock.patch.object(ep, "ler_nfe", side_effect=lambda conteudo, *a, **k: dict(nfes[conteudo])), \
             mock.patch.object(ep, "validar_item", return_value={"ok": True, "erros": [], "envio_existente": None}), \
             mock.patch.object(ep, "info_destinatarios", return_value={}), \
             mock.patch.object(ep, "gravar_destinatario"), \
             mock.patch.object(ep, "classificar_area_envio", side_effect=classificar):
            ep.confirmar_envios(self.conn, "68146976000100", itens, "cliente", {}, "nenhuma")
        self.assertEqual(linhas_ao_classificar, [0, False, 0, False])


def _envio(status, **extra):
    d = {"id": 1, "cnpj_embarcador": "1", "status": status, "criado_em": "2026-09-23 10:00:00", "criado_stokki_em": None,
         "emitida_em": None, "destinatario_doc": "", "destinatario_endereco": "", "destinatario_bairro": "",
         "destinatario_municipio": "Curitiba", "destinatario_uf": "PR", "agendamento_data": None, "numero_nf": "1",
         "referencia": None, "data_expedicao": None, "xml_path": "a.xml", "origem": "xml", "agendamento_pendente": 0,
         "bloqueio_motivo": "fora_sp", "bloqueio_chamado_id": 77, "codigo_pedido": None}
    d.update(extra)
    return d


class LinhaEAcao(unittest.TestCase):
    def test_linha_aguardando(self):
        d = ep._linha(_envio(ep.STATUS_AGUARDANDO_LIBERACAO), {})
        self.assertTrue(d["pode_cancelar"])
        self.assertFalse(d["pode_reagendar"])
        self.assertEqual(d["status_rotulo"], "Aguardando liberação")
        self.assertEqual(d["bloqueio_texto"], "Não atendemos a região de Curitiba/PR.")
        self.assertEqual(d["bloqueio_chamado_id"], 77)

    def test_linha_normal_sem_texto(self):
        self.assertEqual(ep._linha(_envio(ep.STATUS_NA_FILA), {})["bloqueio_texto"], "")

    def test_cancelar_aguardando_resolve_na_hora(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("CREATE TABLE portal_envios (id INTEGER PRIMARY KEY, cnpj_embarcador TEXT, status TEXT, atualizado_em TEXT);"
                           "CREATE TABLE portal_solicitacoes (id INTEGER PRIMARY KEY, envio_id, cnpj_embarcador, tipo, detalhes, status, solicitado_por, criado_em, concluido_em, resposta);"
                           "INSERT INTO portal_envios VALUES (1, '1', 'AGUARDANDO_LIBERACAO', NULL);")
        r = ep.aplicar_acao(conn, _envio(ep.STATUS_AGUARDANDO_LIBERACAO), "cancelar", {}, "cliente")
        self.assertTrue(r["aplicado"])
        self.assertEqual(conn.execute("SELECT status FROM portal_envios").fetchone()[0], "CANCELADO")

    def _conn_acao(self, status, motivo="fora_sp"):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("CREATE TABLE portal_envios (id INTEGER PRIMARY KEY, cnpj_embarcador TEXT, status TEXT, atualizado_em TEXT, erro TEXT, bloqueio_motivo TEXT);"
                           "CREATE TABLE portal_solicitacoes (id INTEGER PRIMARY KEY, envio_id, cnpj_embarcador, tipo, detalhes, status, solicitado_por, criado_em, concluido_em, resposta);"
                           "CREATE TABLE pedidos_dedicados (id INTEGER PRIMARY KEY, envio_id INTEGER, codigo_pedido TEXT, removido_em TEXT, removido_por TEXT);")
        conn.execute("INSERT INTO portal_envios (id, cnpj_embarcador, status, bloqueio_motivo) VALUES (1, '1', ?, ?)", (status, motivo))
        conn.commit()
        return conn

    def test_reenviar_cancelado_com_bloqueio_volta_pra_aguardando(self):
        """Revisao 23/09: Cancelar + Reenviar nao pode furar o bloqueio."""
        conn = self._conn_acao(ep.STATUS_CANCELADO)
        r = ep.aplicar_acao(conn, _envio(ep.STATUS_CANCELADO), "reenviar", {}, "cliente")
        self.assertTrue(r["aplicado"])
        self.assertEqual(conn.execute("SELECT status FROM portal_envios").fetchone()[0], "AGUARDANDO_LIBERACAO")
        self.assertIn("liberação", r["mensagem"])

    def test_reenviar_cancelado_sem_bloqueio_vai_pra_fila(self):
        conn = self._conn_acao(ep.STATUS_CANCELADO, motivo=None)
        ep.aplicar_acao(conn, _envio(ep.STATUS_CANCELADO, bloqueio_motivo=None), "reenviar", {}, "cliente")
        self.assertEqual(conn.execute("SELECT status FROM portal_envios").fetchone()[0], "NA_FILA")

    def test_cancelar_remove_marca_de_dedicado(self):
        """Revisao 23/09: liberado como dedicado e depois cancelado nao pode ir
        pro financeiro."""
        conn = self._conn_acao(ep.STATUS_NA_FILA)
        conn.execute("INSERT INTO pedidos_dedicados (id, envio_id) VALUES (1, 1)")
        conn.commit()
        ep.aplicar_acao(conn, _envio(ep.STATUS_NA_FILA), "cancelar", {}, "cliente")
        self.assertIsNotNone(conn.execute("SELECT removido_em FROM pedidos_dedicados WHERE id = 1").fetchone()[0])

    def test_cancelar_sem_tabela_dedicados_nao_quebra(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("CREATE TABLE portal_envios (id INTEGER PRIMARY KEY, cnpj_embarcador TEXT, status TEXT, atualizado_em TEXT);"
                           "CREATE TABLE portal_solicitacoes (id INTEGER PRIMARY KEY, envio_id, cnpj_embarcador, tipo, detalhes, status, solicitado_por, criado_em, concluido_em, resposta);"
                           "INSERT INTO portal_envios VALUES (1, '1', 'NA_FILA', NULL);")
        r = ep.aplicar_acao(conn, _envio(ep.STATUS_NA_FILA), "cancelar", {}, "cliente")
        self.assertTrue(r["aplicado"])

    def test_listar_envios_inclui_aguardando_antigo(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""CREATE TABLE portal_envios (id INTEGER PRIMARY KEY, cnpj_embarcador TEXT, status TEXT, criado_em TEXT,
            criado_stokki_em, emitida_em, destinatario_doc, destinatario_endereco, destinatario_bairro, destinatario_municipio,
            destinatario_uf, agendamento_data, numero_nf, referencia, data_expedicao, xml_path, origem, agendamento_pendente,
            bloqueio_motivo, bloqueio_chamado_id, codigo_pedido);
            CREATE TABLE portal_solicitacoes (id INTEGER PRIMARY KEY, envio_id, cnpj_embarcador, tipo, detalhes, status, solicitado_por, criado_em, concluido_em, resposta);
            INSERT INTO portal_envios (id, cnpj_embarcador, status, criado_em, numero_nf, xml_path, origem, agendamento_pendente)
              VALUES (1, '1', 'AGUARDANDO_LIBERACAO', '2020-01-01 00:00:00', '1', 'a.xml', 'xml', 0),
                     (2, '1', 'CRIADO', '2020-01-01 00:00:00', '2', 'b.xml', 'xml', 0);""")
        self.assertEqual([l["id"] for l in ep.listar_envios(conn, "1")], [1])

    def test_listar_envios_com_dedicado(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""CREATE TABLE portal_envios (id INTEGER PRIMARY KEY, cnpj_embarcador TEXT, status TEXT, criado_em TEXT,
            criado_stokki_em, emitida_em, destinatario_doc, destinatario_endereco, destinatario_bairro, destinatario_municipio,
            destinatario_uf, agendamento_data, numero_nf, referencia, data_expedicao, xml_path, origem, agendamento_pendente,
            bloqueio_motivo, bloqueio_chamado_id, codigo_pedido);
            CREATE TABLE portal_solicitacoes (id INTEGER PRIMARY KEY, envio_id, cnpj_embarcador, tipo, detalhes, status, solicitado_por, criado_em, concluido_em, resposta);
            CREATE TABLE pedidos_dedicados (id INTEGER PRIMARY KEY, envio_id INTEGER, valor REAL, removido_em TEXT);
            INSERT INTO portal_envios (id, cnpj_embarcador, status, criado_em, numero_nf, xml_path, origem, agendamento_pendente)
              VALUES (1, '1', 'NA_FILA', '2099-01-01 00:00:00', '1', 'a.xml', 'xml', 0), (2, '1', 'NA_FILA', '2099-01-01 00:00:00', '2', 'b.xml', 'xml', 0);
            INSERT INTO pedidos_dedicados VALUES (1, 1, 33.34, NULL), (2, 2, 5, '2026-01-01');""")
        lista = {l["id"]: l for l in ep.listar_envios(conn, "1")}
        self.assertEqual(lista[1]["dedicado"], {"valor": 33.34})
        self.assertIsNone(lista[2]["dedicado"])

    def test_listar_envios_sem_tabela_dedicados(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""CREATE TABLE portal_envios (id INTEGER PRIMARY KEY, cnpj_embarcador TEXT, status TEXT, criado_em TEXT,
            criado_stokki_em, emitida_em, destinatario_doc, destinatario_endereco, destinatario_bairro, destinatario_municipio,
            destinatario_uf, agendamento_data, numero_nf, referencia, data_expedicao, xml_path, origem, agendamento_pendente,
            bloqueio_motivo, bloqueio_chamado_id, codigo_pedido);
            CREATE TABLE portal_solicitacoes (id INTEGER PRIMARY KEY, envio_id, cnpj_embarcador, tipo, detalhes, status, solicitado_por, criado_em, concluido_em, resposta);
            INSERT INTO portal_envios (id, cnpj_embarcador, status, criado_em, numero_nf, xml_path, origem, agendamento_pendente)
              VALUES (1, '1', 'NA_FILA', '2099-01-01 00:00:00', '1', 'a.xml', 'xml', 0);""")
        self.assertIsNone(ep.listar_envios(conn, "1")[0]["dedicado"])


class AbrirBloqueio(unittest.TestCase):
    def _conn(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("CREATE TABLE portal_envios (id INTEGER PRIMARY KEY, status TEXT, bloqueio_chamado_id INTEGER, atualizado_em TEXT);"
                           "INSERT INTO portal_envios VALUES (1,'AGUARDANDO_LIBERACAO',NULL,NULL),(2,'AGUARDANDO_LIBERACAO',NULL,NULL),(3,'NA_FILA',NULL,NULL);")
        return conn

    def test_um_chamado_para_varias_nfs_e_emails(self):
        import bloqueio_area as ba
        conn = self._conn()
        criados = [{"id": 1, "numero_nf": "10", "status": "AGUARDANDO_LIBERACAO", "destinatario_municipio": "Curitiba", "destinatario_uf": "PR", "referencia": None},
                   {"id": 2, "numero_nf": "11", "status": "AGUARDANDO_LIBERACAO", "destinatario_municipio": "Curitiba", "destinatario_uf": "PR", "referencia": None},
                   {"id": 3, "numero_nf": "12", "status": "NA_FILA", "destinatario_municipio": "Sao Paulo", "destinatario_uf": "SP", "referencia": None}]
        cliente = {"cnpj": "68146976000100", "sender_id": 5, "nome": "ACME"}
        config = {"email": {"email_atendimento": "entregas@x"}, "portal_cliente": {"envios": {"forcar_destino": "hugo@x"}}}
        fake_ch = mock.MagicMock()
        fake_ch.criar_chamado.return_value = {"id": 77}
        fake_ch.emails_do_cliente.return_value = ["cliente@x"]
        fake_ch.ORIGEM_SISTEMA, fake_ch.STATUS_AGUARDANDO_FL = "sistema", "AGUARDANDO_FL"
        with mock.patch.object(ba, "_chamados", return_value=fake_ch), mock.patch.object(ba, "enviar_email", return_value=True) as env:
            chamado_id = ba.abrir_bloqueio(conn, cliente, criados, config)
        self.assertEqual(chamado_id, 77)
        fake_ch.criar_chamado.assert_called_once()
        kw = fake_ch.criar_chamado.call_args.kwargs
        self.assertEqual(kw["area"], "envios")
        self.assertEqual(kw["pedido_ref"], "NF 10, 11")
        texto = fake_ch.mensagem_sistema.call_args[0][2]
        self.assertIn("Curitiba/PR", texto)
        self.assertIn("10", texto)
        self.assertNotIn("12", texto)
        self.assertEqual([r[0] for r in conn.execute("SELECT bloqueio_chamado_id FROM portal_envios ORDER BY id")], [77, 77, None])
        destinos = [c.args[0] for c in env.call_args_list]
        self.assertEqual(destinos, [["hugo@x"], ["hugo@x"]])  # cliente e interno, ambos redirecionados
        self.assertIn("cliente@x", env.call_args_list[0].args[2])  # aviso do destino real no corpo

    def test_forcar_destino_vazio_manda_pro_cliente_e_atendimento(self):
        import bloqueio_area as ba
        conn = self._conn()
        criados = [{"id": 1, "numero_nf": "10", "status": "AGUARDANDO_LIBERACAO", "destinatario_municipio": "Curitiba", "destinatario_uf": "PR"}]
        config = {"email": {"email_atendimento": "entregas@x"}, "portal_cliente": {"envios": {"forcar_destino": ""}}}
        fake_ch = mock.MagicMock()
        fake_ch.criar_chamado.return_value = {"id": 1}
        fake_ch.emails_do_cliente.return_value = ["cliente@x"]
        with mock.patch.object(ba, "_chamados", return_value=fake_ch), mock.patch.object(ba, "enviar_email", return_value=True) as env:
            ba.abrir_bloqueio(conn, {"cnpj": "1", "nome": "ACME"}, criados, config)
        self.assertEqual([c.args[0] for c in env.call_args_list], [["cliente@x"], ["entregas@x"]])

    def test_falha_ao_abrir_chamado_libera_as_linhas(self):
        """Revisao 23/09: sem chamado nao ha como liberar; fail-open (NA_FILA) +
        e-mail interno avisando."""
        import bloqueio_area as ba
        conn = self._conn()
        criados = [{"id": 1, "numero_nf": "10", "status": "AGUARDANDO_LIBERACAO", "destinatario_municipio": "Curitiba", "destinatario_uf": "PR"}]
        config = {"email": {"email_atendimento": "entregas@x"}, "portal_cliente": {"envios": {"forcar_destino": "hugo@x"}}}
        fake_ch = mock.MagicMock()
        fake_ch.conectar.side_effect = RuntimeError("banco de chamados fora")
        with mock.patch.object(ba, "_chamados", return_value=fake_ch), mock.patch.object(ba, "enviar_email", return_value=True) as env:
            with self.assertLogs(ba.logger, level="ERROR"):
                self.assertIsNone(ba.abrir_bloqueio(conn, {"cnpj": "1", "nome": "ACME"}, criados, config))
        self.assertEqual(conn.execute("SELECT status FROM portal_envios WHERE id = 1").fetchone()[0], "NA_FILA")
        self.assertEqual(len(env.call_args_list), 1)
        self.assertIn("sem chamado", env.call_args_list[0].args[1].lower())

    def test_avisar_cancelamento_no_chamado(self):
        """Revisao 23/09 (spec, casos de borda): cliente cancela envio bloqueado
        -> mensagem de sistema no chamado."""
        import bloqueio_area as ba
        fake_ch = mock.MagicMock()
        fake_ch.buscar_chamado.return_value = {"id": 77}
        with mock.patch.object(ba, "_chamados", return_value=fake_ch):
            self.assertTrue(ba.avisar_cancelamento({"id": 1, "numero_nf": "10", "bloqueio_chamado_id": 77}, "cliente"))
        texto = fake_ch.mensagem_sistema.call_args[0][2]
        self.assertIn("NF 10", texto)
        self.assertIn("cancel", texto.lower())
        with mock.patch.object(ba, "_chamados", return_value=fake_ch):
            self.assertFalse(ba.avisar_cancelamento({"id": 1, "numero_nf": "10", "bloqueio_chamado_id": None}, "cliente"))

    def test_sem_bloqueados_nao_faz_nada(self):
        import bloqueio_area as ba
        with mock.patch.object(ba, "_chamados") as ch:
            self.assertIsNone(ba.abrir_bloqueio(None, {}, [{"id": 1, "status": "NA_FILA"}], {}))
        ch.assert_not_called()

    def test_default_sem_config_redireciona_pro_hugo(self):
        import bloqueio_area as ba
        self.assertEqual(ba.forcar_destino({}), "hugo@freshlogbr.com")


if __name__ == "__main__":
    unittest.main()
