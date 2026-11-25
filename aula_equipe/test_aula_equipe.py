# -*- coding: utf-8 -*-
"""
Gerador da aula da equipe: ambiente ficticio, roteiros e pecas do gravador.

    py -3.11 -m unittest aula_equipe.test_aula_equipe   (da raiz)
"""
import json
import sqlite3
import tempfile
import unittest
from unittest import mock
from datetime import date, datetime
from pathlib import Path

import aula_equipe  # noqa: F401  (poe painel_agentes/ no sys.path)
from aula_equipe import ambiente, dados, gravar, roteiro, servidor
from aula_portal.ambiente import MARCA_CONFIG


class TestAmbiente(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.raiz = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_config_so_tem_logins_ficticios_e_recusa_o_de_verdade(self):
        texto = ambiente.escrever_config(self.raiz).read_text(encoding="utf-8")
        self.assertTrue(texto.startswith(MARCA_CONFIG))
        self.assertIn("usuario_galpao: galpao", texto)
        for segredo in ("vuupt", "stokki", "gcs", "email", "freshhub_api", "lalamove"):
            self.assertNotIn(segredo, texto.lower())
        (self.raiz / "config.yaml").write_text("painel_agentes:\n  senha: de-verdade\n", encoding="utf-8")
        with self.assertRaises(ambiente.AmbienteRecusado):
            ambiente.escrever_config(self.raiz)

    def test_banco_de_verdade_nao_e_apagado(self):
        (self.raiz / "dados").mkdir()
        sqlite3.connect(self.raiz / "dados" / "dados.db").execute("CREATE TABLE x (a)").connection.close()
        with self.assertRaises(ambiente.AmbienteRecusado):
            ambiente.semear_banco(self.raiz, "retornos", esquema=None)

    def test_retornos_tem_tres_motoristas_e_o_de_ontem_atrasado(self):
        import retornos
        db = ambiente.semear_banco(self.raiz, "retornos", esquema=None)
        conn = retornos.conectar(db)
        try:
            grupos = retornos.listar_pendentes(conn, date.today())
        finally:
            conn.close()
        self.assertEqual([g["motorista_nome"] for g in grupos], ["Marcos Ribeiro", "Carlos Andrade", "Rafael Souza"])
        self.assertTrue(grupos[0]["desde_ontem"])
        carlos = grupos[1]["pedidos"]
        self.assertEqual([p["parada_id"] for p in carlos], [dados.paradas_do_video()[p["codigo"]] for p in carlos])
        self.assertEqual([p["parcial"] for p in carlos], [False, False, True, False])

    def test_coletas_tem_a_fila_e_uma_coleta_de_ontem(self):
        from coletas import regras
        db = ambiente.semear_banco(self.raiz, "coletas", esquema=None)
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        try:
            ids = {p["id_stokki"] for g in regras.a_coletar(conn)["grupos"] for p in g["pedidos"]}
            hist = regras.historico(conn)
        finally:
            conn.close()
        self.assertEqual(ids, {f[0] for f in dados.FILA_COLETA})
        self.assertEqual([h["id"] for h in hist], [1])   # a folha do video vira a 2 (roteiro._COLETA_NOVA)


class TestRoteiros(unittest.TestCase):

    def test_numeracao_formatos_e_publicacao(self):
        self.assertEqual([v.numero for v in roteiro.VIDEOS], [1, 2])
        self.assertEqual([(v.slug, v.formato, v.login) for v in roteiro.VIDEOS],
                         [("retornos", "retrato", "galpao"), ("coletas", "paisagem", "expedicao")])
        self.assertTrue(all(v.publicar for v in roteiro.VIDEOS))
        for v in roteiro.VIDEOS:
            self.assertIn(v.estado, ambiente.ESTADOS)
            self.assertIn(v.login, ambiente.USUARIOS)
            for p in v.passos:
                for acao in p.acoes:
                    self.assertIn(acao[0], gravar.ACOES, acao)

    def _texto(self, video):
        return " ".join(p.fala + " " + (p.legenda or "") for p in video.passos), \
            [a for p in video.passos for a in p.acoes]

    def test_retornos_cobre_caixas_avaria_com_foto_e_o_que_nao_veio(self):
        texto, acoes = self._texto(roteiro.VIDEOS[0])
        for trecho in ("conte as caixas", "tire a foto", "não confirma", "desmarque", "continua pendente"):
            self.assertIn(trecho, texto.lower())
        alvos = [a[1] for a in acoes if len(a) > 1 and isinstance(a[1], str)]
        p = dados.paradas_do_video()
        self.assertIn(f"[data-menos='{p['PS-91201']}']", alvos)
        self.assertIn(f"[data-arq-avaria='{p['PS-91202']}']", alvos)
        self.assertIn(f"[data-veio='{p['PS-91204']}']", alvos)
        self.assertIn(("assinar", "#canvas-ass"), acoes)

    def test_coletas_cobre_foto_da_folha_assinada_e_o_que_nao_saiu(self):
        texto, acoes = self._texto(roteiro.VIDEOS[1])
        for trecho in ("folha preenchida e assinada", "sem a foto", "desmarque", "placa"):
            self.assertIn(trecho, texto.lower())
        self.assertIn(("celular",), acoes)
        self.assertIn(("clicar", f".saiu[value='{dados.NAO_SAIU}']"), acoes)
        self.assertTrue(any(a[0] == "arquivo" and a[1] == ".c-foto" for a in acoes))

    def test_coletas_tem_todos_os_passos_inclusive_os_do_celular(self):
        # Hugo, 16/11: QR + login no celular, foto com a camera, cancelar folha, erro na Stokki e acompanhamento
        texto, acoes = self._texto(roteiro.VIDEOS[1])
        alvos = [a[1] for a in acoes if len(a) > 1 and isinstance(a[1], str)]
        self.assertIn(("sair",), acoes)                              # o celular comeca sem login
        for sel in ("#cam-abrir", "#usuario", "#senha", "#cam-disparar", "[data-acao='cancelar']",
                    "[data-acao='tentar']", "[data-acao='retirar']", ".andamento"):
            self.assertTrue(any(sel in a for a in alvos), sel)
        for trecho in ("qr code", "login", "câmera", "cancelar folha", "tentar de novo", "tirar da coleta", "stokki"):
            self.assertIn(trecho, texto.lower(), trecho)


class TestPecas(unittest.TestCase):

    def test_tamanho_do_video_por_formato(self):
        self.assertEqual(gravar.tamanho_video("retrato"), (720, 1280))
        self.assertEqual(gravar.tamanho_video("paisagem"), (1280, 720))

    def test_assinatura_fica_dentro_do_canvas(self):
        for traco in gravar.tracos_assinatura(10, 400, 336, 180):
            for x, y in traco:
                self.assertTrue(10 <= x <= 346 and 400 <= y <= 580, (x, y))

    def test_manifesto_leva_o_tamanho_do_video(self):
        with tempfile.TemporaryDirectory() as tmp:
            saida = Path(tmp)
            for v in roteiro.VIDEOS:
                (saida / f"{v.slug}.mp4").write_bytes(b"x")
                (saida / f"{v.slug}.json").write_text(json.dumps({"duracao_s": 90}), encoding="utf-8")
            itens = json.loads(gravar.escrever_manifesto(roteiro.VIDEOS, saida).read_text(encoding="utf-8"))["videos"]
        self.assertEqual([(i["slug"], i["largura"], i["altura"]) for i in itens],
                         [("retornos", 720, 1280), ("coletas", 1280, 720)])

    def test_guarda_conta_o_proxy_morto_como_tentativa_de_sair(self):
        import socket
        from aula_portal.servidor import guarda_rede
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "bloqueios.log"
            with guarda_rede(log):
                with socket.socket() as s, self.assertRaises(OSError):
                    s.connect(("127.0.0.1", 9))
            self.assertIn("127.0.0.1', 9", log.read_text(encoding="utf-8"))

    def _coleta_confirmada(self, ids):
        from coletas import banco, regras
        servidor._despachos.clear()   # cada teste e uma gravacao nova (o painel sobe do zero)
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        banco.garantir_esquema(conn)
        agora = datetime.now().strftime(banco.FMT)
        for i in ids:
            conn.execute("INSERT INTO expedicao_coletas_fila (id_stokki, codigo, transportadora, retirada, visto_primeiro_em, "
                         "visto_ultimo_em) VALUES (?, ?, 'T', 1, ?, ?)", (i, f"PS-{i}", agora, agora))
        coleta = regras.gerar_folha(conn, ids, "u")
        return conn, coleta, regras.confirmar(conn, coleta, ids, "Nome", "ABC1D23", "foto.jpg", "u")

    def _andamento(self, conn, coleta):
        from coletas import regras
        c = regras.coleta(conn, coleta)
        r = servidor._andamento_roteirizado({"id": c["execucao_id"]}, c["token"], None)
        if r["estado"] == "fim":
            regras.aplicar_resultados(conn, coleta, r["resultados"])
        return r["estado"], {p["codigo"]: p["estado"] for p in regras.coleta(conn, coleta)["pedidos"]}

    def test_expedicao_roteirizada_mostra_falha_tentar_de_novo_e_cancelado(self):
        from coletas import regras
        conn, coleta, codigos = self._coleta_confirmada(servidor.FALHAS_DO_VIDEO["ids"])
        with mock.patch.object(servidor, "ESPERA_EXPEDICAO_S", 0):
            self.assertEqual(servidor._despachar_roteirizado(conn, coleta, codigos)["ok"], True)
            self.assertEqual(regras.coleta(conn, coleta)["status"], "CONFIRMADA")   # fica "expedindo" ate o andamento
            estado, pedidos = self._andamento(conn, coleta)
            self.assertEqual(estado, "fim")
            self.assertEqual(sorted(pedidos.values()), ["EXPEDIDO", "FALHOU", "FALHOU"])
            servidor._despachar_roteirizado(conn, coleta, regras.tentar_de_novo(conn, coleta))
            _, pedidos = self._andamento(conn, coleta)
        self.assertEqual(pedidos[f"PS-{servidor.FALHAS_DO_VIDEO['cancelado']}"], "FALHOU")   # sai pelo "Tirar da coleta"
        self.assertEqual(sorted(pedidos.values()), ["EXPEDIDO", "EXPEDIDO", "FALHOU"])
        regras.retirar_pedido(conn, coleta, servidor.FALHAS_DO_VIDEO["cancelado"], "u")
        self.assertEqual(regras.coleta(conn, coleta)["status"], "CONCLUIDA")
        na_lista = {p["id_stokki"] for g in regras.a_coletar(conn)["grupos"] for p in g["pedidos"]}
        self.assertNotIn(servidor.FALHAS_DO_VIDEO["cancelado"], na_lista)   # cancelado sumiu da listagem da Stokki
        self.assertFalse(regras.coleta(conn, coleta)["simulada"])   # sem o "(simulada)" da homologacao na tela

    def test_expedicao_roteirizada_espera_antes_de_terminar(self):
        conn, coleta, codigos = self._coleta_confirmada(servidor.FALHAS_DO_VIDEO["ids"])
        servidor._despachar_roteirizado(conn, coleta, codigos)
        self.assertEqual(self._andamento(conn, coleta)[0], "rodando")   # a tela mostra "Expedindo na Stokki..."


if __name__ == "__main__":
    unittest.main()
