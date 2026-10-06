# -*- coding: utf-8 -*-
"""
Rodar (da raiz):  py -3.11 -m unittest documentos_pedido.test_vigia_documentos
"""
import sqlite3
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

_RAIZ = Path(__file__).parent.parent
for _p in (_RAIZ, _RAIZ / "documentos_pedido"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import vigia_documentos as vd  # noqa: E402

AGORA = datetime(2026, 10, 6, 6, 43)


class Rodadas(unittest.TestCase):
    def test_rodada_ok(self):
        linhas = ["2026-10-05 22:31:10,1 [INFO] processar_documentos - Processamento de documentos "
                  "finalizado em 1800.0s. Contadores: {'JA_PROCESSADO': 212, 'ENVIADO': 16, "
                  "'REVISAO_MANUAL': 1, 'ERRO': 0}"]
        r = vd.checar_rodadas(linhas, AGORA)
        self.assertEqual(r["status"], "ok")

    def test_rodada_com_erro(self):
        linhas = ["2026-10-05 22:31:10,1 [INFO] processar_documentos - Processamento de documentos "
                  "finalizado em 10.0s. Contadores: {'ENVIADO': 0, 'ERRO': 54}"]
        self.assertEqual(vd.checar_rodadas(linhas, AGORA)["status"], "erro")

    def test_so_rodada_de_teste_ou_antiga_e_erro(self):
        linhas = [
            "2026-10-05 20:27:20,2 [INFO] processar_documentos - [MODO TESTE] [ESCOPADO] Processamento "
            "de documentos finalizado em 231.2s. Contadores: {'ERRO': 0}",
            "2026-10-03 22:31:10,1 [INFO] processar_documentos - Processamento de documentos "
            "finalizado em 1.0s. Contadores: {'ERRO': 0}",
        ]
        self.assertEqual(vd.checar_rodadas(linhas, AGORA)["status"], "erro")

    def test_so_rodada_escopada_e_erro(self):
        linhas = ["2026-10-05 20:27:20,2 [INFO] processar_documentos - [ESCOPADO] Processamento "
                  "de documentos finalizado em 31.2s. Contadores: {'ERRO': 0}"]
        self.assertEqual(vd.checar_rodadas(linhas, AGORA)["status"], "erro")

    def test_falha_imap_e_erro(self):
        linhas = [
            "2026-10-05 22:31:10,1 [INFO] processar_documentos - Processamento de documentos "
            "finalizado em 1.0s. Contadores: {'ERRO': 0}",
            "2026-10-05 22:01:00,1 [ERROR] email_documentos - Erro ao buscar PDFs por e-mail: timeout",
        ]
        self.assertEqual(vd.checar_rodadas(linhas, AGORA)["status"], "erro")


class Pendencias(unittest.TestCase):
    def test_agrupa_por_tipo_e_embarcador(self):
        servicos = [{"codigo": "PS-1", "sender_id": 1, "embarcador": "QUATRO ESTRELAS"},
                    {"codigo": "PS-2", "sender_id": 2, "embarcador": "NUU"},
                    {"codigo": "PS-3", "sender_id": 2, "embarcador": "NUU"}]
        faltas = {"PS-1": ["sem nota fiscal"], "PS-2": ["sem boleto"],
                  "PS-3": ["arquivo não localizado: x.pdf"]}
        with patch.object(vd, "_avaliar", side_effect=lambda s, docs: {"faltas": faltas[s["codigo"]]}):
            r = vd.checar_pendencias(servicos, {})
        self.assertEqual(r["Nota fiscal (rotas do dia)"]["status"], "erro")
        self.assertIn("QUATRO ESTRELAS: PS-1", r["Nota fiscal (rotas do dia)"]["detalhe"])
        self.assertIn("NUU: PS-2", r["Boleto (rotas do dia)"]["detalhe"])
        self.assertEqual(r["Arquivos (rotas do dia)"]["status"], "erro")

    def test_boleto_faltando_so_no_email(self):
        # Decisão 06/10: boleto faltando vai no e-mail, mas não dispara
        # WhatsApp (status "ok" -- só etapa != "ok" vai pro grupo).
        servicos = [{"codigo": "PS-2", "sender_id": 2, "embarcador": "NUU"}]
        with patch.object(vd, "_avaliar", return_value={"faltas": ["sem boleto"]}):
            r = vd.checar_pendencias(servicos, {})
        self.assertEqual(r["Boleto (rotas do dia)"]["status"], "ok")
        self.assertIn("ATENÇÃO", r["Boleto (rotas do dia)"]["detalhe"])
        self.assertIn("NUU: PS-2", r["Boleto (rotas do dia)"]["detalhe"])

    def test_dia_sem_rotas_nao_e_alerta(self):
        r = vd.checar_pendencias([], {})
        self.assertTrue(all(v["status"] == "ok" for v in r.values()))


class ServicosDoDia(unittest.TestCase):
    def test_le_rotas_do_dia_sem_canceladas_e_sem_coleta(self):
        con = sqlite3.connect(":memory:")
        con.executescript("""
            CREATE TABLE nucleo_rotas (vuupt_route_id INT, data_rota TEXT, status TEXT, cancelada_em TEXT, nome TEXT);
            CREATE TABLE nucleo_pedidos (codigo TEXT, sender_id INT, remetente_nome TEXT, tipo TEXT,
                                         vuupt_route_id INT, excluido_em TEXT);
            INSERT INTO nucleo_rotas VALUES (1,'2026-10-06','PLANEJADA',NULL,'Rota 1'),
                                            (2,'2026-10-06','CANCELADA','2026-10-05','Rota 2'),
                                            (3,'2026-10-05','CONCLUIDA',NULL,'Rota 3');
            INSERT INTO nucleo_pedidos VALUES ('#PS-1',11,'NUU','delivery',1,NULL),
                                              ('#PS-2',11,'NUU','pickup',1,NULL),
                                              ('#PS-3',11,'NUU','delivery',2,NULL),
                                              ('#PS-4',11,'NUU','delivery',3,NULL),
                                              ('#PS-5',11,'NUU','delivery',1,'2026-10-05');
        """)
        s = vd.servicos_do_dia(con, "2026-10-06")
        self.assertEqual([x["codigo"] for x in s], ["PS-1"])


if __name__ == "__main__":
    unittest.main()
