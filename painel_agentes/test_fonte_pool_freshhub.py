# -*- coding: utf-8 -*-
"""
test_fonte_pool_freshhub.py -- com `planejamento.fonte_pool: freshhub` (plano 1.2 do freshhub) o botao
"Atualizar" do pool nao ressincroniza o nucleo (o pool vem da API nova) e diz por que.

    (de dentro de painel_agentes/)  py -3.11 -m unittest test_fonte_pool_freshhub -v
"""
import sys
import unittest
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(Path(__file__).parent))

import planejamento_rotas as pr  # noqa: E402


class TestFontePool(unittest.TestCase):
    def test_fonte_freshhub_nao_ressincroniza_e_diz_por_que(self):
        self.assertEqual(pr.sincronizar_pool_agora({"planejamento": {"fonte_pool": "freshhub"}}),
                         {"pulado": True, "motivo": "fonte freshhub"})
        self.assertEqual(pr.sincronizar_pool_agora({"planejamento": {"fonte_pool": "vuupt"}}), {"pulado": True})


if __name__ == "__main__":
    unittest.main()
