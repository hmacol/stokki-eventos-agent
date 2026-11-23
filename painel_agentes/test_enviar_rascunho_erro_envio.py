# -*- coding: utf-8 -*-
"""
test_enviar_rascunho_erro_envio.py

rascunhos_rota.enviar_rascunho: um card em ERRO_ENVIO pode ser
reenviado pelo botao "Confirmar e enviar" (Hugo, 07/10). Antes o envio
so aceitava status RASCUNHO e o card ficava preso com a mensagem velha:
a unica saida era descartar e recriar a rota, ou mexer no banco.

Usa um SQLite temporario (DB_PATH trocado); a Vuupt e os efeitos
colaterais pos-envio (nucleo, fingerprint, documentacao) sao mockados.
Rodar (de dentro de painel_agentes/):
    py -3.11 -m unittest test_enviar_rascunho_erro_envio -v
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import rascunhos_rota


def _inserir_rascunho(status, erro_envio=None):
    conn = rascunhos_rota._conectar()
    try:
        cur = conn.execute(
            "INSERT INTO rascunhos_rota (data_alvo, lote_id, nome, agent_id, vehicle_id, motorista_nome, "
            "start_location_base_id, end_location_base_id, start_at, status, erro_envio) "
            "VALUES ('2026-10-07', 'lote-teste', 'Planejamento - 07/10/2026 - #12', 47084, 1, 'Vinicius', "
            "6950, 6950, '2026-10-07T09:00:00Z', ?, ?)",
            (status, erro_envio),
        )
        rid = cur.lastrowid
        conn.execute(
            "INSERT INTO rascunhos_parada (rascunho_id, ordem, service_id, codigo) VALUES (?, 0, 53849769, '#PS-41144')",
            (rid,),
        )
        conn.commit()
        return rid
    finally:
        conn.close()


def _ler(rid):
    conn = rascunhos_rota._conectar()
    try:
        return dict(conn.execute("SELECT status, erro_envio, vuupt_route_id FROM rascunhos_rota WHERE id = ?", (rid,)).fetchone())
    finally:
        conn.close()


class EnviarRascunhoErroEnvioTestCase(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patches = [
            mock.patch.object(rascunhos_rota, "DB_PATH", Path(self._tmp.name) / "dados.db"),
            mock.patch("rotas_client.criar_rota_removendo_conflitos",
                       side_effect=lambda token, nome, start_at, sublote, **kw: ({"id": 777}, list(sublote), [])),
            mock.patch("fingerprint_rotas.marcar_alocado"),
            mock.patch("nucleo.rotas.registrar_rota_enviada"),
            mock.patch("nucleo.pedidos.marcar_em_rota_por_service_ids"),
            mock.patch("documentacao_rota.agendar"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_card_em_erro_envio_e_reenviado(self):
        rid = _inserir_rascunho("ERRO_ENVIO", erro_envio="400 Bad Request ... Servico nao pode ser atribuido")

        resultado = rascunhos_rota.enviar_rascunho(rid, "token")

        self.assertTrue(resultado["ok"], resultado)
        self.assertEqual(resultado["vuupt_route_id"], 777)
        gravado = _ler(rid)
        self.assertEqual(gravado["status"], "ENVIADO")
        self.assertIsNone(gravado["erro_envio"])
        self.assertEqual(gravado["vuupt_route_id"], 777)

    def test_card_ja_enviado_continua_recusado(self):
        rid = _inserir_rascunho("ENVIADO")

        resultado = rascunhos_rota.enviar_rascunho(rid, "token")

        self.assertFalse(resultado["ok"])
        self.assertIn("status=ENVIADO", resultado["erro"])


if __name__ == "__main__":
    unittest.main()
