# -*- coding: utf-8 -*-
"""Entrega concluida obedecendo a tela de Notificacoes (controle_notificacoes).

    py -3.11 -m unittest notificacao_entregas.test_controle_entrega
"""
import unittest

import controle_notificacoes as cn
from notificacao_entregas import notificar_entrega_concluida as n
from notificacao_entregas import regras_entrega as r
from notificacao_entregas.test_notificar_entrega_concluida import Base, _servico

try:
    import preferencias_notificacao
except ImportError:                      # modulo de outra frente; pode nao estar no checkout
    preferencias_notificacao = None

CNPJ = "11222333000144"
EMBARCADORES = {900: {"nome": "Fruta Boa", "emails": ["log@frutaboa.com"], "notificar_email": 1,
                      "fator_ponderado": 1.0, "cnpj": CNPJ}}


class TestControleEntrega(Base):
    def _db(self):
        return self.pasta / "t.db"

    def _tela(self, estado):
        c = cn.conectar(self._db())
        try:
            cn.definir_estado(c, "entrega_concluida", estado, "teste")
        finally:
            c.close()

    def _rodar_tela(self, servicos, cfg=None, **kw):
        return self._rodar(servicos, cfg=cfg, embarcadores=EMBARCADORES, **kw)

    def test_desligado_pela_tela_grava_ignorado_e_nao_volta(self):
        self._tela("DESLIGADO")
        self._rodar_tela([_servico(1)])
        self.assertEqual(self.enviados, [])
        self.assertEqual(self._estado(1), "IGNORADO")
        motivo = self.conn.execute("SELECT motivo_estado FROM notificacoes_entrega WHERE service_id=1").fetchone()[0]
        self.assertTrue(motivo.startswith("tela:"), motivo)
        self._tela("LIGADO")
        self._rodar_tela([_servico(1)])
        self.assertEqual(self.enviados, [])

    def test_piloto_pela_tela_grava_estado_piloto(self):
        self._tela("PILOTO")
        self._rodar_tela([_servico(2)])
        self.assertEqual(len(self.enviados), 1)
        self.assertEqual(self.enviados[0]["to"], ["hugo@freshlogbr.com"])
        self.assertEqual(self.enviados[0]["cc"], [])
        self.assertIn("log@frutaboa.com", self.enviados[0]["html"])
        self.assertEqual(self._estado(2), r.ESTADO_PILOTO)
        # estado final: nao reenvia depois, nem ao ligar
        self._tela("LIGADO")
        self._rodar_tela([_servico(2)])
        self.assertEqual(len(self.enviados), 1)

    def test_ligado_pela_tela_ignora_embarcadores_piloto_do_config(self):
        self._tela("LIGADO")
        cfg = r.ConfigEntregas(ativo=True, forcar_destino="", embarcadores_piloto=[999])
        self._rodar_tela([_servico(3)], cfg=cfg)
        self.assertEqual(self.enviados[0]["to"], ["log@frutaboa.com"])
        self.assertEqual(self._estado(3), "ENVIADO")

    def test_ligado_pela_tela_vale_mesmo_com_config_desligado_e_forcar_destino(self):
        self._tela("LIGADO")
        self._rodar_tela([_servico(4)], cfg=r.ConfigEntregas(ativo=False, forcar_destino="hugo@freshlogbr.com"))
        self.assertEqual(self.enviados[0]["to"], ["log@frutaboa.com"])
        self.assertEqual(self._estado(4), "ENVIADO")

    def test_sem_linha_comportamento_atual(self):
        self._rodar_tela([_servico(5)], cfg=r.ConfigEntregas(ativo=False))
        self.assertEqual(self.enviados, [])
        self.assertEqual(self._estado(5), "IGNORADO")
        motivo = self.conn.execute("SELECT motivo_estado FROM notificacoes_entrega WHERE service_id=5").fetchone()[0]
        self.assertEqual(motivo, "desligado")

    def test_sem_linha_com_config_ligado_envia_como_hoje(self):
        self._rodar_tela([_servico(6)])
        self.assertEqual(self.enviados[0]["to"], ["log@frutaboa.com"])
        self.assertEqual(self._estado(6), "ENVIADO")

    def test_excecao_bloqueado_nao_envia(self):
        self._tela("LIGADO")
        c = cn.conectar(self._db())
        try:
            cn.definir_excecao(c, "entrega_concluida", CNPJ, "BLOQUEADO", "teste")
        finally:
            c.close()
        self._rodar_tela([_servico(7)])
        self.assertEqual(self.enviados, [])
        self.assertEqual(self._estado(7), "IGNORADO")

    def test_modo_teste_ignora_a_tela(self):
        self._tela("DESLIGADO")
        self._rodar_tela([_servico(8)], modo_teste=True)
        self.assertEqual(self.enviados[0]["to"], ["hugo@freshlogbr.com"])


class TestCnpjDoEmbarcador(Base):
    def _criar_interno(self):
        self.conn.execute("""CREATE TABLE interno (cnpj_embarcador TEXT PRIMARY KEY, nome_remetente TEXT,
                             apelido TEXT, email TEXT, notificar_email INTEGER, sender_id INTEGER,
                             fator_ponderado REAL, stkkc_id INTEGER)""")
        self.conn.execute("INSERT INTO interno VALUES ('11.222.333/0001-44','FRUTA BOA',NULL,'a@x.com',1,900,1.0,1)")
        self.conn.commit()

    def test_bloqueado_vale_mesmo_sem_o_modulo_de_preferencias(self):
        self._criar_interno()
        embs = n.carregar_embarcadores(self.conn)
        self.assertEqual(embs[900]["cnpj"], CNPJ)
        # modulo de preferencias falhando (banco inexistente): o except devolve embs como veio
        embs = n.aplicar_preferencias(embs, self.pasta / "nao_existe.db")
        self.assertEqual(embs[900]["cnpj"], CNPJ)
        c = cn.conectar(self._db())
        try:
            cn.definir_estado(c, "entrega_concluida", "LIGADO", "teste")
            cn.definir_excecao(c, "entrega_concluida", CNPJ, "BLOQUEADO", "teste")
        finally:
            c.close()
        self._rodar([_servico(9)], embarcadores=embs)
        self.assertEqual(self.enviados, [])
        self.assertEqual(self._estado(9), "IGNORADO")

    def _db(self):
        return self.pasta / "t.db"

    @unittest.skipUnless(preferencias_notificacao, "preferencias_notificacao.py ausente")
    def test_aplicar_preferencias_traz_o_cnpj_pra_tela(self):
        self._criar_interno()
        embs = n.aplicar_preferencias(n.carregar_embarcadores(self.conn), self._db_t())
        self.assertEqual(embs[900]["cnpj"], CNPJ)

    def _db_t(self):
        return self.pasta / "t.db"


if __name__ == "__main__":
    unittest.main()
