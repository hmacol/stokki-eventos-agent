# -*- coding: utf-8 -*-
"""NF ao lado do PS na API do app dos motoristas (campo `nf` da parada).
    py -3.11 -m unittest nucleo.test_nf_motorista -v
"""
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

from nucleo import banco, operacao, rotas


def _rascunho():
    data = date.today().isoformat()
    paradas = [{"ordem": i, "service_id": None, "codigo": cod, "titulo": f"Cliente {i}", "endereco": "Rua A, 1",
                "latitude": -23.5, "longitude": -46.6, "nivel_dificuldade": 1, "volume_caixas": 1}
               for i, cod in enumerate(["PS-10", "PS-20", "PS-30"], 1)]
    return {"id": 1, "data_alvo": data, "lote_id": "L", "nome": "Plan", "agent_id": 1, "vehicle_id": None,
            "motorista_nome": "T", "tipo_veiculo": None, "start_location_base_id": 1, "end_location_base_id": 1,
            "start_at": f"{data} 07:00:00", "km_estimado": 10.0, "status": "RASCUNHO", "vuupt_route_id": None,
            "paradas": paradas}


class NfMotoristaTest(unittest.TestCase):
    def test_parada_com_uma_varias_e_sem_nf(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp, \
                mock.patch.object(banco, "DB_PATH", Path(tmp) / "t.db"):
            conn = banco.conectar()
            conn.execute("CREATE TABLE documentos_pedido (id_pedido, tipo_doc, status, numero_nf)")
            conn.execute("CREATE TABLE documentos_processados (codigo_pedido, tipo, status, numero_nf)")
            conn.executemany("INSERT INTO documentos_pedido VALUES (?,?,?,?)", [
                ("#PS-10", "nf", "casado", "100"),
                ("#PS-20", "nf", "casado", "300"), ("#PS-20", "nf", "baixado", "200"),
            ])
            rota_id = rotas.materializar_rascunho(_rascunho(), banco.PROVEDOR_APP, conn=conn)
            rota = conn.execute("SELECT * FROM nucleo_rotas WHERE id = ?", (rota_id,)).fetchone()
            montada = operacao.montar_rota(conn, rota)
            nfs = {p["codigo"]: p["nf"] for p in montada["paradas"]}
            self.assertEqual(nfs, {"PS-10": "100", "PS-20": "200, 300", "PS-30": ""})
            # montar_parada solta (sem mapa) tambem preenche
            p = conn.execute("SELECT * FROM nucleo_paradas WHERE rota_id = ? AND ordem = 2", (rota_id,)).fetchone()
            self.assertEqual(operacao.montar_parada(conn, p)["nf"], "200, 300")
            conn.close()


if __name__ == "__main__":
    unittest.main()
