# -*- coding: utf-8 -*-
"""
test_carregamento.py

Checklist de carregamento (nucleo/carregamento.py): config, tabelas e
quando a rota exige o checklist.
    py -3.11 -m unittest nucleo.test_carregamento -v
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))

from nucleo import banco, carregamento, operacao, ponte_vuupt


def _cfg(**kw) -> dict:
    return {"api_motorista": {"checklist_carregamento": kw}}


class TestConfig(unittest.TestCase):
    def test_config_desligada_por_padrao(self):
        c = carregamento.config_carregamento({})
        self.assertFalse(c["ativo"])
        self.assertEqual(c["pilotos"], set())

    def test_config_pilotos(self):
        c = carregamento.config_carregamento(_cfg(ativo=True, pilotos=["10", 20]))
        self.assertTrue(c["ativo"])
        self.assertEqual(c["pilotos"], {10, 20})


class TestBanco(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._patch = mock.patch.object(banco, "DB_PATH", Path(self._tmp.name) / "t.db")
        self._patch.start()
        self.conn = banco.conectar()

    def tearDown(self):
        self.conn.close()
        self._patch.stop()
        self._tmp.cleanup()

    def _rota(self, agent_id=10, status="ACEITA", provedor=None):
        cur = self.conn.execute(
            "INSERT INTO nucleo_rotas (data_rota, agent_id, status, provedor) VALUES ('2026-11-23', ?, ?, ?)",
            (agent_id, status, provedor or banco.PROVEDOR_APP))
        self.conn.commit()
        return self.conn.execute("SELECT * FROM nucleo_rotas WHERE id=?", (cur.lastrowid,)).fetchone()

    def test_tabelas_existem(self):
        nomes = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("nucleo_carregamentos", nomes)
        self.assertIn("nucleo_carregamento_itens", nomes)

    def test_exigido_so_com_chave_ligada_e_rota_aceita(self):
        ligada = carregamento.config_carregamento(_cfg(ativo=True, pilotos=[10]))
        desligada = carregamento.config_carregamento(_cfg(ativo=False))
        self.assertTrue(carregamento.exigido(self._rota(), ligada))
        self.assertTrue(carregamento.exigido(self._rota(status="PLANEJADA"), ligada))
        self.assertFalse(carregamento.exigido(self._rota(), desligada))
        self.assertFalse(carregamento.exigido(self._rota(status="EM_ROTA"), ligada))
        self.assertFalse(carregamento.exigido(self._rota(agent_id=99), ligada))
        sem_pilotos = carregamento.config_carregamento(_cfg(ativo=True))
        self.assertTrue(carregamento.exigido(self._rota(agent_id=99), sem_pilotos))

    def test_carregamento_da_rota(self):
        rota = self._rota()
        self.assertIsNone(carregamento.carregamento_da_rota(self.conn, rota["id"]))
        self.conn.execute("INSERT INTO nucleo_carregamentos (rota_id, agent_id, uuid) VALUES (?, 10, 'u1')", (rota["id"],))
        self.conn.commit()
        self.assertEqual(carregamento.carregamento_da_rota(self.conn, rota["id"])["uuid"], "u1")


class _TirarFalso:
    """Substitui a ida a Vuupt: guarda as chamadas; `falhas` = {parada_id: erro}."""

    def __init__(self, falhas=None, cancelar_na_ultima=False, total=None):
        self.chamadas = []
        self.falhas = dict(falhas or {})
        self.cancelar_na_ultima = cancelar_na_ultima
        self.total = total

    def __call__(self, rota, parada, motivo, observacao):
        self.chamadas.append(parada["id"])
        if parada["id"] in self.falhas:
            return {"ok": False, "erro": self.falhas[parada["id"]]}
        cancelada = bool(self.cancelar_na_ultima and len(self.chamadas) == self.total)
        return {"ok": True, "rota_cancelada": cancelada, "rota_iniciada": False}


_FOTO = {"caminho": "dados/carregamento/1/f.jpg", "sha256": "abc", "resultado": "APROVADO",
         "ocupacao_pct": 70, "revisao_humana": False}
_CFG_LIGADA = {"api_motorista": {"checklist_carregamento": {"ativo": True}}, "vuupt_api": {"token": "tok-teste"}}


class TestConfirmar(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._patches = [mock.patch.object(banco, "DB_PATH", Path(self._tmp.name) / "t.db"),
                         mock.patch.object(ponte_vuupt, "_config", _CFG_LIGADA)]
        for p in self._patches:
            p.start()
        self.conn = banco.conectar()
        self.rota = self._rota()

    def tearDown(self):
        self.conn.close()
        for p in reversed(self._patches):
            p.stop()
        self._tmp.cleanup()

    def _rota(self, dados_json=None, provedor=None, vuupt_route_id=None, paradas=3):
        cur = self.conn.execute(
            "INSERT INTO nucleo_rotas (data_rota, nome, agent_id, status, provedor, vuupt_route_id, dados_json, "
            "total_paradas) VALUES (date('now','localtime'), 'Rota 1', 10, 'ACEITA', ?, ?, ?, ?)",
            (provedor or banco.PROVEDOR_APP, vuupt_route_id, dados_json, paradas))
        rota_id = cur.lastrowid
        for i in range(1, paradas + 1):
            sid = rota_id * 100 + i
            self.conn.execute(
                "INSERT INTO nucleo_paradas (rota_id, ordem, service_id, codigo) VALUES (?, ?, ?, ?)",
                (rota_id, i, sid, f"PS-{sid}"))
            self.conn.execute(
                "INSERT OR REPLACE INTO nucleo_pedidos (codigo, vuupt_service_id, status) VALUES (?, ?, 'EM_ROTA')",
                (f"PS-{sid}", sid))
        self.conn.commit()
        return self.conn.execute("SELECT * FROM nucleo_rotas WHERE id=?", (rota_id,)).fetchone()

    def _paradas(self, rota=None):
        return self.conn.execute("SELECT * FROM nucleo_paradas WHERE rota_id=? ORDER BY ordem",
                                 ((rota or self.rota)["id"],)).fetchall()

    def _motorista(self, placa="ABC1D23", tipo="FIORINO"):
        self.conn.execute("INSERT INTO motoristas (cpf, nome, pin_hash, pin_salt, agent_id, placa, tipo_veiculo) "
                          "VALUES ('1', 'Fulano', 'h', 's', 10, ?, ?)", (placa, tipo))
        self.conn.commit()

    def _confirmar(self, desmarcados=(), tirar=None, uuid="u1", carregador="Jonas", rota=None):
        return carregamento.confirmar(self.conn, rota or self.rota, 10, carregador=carregador, uuid=uuid,
                                      desmarcados=list(desmarcados), foto=dict(_FOTO),
                                      tirar=tirar or _TirarFalso())

    def _desm(self, parada, motivo="Não coube", observacao=None):
        return {"parada_id": parada["id"], "motivo": motivo, "observacao": observacao}

    def test_confirma_sem_desmarcados_grava_e_conta(self):
        self._motorista()
        r = self._confirmar()
        self.assertTrue(r["ok"])
        self.assertFalse(r["rota_cancelada"])
        c = carregamento.carregamento_da_rota(self.conn, self.rota["id"])
        self.assertEqual((c["qtd_carregados"], c["qtd_desmarcados"]), (3, 0))
        self.assertEqual((c["placa"], c["tipo_veiculo"], c["carregador"]), ("ABC1D23", "FIORINO", "Jonas"))
        self.assertEqual((c["foto_caminho"], c["ocupacao_pct"], c["revisao_humana"]), (_FOTO["caminho"], 70, 0))
        ev = self.conn.execute("SELECT * FROM nucleo_eventos WHERE tipo='CARREGAMENTO'").fetchone()
        self.assertEqual((ev["rota_id"], ev["agent_id"]), (self.rota["id"], 10))

    def test_desmarcar_tira_parada_e_devolve_pedido_ao_pool(self):
        p = self._paradas()[1]
        tirar = _TirarFalso()
        r = self._confirmar([self._desm(p, "Outros", "caixa molhada")], tirar=tirar)
        self.assertTrue(r["ok"])
        self.assertEqual(tirar.chamadas, [p["id"]])
        self.assertEqual(self.conn.execute("SELECT situacao FROM nucleo_paradas WHERE id=?", (p["id"],)).fetchone()[0],
                         banco.PARADA_CANCELADA)
        self.assertEqual(self.conn.execute("SELECT status FROM nucleo_pedidos WHERE codigo=?",
                                           (p["codigo"],)).fetchone()[0], banco.PEDIDO_ABERTO)
        item = self.conn.execute("SELECT * FROM nucleo_carregamento_itens").fetchone()
        self.assertEqual((item["service_id"], item["codigo"], item["motivo"], item["observacao"]),
                         (p["service_id"], p["codigo"], "Outros", "caixa molhada"))
        self.assertIsNotNone(item["removido_em"])
        self.assertEqual(item["carregamento_id"], r["carregamento"]["id"])
        self.assertEqual((r["carregamento"]["qtd_carregados"], r["carregamento"]["qtd_desmarcados"]), (2, 1))
        self.assertEqual(self.conn.execute("SELECT total_paradas FROM nucleo_rotas WHERE id=?",
                                           (self.rota["id"],)).fetchone()[0], 2)

    def test_desmarcar_todos_cancela_a_rota(self):
        tirar = _TirarFalso(cancelar_na_ultima=True, total=3)
        r = self._confirmar([self._desm(p) for p in self._paradas()], tirar=tirar)
        self.assertTrue(r["ok"])
        self.assertTrue(r["rota_cancelada"])
        rota = self.conn.execute("SELECT * FROM nucleo_rotas WHERE id=?", (self.rota["id"],)).fetchone()
        self.assertEqual(rota["status"], banco.ROTA_CANCELADA)
        self.assertIsNotNone(rota["cancelada_em"])
        self.assertEqual(rota["total_paradas"], 0)
        self.assertEqual(r["carregamento"]["qtd_carregados"], 0)

    def test_falha_parcial_nao_grava_e_repete_so_os_que_faltam(self):
        ps = self._paradas()
        desm = [self._desm(p) for p in ps]
        tirar = _TirarFalso(falhas={ps[1]["id"]: "Vuupt fora do ar"})
        r = self._confirmar(desm, tirar=tirar)
        self.assertFalse(r["ok"])
        self.assertEqual(r["falhas"], [{"parada_id": ps[1]["id"], "codigo": ps[1]["codigo"], "erro": "Vuupt fora do ar"}])
        self.assertEqual(sorted(r["removidos"]), sorted([ps[0]["id"], ps[2]["id"]]))
        self.assertIsNone(carregamento.carregamento_da_rota(self.conn, self.rota["id"]))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM nucleo_carregamento_itens "
                                           "WHERE removido_em IS NOT NULL").fetchone()[0], 2)

        tirar2 = _TirarFalso()
        r2 = self._confirmar(desm, tirar=tirar2)
        self.assertTrue(r2["ok"])
        self.assertEqual(tirar2.chamadas, [ps[1]["id"]])
        self.assertEqual(r2["carregamento"]["qtd_desmarcados"], 3)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM nucleo_carregamento_itens "
                                           "WHERE carregamento_id = ?", (r2["carregamento"]["id"],)).fetchone()[0], 3)

    def test_uuid_repetido_devolve_o_mesmo(self):
        p = self._paradas()[0]
        primeira = self._confirmar([self._desm(p)])
        tirar = _TirarFalso()
        segunda = self._confirmar([self._desm(p)], tirar=tirar)
        self.assertEqual(tirar.chamadas, [])
        self.assertTrue(segunda["ok"])
        self.assertEqual(segunda["carregamento"]["id"], primeira["carregamento"]["id"])

    def test_reenvio_depois_de_iniciada_devolve_o_mesmo(self):
        primeira = self._confirmar()
        self.conn.execute("UPDATE nucleo_rotas SET status='EM_ROTA' WHERE id=?", (self.rota["id"],))
        self.conn.commit()
        rota = self.conn.execute("SELECT * FROM nucleo_rotas WHERE id=?", (self.rota["id"],)).fetchone()
        self.assertEqual(self._confirmar(rota=rota)["carregamento"]["id"], primeira["carregamento"]["id"])

    def test_parada_ja_comecada_erro_claro(self):
        p = self._paradas()[0]
        erro = "Pedido 101 já começou (status na Vuupt: 'on_route') e não pode sair da rota."
        r = self._confirmar([self._desm(p)], tirar=_TirarFalso(falhas={p["id"]: erro}))
        self.assertFalse(r["ok"])
        self.assertEqual(r["falhas"][0]["erro"], erro)
        self.assertEqual(r["removidos"], [])
        self.assertIsNone(carregamento.carregamento_da_rota(self.conn, self.rota["id"]))
        self.assertEqual(self.conn.execute("SELECT situacao FROM nucleo_paradas WHERE id=?", (p["id"],)).fetchone()[0],
                         banco.PARADA_PENDENTE)

    def test_outros_exige_observacao(self):
        p = self._paradas()[0]
        with self.assertRaises(operacao.OperacaoInvalida):
            self._confirmar([self._desm(p, "Outros", "  ")])
        with self.assertRaises(operacao.OperacaoInvalida):
            self._confirmar([self._desm(p, "Sumiu")])

    def test_carregador_vazio(self):
        with self.assertRaises(operacao.OperacaoInvalida):
            self._confirmar(carregador="  ")

    def test_parada_de_outra_rota(self):
        outra = self._rota()
        with self.assertRaises(operacao.OperacaoInvalida):
            self._confirmar([self._desm(self._paradas(outra)[0])])

    def test_rota_em_rota_ou_nao_exigida(self):
        self.conn.execute("UPDATE nucleo_rotas SET status='EM_ROTA' WHERE id=?", (self.rota["id"],))
        self.conn.commit()
        rota = self.conn.execute("SELECT * FROM nucleo_rotas WHERE id=?", (self.rota["id"],)).fetchone()
        with self.assertRaises(operacao.OperacaoInvalida) as ctx:
            self._confirmar(rota=rota)
        self.assertEqual(ctx.exception.codigo, 409)
        with mock.patch.object(ponte_vuupt, "_config", {}):
            with self.assertRaises(operacao.OperacaoInvalida):
                self._confirmar(rota=self._rota())

    def test_rota_replica_nao_mexe_no_pedido_real(self):
        rota = self._rota(dados_json='{"replica_de": 999}')
        p = self._paradas(rota)[0]
        with mock.patch.object(carregamento.nucleo_pedidos, "marcar_aberto_por_service_ids") as marcar:
            r = self._confirmar([self._desm(p)], rota=rota)
        self.assertTrue(r["ok"])
        marcar.assert_not_called()
        self.assertEqual(self.conn.execute("SELECT situacao FROM nucleo_paradas WHERE id=?", (p["id"],)).fetchone()[0],
                         banco.PARADA_CANCELADA)
        self.assertEqual(self.conn.execute("SELECT status FROM nucleo_pedidos WHERE codigo=?",
                                           (p["codigo"],)).fetchone()[0], banco.PEDIDO_EM_ROTA)

    def test_veiculo_sem_placa_grava_null(self):
        self._motorista(placa=None, tipo=None)
        c = self._confirmar()["carregamento"]
        self.assertIsNone(c["placa"])
        self.assertIsNone(c["tipo_veiculo"])


class TestTirarDaVuuptOuLocal(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._patches = [mock.patch.object(banco, "DB_PATH", Path(self._tmp.name) / "t.db"),
                         mock.patch.object(ponte_vuupt, "_config", _CFG_LIGADA)]
        for p in self._patches:
            p.start()
        self.conn = banco.conectar()

    def tearDown(self):
        self.conn.close()
        for p in reversed(self._patches):
            p.stop()
        self._tmp.cleanup()

    def _rota_parada(self, provedor, situacao="PENDENTE"):
        cur = self.conn.execute("INSERT INTO nucleo_rotas (data_rota, nome, agent_id, status, provedor, vuupt_route_id) "
                                "VALUES ('2026-11-23', 'Rota V', 10, 'ACEITA', ?, ?)",
                                (provedor, 5555 if provedor == banco.PROVEDOR_VUUPT else None))
        self.conn.execute("INSERT INTO nucleo_paradas (rota_id, ordem, service_id, codigo, situacao) "
                          "VALUES (?, 1, 777, 'PS-777', ?)", (cur.lastrowid, situacao))
        self.conn.commit()
        rota = self.conn.execute("SELECT * FROM nucleo_rotas WHERE id=?", (cur.lastrowid,)).fetchone()
        return rota, self.conn.execute("SELECT * FROM nucleo_paradas WHERE rota_id=?", (rota["id"],)).fetchone()

    def test_rota_vuupt_chama_tirar_pedido_com_token_rota_e_servico(self):
        sys.path.insert(0, str(_RAIZ / "roteirizacao"))
        import edicao_rota
        rota, parada = self._rota_parada(banco.PROVEDOR_VUUPT)
        resp = {"ok": True, "rota_cancelada": False, "rota_iniciada": False, "servico": {"id": 777}}
        with mock.patch.object(edicao_rota, "tirar_pedido", return_value=resp) as tp, \
                mock.patch.object(carregamento, "_efeitos_no_painel") as painel:
            r = carregamento._tirar_da_vuupt_ou_local(rota, parada, "Não coube", None)
        tp.assert_called_once_with("tok-teste", 5555, 777)
        self.assertTrue(r["ok"])
        painel.assert_called_once()

    def test_rota_vuupt_fora_da_rota_conta_como_sucesso(self):
        sys.path.insert(0, str(_RAIZ / "roteirizacao"))
        import edicao_rota
        rota, parada = self._rota_parada(banco.PROVEDOR_VUUPT)
        resp = {"ok": False, "fora_da_rota": True, "erro": "Pedido 777 não está mais nessa rota."}
        with mock.patch.object(edicao_rota, "tirar_pedido", return_value=resp), \
                mock.patch.object(carregamento, "_efeitos_no_painel"):
            r = carregamento._tirar_da_vuupt_ou_local(rota, parada, "Não coube", None)
        self.assertTrue(r["ok"])
        self.assertFalse(r["rota_cancelada"])

    def test_rota_vuupt_recusa_vira_erro(self):
        sys.path.insert(0, str(_RAIZ / "roteirizacao"))
        import edicao_rota
        rota, parada = self._rota_parada(banco.PROVEDOR_VUUPT)
        resp = {"ok": False, "erro": "Pedido 777 já foi concluído e não pode sair da rota."}
        with mock.patch.object(edicao_rota, "tirar_pedido", return_value=resp), \
                mock.patch.object(carregamento, "_efeitos_no_painel") as painel:
            r = carregamento._tirar_da_vuupt_ou_local(rota, parada, "Não coube", None)
        self.assertEqual(r, resp)
        painel.assert_not_called()

    def test_efeitos_no_painel_sem_retornando_e_best_effort(self):
        rota, parada = self._rota_parada(banco.PROVEDOR_VUUPT)
        falso = mock.Mock()
        falso.rascunho_por_vuupt_route_id.return_value = {"id": 9}
        with mock.patch.dict(sys.modules, {"rascunhos_rota": falso}):
            carregamento._efeitos_no_painel(rota, parada, {"ok": True, "rota_cancelada": True, "rota_iniciada": True},
                                            "Não coube", None)
            kw = falso.aplicar_efeitos_pos_remocao.call_args.kwargs
            self.assertEqual((kw["vuupt_route_id"], kw["service_id"], kw["codigo"]), (5555, 777, "PS-777"))
            self.assertFalse(kw["resultado"]["rota_iniciada"])   # nao carregado: nunca "Retornando"
            self.assertTrue(kw["resultado"]["rota_cancelada"])
            falso.aplicar_efeitos_pos_remocao.side_effect = RuntimeError("painel quebrado")
            carregamento._efeitos_no_painel(rota, parada, {"ok": True, "rota_cancelada": False}, "Não coube", None)

    def test_rota_app_parada_comecada_recusa(self):
        rota, parada = self._rota_parada(banco.PROVEDOR_APP, situacao="ENTREGUE")
        r = carregamento._tirar_da_vuupt_ou_local(rota, parada, "Não coube", None)
        self.assertFalse(r["ok"])
        self.assertIn("PS-777", r["erro"])
        rota, parada = self._rota_parada(banco.PROVEDOR_APP)
        self.assertTrue(carregamento._tirar_da_vuupt_ou_local(rota, parada, "Não coube", None)["ok"])


if __name__ == "__main__":
    unittest.main()
