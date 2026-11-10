# -*- coding: utf-8 -*-
"""
Acoes da tela /pedidos (Hugo, 10/11): Reenvio.

    py -3.11 -m unittest test_acoes_pedidos   (de dentro de painel_agentes/)
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from nucleo import banco  # noqa: E402
import acoes_pedidos  # noqa: E402


class VuuptFalso:
    def __init__(self, servicos):
        self.servicos = servicos
        self.por_code = {}
        self.chamadas = []

    def buscar_servico_por_code(self, code):
        return self.por_code.get(code.lstrip("#"))

    def buscar_servico_por_id(self, sid):
        self.chamadas.append(sid)
        return self.servicos.get(sid)


class TestReenvio(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        p = mock.patch.object(banco, "DB_PATH", Path(self._tmp.name) / "dados.db")
        p.start()
        self.addCleanup(p.stop)
        conn = banco.conectar()
        self._linha(conn, "#PS-1", 101, 10, "ENTREGUE")
        conn.commit()
        conn.close()
        d = mock.patch.object(acoes_pedidos.torre_controle, "duplicar_pedido_manual",
                              return_value={"ok": True, "novo_code": "#PS-1-R1"})
        self.dup = d.start()
        self.addCleanup(d.stop)
        self.stokki = mock.Mock(return_value="Enviado")

    @staticmethod
    def _linha(conn, codigo, sid, sender, status):
        conn.execute("INSERT INTO nucleo_pedidos (codigo, titulo, status, sender_id, vuupt_service_id) "
                     "VALUES (?, ?, ?, ?, ?)", (codigo, codigo, status, sender, sid))

    def _vuupt(self, **status_por_id):
        """s101="done" / "failed" (insucesso = done + status_done failed) / outro status cru."""
        def serv(k, v):
            d = {"id": int(k[1:]), "code": "#PS-1", "title": "#PS-1 - NF1"}
            if v == "failed":
                d.update(status="done", status_done="failed")
            else:
                d["status"] = v
            return d
        return VuuptFalso({int(k[1:]): serv(k, v) for k, v in status_por_id.items()})

    def test_reenvio_de_entregue_cria_e_registra_motivo(self):
        v = self._vuupt(s101="done")
        r = acoes_pedidos.reenviar(101, "cliente recusou", "hugo", vuupt=v, stokki=self.stokki)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["novo_code"], "#PS-1-R1")
        args, kw = self.dup.call_args
        self.assertEqual(kw["origem"], "PEDIDOS")
        self.assertFalse(kw["marcar_tratado"])
        self.assertEqual(kw["servico_original"], v.servicos[101])
        self.assertIn("hugo: ", kw["texto"])
        self.assertIn("cliente recusou", kw["texto"])
        self.stokki.assert_called_once_with("#PS-1")

    def test_reenvio_de_insucesso_marca_tratado(self):
        r = acoes_pedidos.reenviar(101, "reentrega pedida", "hugo", vuupt=self._vuupt(s101="failed"),
                                   stokki=self.stokki)
        self.assertTrue(r["ok"], r)
        self.assertTrue(self.dup.call_args.kwargs["marcar_tratado"])

    def test_recusa_tentativa_viva_que_o_nucleo_nao_conhece(self):
        v = self._vuupt(s101="done")
        v.por_code["PS-1-R2"] = {"id": 777, "code": "#PS-1-R2", "status": "on_route"}
        v.por_code["PS-1-R1"] = {"id": 776, "code": "#PS-1-R1", "status": "done"}
        r = acoes_pedidos.reenviar(101, "motivo ok", "hugo", vuupt=v, stokki=self.stokki)
        self.assertFalse(r["ok"])
        self.assertIn("PS-1-R2", r["erro"])
        self.dup.assert_not_called()

    def test_servico_excluido_conta_como_morto(self):
        v = self._vuupt(s101="done")
        v.por_code["PS-1-R1"] = {"id": 776, "code": "#PS-1-R1", "status": "not_assigned", "deleted_at": "2026-11-01"}
        self.assertTrue(acoes_pedidos.reenviar(101, "motivo ok", "hugo", vuupt=v, stokki=self.stokki)["ok"])

    def test_membro_excluido_do_nucleo_que_sumiu_da_vuupt_nao_bloqueia(self):
        conn = banco.conectar()
        self._linha(conn, "PS-1-R1", 102, 10, "CANCELADO")
        conn.execute("UPDATE nucleo_pedidos SET excluido_em = '2026-11-01' WHERE vuupt_service_id = 102")
        conn.commit()
        conn.close()
        self.assertTrue(acoes_pedidos.reenviar(101, "motivo ok", "hugo", vuupt=self._vuupt(s101="done"),
                                               stokki=self.stokki)["ok"])

    def test_entregue_nao_marca_tratado(self):
        acoes_pedidos.reenviar(101, "motivo ok", "hugo", vuupt=self._vuupt(s101="done"), stokki=self.stokki)
        self.assertFalse(self.dup.call_args.kwargs["marcar_tratado"])

    def test_recusa_retirada(self):
        v = self._vuupt(s101="done")
        v.servicos[101]["title"] = "[RETIRADA] #PS-1 - NF1"
        r = acoes_pedidos.reenviar(101, "motivo ok", "hugo", vuupt=v, stokki=self.stokki)
        self.assertFalse(r["ok"])
        self.assertIn("retirada não vai pra Vuupt", r["erro"])
        self.dup.assert_not_called()

    def test_recusa_pedido_aberto(self):
        r = acoes_pedidos.reenviar(101, "motivo ok", "hugo", vuupt=self._vuupt(s101="not_assigned"),
                                   stokki=self.stokki)
        self.assertFalse(r["ok"])
        self.assertIn("ainda aberto", r["erro"])
        self.dup.assert_not_called()

    def test_recusa_quando_outra_tentativa_do_grupo_esta_viva(self):
        conn = banco.conectar()
        self._linha(conn, "PS-1-R1", 102, 10, "EM_ROTA")
        conn.commit()
        conn.close()
        r = acoes_pedidos.reenviar(101, "motivo ok", "hugo", vuupt=self._vuupt(s101="done", s102="on_route"),
                                   stokki=self.stokki)
        self.assertFalse(r["ok"])
        self.assertIn("PS-1-R1", r["erro"])
        self.dup.assert_not_called()

    def test_grupo_nao_mistura_empresas(self):
        conn = banco.conectar()
        self._linha(conn, "PS-1-R1", 202, 99, "EM_ROTA")
        conn.commit()
        grupo = acoes_pedidos.servicos_do_grupo(conn, 101)
        conn.close()
        self.assertEqual([g["vuupt_service_id"] for g in grupo], [101])

    def test_recusa_cancelado_na_stokki(self):
        r = acoes_pedidos.reenviar(101, "motivo ok", "hugo", vuupt=self._vuupt(s101="done"),
                                   stokki=lambda c: "Cancelado")
        self.assertFalse(r["ok"])
        self.assertIn("cancelado na Stokki", r["erro"])
        self.dup.assert_not_called()

    def test_stokki_ocupada_nao_cria(self):
        r = acoes_pedidos.reenviar(101, "motivo ok", "hugo", vuupt=self._vuupt(s101="done"),
                                   stokki=lambda c: None)
        self.assertFalse(r["ok"])
        self.assertIn("não deu pra conferir a Stokki", r["erro"])
        self.dup.assert_not_called()

    def test_motivo_curto_ou_longo(self):
        v = self._vuupt(s101="done")
        for motivo in ("abc", "x" * 201, "   ab   "):
            r = acoes_pedidos.reenviar(101, motivo, "hugo", vuupt=v, stokki=self.stokki)
            self.assertFalse(r["ok"])
        self.assertEqual(v.chamadas, [])

    def test_servico_sumiu_da_vuupt(self):
        r = acoes_pedidos.reenviar(101, "motivo ok", "hugo", vuupt=VuuptFalso({}), stokki=self.stokki)
        self.assertFalse(r["ok"])
        self.assertIn("não achei o serviço na Vuupt", r["erro"])
        self.dup.assert_not_called()

    def test_segundo_clique_ja_duplicado(self):
        self.dup.return_value = {"ok": False, "erro": "Esse pedido já foi duplicado antes (→ PS-1-R1)."}
        r = acoes_pedidos.reenviar(101, "motivo ok", "hugo", vuupt=self._vuupt(s101="done"), stokki=self.stokki)
        self.assertFalse(r["ok"])
        self.assertEqual(r["erro"], "Esse pedido já foi duplicado antes (→ PS-1-R1).")


class _BancoTemp(unittest.TestCase):
    """Banco temporario + tratativas mockada."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        p = mock.patch.object(banco, "DB_PATH", Path(self._tmp.name) / "dados.db")
        p.start()
        self.addCleanup(p.stop)
        conn = banco.conectar()
        conn.execute("INSERT INTO nucleo_pedidos (codigo, titulo, status, sender_id, vuupt_service_id) "
                     "VALUES ('#PS-1', '#PS-1', 'NOT_ASSIGNED', 10, 101)")
        conn.commit()
        conn.close()
        t = mock.patch.object(acoes_pedidos.tratativas, "registrar_evento")
        self.trat = t.start()
        self.addCleanup(t.stop)


class TestCancelarVuupt(_BancoTemp):
    def _rota_com_agente(self, agent_id):
        conn = banco.conectar()
        cur = conn.execute("INSERT INTO nucleo_rotas (data_rota, agent_id) VALUES ('2026-11-10', ?)", (agent_id,))
        conn.execute("INSERT INTO nucleo_paradas (rota_id, ordem, service_id) VALUES (?, 1, 101)", (cur.lastrowid,))
        conn.commit()
        conn.close()

    def test_cancela_e_registra(self):
        canc = mock.Mock(return_value={"ok": True, "ja_estava": False, "erro": ""})
        r = acoes_pedidos.cancelar_vuupt(101, "hugo", token="t", cancelar=canc)
        self.assertTrue(r["ok"], r)
        canc.assert_called_once_with("t", 101)
        args, kw = self.trat.call_args
        self.assertEqual(args[:3], ("#PS-1", "PEDIDOS", "CANCELADO_MANUAL"))
        self.assertIn("hugo: ", kw["texto"])
        self.assertEqual(kw["service_id"], 101)

    def test_motorista_a_caminho_repassa_recusa(self):
        canc = mock.Mock(return_value={"ok": False, "ja_estava": False,
                                       "erro": "servico 101 esta 'on_route' na Vuupt: nao cancela sozinho"})
        r = acoes_pedidos.cancelar_vuupt(101, "hugo", token="t", cancelar=canc)
        self.assertFalse(r["ok"])
        self.assertEqual(r["erro"], "motorista a caminho ou no local: fale com ele antes de cancelar")
        self.trat.assert_not_called()

    def test_outro_erro_passa_direto(self):
        canc = mock.Mock(return_value={"ok": False, "ja_estava": False, "erro": "Vuupt: 500"})
        r = acoes_pedidos.cancelar_vuupt(101, "hugo", token="t", cancelar=canc)
        self.assertFalse(r["ok"])
        self.assertEqual(r["erro"], "Vuupt: 500")

    def test_ja_cancelado_nao_registra(self):
        canc = mock.Mock(return_value={"ok": True, "ja_estava": True, "erro": ""})
        r = acoes_pedidos.cancelar_vuupt(101, "hugo", token="t", cancelar=canc)
        self.assertTrue(r["ok"])
        self.assertTrue(r["ja_estava"])
        self.trat.assert_not_called()

    def test_lalamove_avisa(self):
        self._rota_com_agente(555)
        canc = mock.Mock(return_value={"ok": True, "ja_estava": False, "erro": ""})
        r = acoes_pedidos.cancelar_vuupt(101, "hugo", token="t", agent_lalamove=555, cancelar=canc)
        self.assertTrue(r["ok"])
        self.assertIn("Lalamove", r["aviso"])

    def test_sem_lalamove_sem_aviso(self):
        self._rota_com_agente(777)
        canc = mock.Mock(return_value={"ok": True, "ja_estava": False, "erro": ""})
        r = acoes_pedidos.cancelar_vuupt(101, "hugo", token="t", agent_lalamove=555, cancelar=canc)
        self.assertEqual(r["aviso"], "")


if __name__ == "__main__":
    unittest.main()
