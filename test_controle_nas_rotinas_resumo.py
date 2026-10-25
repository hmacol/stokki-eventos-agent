# -*- coding: utf-8 -*-
"""As rotinas de resumo periodico (notas em rota, resumo diario, pedidos em
espera) obedecem a tela de Notificacoes (controle_notificacoes.py): a tela vale
por cima do config; sem linha na tela vale o config de hoje.

Rodar: py -3.11 -m unittest test_controle_nas_rotinas_resumo
"""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_RAIZ = Path(__file__).parent
sys.path.insert(0, str(_RAIZ))

import controle_notificacoes as cn
import notificar_nfs_em_rota as nfs
import notificar_pedidos_em_espera as em_espera
import preferencias_notificacao as pn
import resumo_diario_embarcador as resumo
from portal_cliente import dados_cliente
import test_notificar_nfs_em_rota as base_nfs
import test_resumo_diario_embarcador as base_resumo

HUGO = "hugo@freshlogbr.com"
CNPJ_A = "11111111000111"


class _ComControle(unittest.TestCase):
    """Banco temporario com `interno` minimo; o controle cria as proprias tabelas."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "dados.db"
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE interno (cnpj_embarcador TEXT PRIMARY KEY, nome_remetente TEXT, apelido TEXT, "
                     "email TEXT, notificar_email INTEGER NOT NULL DEFAULT 1, sender_id INTEGER, stkkc_id INTEGER)")
        conn.execute("INSERT INTO interno VALUES (?,?,?,?,?,?,?)",
                     (CNPJ_A, "Cliente A Ltda", "Cliente A", "log@a.com; fin@a.com", 1, base_nfs.SENDER_A, 9001))
        conn.commit()
        conn.close()
        self.enviados = []

    def _enviar(self, destinos, assunto, corpo, config_email, **kw):
        self.enviados.append((destinos, assunto, corpo))
        return True

    def _controle(self, fn, *args):
        conn = cn.conectar(self.db)
        try:
            fn(conn, *args, "teste")
        finally:
            conn.close()

    def _chaves_enviadas(self, tipo):
        conn = sqlite3.connect(self.db)
        try:
            return [r[0] for r in conn.execute(
                "SELECT chave FROM notificacoes_enviadas WHERE tipo = ? ORDER BY chave", (tipo,))]
        except sqlite3.OperationalError:    # tabela so nasce quando a rotina chega a enviar
            return []
        finally:
            conn.close()


class TestNfsEmRota(_ComControle):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(dados_cliente, "nf_por_codigo", return_value={})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _executar(self, config, **kw):
        return nfs.executar(config, base_nfs.DATA, rotas=base_nfs.ROTAS, motoristas={},
                            enviar=self._enviar, db_path=self.db, **kw)

    def test_nfs_tela_ligado_envia_mesmo_com_config_desligado(self):
        config = {"notificacoes_automaticas": {"ativo": False}, "notificacao_nfs_em_rota": {"forcar_destino": ""}}
        r = self._executar(config)
        self.assertEqual(self.enviados, [])
        self.assertTrue(r["desativado"])
        self._controle(cn.definir_estado, "nfs_em_rota", "LIGADO")
        r = self._executar(config)
        self.assertEqual([e[0] for e in self.enviados], [["log@a.com", "fin@a.com"]])
        self.assertFalse(r["desativado"])

    def test_nfs_tela_piloto_vai_so_pro_piloto_com_chave_redirecionado(self):
        config = {"notificacao_nfs_em_rota": {"forcar_destino": ""}}
        self._controle(cn.definir_estado, "nfs_em_rota", "PILOTO")
        self._executar(config)
        self.assertEqual([e[0] for e in self.enviados], [[HUGO]])
        self.assertIn("log@a.com", self.enviados[0][2])   # aviso "destino real"
        chaves = self._chaves_enviadas("nfs_em_rota")
        self.assertEqual(len(chaves), 1)
        self.assertTrue(chaves[0].endswith("|redirecionado"))
        self._controle(cn.definir_estado, "nfs_em_rota", "LIGADO")
        self._executar(config)
        self.assertEqual(self.enviados[1][0], ["log@a.com", "fin@a.com"])

    def test_nfs_bloqueado_nao_envia(self):
        config = {"notificacao_nfs_em_rota": {"forcar_destino": ""}}
        self._controle(cn.definir_estado, "nfs_em_rota", "LIGADO")
        self._controle(cn.definir_excecao, "nfs_em_rota", CNPJ_A, "BLOQUEADO")
        r = self._executar(config)
        self.assertEqual(self.enviados, [])
        self.assertEqual(r["enviados"], 0)
        self.assertEqual(self._chaves_enviadas("nfs_em_rota"), [])

    def test_rotina_sem_tabelas_de_controle_segue_config(self):
        # mestra desligada + forcar hugo@ (padrao sem a secao): so o piloto recebe
        r = self._executar({"notificacoes_automaticas": {"ativo": False}})
        self.assertEqual([e[0] for e in self.enviados], [[HUGO]])
        self.assertEqual(r["enviados"], 1)

    def test_nfs_geral_desligado_nao_envia(self):
        self._controle(cn.definir_estado, "nfs_em_rota", "LIGADO")
        conn = cn.conectar(self.db)
        cn.definir_geral(conn, False, "teste")
        conn.close()
        r = self._executar({"notificacao_nfs_em_rota": {"forcar_destino": ""}})
        self.assertEqual(self.enviados, [])
        self.assertTrue(r["desativado"])

    def test_nfs_reenviar_ignora_ja_enviado_do_sender(self):
        config = {"notificacao_nfs_em_rota": {"forcar_destino": ""}}
        self._executar(config)
        self._executar(config)
        self.assertEqual(len(self.enviados), 1)            # dedup normal
        r = self._executar(config, sender_id=base_nfs.SENDER_A, reenviar=True)
        self.assertEqual(len(self.enviados), 2)
        self.assertEqual(self.enviados[1][0], ["log@a.com", "fin@a.com"])
        self.assertEqual(r["ja_enviados"], 0)

    def test_nfs_reenviar_exige_sender_id(self):
        with self.assertRaises(ValueError):
            self._executar({"notificacao_nfs_em_rota": {"forcar_destino": ""}}, reenviar=True)


class TestResumoDiario(_ComControle):
    def setUp(self):
        super().setUp()
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE torre_excecoes_tratadas (id TEXT PRIMARY KEY, data_alvo TEXT, tipo TEXT, "
                     "descricao TEXT, motivo TEXT, tratado_em TEXT)")
        conn.commit()
        conn.execute("UPDATE interno SET sender_id = ?", (base_resumo.SENDER_A,))
        conn.commit()
        pn.salvar(conn, CNPJ_A, [], {"resumo_diario": True}, "cliente")
        conn.close()
        patcher = mock.patch.object(dados_cliente, "nf_por_codigo", return_value={})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _executar(self, config=None, **kw):
        return resumo.executar(base_resumo.CONFIG if config is None else config, base_resumo.DATA,
                               rotas=base_resumo.ROTAS, motoristas={}, enviar=self._enviar,
                               db_path=self.db, **kw)

    def test_resumo_diario_respeita_tela(self):
        self._controle(cn.definir_estado, "resumo_diario", "DESLIGADO")
        r = self._executar()
        self.assertEqual(self.enviados, [])
        self.assertTrue(r["desativado"])
        self._controle(cn.definir_estado, "resumo_diario", "LIGADO")
        self._executar()
        self.assertEqual([e[0] for e in self.enviados], [["log@a.com", "fin@a.com"]])

    def test_resumo_tela_ligado_envia_mesmo_com_config_desligado(self):
        config = {**base_resumo.CONFIG, "resumo_diario_embarcador": {"ativo": False, "forcar_destino": ""}}
        self._controle(cn.definir_estado, "resumo_diario", "LIGADO")
        self._executar(config)
        self.assertEqual([e[0] for e in self.enviados], [["log@a.com", "fin@a.com"]])

    def test_resumo_piloto_vai_so_pro_piloto(self):
        self._controle(cn.definir_estado, "resumo_diario", "PILOTO")
        self._executar()
        self.assertEqual([e[0] for e in self.enviados], [[HUGO]])
        self.assertTrue(self._chaves_enviadas("resumo_diario")[0].endswith("|redirecionado"))

    def test_resumo_bloqueado_nao_envia(self):
        self._controle(cn.definir_estado, "resumo_diario", "LIGADO")
        self._controle(cn.definir_excecao, "resumo_diario", CNPJ_A, "BLOQUEADO")
        self._executar()
        self.assertEqual(self.enviados, [])

    def test_resumo_sem_linha_na_tela_segue_config(self):
        r = self._executar({"resumo_diario_embarcador": {"ativo": False}})
        self.assertEqual(self.enviados, [])
        self.assertTrue(r["desativado"])

    def test_resumo_reenviar_ignora_ja_enviado_do_sender(self):
        self._executar()
        self._executar()
        self.assertEqual(len(self.enviados), 1)
        self._executar(sender_id=base_resumo.SENDER_A, reenviar=True)
        self.assertEqual(len(self.enviados), 2)
        with self.assertRaises(ValueError):
            self._executar(reenviar=True)


class TestPedidosEmEspera(_ComControle):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(pn, "DB_PATH", self.db)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _main(self, ativo=True):
        pedido = {"codigo_ps": "#PS-1", "id_stokki": 1, "data": "18/09/2026", "destino": "Cliente",
                  "marker": "Aguardando faturamento", "stkkc_id": 9001}
        execucao = mock.MagicMock()
        with mock.patch.object(em_espera, "_carregar_config", return_value={"email": {}}), \
             mock.patch.object(em_espera, "StokkiSession"), \
             mock.patch.object(em_espera, "_buscar_pedidos_em_espera", return_value=[pedido]), \
             mock.patch.object(em_espera, "notificacao_pedidos_em_espera_ativa", return_value=ativo), \
             mock.patch.object(em_espera, "notificar_execucao", execucao), \
             mock.patch.object(em_espera, "enviar_email", side_effect=self._enviar):
            em_espera.main(modo_teste=False)
        return execucao.call_args.args[0]["Notificador de Faturamento"]

    def test_espera_piloto_vai_pro_piloto(self):
        self._controle(cn.definir_estado, "pedidos_em_espera", "PILOTO")
        self._main()
        self.assertEqual([e[0] for e in self.enviados], [[HUGO]])
        self.assertIn("log@a.com", self.enviados[0][2])    # cabecalho de redirecionamento

    def test_espera_tela_ligado_envia_mesmo_com_config_desligado(self):
        self._controle(cn.definir_estado, "pedidos_em_espera", "LIGADO")
        self._main(ativo=False)
        self.assertEqual([e[0] for e in self.enviados], [["log@a.com", "fin@a.com"]])

    def test_espera_sem_linha_e_config_desligado_nao_envia(self):
        resultado = self._main(ativo=False)
        self.assertEqual(self.enviados, [])
        self.assertEqual(resultado["status"], "ok")

    def test_espera_bloqueado_nao_envia(self):
        self._controle(cn.definir_estado, "pedidos_em_espera", "LIGADO")
        self._controle(cn.definir_excecao, "pedidos_em_espera", CNPJ_A, "BLOQUEADO")
        self._main()
        self.assertEqual(self.enviados, [])


if __name__ == "__main__":
    unittest.main()
