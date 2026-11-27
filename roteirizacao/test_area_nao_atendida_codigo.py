# -*- coding: utf-8 -*-
"""
Aviso de area nao atendida deduplicado pelo CODIGO (Task 7, pool direto da Stokki): o pedido
nasce com id sintetico (-id_pool) e ganha o id real da Vuupt so no envio. Pelo service_id o
aviso repetiria quando o id troca. Registro antigo (so service_id) continua valendo.
Banco temporario, e-mail falso. Rodar (da raiz):
    py -3.11 -m unittest roteirizacao.test_area_nao_atendida_codigo
"""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_AQUI = Path(__file__).parent
for _p in (_AQUI.parent, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import fingerprint_area_nao_atendida as fp  # noqa: E402
import notificar_area_nao_atendida as na  # noqa: E402


class _Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name) / "dados.db"
        p = mock.patch.object(fp, "DB_PATH", self.db)
        p.start()
        self.addCleanup(p.stop)


class DedupePorCodigo(_Base):
    def test_aviso_area_nao_repete_ao_adotar_servico(self):
        enviados = []
        with mock.patch.object(na, "_carregar_embarcadores_por_sender_id",
                               return_value={10: {"nome": "EMB", "emails": ["emb@x.com"]}}), \
                mock.patch.object(na, "enviar_email", side_effect=lambda d, a, c, cfg: enviados.append(d) or True), \
                mock.patch.object(na, "_montar_conteudo", return_value="<p>x</p>"):
            # 1a rodada: pedido ainda sem servico na Vuupt (id sintetico)
            r1 = na.notificar_remetentes([({"id": -7, "code": "#PS-1", "sender_id": 10}, na.TIPO_FORA_SP)], {})
            # 2a rodada: o mesmo pedido, ja com o servico real (id trocou, codigo nao)
            r2 = na.notificar_remetentes([({"id": 900, "code": "#PS-1", "sender_id": 10}, na.TIPO_FORA_SP)], {})
            # outro pedido continua sendo avisado
            r3 = na.notificar_remetentes([({"id": 901, "code": "#PS-2", "sender_id": 10}, na.TIPO_FORA_SP)], {})
        self.assertEqual((r1["enviados"], r2["enviados"], r3["enviados"]), (1, 0, 1))
        self.assertEqual(len(enviados), 2)

    def test_codigo_normalizado(self):
        fp.marcar_notificado(-7, na.TIPO_FORA_SP, codigo="#ps-1 ")
        self.assertTrue(fp.ja_notificado(900, "PS-1"))
        self.assertTrue(fp.ja_notificado(900, "#PS-1"))
        self.assertFalse(fp.ja_notificado(900, "PS-1-R1"))     # reentrega e outro pedido (igual a hoje)
        self.assertFalse(fp.ja_notificado(900))


class RegistroAntigo(_Base):
    def test_registro_antigo_por_service_id_continua_valendo(self):
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE pedidos_area_notificada (service_id INTEGER PRIMARY KEY, tipo TEXT NOT NULL, "
                     "notificado_em TEXT NOT NULL)")
        conn.execute("INSERT INTO pedidos_area_notificada VALUES (500, 'fora_sp', '2026-11-01 10:00:00')")
        conn.commit()
        conn.close()
        self.assertTrue(fp.ja_notificado(500, "PS-5"))          # tabela migrada, chave antiga vale
        self.assertTrue(fp.ja_notificado(500))
        self.assertFalse(fp.ja_notificado(501, "PS-6"))
        fp.marcar_notificado(501, na.TIPO_FORA_SP, codigo="PS-6")
        self.assertTrue(fp.ja_notificado(-3, "PS-6"))
        conn = sqlite3.connect(self.db)
        colunas = {r[1] for r in conn.execute("PRAGMA table_info(pedidos_area_notificada)")}
        conn.close()
        self.assertIn("codigo", colunas)


if __name__ == "__main__":
    unittest.main()
