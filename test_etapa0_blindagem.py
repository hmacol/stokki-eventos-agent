# -*- coding: utf-8 -*-
"""
test_etapa0_blindagem.py

Testes da Etapa 0 do DOC_EXECUCAO_CLAUDE_SAIDA_VUUPT.md, sem rede e sem
e-mail: janela anti-enxurrada do alerta de falha, escape do log no corpo,
seleção de fotos pendentes do backup e a comparação do ensaio de
restauração. Rodar (raiz do repo):
    python -m unittest test_etapa0_blindagem -v
"""
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import alertar_falha_job as alerta
import backup_dados_gcs as backup
import ensaiar_restauracao_backup as ensaio


class TestAlertaFalha(unittest.TestCase):
    def test_um_email_por_janela_e_conta_as_suprimidas(self):
        estado = {}
        t0 = datetime(2026, 9, 15, 10, 0, 0)
        self.assertEqual(alerta.decidir_envio(estado, "x.service", t0, 120), (True, 0))
        self.assertEqual(alerta.decidir_envio(estado, "x.service", t0 + timedelta(minutes=1), 120), (False, 1))
        self.assertEqual(alerta.decidir_envio(estado, "x.service", t0 + timedelta(minutes=2), 120), (False, 2))
        # outra unidade não é afetada
        self.assertEqual(alerta.decidir_envio(estado, "y.service", t0 + timedelta(minutes=2), 120), (True, 0))
        # passou a janela: envia e informa quantas ficaram sem e-mail
        self.assertEqual(alerta.decidir_envio(estado, "x.service", t0 + timedelta(minutes=121), 120), (True, 2))
        self.assertEqual(estado["x.service"]["suprimidos"], 0)

    def test_estado_corrompido_nao_impede_alerta(self):
        estado = {"x.service": {"ultimo_alerta": "ontem", "suprimidos": 3}}
        enviar, suprimidos = alerta.decidir_envio(estado, "x.service", datetime(2026, 9, 15), 120)
        self.assertTrue(enviar)
        self.assertEqual(suprimidos, 3)

    def test_corpo_escapa_o_log(self):
        corpo = alerta.montar_corpo("x.service", {"Result": "exit-code", "ExecMainStatus": "1"},
                                    "Traceback <script>alert(1)</script>", 0)
        self.assertNotIn("<script>", corpo)
        self.assertIn("&lt;script&gt;", corpo)


class TestBackupComprovantes(unittest.TestCase):
    def test_so_sobe_foto_nova_ou_alterada(self):
        with tempfile.TemporaryDirectory() as d:
            pasta = Path(d) / "comprovantes"
            (pasta / "PS-1").mkdir(parents=True)
            (pasta / "rota-9").mkdir()
            (pasta / "PS-1" / "canhoto_a.jpg").write_bytes(b"123")
            (pasta / "rota-9" / "pedagio_b.jpeg").write_bytes(b"12345")
            with mock.patch.object(backup, "PASTA_COMPROVANTES", pasta):
                todos = backup.comprovantes_pendentes({})
                self.assertEqual(sorted(r for _, r in todos), ["PS-1/canhoto_a.jpg", "rota-9/pedagio_b.jpeg"])
                ja_enviados = {"PS-1/canhoto_a.jpg": 3, "rota-9/pedagio_b.jpeg": 4}   # tamanho mudou
                self.assertEqual([r for _, r in backup.comprovantes_pendentes(ja_enviados)], ["rota-9/pedagio_b.jpeg"])

    def test_sem_pasta_nada_a_fazer(self):
        with mock.patch.object(backup, "PASTA_COMPROVANTES", Path(tempfile.gettempdir()) / "nao-existe-xyz"):
            self.assertEqual(backup.comprovantes_pendentes({}), [])

    def test_retencao_exige_janela_positiva(self):
        with self.assertRaises(ValueError):
            backup.limpar_backups_antigos({}, "backups/x/")


class TestEnsaioRestauracao(unittest.TestCase):
    def test_tabela_critica_faltando_ou_encolhida_reprova(self):
        producao = {"nucleo_rotas": 100, "nucleo_paradas": 1000, "tabela_qualquer": 5}
        copia = {"nucleo_rotas": 99, "nucleo_paradas": 800}
        problemas, avisos = ensaio.comparar(producao, copia)
        self.assertEqual(len(problemas), 1)
        self.assertIn("nucleo_paradas", problemas[0])
        self.assertTrue(any("tabela_qualquer" in a for a in avisos))

    def test_copia_um_pouco_atrasada_passa(self):
        producao = {t: 1000 for t in ensaio.TABELAS_CRITICAS}
        copia = {t: 950 for t in ensaio.TABELAS_CRITICAS}
        problemas, _ = ensaio.comparar(producao, copia)
        self.assertEqual(problemas, [])


if __name__ == "__main__":
    unittest.main()
