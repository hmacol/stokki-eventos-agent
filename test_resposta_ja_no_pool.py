"""Embarcador confirma o reenvio e o pedido ja esta no pool: nao duplica,
responde 'disponivel' (sem falar de bloqueio) e tira o pedido da fila."""
import sys
import unittest
from pathlib import Path
from unittest import mock

_RAIZ = Path(__file__).parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(1, str(_RAIZ / "insucesso_entrega"))

import aplicar_resposta_insucesso as ari  # noqa: E402
import fingerprint_duplicacao_insucesso as fp  # noqa: E402
import fingerprint_duplicacao_agendada as fa  # noqa: E402


class TestRespostaJaNoPool(unittest.TestCase):
    def test_reenviar_com_reentrega_no_pool_responde_disponivel(self):
        achados = {"#PS-100": {"id": 1, "code": "#PS-100", "status": "done"},
                   "#PS-100-R1": {"id": 2, "code": "#PS-100-R1", "status": "not_assigned"}}
        vuupt = mock.Mock()
        vuupt.buscar_servico_por_code.side_effect = lambda c: achados.get(c) or achados.get("#" + c)
        pendente = {"service_id": 1, "code": "#PS-100"}
        config = {"vuupt_api": {"token": "t"}, "email": {}}
        with mock.patch.object(ari, "VuuptClient", return_value=vuupt), \
             mock.patch.object(ari, "buscar_pendentes_por_grupo", return_value=[pendente]), \
             mock.patch.object(ari, "_adquirir_trava", return_value=True), \
             mock.patch.object(ari, "_liberar_trava"), \
             mock.patch.object(fp, "ja_duplicado", return_value=False), \
             mock.patch.object(fa, "ja_agendado", return_value=False), \
             mock.patch.object(fp, "marcar_duplicado") as marcar, \
             mock.patch.object(ari.tratativas, "registrar_evento"):
            r = ari.aplicar_decisao(7, 5563, "reenviar", None, config)
        self.assertEqual([x["resultado"] for x in r], ["disponivel"])
        vuupt.criar_servico.assert_not_called()
        marcar.assert_called_once_with(1, "#PS-100-R1")


if __name__ == "__main__":
    unittest.main()
