# -*- coding: utf-8 -*-
"""
Acoes da tela /pedidos (Hugo, 10/11): Reenvio.

    py -3.11 -m unittest test_acoes_pedidos   (de dentro de painel_agentes/)
"""
import contextlib
import sys
import tempfile
import threading
import time
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from nucleo import banco  # noqa: E402
import acoes_pedidos  # noqa: E402

import importlib.util  # noqa: E402
_spec = importlib.util.spec_from_file_location("painel_agentes_app", _AQUI / "painel_agentes.py")
painel_agentes = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = painel_agentes  # o Flask acha a pasta templates/ pelo modulo registrado
_spec.loader.exec_module(painel_agentes)


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


FALHA_FIXA = "Falha inesperada; o erro ficou no log do painel."


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

    def test_r1_ausente_nao_para_a_busca_e_r2_vivo_recusa(self):
        v = self._vuupt(s101="done")
        v.por_code["PS-1-R2"] = {"id": 777, "code": "#PS-1-R2", "status": "on_route"}
        r = acoes_pedidos.reenviar(101, "motivo ok", "hugo", vuupt=v, stokki=self.stokki)
        self.assertFalse(r["ok"])
        self.assertIn("PS-1-R2", r["erro"])
        self.dup.assert_not_called()

    def test_reenvios_simultaneos_criam_um_so(self):
        feitos, criados = set(), []

        def duplicar_lento(service_id, codigo, **kw):
            # como o real: confere o fingerprint, cria, marca -- sem trava propria
            if service_id in feitos:
                return {"ok": False, "erro": "Esse pedido já foi duplicado antes (→ PS-1-R1)."}
            time.sleep(0.3)
            criados.append(service_id)
            feitos.add(service_id)
            return {"ok": True, "novo_code": "#PS-1-R1"}
        self.dup.side_effect = duplicar_lento
        v = self._vuupt(s101="done")
        resultados = []

        def clicar():
            resultados.append(acoes_pedidos.reenviar(101, "motivo ok", "hugo", vuupt=v, stokki=self.stokki))
        threads = [threading.Thread(target=clicar) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(criados, [101])
        self.assertEqual(sorted(r["ok"] for r in resultados), [False, True])

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
    @staticmethod
    def _v(status="not_assigned"):
        return VuuptFalso({101: {"id": 101, "code": "#PS-1", "status": status}})

    def _rota_com_agente(self, agent_id):
        conn = banco.conectar()
        cur = conn.execute("INSERT INTO nucleo_rotas (data_rota, agent_id) VALUES ('2026-11-10', ?)", (agent_id,))
        conn.execute("INSERT INTO nucleo_paradas (rota_id, ordem, service_id) VALUES (?, 1, 101)", (cur.lastrowid,))
        conn.commit()
        conn.close()

    def test_cancela_e_registra(self):
        canc = mock.Mock(return_value={"ok": True, "ja_estava": False, "erro": ""})
        r = acoes_pedidos.cancelar_vuupt(101, "hugo", token="t", cancelar=canc, vuupt=self._v())
        self.assertTrue(r["ok"], r)
        canc.assert_called_once()
        args, kw = self.trat.call_args
        self.assertEqual(args[:3], ("#PS-1", "PEDIDOS", "CANCELADO_MANUAL"))
        self.assertIn("hugo: ", kw["texto"])
        self.assertEqual(kw["service_id"], 101)

    def test_servico_nao_lido_nao_cancela(self):
        canc = mock.Mock()
        r = acoes_pedidos.cancelar_vuupt(101, "hugo", token="t", cancelar=canc, vuupt=VuuptFalso({}))
        self.assertFalse(r["ok"])
        self.assertIn("não achei o serviço na Vuupt agora", r["erro"])
        canc.assert_not_called()

    def test_on_route_ao_vivo_recusa_sem_cancelar(self):
        for st in ("on_route", "arrived"):
            canc = mock.Mock()
            r = acoes_pedidos.cancelar_vuupt(101, "hugo", token="t", cancelar=canc, vuupt=self._v(st))
            self.assertEqual(r["erro"], "motorista a caminho ou no local: fale com ele antes de cancelar")
            canc.assert_not_called()

    def test_done_recusa_em_portugues(self):
        canc = mock.Mock()
        r = acoes_pedidos.cancelar_vuupt(101, "hugo", token="t", cancelar=canc, vuupt=self._v("done"))
        self.assertEqual(r["erro"], "pedido já finalizado na Vuupt: não dá pra cancelar")
        canc.assert_not_called()

    def test_repassa_o_mesmo_cliente_vuupt(self):
        canc = mock.Mock(return_value={"ok": True, "ja_estava": False, "erro": ""})
        v = self._v()
        acoes_pedidos.cancelar_vuupt(101, "hugo", token="t", cancelar=canc, vuupt=v)
        canc.assert_called_once_with("t", 101, vuupt=v)

    def test_motorista_a_caminho_repassa_recusa(self):
        canc = mock.Mock(return_value={"ok": False, "ja_estava": False,
                                       "erro": "servico 101 esta 'on_route' na Vuupt: nao cancela sozinho"})
        r = acoes_pedidos.cancelar_vuupt(101, "hugo", token="t", cancelar=canc, vuupt=self._v())
        self.assertFalse(r["ok"])
        self.assertEqual(r["erro"], "motorista a caminho ou no local: fale com ele antes de cancelar")
        self.trat.assert_not_called()

    def test_outro_erro_passa_direto(self):
        canc = mock.Mock(return_value={"ok": False, "ja_estava": False, "erro": "Vuupt: 500"})
        r = acoes_pedidos.cancelar_vuupt(101, "hugo", token="t", cancelar=canc, vuupt=self._v())
        self.assertFalse(r["ok"])
        self.assertEqual(r["erro"], "Vuupt: 500")

    def test_ja_cancelado_nao_registra(self):
        canc = mock.Mock(return_value={"ok": True, "ja_estava": True, "erro": ""})
        r = acoes_pedidos.cancelar_vuupt(101, "hugo", token="t", cancelar=canc, vuupt=self._v())
        self.assertTrue(r["ok"])
        self.assertTrue(r["ja_estava"])
        self.trat.assert_not_called()

    def test_lalamove_avisa(self):
        self._rota_com_agente(555)
        canc = mock.Mock(return_value={"ok": True, "ja_estava": False, "erro": ""})
        r = acoes_pedidos.cancelar_vuupt(101, "hugo", token="t", agent_lalamove=555, cancelar=canc, vuupt=self._v())
        self.assertTrue(r["ok"])
        self.assertIn("Lalamove", r["aviso"])

    def test_sem_lalamove_sem_aviso(self):
        self._rota_com_agente(777)
        canc = mock.Mock(return_value={"ok": True, "ja_estava": False, "erro": ""})
        r = acoes_pedidos.cancelar_vuupt(101, "hugo", token="t", agent_lalamove=555, cancelar=canc, vuupt=self._v())
        self.assertEqual(r["aviso"], "")


class TestCancelarStokki(_BancoTemp):
    def setUp(self):
        super().setUp()
        self.sessao = object()
        self.aberturas = 0

        @contextlib.contextmanager
        def abrir():
            self.aberturas += 1
            yield self.sessao
        self.abrir = abrir
        c = mock.patch("stokki.cancelar.cancelar_pedido",
                       side_effect=lambda s, i, m, prefixo=None: {"ok": True, "situacao": "Cancelado",
                                                                  "situacao_antes": "Aguardando Transportador",
                                                                  "ja_estava": False, "erro": ""})
        self.canc = c.start()
        self.addCleanup(c.stop)

    def test_cancela_todos_os_ps_do_codigo(self):
        r = acoes_pedidos.cancelar_stokki("PS-1, PS-2", "cliente desistiu do pedido", "hugo", abrir_sessao=self.abrir)
        self.assertTrue(r["ok"], r)
        self.assertEqual([c.args[1] for c in self.canc.call_args_list], [1, 2])
        for c in self.canc.call_args_list:
            self.assertEqual(c.kwargs["prefixo"], "Fresh Hub Pedidos (hugo)")
        self.assertEqual([x["id_stokki"] for x in r["resultados"]], [1, 2])

    def test_reentrega_cancela_o_base(self):
        acoes_pedidos.cancelar_stokki("PS-7-R1", "cliente desistiu do pedido", "hugo", abrir_sessao=self.abrir)
        self.assertEqual([c.args[1] for c in self.canc.call_args_list], [7])

    def test_motivo_menor_que_15(self):
        r = acoes_pedidos.cancelar_stokki("PS-1", "curto demais", "hugo", abrir_sessao=self.abrir)
        self.assertFalse(r["ok"])
        self.assertIn("15", r["erro"])
        self.assertEqual(self.aberturas, 0)

    def test_codigo_sem_ps(self):
        r = acoes_pedidos.cancelar_stokki("ABC", "cliente desistiu do pedido", "hugo", abrir_sessao=self.abrir)
        self.assertFalse(r["ok"])
        self.assertEqual(self.aberturas, 0)

    def test_stokki_ocupada(self):
        def ocupada():
            raise RuntimeError("Stokki ocupada por outra-rotina")
        r = acoes_pedidos.cancelar_stokki("PS-1", "cliente desistiu do pedido", "hugo", abrir_sessao=ocupada)
        self.assertFalse(r["ok"])
        self.assertIn("Stokki ocupada", r["erro"])
        self.assertIn("tente em alguns minutos", r["erro"])
        self.canc.assert_not_called()

    def test_registra_tratativa_com_situacao(self):
        acoes_pedidos.cancelar_stokki("PS-1", "cliente desistiu do pedido", "hugo", abrir_sessao=self.abrir)
        args, kw = self.trat.call_args
        self.assertEqual(args[1:3], ("PEDIDOS", "CANCELADO_STOKKI"))
        for trecho in ("hugo: ", "cliente desistiu do pedido", "Aguardando Transportador → Cancelado"):
            self.assertIn(trecho, kw["texto"])

    def test_ja_estava_cancelado_registra_com_nota(self):
        # repetir depois de um poll perdido: a tratativa guarda o autor mesmo assim
        self.canc.side_effect = lambda s, i, m, prefixo=None: {"ok": True, "situacao": "Cancelado",
                                                               "situacao_antes": "Cancelado", "ja_estava": True,
                                                               "erro": ""}
        r = acoes_pedidos.cancelar_stokki("PS-1", "cliente desistiu do pedido", "hugo", abrir_sessao=self.abrir)
        self.assertTrue(r["ok"], r)
        args, kw = self.trat.call_args
        self.assertEqual(args[1:3], ("PEDIDOS", "CANCELADO_STOKKI"))
        for trecho in ("hugo: ", "já estava cancelado", "cliente desistiu do pedido"):
            self.assertIn(trecho, kw["texto"])

    def test_mais_de_5_ps_recusa_sem_abrir_a_stokki(self):
        r = acoes_pedidos.cancelar_stokki("PS-1 PS-2 PS-3 PS-4 PS-5 PS-6", "cliente desistiu do pedido", "hugo",
                                          abrir_sessao=self.abrir)
        self.assertFalse(r["ok"])
        self.assertIn("no máximo 5 pedidos por vez", r["erro"])
        self.assertEqual(self.aberturas, 0)
        r = acoes_pedidos.cancelar_stokki("PS-1 PS-2 PS-3 PS-4 PS-5", "cliente desistiu do pedido", "hugo",
                                          abrir_sessao=self.abrir)
        self.assertTrue(r["ok"], r)

    def test_falha_de_um_nao_para_os_outros_e_nao_registra_o_que_falhou(self):
        def lado(s, i, m, prefixo=None):
            if i == 1:
                return {"ok": False, "situacao": "Enviado", "ja_estava": False, "erro": "pedido ja expedido"}
            return {"ok": True, "situacao": "Cancelado", "ja_estava": False, "erro": ""}
        self.canc.side_effect = lado
        r = acoes_pedidos.cancelar_stokki("PS-1, PS-2", "cliente desistiu do pedido", "hugo", abrir_sessao=self.abrir)
        self.assertFalse(r["ok"])
        self.assertEqual([x["ok"] for x in r["resultados"]], [False, True])
        self.assertEqual(self.trat.call_count, 1)


DEPOIS = (date.today() + timedelta(days=2)).isoformat()   # agendar recusa data no passado


class TestEnderecoEAgendar(_BancoTemp):
    def setUp(self):
        super().setUp()
        for alvo, ret in (("planejamento_rotas.editar_endereco_pedido", {"ok": True, "erro": ""}),
                          ("planejamento_rotas.reagendar_pedido", {"ok": True, "erro": ""}),
                          ("roteirizacao.cancelar_servico.rascunho_do_servico", (55, "RASCUNHO"))):
            p = mock.patch(alvo, return_value=ret)
            setattr(self, alvo.rsplit(".", 1)[1], p.start())
            self.addCleanup(p.stop)

    @staticmethod
    def _vuupt(status, **extra):
        return VuuptFalso({101: dict(id=101, code="#PS-1", status=status, **extra)})

    def test_endereco_passa_rascunho_e_registra(self):
        r = acoes_pedidos.alterar_endereco(101, "Rua X, 10", "hugo", vuupt=self._vuupt("not_assigned"))
        self.assertTrue(r["ok"], r)
        self.editar_endereco_pedido.assert_called_once_with(101, "Rua X, 10", 55)
        args, kw = self.trat.call_args
        self.assertEqual(args[:3], ("#PS-1", "PEDIDOS", "ENDERECO_ALTERADO"))
        self.assertIn("hugo: ", kw["texto"])
        self.assertIn("Rua X, 10", kw["texto"])

    def test_endereco_recusa_entregue(self):
        r = acoes_pedidos.alterar_endereco(101, "Rua X, 10", "hugo", vuupt=self._vuupt("done"))
        self.assertFalse(r["ok"])
        self.editar_endereco_pedido.assert_not_called()

    def test_endereco_recusa_insucesso(self):
        r = acoes_pedidos.alterar_endereco(101, "Rua X, 10", "hugo",
                                           vuupt=self._vuupt("done", status_done="failed"))
        self.assertFalse(r["ok"])
        self.editar_endereco_pedido.assert_not_called()

    def test_endereco_vazio(self):
        r = acoes_pedidos.alterar_endereco(101, "  ", "hugo", vuupt=self._vuupt("not_assigned"))
        self.assertFalse(r["ok"])
        self.editar_endereco_pedido.assert_not_called()

    def test_endereco_erro_da_funcao_nao_registra(self):
        self.editar_endereco_pedido.return_value = {"ok": False, "erro": "Vuupt 500"}
        r = acoes_pedidos.alterar_endereco(101, "Rua X, 10", "hugo", vuupt=self._vuupt("not_assigned"))
        self.assertEqual((r["ok"], r["erro"]), (False, "Vuupt 500"))
        self.trat.assert_not_called()

    def test_agendar_registra_janela(self):
        r = acoes_pedidos.agendar(101, DEPOIS, "08:00", "12:00", "hugo", vuupt=self._vuupt("not_assigned"))
        self.assertTrue(r["ok"], r)
        self.reagendar_pedido.assert_called_once_with(101, DEPOIS, "08:00", "12:00")
        args, kw = self.trat.call_args
        self.assertEqual(args[:3], ("#PS-1", "PEDIDOS", "REAGENDADO"))
        self.assertEqual(kw["texto"], f"hugo: agendado {DEPOIS[8:10]}/{DEPOIS[5:7]} 08:00–12:00")

    def test_agendar_recusa_data_no_passado_e_aceita_hoje(self):
        ontem = (date.today() - timedelta(days=1)).isoformat()
        r = acoes_pedidos.agendar(101, ontem, "08:00", "12:00", "hugo", vuupt=self._vuupt("not_assigned"))
        self.assertFalse(r["ok"])
        self.assertIn("data no passado", r["erro"])
        self.reagendar_pedido.assert_not_called()
        r = acoes_pedidos.agendar(101, date.today().isoformat(), "08:00", "12:00", "hugo",
                                  vuupt=self._vuupt("not_assigned"))
        self.assertTrue(r["ok"], r)

    def test_agendar_recusa_cancelado(self):
        r = acoes_pedidos.agendar(101, DEPOIS, "08:00", "12:00", "hugo", vuupt=self._vuupt("canceled"))
        self.assertFalse(r["ok"])
        self.reagendar_pedido.assert_not_called()

    def test_agendar_recusa_excluido(self):
        r = acoes_pedidos.agendar(101, DEPOIS, "08:00", "12:00", "hugo",
                                  vuupt=self._vuupt("not_assigned", deleted_at="2026-11-01"))
        self.assertFalse(r["ok"])
        self.reagendar_pedido.assert_not_called()

    def test_agendar_data_invalida(self):
        r = acoes_pedidos.agendar(101, "12/11", "08:00", "12:00", "hugo", vuupt=self._vuupt("not_assigned"))
        self.assertFalse(r["ok"])
        self.reagendar_pedido.assert_not_called()

    def test_servico_sumiu_nao_altera(self):
        v = VuuptFalso({})
        for r in (acoes_pedidos.alterar_endereco(101, "Rua X, 10", "hugo", vuupt=v),
                  acoes_pedidos.agendar(101, DEPOIS, "08:00", "12:00", "hugo", vuupt=v)):
            self.assertFalse(r["ok"])
            self.assertIn("não achei o serviço na Vuupt agora", r["erro"])
        self.editar_endereco_pedido.assert_not_called()
        self.reagendar_pedido.assert_not_called()


class TestComprovantes(_BancoTemp):
    def _preparar_app_e_manual(self, codigo="PS-7"):
        from nucleo import canhotos_manuais
        conn = banco.conectar()
        canhotos_manuais.salvar(conn, codigo, 70, b"%PDF-1.4 teste", "teste")
        conn.execute("INSERT INTO nucleo_rotas (id, data_rota) VALUES (1, '2026-11-10')")
        conn.execute("INSERT INTO nucleo_paradas (id, rota_id, ordem, service_id, codigo) VALUES (5, 1, 1, 70, ?)",
                     (f"#{codigo}",))
        conn.execute("INSERT INTO nucleo_comprovantes (id, parada_id, tipo, caminho_local) VALUES (9, 5, 'CANHOTO', 'x.jpg')")
        conn.commit()
        conn.close()

    def test_comprovantes_junta_as_tres_origens(self):
        self._preparar_app_e_manual()
        servico = {"id": 70, "status": "done",
                   "checklistAnswers": {"data": [{"id": 555, "images_quantity": 1}]}}
        conn = banco.conectar()
        with mock.patch("notificacao_entregas.vuupt_entregas.buscar_por_codigo", return_value=[servico]) as m, \
                painel_agentes.app.test_request_context():
            itens = acoes_pedidos.comprovantes("PS-7", token="tok", conn=conn)
        conn.close()
        m.assert_called_once_with("tok", "PS-7")
        self.assertEqual([i["origem"] for i in itens], ["VUUPT", "MANUAL", "APP"])
        self.assertTrue(itens[0]["url"].endswith("/pedidos/canhoto/vuupt/555.pdf?codigo=PS-7"))
        self.assertTrue(itens[1]["url"].endswith("/pedidos/canhoto/manual/PS-7.pdf"))
        self.assertTrue(itens[2]["url"].endswith("/canhotos/9/foto"))

    def test_foto_de_insucesso_tem_rotulo_proprio(self):
        servicos = [{"id": 70, "status": "done", "status_done": "failed",
                     "checklistAnswers": {"data": [{"id": 554, "images_quantity": 1}]}},
                    {"id": 71, "status": "done", "status_done": "success",
                     "checklistAnswers": {"data": [{"id": 555, "images_quantity": 1}]}}]
        conn = banco.conectar()
        with mock.patch("notificacao_entregas.vuupt_entregas.buscar_por_codigo", return_value=servicos), \
                painel_agentes.app.test_request_context():
            itens = acoes_pedidos.comprovantes("PS-7", token="tok", conn=conn)
        conn.close()
        self.assertEqual([i["rotulo"] for i in itens], ["Foto do insucesso (Vuupt)", "Canhoto da Vuupt (PDF)"])

    def test_sem_canhoto_lista_vazia(self):
        conn = banco.conectar()
        with mock.patch("notificacao_entregas.vuupt_entregas.buscar_por_codigo", return_value=[]), \
                painel_agentes.app.test_request_context():
            self.assertEqual(acoes_pedidos.comprovantes("PS-8", token="tok", conn=conn), [])
        conn.close()

    def test_vuupt_fora_do_ar_nao_derruba_as_outras_origens(self):
        self._preparar_app_e_manual()
        conn = banco.conectar()
        with mock.patch("notificacao_entregas.vuupt_entregas.buscar_por_codigo", side_effect=RuntimeError("x")), \
                painel_agentes.app.test_request_context():
            itens = acoes_pedidos.comprovantes("PS-7", token="tok", conn=conn)
        conn.close()
        self.assertEqual([i["origem"] for i in itens], ["MANUAL", "APP"])


class TestRotasCanhoto(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        db = Path(self._tmp.name) / "dados.db"
        for p in (mock.patch.object(banco, "DB_PATH", db),
                  mock.patch.object(painel_agentes.acesso_telas, "DB_PATH", db),
                  mock.patch.object(painel_agentes.contadores_menu, "contadores", return_value={})):
            p.start()
            self.addCleanup(p.stop)
        painel_agentes.app.config["TESTING"] = True
        self.cliente = painel_agentes.app.test_client()
        self.origem = {"Origin": "http://localhost"}

    def _logar(self, nivel):
        with self.cliente.session_transaction() as sess:
            sess["nivel_acesso"] = nivel
            sess["usuario"] = "teste"

    def test_canhoto_manual_rejeita_codigo_estranho(self):
        self._logar("leitura")
        for url in ("/pedidos/canhoto/manual/..%2Fconfig.pdf", "/pedidos/canhoto/manual/ps-1.pdf",
                    "/pedidos/canhoto/manual/PS-1-X1.pdf"):
            self.assertEqual(self.cliente.get(url).status_code, 404, url)

    def test_canhoto_manual_recusa_quebra_de_linha_no_fim(self):
        # re.match com $ aceitava "PS-1\n"; fullmatch nao
        self._logar("leitura")
        with mock.patch("nucleo.canhotos_manuais.caminho_canhoto_manual") as m:
            self.assertEqual(self.cliente.get("/pedidos/canhoto/manual/PS-1%0A.pdf").status_code, 404)
        m.assert_not_called()

    def test_canhoto_manual_serve_o_pdf(self):
        from nucleo import canhotos_manuais
        conn = banco.conectar()
        canhotos_manuais.salvar(conn, "PS-9", 90, b"%PDF-1.4 abc", "teste")
        conn.close()
        self._logar("leitura")
        r = self.cliente.get("/pedidos/canhoto/manual/PS-9.pdf")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_data().startswith(b"%PDF"))
        self.assertEqual(self.cliente.get("/pedidos/canhoto/manual/PS-10.pdf").status_code, 404)

    def test_canhoto_vuupt_baixa_com_cache_por_checklist(self):
        arq = Path(self._tmp.name) / "c.pdf"
        arq.write_bytes(b"%PDF-1.4 v")
        servico = {"id": 70, "status": "done", "checklistAnswers": {"data": [{"id": 555, "images_quantity": 1}]}}
        self._logar("leitura")
        with mock.patch("notificacao_entregas.vuupt_entregas.buscar_por_codigo", return_value=[servico]),                 mock.patch("notificacao_entregas.vuupt_entregas.baixar_canhoto_pdf", return_value=arq) as m:
            r = self.cliente.get("/pedidos/canhoto/vuupt/555.pdf?codigo=PS-7")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(m.call_args.args[1:], (555, "PS-7_555"))
        with mock.patch("notificacao_entregas.vuupt_entregas.buscar_por_codigo", return_value=[servico]),                 mock.patch("notificacao_entregas.vuupt_entregas.baixar_canhoto_pdf", return_value=None):
            self.assertEqual(self.cliente.get("/pedidos/canhoto/vuupt/555.pdf?codigo=PS-7").status_code, 404)

    def test_canhoto_vuupt_checklist_de_outro_pedido_da_404_sem_baixar(self):
        servico = {"id": 70, "status": "done", "checklistAnswers": {"data": [{"id": 555, "images_quantity": 1}]}}
        self._logar("leitura")
        with mock.patch("notificacao_entregas.vuupt_entregas.buscar_por_codigo", return_value=[servico]),                 mock.patch("notificacao_entregas.vuupt_entregas.baixar_canhoto_pdf") as m:
            self.assertEqual(self.cliente.get("/pedidos/canhoto/vuupt/999.pdf?codigo=PS-7").status_code, 404)
            self.assertEqual(self.cliente.get("/pedidos/canhoto/vuupt/555.pdf?codigo=..%2Fx").status_code, 404)
            self.assertEqual(self.cliente.get("/pedidos/canhoto/vuupt/555.pdf").status_code, 404)
        m.assert_not_called()

    def test_rota_comprovantes_json(self):
        self._logar("leitura")
        with mock.patch("notificacao_entregas.vuupt_entregas.buscar_por_codigo", return_value=[]):
            r = self.cliente.get("/api/pedidos/acao/comprovantes?codigo=PS-1")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json(), {"ok": True, "comprovantes": []})
        self.assertEqual(self.cliente.get("/api/pedidos/acao/comprovantes?codigo=").status_code, 400)

    def test_marcar_entregue_pela_tela_chama_a_torre(self):
        self._logar("atendimento")
        with mock.patch.object(painel_agentes.torre_controle, "marcar_entregue_manual",
                               return_value={"ok": True}) as m:
            r = self.cliente.post("/api/pedidos/acao/marcar-entregue", data={"service_id": "70"},
                                  headers=self.origem)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(m.call_args.args[:2], (70, "teste"))

    def test_sem_sucesso_pela_tela_pedidos_e_recusado(self):
        self._logar("atendimento")
        corpo = {"service_id": "70", "resultado": "sem_sucesso", "failed_reason_id": "1"}
        with mock.patch.object(painel_agentes.torre_controle, "marcar_sem_sucesso_manual",
                               return_value={"ok": True, "codigo": "#PS-70"}) as fn:
            r = self.cliente.post("/api/pedidos/acao/marcar-entregue", data=corpo, headers=self.origem)
            self.assertEqual(r.status_code, 400)
            self.assertEqual(r.get_json(), {"erro": "Sem sucesso não é feito pela tela Pedidos."})
            fn.assert_not_called()
            # a Torre continua fazendo
            self._logar("operador")
            r = self.cliente.post("/api/torre/marcar-entregue", data=corpo, headers=self.origem)
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
            fn.assert_called_once()

    def test_marcar_entregue_sem_service_id_json_400(self):
        self._logar("atendimento")
        r = self.cliente.post("/api/pedidos/acao/marcar-entregue", data={}, headers=self.origem)
        self.assertEqual(r.status_code, 400)
        self.assertTrue(r.is_json)

    def test_marcar_entregue_leitura_recusado(self):
        self._logar("leitura")
        r = self.cliente.post("/api/pedidos/acao/marcar-entregue", data={"service_id": "70"},
                              headers=self.origem)
        self.assertIn(r.status_code, (302, 401, 403))

    def test_torre_continua_igual(self):
        with mock.patch.object(painel_agentes.torre_controle, "marcar_entregue_manual",
                               return_value={"ok": True}):
            self._logar("logistica")
            r = self.cliente.post("/api/torre/marcar-entregue", data={"service_id": "70"}, headers=self.origem)
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.get_json(), {"ok": True})
            self._logar("atendimento")
            r = self.cliente.post("/api/torre/marcar-entregue", data={"service_id": "70"}, headers=self.origem)
            self.assertNotEqual(r.status_code, 200)

    def test_falha_inesperada_vira_json_500(self):
        self._logar("atendimento")
        with mock.patch.object(painel_agentes.torre_controle, "marcar_entregue_manual", side_effect=RuntimeError("x")):
            r = self.cliente.post("/api/pedidos/acao/marcar-entregue", data={"service_id": "70"},
                                  headers=self.origem)
        self.assertEqual(r.status_code, 500)
        self.assertEqual(r.get_json(), {"erro": FALHA_FIXA})
        with mock.patch.object(painel_agentes.acoes_pedidos, "comprovantes", side_effect=RuntimeError("x")):
            r = self.cliente.get("/api/pedidos/acao/comprovantes?codigo=PS-1")
        self.assertEqual(r.status_code, 500)
        self.assertEqual(r.get_json(), {"erro": FALHA_FIXA})
        with mock.patch.object(painel_agentes.torre_controle, "validar_lote_foto", side_effect=RuntimeError("x")):
            r = self.cliente.post("/api/pedidos/acao/validar-lote-foto", json={"service_ids": [1]},
                                  headers=self.origem)
        self.assertEqual(r.status_code, 500)
        self.assertEqual(r.get_json(), {"erro": FALHA_FIXA})

    def test_validar_lote_foto_pela_tela(self):
        self._logar("atendimento")
        with mock.patch.object(painel_agentes.torre_controle, "validar_lote_foto",
                               return_value={"ok": False, "erro": "mistura", "fora": [2]}) as m:
            r = self.cliente.post("/api/pedidos/acao/validar-lote-foto", json={"service_ids": [1, 2]},
                                  headers=self.origem)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json(), {"erro": "mistura", "fora": [2]})
        m.assert_called_once_with([1, 2])
        with mock.patch.object(painel_agentes.torre_controle, "validar_lote_foto", return_value={"ok": True}):
            r = self.cliente.post("/api/pedidos/acao/validar-lote-foto", json={"service_ids": [1]},
                                  headers=self.origem)
        self.assertEqual(r.get_json(), {"ok": True})
        r = self.cliente.post("/api/pedidos/acao/validar-lote-foto", json={"service_ids": "x"},
                              headers=self.origem)
        self.assertEqual(r.status_code, 400)


class TestRotasCorpoInvalido(unittest.TestCase):
    """Rotas /api/pedidos/acao/*: corpo JSON que nao e objeto vira 400 JSON, nunca HTML 500."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        db = Path(self._tmp.name) / "dados.db"
        for p in (mock.patch.object(banco, "DB_PATH", db),
                  mock.patch.object(painel_agentes.acesso_telas, "DB_PATH", db),
                  mock.patch.object(painel_agentes.contadores_menu, "contadores", return_value={})):
            p.start()
            self.addCleanup(p.stop)
        painel_agentes.app.config["TESTING"] = True
        self.cliente = painel_agentes.app.test_client()
        with self.cliente.session_transaction() as sess:
            sess["nivel_acesso"] = "operador"
            sess["usuario"] = "teste"

    def test_excecao_na_acao_da_500_json(self):
        casos = (
            ("reenvio", mock.patch.object(painel_agentes.acoes_pedidos, "reenviar", side_effect=RuntimeError("x")),
             {"service_id": 1, "motivo": "m"}),
            ("cancelar-vuupt", mock.patch.object(painel_agentes.acoes_pedidos, "cancelar_vuupt",
                                                 side_effect=RuntimeError("x")), {"service_id": 1}),
            ("cancelar-stokki", mock.patch.object(painel_agentes.acoes_pedidos, "cancelar_stokki",
                                                  side_effect=RuntimeError("x")), {"codigo": "PS-1", "motivo": "m"}),
            ("endereco", mock.patch.object(painel_agentes.acoes_pedidos, "alterar_endereco",
                                           side_effect=RuntimeError("x")), {"service_id": 1, "endereco": "e"}),
            ("agendar", mock.patch.object(painel_agentes.acoes_pedidos, "agendar", side_effect=RuntimeError("x")),
             {"service_id": 1, "data": "2026-11-11"}),
            ("checar-dia-fixo", mock.patch.object(painel_agentes, "checar_reagendamento_dia_fixo",
                                                  side_effect=RuntimeError("x")),
             {"service_ids": [1], "data": "2026-11-11"}),
        )
        for rota, patch, corpo in casos:
            with patch:
                r = self.cliente.post(f"/api/pedidos/acao/{rota}", json=corpo, headers={"Origin": "http://localhost"})
            self.assertEqual(r.status_code, 500, rota)
            self.assertTrue(r.is_json, rota)
            self.assertEqual(r.get_json(), {"erro": FALHA_FIXA}, rota)   # sem o texto da excecao

    def test_autor_cai_no_nivel_sem_usuario_na_sessao(self):
        # login por nivel (sem "usuario"): a tratativa nao pode comecar com ": "
        with self.cliente.session_transaction() as sess:
            sess.pop("usuario", None)
        casos = (("reenvio", "reenviar", {"service_id": 1, "motivo": "motivo ok"}, 2),
                 ("cancelar-vuupt", "cancelar_vuupt", {"service_id": 1}, 1),
                 ("cancelar-stokki", "cancelar_stokki", {"codigo": "PS-1", "motivo": "m"}, 2),
                 ("endereco", "alterar_endereco", {"service_id": 1, "endereco": "e"}, 2),
                 ("agendar", "agendar", {"service_id": 1, "data": "2026-12-01"}, 4))
        for rota, funcao, corpo, pos in casos:
            with mock.patch.object(painel_agentes.acoes_pedidos, funcao, return_value={"ok": True}) as m:
                r = self.cliente.post(f"/api/pedidos/acao/{rota}", json=corpo, headers={"Origin": "http://localhost"})
            self.assertEqual(r.status_code, 200, (rota, r.get_data(as_text=True)))
            self.assertEqual(m.call_args.args[pos], "operador", rota)

    def test_corpo_nao_objeto_da_400_json(self):
        for rota in ("cancelar-stokki", "cancelar-vuupt", "reenvio", "endereco", "agendar", "checar-dia-fixo"):
            for corpo in ([1], "texto"):
                r = self.cliente.post(f"/api/pedidos/acao/{rota}", json=corpo,
                                      headers={"Origin": "http://localhost"})
                self.assertEqual(r.status_code, 400, (rota, corpo))
                self.assertTrue(r.is_json, (rota, corpo))
                self.assertIn("erro", r.get_json())


class TestExpedirStokki(unittest.TestCase):
    """Expedir na Stokki (Hugo, 12/11): a tela poe o expedir_pedidos.py na fila e le o resultado depois."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.pasta = Path(self._tmp.name)
        p = mock.patch.object(acoes_pedidos.tratativas, "registrar_evento")
        self.evento = p.start()
        self.addCleanup(p.stop)

    def test_monta_a_execucao_so_com_os_codigos_base_sem_repetir(self):
        chamadas = []

        def iniciar(args):
            chamadas.append(args)
            return 77, True
        r = acoes_pedidos.expedir_stokki(["#PS-43333", "PS-43333-R1", "ps-500, PS-501"], "maria",
                                         iniciar=iniciar, pasta=self.pasta)
        self.assertEqual((r["ok"], r["execucao_id"], r["na_fila"]), (True, 77, True))
        self.assertEqual(r["codigos"], ["PS-43333", "PS-500", "PS-501"])
        args = chamadas[0]
        self.assertEqual(args[:5], ["--pedido", "PS-43333", "PS-500", "PS-501", "--tela-pedidos"])
        self.assertEqual(args[5:], ["--resultado-json", str(self.pasta / f"expedir_tela_{r['token']}.json")])
        self.assertEqual(self.evento.call_count, 3)
        self.assertIn("maria", self.evento.call_args.kwargs["texto"])

    def test_recusa_sem_codigo_ps_e_acima_do_teto_sem_iniciar(self):
        iniciar = mock.Mock()
        self.assertFalse(acoes_pedidos.expedir_stokki([], "m", iniciar=iniciar, pasta=self.pasta)["ok"])
        self.assertFalse(acoes_pedidos.expedir_stokki("PS-1", "m", iniciar=iniciar, pasta=self.pasta)["ok"])
        self.assertFalse(acoes_pedidos.expedir_stokki(["NF 123"], "m", iniciar=iniciar, pasta=self.pasta)["ok"])
        muitos = [f"PS-{n}" for n in range(1000, 1000 + acoes_pedidos._MAX_EXPEDIR + 1)]
        self.assertFalse(acoes_pedidos.expedir_stokki(muitos, "m", iniciar=iniciar, pasta=self.pasta)["ok"])
        iniciar.assert_not_called()

    def test_andamento_na_fila_rodando_e_fim_com_resultado(self):
        token = "a" * 32
        ex = {"id": 9, "agente_id": "somente_expedicao", "status": "NA_FILA"}
        self.assertEqual(acoes_pedidos.andamento_expedicao(ex, token, self.pasta), {"ok": True, "estado": "na_fila"})
        ex["status"] = "RODANDO"
        self.assertEqual(acoes_pedidos.andamento_expedicao(ex, token, self.pasta)["estado"], "rodando")
        ex["status"] = "SUCESSO"
        sem = acoes_pedidos.andamento_expedicao(ex, token, self.pasta)
        self.assertEqual((sem["estado"], sem["resultados"]), ("fim", {}))
        self.assertIn("execução 9", sem["erro"])
        (self.pasta / f"expedir_tela_{token}.json").write_text(
            '{"PS-1": {"ok": true, "mensagem": "Expedido na Stokki"}}', encoding="utf-8")
        fim = acoes_pedidos.andamento_expedicao(ex, token, self.pasta)
        self.assertEqual(fim, {"ok": True, "estado": "fim",
                               "resultados": {"PS-1": {"ok": True, "mensagem": "Expedido na Stokki"}}})

    def test_andamento_recusa_outro_agente_e_token_fora_do_formato(self):
        ok = {"id": 9, "agente_id": "somente_expedicao", "status": "SUCESSO"}
        self.assertFalse(acoes_pedidos.andamento_expedicao(None, "a" * 32, self.pasta)["ok"])
        self.assertFalse(acoes_pedidos.andamento_expedicao({**ok, "agente_id": "pipeline"}, "a" * 32, self.pasta)["ok"])
        self.assertFalse(acoes_pedidos.andamento_expedicao(ok, "../../config", self.pasta)["ok"])

    def test_rotas_iniciam_na_fila_do_planejamento_e_devolvem_o_andamento(self):
        painel_agentes.app.config["TESTING"] = True
        cliente = painel_agentes.app.test_client()
        origem = {"Origin": "http://localhost"}
        with mock.patch.object(painel_agentes, "_carregar_config",
                               return_value={"painel_agentes": {"usuario": "u", "senha": "s"}}), \
                mock.patch.object(painel_agentes, "_LOGS_EXECUCOES", self.pasta), \
                mock.patch.object(painel_agentes, "iniciar_execucao_com_fila", return_value=(31, False)) as ini, \
                mock.patch.object(painel_agentes, "buscar_execucao",
                                  return_value={"id": 31, "agente_id": "somente_expedicao", "status": "RODANDO"}):
            with cliente.session_transaction() as sess:
                sess["nivel_acesso"] = "atendimento"
                sess["usuario"] = "maria"
            r = cliente.post("/api/pedidos/acao/expedir-stokki", json={"codigos": ["#PS-43333"]}, headers=origem)
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
            d = r.get_json()
            self.assertEqual((d["execucao_id"], d["codigos"]), (31, ["PS-43333"]))
            self.assertEqual(ini.call_args.args[0]["id"], "somente_expedicao")
            self.assertFalse(ini.call_args.kwargs["modo_teste"])
            self.assertEqual(ini.call_args.kwargs["ids_do_grupo"], painel_agentes.AGENTES_PLANEJAMENTO_IDS)
            a = cliente.get(f"/api/pedidos/acao/expedir-stokki/andamento?execucao_id=31&token={d['token']}")
            self.assertEqual(a.get_json(), {"ok": True, "estado": "rodando"})
            self.assertEqual(
                cliente.get("/api/pedidos/acao/expedir-stokki/andamento?execucao_id=x&token=y").status_code, 404)
            self.assertEqual(cliente.post("/api/pedidos/acao/expedir-stokki", json={"codigos": []},
                                          headers=origem).status_code, 400)


if __name__ == "__main__":
    unittest.main()
