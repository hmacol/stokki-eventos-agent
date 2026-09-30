# -*- coding: utf-8 -*-
"""
Testes do `gerenciar_clientes.py checar` (24/09): o que conferir antes de
mandar o convite do portal pra um embarcador novo.

    py -3.11 -m unittest portal_cliente.test_checar_cliente
"""
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import auth_cliente as auth  # noqa: E402
import gerenciar_clientes as gc  # noqa: E402

MAIZ, MARCHEF, ITAUEIRA, VIDAVEG = "22830809000183", "54993021000184", "48654566000163", "22310186000118"
HOJE = datetime(2026, 9, 24, 10, 0)


class ChecarTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patch = mock.patch.object(auth, "DB_PATH", Path(self._tmp.name) / "dados.db")
        self._patch.start()
        self.conn = auth.conectar()
        self.conn.executescript("""
            CREATE TABLE interno (cnpj_embarcador TEXT, sender_id INTEGER, nome_remetente TEXT,
                                  apelido TEXT, email TEXT, stkkc_id INTEGER);
            CREATE TABLE insucessos_aguardando_resposta (service_id INTEGER PRIMARY KEY, sender_id INTEGER,
                                  status TEXT, primeira_notificacao_em TEXT);
            CREATE TABLE portal_clientes_envio (cnpj TEXT PRIMARY KEY, envio_ativo INTEGER NOT NULL DEFAULT 1);
        """)
        self.conn.executemany("INSERT INTO interno VALUES (?, ?, ?, ?, ?, ?)", [
            (MAIZ, 300, "MAIZ FOOD LTDA", "MAÍZ FOOD", "maiz@maizfood.com.br, comercial@maizfood.com.br", 93),
            (MARCHEF, 100, "MARCHEF", None, "logistica@marchef.com", 50),
            (ITAUEIRA, 101, "ITAUEIRA CAMAROES", None, "LOGISTICA@marchef.com", None),
            (VIDAVEG, 200, "VIDAVEG S.A.", None, "", 60)])
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self._patch.stop()
        self._tmp.cleanup()

    def test_procurar_por_nome_ignora_acento_e_caixa(self):
        self.assertEqual([e["cnpj"] for e in gc.procurar(self.conn, "maiz")], [MAIZ])

    def test_procurar_por_cnpj_formatado(self):
        self.assertEqual([e["cnpj"] for e in gc.procurar(self.conn, "22.830.809/0001-83")], [MAIZ])
        self.assertEqual(gc.procurar(self.conn, "11111111000111"), [])

    def test_cliente_limpo_fica_pronto_pro_convite(self):
        r = gc.checar(self.conn, MAIZ, agora=HOJE)
        self.assertEqual(r["alertas"], [])
        self.assertEqual(r["conta"], "sem conta")
        self.assertEqual(r["stkkc_id"], 93)

    def test_insucesso_pendente_velho_vira_alerta(self):
        self.conn.executemany("INSERT INTO insucessos_aguardando_resposta VALUES (?, ?, ?, ?)", [
            (1, 300, "PENDENTE", "2026-09-10 08:00:00"), (2, 300, "PENDENTE", "2026-09-23 08:00:00"),
            (3, 300, "RESPONDIDO", "2026-09-01 08:00:00"), (4, 200, "PENDENTE", "2026-09-01 08:00:00")])
        r = gc.checar(self.conn, MAIZ, agora=HOJE)
        self.assertEqual((r["insucessos_pendentes"], r["insucessos_velhos"]), (2, 1))
        self.assertTrue(any("insucesso" in a for a in r["alertas"]))

    def test_sem_email_sem_stokki_e_envio_desativado_viram_alerta(self):
        self.conn.execute("INSERT INTO portal_clientes_envio VALUES (?, 0)", (VIDAVEG,))
        r = gc.checar(self.conn, VIDAVEG, agora=HOJE)
        self.assertTrue(any("e-mail" in a for a in r["alertas"]))
        self.assertTrue(any("desativado" in a for a in r["alertas"]))
        self.assertTrue(any("stkkc_id" in a for a in gc.checar(self.conn, ITAUEIRA, agora=HOJE)["alertas"]))

    def test_mesmo_email_de_outro_embarcador_sugere_grupo(self):
        r = gc.checar(self.conn, MARCHEF, agora=HOJE)
        self.assertEqual([e["cnpj"] for e in r["mesmo_email"]], [ITAUEIRA])
        self.assertTrue(any("grupo" in a for a in r["alertas"]))

    def test_membro_de_grupo_aponta_o_login_do_grupo(self):
        auth.definir_grupo(self.conn, MARCHEF, [ITAUEIRA])
        self.assertEqual([e["cnpj"] for e in gc.checar(self.conn, MARCHEF, agora=HOJE)["grupo"]], [ITAUEIRA])
        r = gc.checar(self.conn, ITAUEIRA, agora=HOJE)
        self.assertEqual(r["membro_de"], [MARCHEF])
        self.assertTrue(any(auth.formatar_cnpj(MARCHEF) in a for a in r["alertas"]))

    def test_conta_ja_ativa_vira_alerta(self):
        auth.definir_pin(self.conn, MAIZ, "123456")
        r = gc.checar(self.conn, MAIZ, agora=HOJE)
        self.assertEqual(r["conta"], "ATIVA")
        self.assertTrue(any("já tem conta" in a for a in r["alertas"]))


if __name__ == "__main__":
    unittest.main()
