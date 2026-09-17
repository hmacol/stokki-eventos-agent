# -*- coding: utf-8 -*-
"""Orquestração com Vuupt, SMTP e canhoto falsos.

    py -3.11 -m unittest notificacao_entregas.test_notificar_entrega_concluida
"""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from notificacao_entregas import fingerprint_notificacao_entrega as fp
from notificacao_entregas import notificar_entrega_concluida as n
from notificacao_entregas import regras_entrega as r

AGORA = datetime(2026, 9, 17, 15, 0, 0, tzinfo=timezone.utc)
EMBARCADORES = {900: {"nome": "Fruta Boa", "emails": ["log@frutaboa.com", "fin@frutaboa.com"],
                      "notificar_email": 1, "fator_ponderado": 1.0}}


def _servico(sid, status_done="success", fotos=1, minutos_atras=5, **extra) -> dict:
    s = {"id": sid, "code": f"#PS-{sid}", "title": "t", "sender_id": 900, "status": "done",
         "status_done": status_done, "failed_reason_id": 8490 if status_done == "failed" else None,
         "completed_at": (AGORA - timedelta(minutes=minutos_atras)).strftime("%Y-%m-%d %H:%M:%S"),
         "address": "Rua X 1, Sao Paulo - SP", "customer": {"data": {"name": f"Cliente {sid}"}},
         "checklistAnswers": {"data": [{"id": sid * 10, "images_quantity": fotos}]}}
    s.update(extra)
    return s


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.pasta = Path(self._tmp.name)
        self.conn = fp.conectar(self.pasta / "t.db")
        self.enviados: list[dict] = []
        self.smtp_ok = True
        self.canhoto_ok = True
        self.baixados: list[int] = []

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def _enviar(self, destinatarios, assunto, corpo_html, cc=None, anexos=None, cabecalhos_extra=None):
        self.enviados.append({"to": destinatarios, "assunto": assunto, "html": corpo_html, "cc": cc or [],
                              "anexos": anexos or [], "headers": cabecalhos_extra or {}})
        return self.smtp_ok

    def _baixar(self, checklist_id, codigo):
        self.baixados.append(checklist_id)
        if not self.canhoto_ok:
            return None
        p = self.pasta / f"canhoto_{codigo}.pdf"
        p.write_bytes(b"%PDF-1.4 fake")
        return p

    def _rodar(self, servicos, cfg=None, embarcadores=None, **kw):
        return n.processar(
            servicos, embarcadores=embarcadores or EMBARCADORES,
            cfg=cfg or r.ConfigEntregas(ativo=True, forcar_destino=""), agora=AGORA, conn=self.conn,
            enviar=self._enviar, baixar_canhoto=self._baixar, buscar_nf=lambda codigo: "4567",
            portal_url="https://app.freshhub.com.br/cliente", promete_email_reenvio=False,
            dormir=lambda s: None, **kw)

    def _estado(self, sid):
        return fp.estados(self.conn, [sid]).get(sid)


class TestProcessar(Base):
    def test_sucesso_com_canhoto_vai_pro_embarcador_com_pdf_anexo(self):
        c = self._rodar([_servico(1)])
        self.assertEqual(len(self.enviados), 1)
        e = self.enviados[0]
        self.assertEqual(e["to"], ["log@frutaboa.com", "fin@frutaboa.com"])
        self.assertEqual(e["assunto"], "Pedido PS-1 entregue · Cliente 1")
        self.assertEqual([nome for _, nome in e["anexos"]], ["Canhoto_PS-1.pdf"])
        self.assertEqual(e["headers"].get("Reply-To"), "entregas@freshlogbr.com")
        self.assertIn("4567", e["html"])
        self.assertEqual(self._estado(1), "ENVIADO")
        self.assertEqual((c["enviados"], c["com_canhoto"]), (1, 1))

    def test_segunda_rodada_nao_reenvia(self):
        self._rodar([_servico(1)])
        self._rodar([_servico(1)])
        self.assertEqual(len(self.enviados), 1)

    def test_falha_sai_na_hora_sem_anexo_e_sem_baixar_canhoto(self):
        self._rodar([_servico(2, status_done="failed", fotos=1, minutos_atras=1)])
        e = self.enviados[0]
        self.assertEqual(e["assunto"], "Pedido PS-2 não entregue · Cliente ausente")
        self.assertEqual(e["anexos"], [])
        self.assertEqual(self.baixados, [])
        self.assertEqual(self._estado(2), "ENVIADO")

    def test_sucesso_sem_canhoto_espera_e_depois_envia_sem_anexo(self):
        self._rodar([_servico(3, fotos=0, minutos_atras=10)])
        self.assertEqual(self.enviados, [])
        self.assertEqual(self._estado(3), "AGUARDANDO_CANHOTO")
        self._rodar([_servico(3, fotos=0, minutos_atras=31)])
        self.assertEqual(len(self.enviados), 1)
        self.assertEqual(self.enviados[0]["anexos"], [])
        self.assertIn("ainda não foi processado", self.enviados[0]["html"])
        linha = self.conn.execute("SELECT com_canhoto FROM notificacoes_entrega WHERE service_id=3").fetchone()
        self.assertEqual(linha["com_canhoto"], 0)

    def test_canhoto_chega_durante_a_espera(self):
        self._rodar([_servico(3, fotos=0, minutos_atras=10)])
        self._rodar([_servico(3, fotos=1, minutos_atras=15)])
        self.assertEqual(len(self.enviados[0]["anexos"]), 1)

    def test_download_do_canhoto_falhou_dentro_do_prazo_espera(self):
        self.canhoto_ok = False
        self._rodar([_servico(4, fotos=1, minutos_atras=10)])
        self.assertEqual(self.enviados, [])
        self.assertEqual(self._estado(4), "AGUARDANDO_CANHOTO")

    def test_download_do_canhoto_falhou_fora_do_prazo_envia_sem(self):
        self.canhoto_ok = False
        self._rodar([_servico(4, fotos=1, minutos_atras=40)])
        self.assertEqual(self.enviados[0]["anexos"], [])
        self.assertIn("ainda não foi processado", self.enviados[0]["html"])

    def test_pdf_grande_demais_nao_anexa(self):
        original = n.MAX_BYTES_ANEXO
        n.MAX_BYTES_ANEXO = 5
        try:
            self._rodar([_servico(5)])
        finally:
            n.MAX_BYTES_ANEXO = original
        self.assertEqual(self.enviados[0]["anexos"], [])

    def test_flag_desligada_so_registra_ignorado(self):
        self._rodar([_servico(6), _servico(7, status_done="failed")], cfg=r.ConfigEntregas(ativo=False))
        self.assertEqual(self.enviados, [])
        self.assertEqual((self._estado(6), self._estado(7)), ("IGNORADO", "IGNORADO"))
        # ligar depois NAO dispara os antigos
        self._rodar([_servico(6), _servico(7, status_done="failed")])
        self.assertEqual(self.enviados, [])

    def test_forcar_destino_redireciona_e_mostra_pra_quem_iria(self):
        self._rodar([_servico(8)], cfg=r.ConfigEntregas(ativo=True, forcar_destino="hugo@freshlogbr.com",
                                                        cc=["ops@freshlogbr.com"]))
        e = self.enviados[0]
        self.assertEqual((e["to"], e["cc"]), (["hugo@freshlogbr.com"], []))
        self.assertIn("log@frutaboa.com", e["html"])
        self.assertEqual(self._estado(8), "ENVIADO")

    def test_embarcador_sem_email(self):
        self._rodar([_servico(9, sender_id=555)])
        self.assertEqual(self.enviados, [])
        self.assertEqual(self._estado(9), "SEM_DESTINATARIO")

    def test_smtp_falhando_tres_vezes_desiste_e_reporta(self):
        self.smtp_ok = False
        self._rodar([_servico(10)])
        self.assertEqual(self._estado(10), "FALHA_ENVIO")
        self._rodar([_servico(10)])
        c = self._rodar([_servico(10)])
        self.assertEqual(self._estado(10), "ERRO_ENVIO")
        self.assertEqual(c["erros_definitivos"], ["PS-10"])
        self._rodar([_servico(10)])
        self.assertEqual(len(self.enviados), 3)

    def test_teto_de_envios_por_rodada(self):
        c = self._rodar([_servico(i) for i in range(20, 30)], max_envios=4)
        self.assertEqual(len(self.enviados), 4)
        self.assertEqual(c["adiados"], 6)
        self._rodar([_servico(i) for i in range(20, 30)], max_envios=100)
        self.assertEqual(len(self.enviados), 10)

    def test_retirada_no_galpao_gera_email_de_retirado_sem_baixar_canhoto(self):
        c = self._rodar([_servico(33, fotos=0, minutos_atras=1, title="[RETIRADA] #PS-33 / X",
                                  note="Quem retira: TRANSP X. Destinatário final: LOJA Y. Fechado automaticamente")])
        e = self.enviados[0]
        self.assertEqual(e["assunto"], "Pedido PS-33 retirado · TRANSP X")
        self.assertEqual((e["anexos"], self.baixados), ([], []))
        self.assertEqual(self._estado(33), "ENVIADO")
        self.assertEqual((c["retiradas"], c["sem_canhoto"]), (1, 0))

    def test_mais_antigo_primeiro(self):
        self._rodar([_servico(31, minutos_atras=5), _servico(32, minutos_atras=50)])
        self.assertIn("PS-32", self.enviados[0]["assunto"])


class TestModoTeste(Base):
    def test_nao_grava_nada_e_manda_pro_hugo_mesmo_com_flag_desligada(self):
        servicos = [_servico(i) for i in range(40, 46)] + [_servico(46, status_done="failed")]
        self._rodar(servicos, cfg=r.ConfigEntregas(ativo=False, forcar_destino=""), modo_teste=True, max_envios=3)
        self.assertEqual(len(self.enviados), 3)
        for e in self.enviados:
            self.assertEqual(e["to"], [n.EMAIL_TESTE])
            self.assertTrue(e["assunto"].startswith("[TESTE] "))
            self.assertIn("MODO TESTE", e["html"])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM notificacoes_entrega").fetchone()[0], 0)

    def test_amostra_mistura_falha_e_sucesso(self):
        servicos = [_servico(i) for i in range(50, 56)] + [_servico(56, status_done="failed")]
        self._rodar(servicos, modo_teste=True, max_envios=3)
        assuntos = " | ".join(e["assunto"] for e in self.enviados)
        self.assertIn("não entregue", assuntos)
        self.assertIn(" entregue", assuntos)

    def test_ignora_estado_ja_gravado(self):
        self._rodar([_servico(60)])
        self._rodar([_servico(60)], modo_teste=True)
        self.assertEqual(len(self.enviados), 2)


class TestEmbarcadores(Base):
    def test_carrega_de_interno_juntando_linhas_do_mesmo_sender(self):
        self.conn.execute("""CREATE TABLE interno (cnpj_embarcador TEXT PRIMARY KEY, nome_remetente TEXT,
                             apelido TEXT, email TEXT, notificar_email INTEGER, sender_id INTEGER,
                             fator_ponderado REAL)""")
        self.conn.executemany("INSERT INTO interno VALUES (?,?,?,?,?,?,?)", [
            ("1", "FRUTA BOA LTDA", "Fruta Boa", "a@x.com; b@x.com", 1, 900, 0.5),
            ("2", "FRUTA BOA FILIAL", None, "b@x.com,c@x.com", None, 900, 0.5),
            ("3", "SEM SENDER", None, "z@x.com", 1, None, 1.0),
            ("4", "OPTOU SAIR", None, "o@x.com", 0, 901, 1.0),
        ])
        embs = n.carregar_embarcadores(self.conn)
        self.assertEqual(set(embs), {900, 901})
        self.assertEqual(embs[900]["emails"], ["a@x.com", "b@x.com", "c@x.com"])
        self.assertEqual(embs[900]["nome"], "Fruta Boa")
        self.assertEqual(embs[900]["fator_ponderado"], 0.5)
        self.assertEqual(embs[900]["notificar_email"], 1)   # NULL conta como 1 (default da coluna)
        self.assertEqual(embs[901]["notificar_email"], 0)


try:
    import preferencias_notificacao
except ImportError:                      # modulo de outra frente; pode nao estar no checkout
    preferencias_notificacao = None


@unittest.skipUnless(preferencias_notificacao, "preferencias_notificacao.py ausente")
class TestPreferenciasDoPortal(Base):
    def setUp(self):
        super().setUp()
        self.conn.execute("""CREATE TABLE interno (cnpj_embarcador TEXT PRIMARY KEY, nome_remetente TEXT,
                             apelido TEXT, email TEXT, notificar_email INTEGER, sender_id INTEGER,
                             fator_ponderado REAL, stkkc_id INTEGER)""")
        self.conn.executemany("INSERT INTO interno VALUES (?,?,?,?,?,?,?,?)", [
            ("11111111000111", "FRUTA BOA", None, "cadastro@x.com", 1, 900, 1.0, 1),
            ("22222222000122", "OUTRO", None, "outro@x.com", 1, 901, 1.0, 2),
        ])
        self.conn.commit()

    def test_sem_preferencia_gravada_vale_o_cadastro(self):
        embs = n.aplicar_preferencias(n.carregar_embarcadores(self.conn), self.pasta / "t.db")
        self.assertEqual(embs[900]["emails"], ["cadastro@x.com"])
        self.assertFalse(embs[900].get("desligado"))

    def test_email_de_notificacoes_e_chave_desligada(self):
        preferencias_notificacao.salvar(self.conn, "11111111000111", ["avisos@x.com"], {}, "cliente")
        preferencias_notificacao.salvar(self.conn, "22222222000122", [], {"entrega_concluida": False}, "cliente")
        embs = n.aplicar_preferencias(n.carregar_embarcadores(self.conn), self.pasta / "t.db")
        self.assertEqual(embs[900]["emails"], ["avisos@x.com"])
        self.assertFalse(embs[900]["desligado"])
        self.assertTrue(embs[901]["desligado"])
        self._rodar([_servico(70, sender_id=901)], embarcadores=embs)
        self.assertEqual(self.enviados, [])
        self.assertEqual(self._estado(70), "SEM_DESTINATARIO")

    def test_notificar_email_0_nasce_desmarcado_e_o_cliente_liga_pelo_portal(self):
        self.conn.execute("UPDATE interno SET notificar_email = 0 WHERE sender_id = 901")
        self.conn.commit()
        embs = n.aplicar_preferencias(n.carregar_embarcadores(self.conn), self.pasta / "t.db")
        self._rodar([_servico(71, sender_id=901)], embarcadores=embs)
        self.assertEqual((self.enviados, self._estado(71)), ([], "SEM_DESTINATARIO"))

        preferencias_notificacao.salvar(self.conn, "22222222000122", [], {"entrega_concluida": True}, "cliente")
        embs = n.aplicar_preferencias(n.carregar_embarcadores(self.conn), self.pasta / "t.db")
        self._rodar([_servico(72, sender_id=901)], embarcadores=embs)
        self.assertEqual(self.enviados[0]["to"], ["outro@x.com"])
        self.assertEqual(self._estado(72), "ENVIADO")

    def test_sem_o_modulo_de_preferencias_notificar_email_0_continua_bloqueando(self):
        self.conn.execute("UPDATE interno SET notificar_email = 0 WHERE sender_id = 901")
        self.conn.commit()
        self._rodar([_servico(73, sender_id=901)], embarcadores=n.carregar_embarcadores(self.conn))
        self.assertEqual((self.enviados, self._estado(73)), ([], "SEM_DESTINATARIO"))

    def test_modulo_quebrado_nao_derruba_a_rotina(self):
        embs = n.aplicar_preferencias({900: {"emails": ["a@x.com"]}}, self.pasta / "nao_existe.db")
        self.assertEqual(embs[900]["emails"], ["a@x.com"])


if __name__ == "__main__":
    unittest.main()
