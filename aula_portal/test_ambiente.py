# -*- coding: utf-8 -*-
"""
Ambiente de gravacao da aula: config e banco ficticios, nada real.

    py -3.11 -m unittest aula_portal.test_ambiente
"""
import json
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import yaml

from aula_portal import ambiente, dados_teste

CHAVES_PROIBIDAS = ("email", "vuupt_api", "stokki", "stokki_api", "freshhub_api", "anthropic", "claude",
                    "whatsapp", "openwa", "lalamove", "google_maps", "evolution")


class AmbienteTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.raiz = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_config_sem_credenciais(self):
        cfg = yaml.safe_load(ambiente.escrever_config(self.raiz).read_text(encoding="utf-8"))
        for chave in CHAVES_PROIBIDAS:
            self.assertNotIn(chave, cfg)
        self.assertGreaterEqual(len(cfg["portal_cliente"]["secret_key"]), 32)
        self.assertTrue(cfg["portal_cliente"]["alteracao_entrega"]["ativo"])
        self.assertFalse(cfg["portal_cliente"]["envios"]["bloqueio_area_ativo"])
        # ligado pra todos em producao desde 12/11: a previa mostra "Endereco de entrega"
        self.assertTrue(cfg["portal_cliente"]["envios"]["endereco_entrega_ativo"])

    def test_config_nao_sobrescreve_config_real(self):
        real = self.raiz / "config.yaml"
        real.write_text("portal_cliente: {secret_key: segredo-de-verdade}\n", encoding="utf-8")
        with self.assertRaises(ambiente.AmbienteRecusado):
            ambiente.escrever_config(self.raiz)
        self.assertIn("segredo-de-verdade", real.read_text(encoding="utf-8"))

    def test_config_regrava_o_proprio(self):
        ambiente.escrever_config(self.raiz)
        ambiente.escrever_config(self.raiz)  # segunda vez: e o nosso, pode

    def test_banco_nao_apaga_banco_real(self):
        (self.raiz / "dados").mkdir()
        real = self.raiz / "dados" / "dados.db"
        conn = sqlite3.connect(real)
        conn.execute("CREATE TABLE interno (x)")
        conn.commit()
        conn.close()
        with self.assertRaises(ambiente.AmbienteRecusado):
            ambiente.semear_banco(self.raiz, "padrao", esquema=None)
        self.assertTrue(real.exists())

    def test_banco_padrao_tem_so_o_cliente_ficticio(self):
        db = ambiente.semear_banco(self.raiz, "padrao", esquema=None)
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        contas = conn.execute("SELECT cnpj, trocar_pin FROM clientes_portal").fetchall()
        self.assertEqual([r["cnpj"] for r in contas], [dados_teste.CNPJ])
        self.assertEqual(contas[0]["trocar_pin"], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM interno").fetchone()[0], 1)
        status = {r[0] for r in conn.execute("SELECT status FROM portal_envios")}
        self.assertTrue({"CRIADO", "ERRO", "EM_ESPERA_CLIENTE"} <= status)
        conn.close()
        self.assertTrue(ambiente.pin_confere(db, dados_teste.PIN_GRAVACAO))
        self.assertFalse(ambiente.pin_confere(db, "123456"))

    def test_banco_status_tem_um_envio_em_cada_status(self):
        # video "Status do pedido": todo rotulo que a lista de envios pode mostrar aparece numa linha
        import envio_pedidos
        db = ambiente.semear_banco(self.raiz, "status", esquema=None)
        conn = sqlite3.connect(db)
        status = {r[0] for r in conn.execute("SELECT status FROM portal_envios")}
        bloqueio = conn.execute("SELECT bloqueio_motivo FROM portal_envios WHERE status = 'AGUARDANDO_LIBERACAO'").fetchone()
        conn.close()
        self.assertEqual(status, set(envio_pedidos.ROTULOS_STATUS))
        self.assertEqual(bloqueio[0], "sp_nao_atendido")

    def test_banco_padrao_nao_ganha_os_envios_do_video_de_status(self):
        db = ambiente.semear_banco(self.raiz, "padrao", esquema=None)
        conn = sqlite3.connect(db)
        n = conn.execute("SELECT COUNT(*) FROM portal_envios").fetchone()[0]
        conn.close()
        self.assertEqual(n, 4)   # o video 7 conta linhas da tabela (nth-child)

    def test_banco_primeiro_acesso(self):
        db = ambiente.semear_banco(self.raiz, "primeiro_acesso", esquema=None)
        conn = sqlite3.connect(db)
        self.assertEqual(conn.execute("SELECT trocar_pin FROM clientes_portal").fetchone()[0], 1)
        import cadastro_embarcador as cad_emb
        self.assertTrue(cad_emb.precisa_completar(conn, dados_teste.CNPJ))
        conn.close()

    def test_banco_resemeia(self):
        ambiente.semear_banco(self.raiz, "padrao", esquema=None)
        db = ambiente.semear_banco(self.raiz, "primeiro_acesso", esquema=None)
        conn = sqlite3.connect(db)
        self.assertEqual(conn.execute("SELECT trocar_pin FROM clientes_portal").fetchone()[0], 1)
        conn.close()

    def test_cnpj_nao_e_o_cliente_teste_de_producao(self):
        # 00.000.000/0001-91 e o cliente teste da VPS, que ainda entra com o PIN padrao:
        # aparecer digitado numa aula publica entregaria o acesso (revisao final, 12/11)
        self.assertNotEqual(dados_teste.CNPJ, "00000000000191")

    def test_dia_respeita_o_periodo_pedido(self):
        dia = dados_teste.dia(date(2026, 11, 12), dias=30)
        self.assertEqual(dia["periodo"]["dias"], 30)
        self.assertEqual(dia["periodo"]["de"], "2026-10-14")

    def test_dia_tem_todas_as_situacoes(self):
        dia = dados_teste.dia(date(2026, 11, 12))
        self.assertTrue({"entregue", "em_rota", "programado", "aguardando_saida", "insucesso"}
                        <= {p["situacao"] for p in dia["pedidos"]})
        codigos_atencao = {c for g in dia["atencao"] for c in g["codigos"]}
        for p in dia["pedidos"]:
            if p["situacao"] == "insucesso" and p["data"] == "2026-11-12":
                self.assertIn(p.get("codigo_servico") or p["codigo"], codigos_atencao)
        self.assertTrue(dia["alteracao_entrega"])
        self.assertEqual(dia["empresas"][0]["sender_id"], dados_teste.SENDER_ID)

    def test_dados_sem_pin_padrao(self):
        textos = [json.dumps(dados_teste.dia(date(2026, 11, 12)), ensure_ascii=False)]
        textos += [p.read_text(encoding="utf-8") for p in dados_teste.xmls_nfe(self.raiz)]
        for t in textos:
            self.assertNotIn("123456", t)
        self.assertNotEqual(dados_teste.PIN_GRAVACAO, "123456")

    def test_xmls_passam_na_previa(self):
        import envio_pedidos as envios
        bons, ruim = dados_teste.xmls_nfe(self.raiz)[:2], dados_teste.xmls_nfe(self.raiz)[2]
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE portal_envios (id, status, criado_em, codigo_pedido, chave_nfe)")
        for p in bons:
            v = envios.validar_item(conn, envios.ler_nfe(p.read_bytes(), p.name), dados_teste.CNPJ)
            self.assertTrue(v["ok"], v["erros"])
        v = envios.validar_item(conn, envios.ler_nfe(ruim.read_bytes(), ruim.name), dados_teste.CNPJ)
        self.assertFalse(v["ok"])

    def test_boleto_cai_no_pedido_da_nf(self):
        import anexos_pedido
        pdf = dados_teste.boleto_pdf(self.raiz)
        info = anexos_pedido.ler_pdf(pdf.name, pdf.read_bytes())
        pedidos = [{"chave": "a", "numero_nf": "5201", "destinatario_doc": "", "referencia": ""},
                   {"chave": "b", "numero_nf": "5202", "destinatario_doc": "", "referencia": ""}]
        self.assertEqual(anexos_pedido.sugerir_pedido(info, pedidos)[0], "a")

    def test_alteracao_no_formato_do_get(self):
        a = dados_teste.alteracao(9007)
        self.assertTrue(a["pode"])
        self.assertEqual(set(a["atual"]), {"endereco", "data", "horario"})
        self.assertEqual(set(a["atual"]["horario"]), {"inicio", "fim"})

    def test_alteracao_do_agendado_traz_a_data_agendada(self):
        hoje = date.today()
        a = dados_teste.alteracao(9011)   # PS-90112, aba Agendados
        self.assertEqual(a["destinatario"], "Pizzaria Forno a Lenha")
        self.assertEqual(a["atual"]["data"], (hoje + timedelta(days=4)).isoformat())
        self.assertFalse(a["em_rota"])

    def test_alteracao_do_programado_esta_em_rota(self):
        self.assertTrue(dados_teste.alteracao(9007)["em_rota"])   # PS-90108, rota que ainda nao saiu

    def test_salvar_em_rota_pede_confirmacao_e_depois_aplica(self):
        corpo = {"horario": {"inicio": "15:00", "fim": "18:00"}}
        r = dados_teste.resposta_alteracao(9007, corpo)
        self.assertTrue(r["precisa_confirmar"])
        self.assertIn("atrasar 1 dia", r["avisos"][0]["texto"])
        r = dados_teste.resposta_alteracao(9007, dict(corpo, confirmar=True))
        self.assertTrue(r["ok"])
        self.assertIn("saiu da rota", r["mensagem"])

    def test_salvar_agendado_aplica_na_hora_com_a_mensagem_do_portal(self):
        nova = date(2026, 11, 20)
        r = dados_teste.resposta_alteracao(9011, {"data": nova.isoformat(),
                                                   "horario": {"inicio": "14:00", "fim": "18:00"}})
        self.assertEqual(r, {"ok": True, "mensagem": "Alteração feita. Entrega em 20/11, "
                                                     "recebimento das 14:00 às 18:00.", "feito": []})

    def test_cep_no_formato_da_api(self):
        self.assertEqual(set(dados_teste.cep("05425000")), {"cep", "logradouro", "bairro", "cidade", "uf"})

    def test_canhoto_e_png_ficticio(self):
        png = dados_teste.canhoto_png("5100")
        self.assertEqual(png[1:4], b"PNG")

    def test_exportacao_e_xlsx(self):
        self.assertEqual(dados_teste.exportacao_xlsx()[:2], b"PK")


if __name__ == "__main__":
    unittest.main()


class ServidorGravacaoTest(unittest.TestCase):
    """O portal da gravacao sobe por aula_portal.servidor: so a lista de pedidos
    e a fala do modelo no chat sao roteirizadas; o resto e o portal real."""

    def test_assistente_ve_os_pedidos_ficticios(self):
        from aula_portal import servidor
        import assistente
        with servidor.roteirizado():
            pedidos, _kpis = assistente._pedidos_do_cliente({"cnpj": dados_teste.CNPJ, "sender_id": dados_teste.SENDER_ID}, {})
        self.assertIn("#PS-90108", {p["codigo"] for p in pedidos})

    def test_modelo_roteirizado_responde_no_formato(self):
        from aula_portal import servidor
        import assistente
        with servidor.roteirizado():
            resp = assistente._cliente_anthropic({}).messages.create(model="x", max_tokens=1, system="", messages=[])
        dados = json.loads(resp.content[0].text)
        self.assertEqual(resp.stop_reason, "end_turn")
        self.assertTrue(dados["precisa_atendente"])
        self.assertIn(dados["area"], __import__("chamados").AREAS)
        self.assertTrue(dados["resposta"])

    def test_roteirizado_desfaz_ao_sair(self):
        from aula_portal import servidor
        import assistente
        original = assistente._cliente_anthropic
        with servidor.roteirizado():
            pass
        self.assertIs(assistente._cliente_anthropic, original)


class GuardaRedeServidorTest(unittest.TestCase):
    """Dentro do portal da gravacao nada sai: SMTP recusado, socket so pra
    loopback, e cada tentativa fica registrada (o gravar reprova o video)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.log = Path(self._tmp.name) / "bloqueios.log"

    def tearDown(self):
        self._tmp.cleanup()

    def test_smtp_recusado_e_registrado(self):
        import smtplib
        from aula_portal import servidor
        with servidor.guarda_rede(self.log):
            with self.assertRaises(OSError):
                smtplib.SMTP("smtp.gmail.com", 587)
        self.assertIn("smtp", self.log.read_text(encoding="utf-8"))
        self.assertIsNot(smtplib.SMTP, servidor._SMTPBloqueado)

    def test_socket_pra_fora_recusado(self):
        import socket
        from aula_portal import servidor
        with servidor.guarda_rede(self.log):
            s = socket.socket()
            with self.assertRaises(OSError):
                s.connect(("8.8.8.8", 53))
            s.close()
        self.assertIn("8.8.8.8", self.log.read_text(encoding="utf-8"))

    def test_loopback_passa(self):
        import socket
        from aula_portal import servidor
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        with servidor.guarda_rede(self.log):
            c = socket.create_connection(srv.getsockname(), timeout=2)
            c.close()
        srv.close()
        self.assertFalse(self.log.exists() and self.log.read_text(encoding="utf-8").strip())
