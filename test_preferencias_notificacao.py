"""Testes das preferencias de notificacao por embarcador (preferencias_notificacao.py).

Rodar: py -3.11 -m unittest test_preferencias_notificacao
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path

import preferencias_notificacao as pn

CNPJ_A = "11111111000111"
CNPJ_B = "22222222000122"
CNPJ_VETADO = "33333333000133"


def _criar_banco(caminho: Path) -> None:
    conn = sqlite3.connect(caminho)
    conn.execute(
        "CREATE TABLE interno (cnpj_embarcador TEXT PRIMARY KEY, nome_remetente TEXT, apelido TEXT, "
        "email TEXT, notificar_email INTEGER NOT NULL DEFAULT 1, sender_id INTEGER, stkkc_id INTEGER)"
    )
    conn.executemany(
        "INSERT INTO interno VALUES (?,?,?,?,?,?,?)",
        [
            (CNPJ_A, "Alfa Alimentos LTDA", "Alfa", "fiscal@alfa.com; logistica@alfa.com", 1, 101, 9001),
            (CNPJ_B, "Beta Bebidas SA", None, "contato@beta.com", 1, 102, None),
            (CNPJ_VETADO, "Vetado LTDA", "Vetado", "x@vetado.com", 0, 103, 9003),
            ("44444444000144", "Sem Sender", None, "y@semsender.com", 1, None, None),
        ],
    )
    conn.commit()
    conn.close()


class _ComBanco(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "dados.db"
        _criar_banco(self.db)
        self.conn = sqlite3.connect(self.db)
        self.conn.row_factory = sqlite3.Row

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()


class TestDefaults(_ComBanco):
    def test_sem_linha_tudo_ligado_menos_resumo_diario(self):
        prefs = pn.ler(self.conn, CNPJ_A)
        self.assertEqual(prefs["tipos"], {
            "nfs_em_rota": True, "entrega_concluida": True, "faltas_recebimento": True,
            "resumo_diario": False,
            "insucesso": True, "pedidos_em_espera": True, "agendamento": True,
        })

    def test_sem_linha_emails_vazio_e_cadastro_visivel(self):
        prefs = pn.ler(self.conn, CNPJ_A)
        self.assertEqual(prefs["emails"], [])
        self.assertEqual(prefs["emails_cadastro"], ["fiscal@alfa.com", "logistica@alfa.com"])

    def test_ler_cnpj_inexistente_levanta(self):
        with self.assertRaises(ValueError):
            pn.ler(self.conn, "99999999000199")

    def test_ler_aceita_cnpj_formatado(self):
        self.assertEqual(pn.ler(self.conn, "11.111.111/0001-11")["emails_cadastro"][0], "fiscal@alfa.com")


class TestSalvar(_ComBanco):
    def test_salvar_e_ler_de_volta(self):
        pn.salvar(self.conn, CNPJ_A, ["avisos@alfa.com"], {"resumo_diario": True, "insucesso": False}, "cliente")
        prefs = pn.ler(self.conn, CNPJ_A)
        self.assertEqual(prefs["emails"], ["avisos@alfa.com"])
        self.assertTrue(prefs["tipos"]["resumo_diario"])
        self.assertFalse(prefs["tipos"]["insucesso"])
        self.assertTrue(prefs["tipos"]["agendamento"])  # nao informado mantem o default
        self.assertEqual(prefs["atualizado_por"], "cliente")

    def test_salvar_de_novo_atualiza_a_mesma_linha(self):
        pn.salvar(self.conn, CNPJ_A, ["a@alfa.com"], {"resumo_diario": True}, "cliente")
        pn.salvar(self.conn, CNPJ_A, [], {"resumo_diario": False}, "equipe")
        prefs = pn.ler(self.conn, CNPJ_A)
        self.assertEqual(prefs["emails"], [])
        self.assertFalse(prefs["tipos"]["resumo_diario"])
        self.assertEqual(prefs["atualizado_por"], "equipe")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM preferencias_notificacao").fetchone()[0], 1)

    def test_flag_nao_informada_preserva_o_que_ja_estava_gravado(self):
        pn.salvar(self.conn, CNPJ_A, [], {"insucesso": False}, "cliente")
        pn.salvar(self.conn, CNPJ_A, [], {"resumo_diario": True}, "cliente")
        self.assertFalse(pn.ler(self.conn, CNPJ_A)["tipos"]["insucesso"])

    def test_emails_sao_limpos_e_sem_repeticao(self):
        pn.salvar(self.conn, CNPJ_A, ["  Avisos@Alfa.com ", "avisos@alfa.com", ""], {}, "cliente")
        self.assertEqual(pn.ler(self.conn, CNPJ_A)["emails"], ["avisos@alfa.com"])

    def test_email_invalido_levanta(self):
        for ruim in ("sem-arroba", "a@b", "a b@c.com", "a@b.com\nBcc: x@y.com", "<a@b.com>", "a@b.com,c@d.com"):
            with self.subTest(email=ruim), self.assertRaises(ValueError):
                pn.salvar(self.conn, CNPJ_A, [ruim], {}, "cliente")

    def test_mais_de_cinco_emails_levanta(self):
        with self.assertRaises(ValueError):
            pn.salvar(self.conn, CNPJ_A, [f"p{i}@alfa.com" for i in range(6)], {}, "cliente")

    def test_tipo_desconhecido_levanta(self):
        with self.assertRaises(ValueError):
            pn.salvar(self.conn, CNPJ_A, [], {"promocoes": True}, "cliente")

    def test_cnpj_fora_da_interno_levanta_e_nao_grava(self):
        with self.assertRaises(ValueError):
            pn.salvar(self.conn, "99999999000199", [], {"resumo_diario": True}, "cliente")

    def test_erro_de_validacao_nao_grava_nada(self):
        with self.assertRaises(ValueError):
            pn.salvar(self.conn, CNPJ_A, ["ruim"], {"resumo_diario": True}, "cliente")
        self.assertFalse(pn.ler(self.conn, CNPJ_A)["tipos"]["resumo_diario"])


class TestCarregarEmbarcadores(_ComBanco):
    def test_formato_compativel_com_as_rotinas(self):
        embs = pn.carregar_embarcadores("insucesso", db_path=self.db)
        self.assertEqual(embs[101]["nome"], "Alfa")
        self.assertEqual(embs[102]["nome"], "Beta Bebidas SA")
        self.assertEqual(embs[101]["emails"], ["fiscal@alfa.com", "logistica@alfa.com"])
        self.assertEqual(embs[101]["cnpj"], CNPJ_A)
        self.assertFalse(embs[101]["desligado"])

    def test_so_entra_quem_tem_a_chave(self):
        self.assertEqual(set(pn.carregar_embarcadores("insucesso", db_path=self.db)), {101, 102, 103})
        self.assertEqual(set(pn.carregar_embarcadores("pedidos_em_espera", chave="stkkc_id", db_path=self.db)),
                         {9001, 9003})

    def test_email_de_notificacao_vence_o_do_cadastro(self):
        pn.salvar(self.conn, CNPJ_A, ["avisos@alfa.com"], {}, "cliente")
        self.assertEqual(pn.carregar_embarcadores("agendamento", db_path=self.db)[101]["emails"], ["avisos@alfa.com"])

    def test_tipo_desligado_so_afeta_aquele_tipo(self):
        pn.salvar(self.conn, CNPJ_A, [], {"insucesso": False}, "cliente")
        self.assertTrue(pn.carregar_embarcadores("insucesso", db_path=self.db)[101]["desligado"])
        self.assertFalse(pn.carregar_embarcadores("agendamento", db_path=self.db)[101]["desligado"])
        self.assertFalse(pn.carregar_embarcadores("insucesso", db_path=self.db)[102]["desligado"])

    def test_resumo_diario_nasce_desligado(self):
        self.assertTrue(pn.carregar_embarcadores("resumo_diario", db_path=self.db)[101]["desligado"])
        pn.salvar(self.conn, CNPJ_A, [], {"resumo_diario": True}, "cliente")
        self.assertFalse(pn.carregar_embarcadores("resumo_diario", db_path=self.db)[101]["desligado"])

    # Hugo, 17/09 (2a decisao): interno.notificar_email = 0 deixou de ser VETO
    # pros tipos novos e virou "nasce desmarcado" -- o embarcador liga sozinho
    # no botao Notificacoes. Pipeline e triagem seguem lendo a coluna como antes.
    def test_notificar_email_0_nasce_com_os_tipos_novos_desmarcados(self):
        for tipo in ("nfs_em_rota", "entrega_concluida", "resumo_diario"):
            with self.subTest(tipo=tipo):
                self.assertTrue(pn.carregar_embarcadores(tipo, db_path=self.db)[103]["desligado"])
        tipos = pn.ler(self.conn, CNPJ_VETADO)["tipos"]
        self.assertEqual((tipos["nfs_em_rota"], tipos["entrega_concluida"], tipos["resumo_diario"]),
                         (False, False, False))
        self.assertTrue(tipos["insucesso"])

    def test_notificar_email_0_pode_ligar_pelo_portal(self):
        pn.salvar(self.conn, CNPJ_VETADO, [], {"entrega_concluida": True}, "cliente")
        self.assertFalse(pn.carregar_embarcadores("entrega_concluida", db_path=self.db)[103]["desligado"])

    def test_salvar_parcial_nao_liga_de_carona_os_outros_tipos_novos(self):
        pn.salvar(self.conn, CNPJ_VETADO, ["avisos@vetado.com"], {"entrega_concluida": True}, "cliente")
        self.assertTrue(pn.carregar_embarcadores("nfs_em_rota", db_path=self.db)[103]["desligado"])
        self.assertTrue(pn.carregar_embarcadores("resumo_diario", db_path=self.db)[103]["desligado"])

    def test_notificar_email_0_nao_muda_os_tipos_antigos(self):
        for tipo in ("insucesso", "pedidos_em_espera", "agendamento"):
            with self.subTest(tipo=tipo):
                chave = "stkkc_id" if tipo == "pedidos_em_espera" else "sender_id"
                emb = pn.carregar_embarcadores(tipo, chave=chave, db_path=self.db)[9003 if chave == "stkkc_id" else 103]
                self.assertFalse(emb["desligado"])

    def test_tipo_ou_chave_desconhecidos_levantam(self):
        with self.assertRaises(ValueError):
            pn.carregar_embarcadores("promocoes", db_path=self.db)
        with self.assertRaises(ValueError):
            pn.carregar_embarcadores("insucesso", chave="cnpj; DROP TABLE interno", db_path=self.db)

    def test_funciona_em_banco_sem_a_tabela_de_preferencias(self):
        # banco da VPS antes do primeiro salvar: a rotina em lote nao pode quebrar
        self.assertFalse(pn.carregar_embarcadores("insucesso", db_path=self.db)[101]["desligado"])


if __name__ == "__main__":
    unittest.main()
