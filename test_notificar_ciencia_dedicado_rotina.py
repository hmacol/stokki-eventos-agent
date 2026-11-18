# -*- coding: utf-8 -*-
"""
Rotina do timer da ciencia do dedicado: encerramento pelo nucleo, janela
08h-20h, lembrete a cada 4h e alerta da equipe na vespera. Canais falsos,
banco temporario. Rodar (da raiz): py -3.11 -m unittest test_notificar_ciencia_dedicado_rotina
"""
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta
from unittest import mock

import ciencia_dedicado as cd
import controle_notificacoes as cn
import notificar_ciencia_dedicado as ncd
import pedidos_dedicados as pd
import regioes_dia_fixo
from test_notificar_ciencia_dedicado import CanaisFalsos

AGORA = datetime(2026, 11, 17, 10, 0, 0)   # terca
ANTES = AGORA - timedelta(hours=1)          # ciencia aberta ha mais que a carencia do espelho (30 min)
ENTREGA = date(2026, 11, 19)               # quinta: Campinas so recebe na quarta
SENDER, OUTRO = 5, 6
CNPJS = {SENDER: "12345678000199", OUTRO: "98765432000111"}
CONFIG = {"portal_cliente": {"secret_key": "segredo", "url_base": "https://portal.teste/cliente"},
          "ciencia_dedicado": {"ativo": True}}
CAMPINAS = regioes_dia_fixo.regra_dia_fixo_do_servico({"address": "Rua A, 10, Campinas - SP, 13000-000, Brasil"})


class TestMotivo(unittest.TestCase):
    CIENCIA = {"codigo": "PS-1", "data_entrega": "2026-11-19"}
    HOJE = date(2026, 11, 17)

    def _estado(self, **kw):
        return {"cancelado": False, "finalizado": False, "rota_iniciada": False, "data": ENTREGA, **kw}

    def _motivo(self, estado="padrao", dedicado_ativo=True, regra=CAMPINAS, hoje=HOJE, ciencia=None):
        estado = self._estado() if estado == "padrao" else estado
        return ncd.motivo_de_encerramento(ciencia or self.CIENCIA, estado, dedicado_ativo, regra, hoje)

    def test_motivo_de_encerramento(self):
        self.assertEqual(self._motivo(self._estado(cancelado=True)), "cancelado")
        self.assertEqual(self._motivo(self._estado(finalizado=True)), "entregue ou insucesso")
        self.assertEqual(self._motivo(self._estado(rota_iniciada=True)), "rota iniciada")
        self.assertEqual(self._motivo(dedicado_ativo=False), "dedicado removido")
        self.assertEqual(self._motivo(hoje=date(2026, 11, 20)), "data passou")
        self.assertEqual(self._motivo(self._estado(data=date(2026, 11, 25))), "data em dia de visita")
        # nada a encerrar: pedido aberto, dedicado ativo, data futura fora do dia de visita
        self.assertIsNone(self._motivo())
        self.assertIsNone(self._motivo(hoje=ENTREGA))   # entrega hoje ainda nao passou
        self.assertIsNone(self._motivo(self._estado(data=date(2026, 11, 23))))   # mudou e segue fora
        self.assertIsNone(self._motivo(self._estado(data=date(2026, 11, 25)), regra=None))   # regra sumiu
        # nucleo nao conhece: so data passada ou dedicado removido encerram
        self.assertIsNone(self._motivo(None))
        self.assertEqual(self._motivo(None, dedicado_ativo=False), "dedicado removido")
        self.assertEqual(self._motivo(None, hoje=date(2026, 11, 20)), "data passou")
        self.assertIsNone(self._motivo(None, ciencia={"codigo": "PS-1", "data_entrega": "2026-11-25"}))


class _Base(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.addCleanup(os.remove, self.db)
        self.conn = cd.conectar(self.db)
        self.addCleanup(self.conn.close)
        self.pd = pd.conectar(self.db)
        self.addCleanup(self.pd.close)
        self.conn.executescript("""
            CREATE TABLE nucleo_pedidos (codigo TEXT PRIMARY KEY, vuupt_service_id INTEGER, status TEXT,
                                         agendamento_inicio TEXT);
            CREATE TABLE nucleo_rotas (id INTEGER PRIMARY KEY, status TEXT);
            CREATE TABLE nucleo_paradas (rota_id INTEGER, service_id INTEGER, codigo TEXT, situacao TEXT);
        """)
        c = cn.conectar(self.db)
        try:
            cn.definir_estado(c, "ciencia_dedicado", "LIGADO", "t")
        finally:
            c.close()
        self.embs = {s: {"nome": f"Emb {s}", "emails": [f"a{s}@emb.com"], "cnpj": CNPJS[s], "desligado": False}
                     for s in CNPJS}

    def _abrir(self, codigo="#PS-1", sid=10, sender=SENDER, data=ENTREGA, agora=AGORA, nucleo=True, marcar=True,
               sem_cnpj=False):
        item = {"servico": {"id": sid, "code": codigo, "sender_id": sender, "title": f"{codigo} - 1 / EMB / Padaria",
                            "address": "Rua A, 10, Campinas - SP, 13000-000, Brasil"},
                "regra": CAMPINAS, "data": data, "valor": 250.0}
        cid = cd.abrir(self.conn, item, None if sem_cnpj else CNPJS[sender], agora)
        if marcar:
            pd.marcar(self.pd, [{"codigo_pedido": codigo, "service_id": sid, "sender_id": sender}], 250.0,
                      pd.POR_FORA_DIA_FIXO)
        if nucleo:
            self._nucleo(codigo.lstrip("#"), agendamento=f"{data.isoformat()} 08:00:00", service_id=sid)
        return cid

    def _nucleo(self, codigo, status="ABERTO", agendamento="2026-11-19 08:00:00", service_id=None):
        """Upsert: chamada sem service_id mantem o que ja estava gravado."""
        self.conn.execute("""
            INSERT INTO nucleo_pedidos (codigo, vuupt_service_id, status, agendamento_inicio) VALUES (?, ?, ?, ?)
            ON CONFLICT(codigo) DO UPDATE SET status = excluded.status, agendamento_inicio = excluded.agendamento_inicio,
                vuupt_service_id = COALESCE(excluded.vuupt_service_id, vuupt_service_id)
        """, (codigo, service_id, status, agendamento))
        self.conn.commit()

    def _rota(self, codigo, status_rota, situacao="PENDENTE", service_id=None):
        rota_id = self.conn.execute("INSERT INTO nucleo_rotas (status) VALUES (?)", (status_rota,)).lastrowid
        self.conn.execute("INSERT INTO nucleo_paradas VALUES (?, ?, ?, ?)", (rota_id, service_id, codigo, situacao))
        self.conn.commit()

    def _executar(self, agora, falsos, config=CONFIG, **kw):
        return ncd.executar(config, agora=agora, canais=falsos.canais(), db_path=self.db,
                            embarcadores=self.embs, **kw)

    def _ciencia(self, cid):
        return cd.buscar(self.conn, cid)


class TestEstadoDoPedido(_Base):
    def test_estado_pelo_espelho_do_nucleo(self):
        self.assertIsNone(ncd.estado_do_pedido(self.conn, "PS-9"))
        self._nucleo("PS-1", "CANCELADO", "2026-11-23 08:00:00")
        self.assertEqual(ncd.estado_do_pedido(self.conn, "PS-1"),
                         {"cancelado": True, "finalizado": False, "rota_iniciada": False, "data": date(2026, 11, 23)})
        for status in ("ENTREGUE", "INSUCESSO"):
            self._nucleo("PS-1", status)
            self.assertTrue(ncd.estado_do_pedido(self.conn, "PS-1")["finalizado"])
        self._nucleo("PS-1", "ABERTO", None)
        self.assertIsNone(ncd.estado_do_pedido(self.conn, "PS-1")["data"])

    def test_sumir_do_pool_por_ganhar_rota_nao_e_motivo_so_a_rota_iniciada(self):
        self._nucleo("PS-1", "EM_ROTA")
        for status_rota in ("PLANEJADA", "ACEITA", "CANCELADA"):
            self._rota("PS-1", status_rota)
        self._rota("PS-1", "EM_ROTA", situacao="CANCELADA")   # tirado da rota que saiu
        estado = ncd.estado_do_pedido(self.conn, "PS-1")
        self.assertFalse(estado["rota_iniciada"])
        self.assertIsNone(ncd.motivo_de_encerramento({"data_entrega": "2026-11-19"}, estado, True, CAMPINAS,
                                                     date(2026, 11, 17)))
        self._rota("PS-1", "EM_ROTA")
        self.assertTrue(ncd.estado_do_pedido(self.conn, "PS-1")["rota_iniciada"])

    def test_reentrega_do_mesmo_codigo_nao_herda_a_rota_antiga(self):
        # rota antiga (servico 10) concluida com insucesso; a reentrega e o servico 20, mesmo codigo
        self._rota("PS-1", "CONCLUIDA", situacao="INSUCESSO", service_id=10)
        self._nucleo("PS-1", "ABERTO", "2026-11-23 08:00:00", service_id=20)
        estado = ncd.estado_do_pedido(self.conn, "PS-1", service_id=20)
        self.assertEqual(estado, {"cancelado": False, "finalizado": False, "rota_iniciada": False,
                                  "data": date(2026, 11, 23)})
        self.assertTrue(ncd.estado_do_pedido(self.conn, "PS-1")["rota_iniciada"])   # so pelo codigo, erraria
        self._rota("PS-1", "EM_ROTA", service_id=20)
        self.assertTrue(ncd.estado_do_pedido(self.conn, "PS-1", service_id=20)["rota_iniciada"])

    def test_servico_com_codigos_combinados_achado_pelo_service_id(self):
        self._nucleo("PS-1, PS-2", "ENTREGUE", service_id=10)
        self.assertIsNone(ncd.estado_do_pedido(self.conn, "PS-1"))
        self.assertTrue(ncd.estado_do_pedido(self.conn, "PS-1", service_id=10)["finalizado"])
        self._nucleo("PS-1, PS-2", "EM_ROTA")
        self._rota("PS-1, PS-2", "EM_ROTA", service_id=10)
        self.assertTrue(ncd.estado_do_pedido(self.conn, "PS-1", service_id=10)["rota_iniciada"])

    def test_service_id_que_o_nucleo_nao_tem_cai_no_codigo(self):
        self._nucleo("PS-1", "CANCELADO")
        self.assertTrue(ncd.estado_do_pedido(self.conn, "PS-1", service_id=99)["cancelado"])

    def test_codigo_de_outro_servico_nao_vale_pro_servico_que_o_nucleo_ainda_nao_tem(self):
        # servico recriado (21) com o mesmo codigo, ainda fora do espelho: a linha do codigo e do servico velho
        self._nucleo("PS-1", "ENTREGUE", service_id=10)
        self._rota("PS-1", "EM_ROTA", service_id=10)
        self.assertIsNone(ncd.estado_do_pedido(self.conn, "PS-1", service_id=21))
        self.assertTrue(ncd.estado_do_pedido(self.conn, "PS-1", service_id=10)["finalizado"])
        self.assertTrue(ncd.estado_do_pedido(self.conn, "PS-1")["finalizado"])


class TestRotina(_Base):
    def test_encerra_pelo_nucleo_e_pelo_dedicado_removido_sem_enviar(self):
        cancelado = self._abrir("#PS-1", 10, agora=ANTES)
        removido = self._abrir("#PS-2", 11, nucleo=False, agora=ANTES)
        em_rota = self._abrir("#PS-3", 12, agora=ANTES)
        self._nucleo("PS-1", "CANCELADO")
        pd.remover(self.pd, service_id=11, por="equipe")
        self._rota("PS-3", "EM_ROTA", service_id=12)
        f = CanaisFalsos()
        res = self._executar(AGORA, f)
        self.assertEqual(res["encerrados"], 3)
        self.assertEqual(f.emails, [])
        motivos = {cid: (self._ciencia(cid)["situacao"], self._ciencia(cid)["motivo_encerramento"])
                   for cid in (cancelado, removido, em_rota)}
        self.assertEqual(motivos, {cancelado: (cd.ENCERRADO, "cancelado"), removido: (cd.ENCERRADO, "dedicado removido"),
                                   em_rota: (cd.ENCERRADO, "rota iniciada")})
        for cid in (cancelado, removido, em_rota):
            self.assertEqual(cd.envios(self.conn, cid), [])

    def test_dedicado_de_reentrega_achado_pelo_codigo_quando_nao_ha_service_id(self):
        cid = self._abrir("#PS-1-R2", None)
        self.assertEqual(self._ciencia(cid)["codigo"], "PS-1-R2")
        f = CanaisFalsos()
        self.assertEqual(self._executar(AGORA, f)["encerrados"], 0)
        self.assertEqual(len(f.emails), 1)

    def test_dedicado_ativo_com_outro_service_id_vale_pelo_codigo(self):
        # o dedicado foi marcado no servico 10; a reentrega (servico 20) casa pelo codigo
        pd.marcar(self.pd, [{"codigo_pedido": "PS-1", "service_id": 10, "sender_id": SENDER}], 250.0,
                  pd.POR_FORA_DIA_FIXO)
        cid = self._abrir("#PS-1-R2", 20, marcar=False)
        f = CanaisFalsos()
        self.assertEqual(self._executar(AGORA, f)["encerrados"], 0)
        self.assertEqual(self._ciencia(cid)["situacao"], cd.PENDENTE)
        self.assertEqual(len(f.emails), 1)
        pd.remover(self.pd, codigo_pedido="PS-1", por="equipe")
        self.assertEqual(self._executar(datetime(2026, 11, 17, 14, 0), f)["encerrados"], 1)
        self.assertEqual(self._ciencia(cid)["motivo_encerramento"], "dedicado removido")

    def test_reentrega_nao_herda_a_rota_antiga_e_combinado_entregue_encerra(self):
        reentrega = self._abrir("#PS-1", 20, agora=ANTES)   # nucleo: PS-1 agora no servico 20
        self._rota("PS-1", "CONCLUIDA", situacao="INSUCESSO", service_id=10)
        combinado = self._abrir("#PS-5, PS-6", 30, nucleo=False, agora=ANTES)
        self._nucleo("PS-5, PS-6", "ENTREGUE", service_id=30)
        f = CanaisFalsos()
        res = self._executar(AGORA, f)
        self.assertEqual(self._ciencia(reentrega)["situacao"], cd.PENDENTE)
        self.assertEqual((self._ciencia(combinado)["situacao"], self._ciencia(combinado)["motivo_encerramento"]),
                         (cd.ENCERRADO, "entregue ou insucesso"))
        self.assertEqual((res["encerrados"], res["enviados"]), (1, 1))
        self.assertNotIn("PS-5", f.emails[0][2])

    def test_data_mudou_e_segue_fora_do_dia_atualiza_e_continua_pendente(self):
        fora = self._abrir("#PS-1", 10, agora=ANTES)
        visita = self._abrir("#PS-2", 11, agora=ANTES)
        self._nucleo("PS-1", agendamento="2026-11-23 08:00:00")   # segunda: segue fora
        self._nucleo("PS-2", agendamento="2026-11-25 08:00:00")   # quarta: dia de visita
        f = CanaisFalsos()
        res = self._executar(AGORA, f)
        self.assertEqual((self._ciencia(fora)["situacao"], self._ciencia(fora)["data_entrega"]),
                         (cd.PENDENTE, "2026-11-23"))
        self.assertEqual((self._ciencia(visita)["situacao"], self._ciencia(visita)["motivo_encerramento"]),
                         (cd.ENCERRADO, "data em dia de visita"))
        self.assertEqual((res["encerrados"], res["enviados"]), (1, 1))
        self.assertIn("23/11/2026", f.emails[0][2])
        self.assertNotIn("PS-2", f.emails[0][2])

    def test_fora_da_janela_nao_envia_e_conta_adiado(self):
        cid = self._abrir()
        for agora in (datetime(2026, 11, 17, 21, 0), datetime(2026, 11, 18, 7, 30)):
            with self.subTest(agora=agora):
                f = CanaisFalsos()
                res = self._executar(agora, f)
                self.assertEqual((res["adiados"], res["enviados"], res["embarcadores"]), (1, 0, 0))
                self.assertEqual((f.emails, f.whats, f.abertos, f.alertas), ([], [], [], []))
                self.assertEqual(cd.envios(self.conn, cid), [])
                self.assertEqual(self._ciencia(cid)["proximo_lembrete_em"], "2026-11-17 10:00:00")
        f = CanaisFalsos()
        self.assertEqual(self._executar(datetime(2026, 11, 18, 8, 0), f)["enviados"], 1)
        self.assertEqual(len(f.emails), 1)

    def test_lembrete_sai_4h_depois_e_nao_antes(self):
        cid = self._abrir()
        f = CanaisFalsos()
        self.assertEqual(self._executar(AGORA, f)["enviados"], 1)
        self.assertEqual(self._executar(datetime(2026, 11, 17, 13, 59), f)["enviados"], 0)
        self.assertEqual(len(f.emails), 1)
        res = self._executar(datetime(2026, 11, 17, 14, 0), f)
        self.assertEqual((res["enviados"], res["embarcadores"]), (1, 1))
        self.assertEqual(len(f.emails), 2)
        self.assertTrue(f.emails[1][1].startswith("Lembrete: "))
        self.assertEqual(self._ciencia(cid)["proximo_lembrete_em"], "2026-11-17 18:00:00")

    def test_resposta_entre_a_selecao_e_o_envio_nao_recebe_lembrete(self):
        self._abrir("#PS-1", 10, sender=SENDER)
        respondido = self._abrir("#PS-2", 11, sender=OUTRO)
        outro = self._abrir("#PS-3", 12, sender=OUTRO)
        f = CanaisFalsos()
        email_original = f._email

        def email(destinos, assunto, html):
            if not f.emails:   # o embarcador OUTRO responde enquanto sai a mensagem do primeiro
                cd.responder(self.conn, respondido, cd.CIENTE, "Fulano", "1.2.3.4", AGORA)
            return email_original(destinos, assunto, html)

        f._email = email
        res = self._executar(AGORA, f)
        self.assertEqual((res["embarcadores"], res["enviados"]), (2, 2))
        self.assertEqual([d for d, _, _ in f.emails], [["a5@emb.com"], ["a6@emb.com"]])
        self.assertIn("PS-3", f.emails[1][2])
        self.assertNotIn("PS-2", f.emails[1][2])
        self.assertEqual(cd.envios(self.conn, respondido), [])
        self.assertEqual(self._ciencia(respondido)["situacao"], cd.CIENTE)
        self.assertTrue(cd.envios(self.conn, outro))

    def test_alerta_equipe_as_16h_da_vespera_uma_vez_so(self):
        cid = self._abrir()
        f = CanaisFalsos()
        for agora in (AGORA, datetime(2026, 11, 18, 15, 59)):
            self.assertEqual(self._executar(agora, f)["alertas"], 0)
        self.assertEqual(f.alertas, [])
        self.assertEqual(self._executar(datetime(2026, 11, 18, 16, 0), f)["alertas"], 1)
        self.assertEqual(len(f.alertas), 1)
        for trecho in ("PS-1", "Emb 5", "19/11/2026", "2 aviso(s)"):
            self.assertIn(trecho, f.alertas[0])
        self.assertEqual(self._ciencia(cid)["alerta_equipe_em"], "2026-11-18 16:00:00")
        for agora in (datetime(2026, 11, 18, 16, 15), datetime(2026, 11, 18, 20, 0)):
            self.assertEqual(self._executar(agora, f)["alertas"], 0)
        self.assertEqual(len(f.alertas), 1)

    def test_alerta_equipe_imediato_quando_marcado_depois_das_16h_da_vespera(self):
        tarde = self._abrir("#PS-1", 10, agora=datetime(2026, 11, 18, 18, 0))
        f = CanaisFalsos()
        res = self._executar(datetime(2026, 11, 18, 18, 0), f)
        self.assertEqual((res["alertas"], res["enviados"]), (1, 1))
        self.assertIn("PS-1", f.alertas[0])
        self.assertIn("1 aviso(s)", f.alertas[0])   # o alerta sai depois do primeiro aviso e o conta
        self.assertTrue(self._ciencia(tarde)["alerta_equipe_em"])
        self.assertEqual(self._executar(datetime(2026, 11, 18, 18, 15), f)["alertas"], 0)
        self.assertEqual(len(f.alertas), 1)
        # marcado as 22h pra amanha: fora da janela espera as 08h
        noite = self._abrir("#PS-2", 11, sender=OUTRO, agora=datetime(2026, 11, 18, 22, 0))
        g = CanaisFalsos()
        self.assertEqual(self._executar(datetime(2026, 11, 18, 22, 0), g)["alertas"], 0)
        self.assertEqual(g.alertas, [])
        self.assertEqual(self._executar(datetime(2026, 11, 19, 8, 0), g)["alertas"], 1)
        self.assertIn("PS-2", g.alertas[0])
        self.assertIn("1 aviso(s)", g.alertas[0])
        self.assertTrue(self._ciencia(noite)["alerta_equipe_em"])

    def test_alerta_nao_sai_se_respondeu_durante_o_envio(self):
        cid = self._abrir("#PS-1", 10, agora=datetime(2026, 11, 18, 18, 0))
        f = CanaisFalsos()
        email_original = f._email

        def email(destinos, assunto, html):
            cd.responder(self.conn, cid, cd.CIENTE, "Fulano", "1.2.3.4", datetime(2026, 11, 18, 18, 0))
            return email_original(destinos, assunto, html)

        f._email = email
        res = self._executar(datetime(2026, 11, 18, 18, 0), f)
        self.assertEqual((res["enviados"], res["alertas"]), (1, 0))
        self.assertEqual(f.alertas, [])
        self.assertIsNone(self._ciencia(cid)["alerta_equipe_em"])

    def test_modo_teste_nao_grava_nada(self):
        self._abrir("#PS-1", 10)
        self._abrir("#PS-2", 11)
        self._abrir("#PS-3", 12)
        self._nucleo("PS-2", "CANCELADO")
        self._nucleo("PS-3", agendamento="2026-11-23 08:00:00")

        def foto():
            return ([tuple(r) for r in self.conn.execute("SELECT * FROM ciencias_dedicado ORDER BY id")],
                    [tuple(r) for r in self.conn.execute("SELECT * FROM ciencias_dedicado_envios")])

        antes = foto()
        f = CanaisFalsos()
        res = self._executar(datetime(2026, 11, 18, 16, 0), f, modo_teste=True)
        # PS-1 alerta (vespera de 19/11); PS-3 mudou pra 23/11 e ainda nao
        self.assertEqual(res, {"encerrados": 1, "embarcadores": 1, "enviados": 2, "adiados": 0, "alertas": 1})
        self.assertEqual((f.emails, f.whats, f.abertos, f.mensagens, f.alertas), ([], [], [], [], []))
        self.assertEqual(foto(), antes)

    def test_desligado_no_config_nao_faz_nada(self):
        cid = self._abrir()
        self._nucleo("PS-1", "CANCELADO")
        f = CanaisFalsos()
        for config in ({**CONFIG, "ciencia_dedicado": {"ativo": False}}, {"portal_cliente": CONFIG["portal_cliente"]}):
            with self.subTest(config=config):
                res = self._executar(datetime(2026, 11, 18, 16, 0), f, config=config)
                self.assertEqual(res, {"encerrados": 0, "embarcadores": 0, "enviados": 0, "adiados": 0, "alertas": 0})
        self.assertEqual((f.emails, f.whats, f.abertos, f.alertas), ([], [], [], []))
        c = self._ciencia(cid)
        self.assertEqual((c["situacao"], c["alerta_equipe_em"]), (cd.PENDENTE, None))
        self.assertEqual(cd.envios(self.conn, cid), [])

    def test_ciencia_recente_nao_confia_no_espelho(self):
        criada = AGORA - timedelta(minutes=5)
        entregue = self._abrir("#PS-1", 10, agora=criada)
        em_rota = self._abrir("#PS-2", 11, agora=criada)
        visita = self._abrir("#PS-3", 12, agora=criada)
        removido = self._abrir("#PS-4", 13, agora=criada)
        passou = self._abrir("#PS-5", 14, data=date(2026, 11, 16), agora=criada)
        self._nucleo("PS-1", "ENTREGUE")
        self._rota("PS-2", "EM_ROTA", service_id=11)
        self._nucleo("PS-3", agendamento="2026-11-25 08:00:00")   # quarta (dia de visita) no espelho atrasado
        pd.remover(self.pd, service_id=13, por="equipe")
        f = CanaisFalsos()
        res = self._executar(AGORA, f)
        # na carencia so encerram as tabelas locais: dedicado removido e data passada
        self.assertEqual(res["encerrados"], 2)
        self.assertEqual((self._ciencia(removido)["motivo_encerramento"], self._ciencia(passou)["motivo_encerramento"]),
                         ("dedicado removido", "data passou"))
        for cid in (entregue, em_rota, visita):
            self.assertEqual(self._ciencia(cid)["situacao"], cd.PENDENTE)
        self.assertEqual(self._ciencia(visita)["data_entrega"], "2026-11-19")   # a data do espelho tambem nao vale
        self.assertEqual(len(f.emails), 1)
        for codigo in ("PS-1", "PS-2", "PS-3"):
            self.assertIn(codigo, f.emails[0][2])
        # com 31 min de vida o espelho passa a valer
        res = self._executar(criada + timedelta(minutes=31), f)
        self.assertEqual(res["encerrados"], 3)
        motivos = {cid: self._ciencia(cid)["motivo_encerramento"] for cid in (entregue, em_rota, visita)}
        self.assertEqual(motivos, {entregue: "entregue ou insucesso", em_rota: "rota iniciada",
                                   visita: "data em dia de visita"})

    def test_rodadas_sobrepostas_mandam_cada_lembrete_uma_vez(self):
        # o timer ainda percorre os embarcadores quando a roteirizacao chama a rotina
        primeiro = self._abrir("#PS-1", 10, sender=SENDER, agora=ANTES)
        segundo = self._abrir("#PS-2", 11, sender=OUTRO, agora=ANTES)
        f, g = CanaisFalsos(), CanaisFalsos()
        email_original = f._email

        def email(destinos, assunto, html):
            if not f.emails:   # no meio do primeiro embarcador a outra rodada roda inteira
                self._executar(AGORA, g)
            return email_original(destinos, assunto, html)

        f._email = email
        self._executar(AGORA, f)
        self.assertEqual(sorted(d[0] for d, _, _ in f.emails + g.emails), ["a5@emb.com", "a6@emb.com"])
        self.assertEqual(len(f.abertos) + len(g.abertos), 2)
        for cid in (primeiro, segundo):
            self.assertEqual(len([e for e in cd.envios(self.conn, cid) if e["canal"] == "email"]), 1)
        # e outra rodada no mesmo instante nao manda nada
        h = CanaisFalsos()
        self.assertEqual(self._executar(AGORA, h)["enviados"], 0)
        self.assertEqual((h.emails, h.abertos), ([], []))

    def test_somente_ids_manda_so_esses_e_nao_encerra_nem_alerta(self):
        cancelado = self._abrir("#PS-1", 10, sender=SENDER, agora=ANTES)   # vencido e encerravel
        alheio = self._abrir("#PS-2", 11, sender=OUTRO, agora=ANTES)        # lembrete vencido de outro embarcador
        novo = self._abrir("#PS-3", 12, sender=SENDER)
        self._nucleo("PS-1", "CANCELADO")
        f = CanaisFalsos()
        res = self._executar(datetime(2026, 11, 18, 16, 0), f, somente_ids={novo})   # vespera: alertaria
        self.assertEqual(res, {"encerrados": 0, "embarcadores": 1, "enviados": 1, "adiados": 0, "alertas": 0})
        self.assertEqual([d for d, _, _ in f.emails], [["a5@emb.com"]])
        self.assertIn("PS-3", f.emails[0][2])
        self.assertNotIn("PS-1", f.emails[0][2])
        self.assertEqual(f.alertas, [])
        self.assertEqual(self._ciencia(cancelado)["situacao"], cd.PENDENTE)
        self.assertEqual((cd.envios(self.conn, cancelado), cd.envios(self.conn, alheio)), ([], []))
        self.assertIsNone(self._ciencia(novo)["alerta_equipe_em"])

    def test_alerta_que_nao_saiu_nao_marca_e_tenta_na_rodada_seguinte(self):
        cid = self._abrir(agora=ANTES)
        f = CanaisFalsos(alerta_ok=False)
        with self.assertLogs("notificar_ciencia_dedicado", "WARNING"):
            res = self._executar(datetime(2026, 11, 18, 16, 0), f)
        self.assertEqual((res["alertas"], len(f.alertas)), (0, 1))
        self.assertIsNone(self._ciencia(cid)["alerta_equipe_em"])
        f.alerta_ok = True
        self.assertEqual(self._executar(datetime(2026, 11, 18, 16, 15), f)["alertas"], 1)
        self.assertEqual(len(f.alertas), 2)
        self.assertEqual(self._ciencia(cid)["alerta_equipe_em"], "2026-11-18 16:15:00")
        self.assertEqual(self._executar(datetime(2026, 11, 18, 16, 30), f)["alertas"], 0)
        self.assertEqual(len(f.alertas), 2)

    def test_alerta_da_vespera_e_uma_mensagem_so_com_todos_os_pedidos(self):
        ids = [self._abrir("#PS-1", 10, sender=SENDER, agora=ANTES),
               self._abrir("#PS-2", 11, sender=SENDER, agora=ANTES),
               self._abrir("#PS-3", 12, sender=OUTRO, agora=ANTES)]
        f = CanaisFalsos()
        res = self._executar(datetime(2026, 11, 18, 16, 0), f)
        self.assertEqual((res["alertas"], len(f.alertas)), (3, 1))
        for trecho in ("PS-1", "PS-2", "PS-3", "Emb 5", "Emb 6", "19/11/2026", "1 aviso(s)"):
            self.assertIn(trecho, f.alertas[0])
        self.assertLess(f.alertas[0].index("PS-1"), f.alertas[0].index("PS-3"))
        for cid in ids:
            self.assertEqual(self._ciencia(cid)["alerta_equipe_em"], "2026-11-18 16:00:00")

    def test_piloto_e_cnpj_desconhecido_nao_alertam_a_equipe(self):
        c = cn.conectar(self.db)
        try:
            cn.definir_estado(c, "ciencia_dedicado", "PILOTO", "t")
            cn.definir_piloto(c, "piloto@x.com", "5511977776666", "t")
        finally:
            c.close()
        piloto = self._abrir(agora=ANTES)
        f = CanaisFalsos()
        res = self._executar(datetime(2026, 11, 18, 16, 0), f)
        self.assertEqual((res["alertas"], f.alertas), (0, []))
        self.assertEqual(f.emails[0][0], ["piloto@x.com"])   # a copia do piloto segue saindo
        self.assertIsNone(self._ciencia(piloto)["alerta_equipe_em"])
        # tela em ENVIAR: o pedido real alerta; o de embarcador sem CNPJ conhecido conta como piloto
        c = cn.conectar(self.db)
        try:
            cn.definir_estado(c, "ciencia_dedicado", "LIGADO", "t")
        finally:
            c.close()
        sem_cnpj = self._abrir("#PS-9", 19, sender=8, agora=ANTES, sem_cnpj=True)
        g = CanaisFalsos()
        res = self._executar(datetime(2026, 11, 18, 16, 15), g)
        self.assertEqual((res["alertas"], len(g.alertas)), (1, 1))
        self.assertIn("PS-1", g.alertas[0])
        self.assertNotIn("PS-9", g.alertas[0])
        self.assertIsNone(self._ciencia(sem_cnpj)["alerta_equipe_em"])

    def test_erro_num_embarcador_nao_impede_os_outros(self):
        self._abrir("#PS-1", 10, sender=SENDER)
        outro = self._abrir("#PS-2", 11, sender=OUTRO)
        original = ncd.enviar_embarcador

        def enviar(conn, sender_id, *args, **kw):
            if sender_id == SENDER:
                raise RuntimeError("quebrou")
            return original(conn, sender_id, *args, **kw)

        f = CanaisFalsos()
        with mock.patch.object(ncd, "enviar_embarcador", enviar), \
                self.assertLogs("notificar_ciencia_dedicado", "ERROR") as logs:
            res = self._executar(AGORA, f)
        self.assertIn("quebrou", " ".join(logs.output))
        self.assertEqual([d for d, _, _ in f.emails], [["a6@emb.com"]])
        self.assertEqual((res["embarcadores"], res["enviados"]), (1, 1))
        self.assertTrue(cd.envios(self.conn, outro))


class TestMain(unittest.TestCase):
    def test_main_modo_teste_com_chave_desligada_devolve_0(self):
        with mock.patch("builtins.open", mock.mock_open(read_data="ciencia_dedicado:\n  ativo: false\n")), \
                mock.patch.object(ncd.logging, "basicConfig"), \
                mock.patch.object(ncd, "executar", wraps=ncd.executar) as executar:
            self.assertEqual(ncd.main(["--modo-teste"]), 0)
        executar.assert_called_once_with({"ciencia_dedicado": {"ativo": False}}, modo_teste=True)


if __name__ == "__main__":
    unittest.main()
