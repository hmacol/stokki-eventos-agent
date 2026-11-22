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

    def test_outro_uuid_na_mesma_rota_409(self):
        self._confirmar(uuid="u1")
        tirar = _TirarFalso()
        with self.assertRaises(operacao.OperacaoInvalida) as ctx:
            self._confirmar([self._desm(self._paradas()[0])], uuid="u2", tirar=tirar)
        self.assertEqual(ctx.exception.codigo, 409)
        self.assertEqual(tirar.chamadas, [])

    def test_uuid_de_outra_rota_nao_devolve_o_carregamento_dela(self):
        self._confirmar(uuid="u1")
        outra = self._rota()
        tirar = _TirarFalso()
        with self.assertRaises(operacao.OperacaoInvalida) as ctx:
            self._confirmar([self._desm(self._paradas(outra)[0])], uuid="u1", tirar=tirar, rota=outra)
        self.assertEqual(ctx.exception.codigo, 409)
        self.assertEqual(tirar.chamadas, [])
        self.assertIsNone(carregamento.carregamento_da_rota(self.conn, outra["id"]))

    def test_rota_vuupt_sem_rota_cancelada_nao_cancela_pelo_contador(self):
        cfg = {**_CFG_LIGADA, "api_motorista": {"checklist_carregamento": {"ativo": True},
                                                "ponte_vuupt": {"ativo": True, "todos": True}}}
        with mock.patch.object(ponte_vuupt, "_config", cfg):
            rota = self._rota(provedor=banco.PROVEDOR_VUUPT, vuupt_route_id=4242, paradas=1)
            r = self._confirmar([self._desm(self._paradas(rota)[0])], rota=rota)
        self.assertTrue(r["ok"])
        self.assertFalse(r["rota_cancelada"])
        self.assertEqual(self.conn.execute("SELECT status FROM nucleo_rotas WHERE id=?",
                                           (rota["id"],)).fetchone()[0], banco.ROTA_ACEITA)

    def test_item_removido_antes_e_omitido_na_retentativa_fica_ligado(self):
        ps = self._paradas()
        r = self._confirmar([self._desm(ps[0]), self._desm(ps[1])],
                            tirar=_TirarFalso(falhas={ps[1]["id"]: "Vuupt fora do ar"}))
        self.assertFalse(r["ok"])
        r2 = self._confirmar([self._desm(ps[2])])          # ps[0] ja saiu; ps[1] foi carregado afinal
        self.assertTrue(r2["ok"])
        c = r2["carregamento"]
        self.assertEqual((c["qtd_desmarcados"], c["qtd_carregados"]), (2, 1))
        ligados = {row[0] for row in self.conn.execute(
            "SELECT service_id FROM nucleo_carregamento_itens WHERE carregamento_id = ?", (c["id"],))}
        self.assertEqual(ligados, {ps[0]["service_id"], ps[2]["service_id"]})

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

    def test_ja_fora_da_rota_nao_devolve_pedido_ao_pool(self):
        p = self._paradas()[0]
        tirar = mock.Mock(return_value={"ok": True, "ja_fora_da_rota": True, "rota_cancelada": False})
        with mock.patch.object(carregamento.nucleo_pedidos, "marcar_aberto_por_service_ids") as marcar:
            r = self._confirmar([self._desm(p)], tirar=tirar)
        self.assertTrue(r["ok"])
        marcar.assert_not_called()

    def test_falha_em_marcar_aberto_nao_derruba_o_confirmar(self):
        p = self._paradas()[0]
        with mock.patch.object(carregamento.nucleo_pedidos, "marcar_aberto_por_service_ids",
                               side_effect=RuntimeError("banco travado")):
            r = self._confirmar([self._desm(p)])
        self.assertTrue(r["ok"])
        self.assertEqual(self.conn.execute("SELECT situacao FROM nucleo_paradas WHERE id=?", (p["id"],)).fetchone()[0],
                         banco.PARADA_CANCELADA)

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


# ── API (POST /api/rotas/<id>/carregamento) ───────────────────────────────────

def json_sem_parada(_parada_id):
    return '[{"motivo": "Não coube"}]'


class TestApiCarregamento(unittest.TestCase):
    CPF, PIN, AGENT = "12345678901", "123456", 4242

    def _config(self, ativo=True):
        return {"api_motorista": {"secret_key": "segredo-de-teste", "pasta_comprovantes": str(self.raiz / "fotos"),
                                  "gcs_ativo": False, "validacao_fotos": {"ativo": True, "max_tentativas": 2},
                                  "checklist_carregamento": {"ativo": ativo}},
                "anthropic": {"api_key": "sk-teste"}, "vuupt_api": {"token": "tok-teste"}}

    def setUp(self):
        from nucleo import api_motorista, auth_motorista as auth
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.raiz = Path(self._tmp.name)
        self._patch_db = mock.patch.object(banco, "DB_PATH", self.raiz / "t.db")
        self._patch_db.start()
        self._cfg_ponte = ponte_vuupt._config
        self.app = api_motorista.criar_app(self._config())
        self.cli = self.app.test_client()
        conn = banco.conectar()
        auth.criar_ou_atualizar_motorista(conn, self.CPF, "Motorista Teste", self.PIN, agent_id=self.AGENT,
                                          tipo_veiculo="VAN_HR")
        auth.criar_ou_atualizar_motorista(conn, "98765432100", "Outro", self.PIN, agent_id=777, tipo_veiculo="VAN_HR")
        conn.close()
        self.h = self._login(self.CPF)
        self.rota_id = self._rota()

    def tearDown(self):
        ponte_vuupt.definir_config(self._cfg_ponte)
        self._patch_db.stop()
        self._tmp.cleanup()

    def _login(self, cpf):
        r = self.cli.post("/api/login", json={"cpf": cpf, "pin": self.PIN})
        return {"Authorization": f"Bearer {r.get_json()['acesso']}"}

    def _rota(self):
        from datetime import date
        from nucleo import rotas
        hoje = date.today().isoformat()
        conn = banco.conectar()
        rota_id = rotas.materializar_rascunho({
            "id": 1, "data_alvo": hoje, "lote_id": "L", "nome": "R", "agent_id": self.AGENT,
            "vehicle_id": None, "motorista_nome": "Teste", "tipo_veiculo": None, "start_location_base_id": 1,
            "end_location_base_id": 1, "start_at": f"{hoje} 07:00:00", "km_estimado": 10.0,
            "status": "RASCUNHO", "vuupt_route_id": None,
            "paradas": [{"ordem": i, "service_id": 900 + i, "codigo": f"PS-{900 + i}", "titulo": "A",
                         "endereco": "Rua A", "latitude": -23.5, "longitude": -46.6, "nivel_dificuldade": 1,
                         "volume_caixas": 1} for i in (1, 2)],
        }, banco.PROVEDOR_APP, conn=conn)
        conn.close()
        return rota_id

    def _paradas(self):
        conn = banco.conectar()
        ids = [r["id"] for r in conn.execute("SELECT id FROM nucleo_paradas WHERE rota_id = ? ORDER BY ordem",
                                             (self.rota_id,))]
        conn.close()
        return ids

    def _fotos_em_disco(self):
        return [p for p in (self.raiz / "fotos").rglob("*") if p.is_file()]

    @staticmethod
    def _leitura(eh_carga=True):
        return {"eh_carga": eh_carga, "legivel": True, "bau_inteiro": True, "ocupacao_pct": 70, "problemas": [],
                "motivo": "ok"}

    def _modelo(self, eh_carga=True):
        from nucleo import validacao_fotos as vf
        return mock.patch.object(vf, "_chamar_modelo", return_value=(self._leitura(eh_carga), {"entrada": 1, "saida": 1}))

    def _enviar(self, foto=None, uuid="c1", desmarcados=None, tentativa=1, rota_id=None, headers=None):
        import io
        import json
        from nucleo.test_validacao_fotos import _imagem
        foto = foto if foto is not None else _imagem()
        dados = {"arquivo": (io.BytesIO(foto), "foto.jpg"), "carregador": "Joao", "uuid": uuid,
                 "desmarcados": json.dumps(desmarcados or []), "tentativa": str(tentativa)}
        return self.cli.post(f"/api/rotas/{rota_id or self.rota_id}/carregamento", headers=headers or self.h,
                             data=dados, content_type="multipart/form-data")

    def _iniciar(self):
        return self.cli.post(f"/api/rotas/{self.rota_id}/iniciar", json={}, headers=self.h)

    def test_checklist_anuncia_a_chave(self):
        c = self.cli.get("/api/checklist", headers=self.h).get_json()
        self.assertEqual(c["carregamento"], {"ativo": True})

    def test_aprovada_sem_desmarcados(self):
        with self._modelo():
            r = self._enviar()
        j = r.get_json()
        self.assertEqual(r.status_code, 200, j)
        self.assertTrue(j["ok"])
        self.assertEqual(j["carregamento"]["ocupacao_pct"], 70)
        self.assertEqual(j["rota"]["carregamento"], {"exigido": True, "feito": True})
        self.assertEqual(len(self._fotos_em_disco()), 1)

    def test_rota_anuncia_exigido_antes_do_checklist(self):
        rota = self.cli.get(f"/api/rotas/{self.rota_id}", headers=self.h).get_json()
        self.assertEqual(rota["carregamento"], {"exigido": True, "feito": False})

    def test_iniciar_antes_do_checklist_recusa(self):
        r = self._iniciar()
        self.assertEqual(r.status_code, 409)
        self.assertIn("checklist de carregamento", r.get_json()["erro"])

    def test_iniciar_depois_do_checklist_passa(self):
        with self._modelo():
            self._enviar()
        self.assertEqual(self._iniciar().status_code, 200)

    def test_iniciar_com_chave_desligada_como_hoje(self):
        ponte_vuupt.definir_config(self._config(ativo=False))
        self.assertEqual(self._iniciar().status_code, 200)

    def test_reprovada_na_primeira_tentativa_nao_grava_nada(self):
        with self._modelo(eh_carga=False):
            r = self._enviar(tentativa=1)
        j = r.get_json()
        self.assertEqual(r.status_code, 409, j)
        self.assertFalse(j["pode_seguir"])
        self.assertEqual(self._fotos_em_disco(), [])
        conn = banco.conectar()
        self.assertIsNone(carregamento.carregamento_da_rota(conn, self.rota_id))
        conn.close()

    def test_reprovada_na_ultima_tentativa_segue_com_revisao_humana(self):
        with self._modelo(eh_carga=False):
            r = self._enviar(tentativa=2)
        j = r.get_json()
        self.assertEqual(r.status_code, 200, j)
        self.assertEqual(j["carregamento"]["revisao_humana"], 1)

    def test_nao_verificado_segue(self):
        from nucleo import validacao_fotos as vf
        with mock.patch.object(vf, "_chamar_modelo", side_effect=RuntimeError("sem rede")):
            r = self._enviar()
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["carregamento"]["resultado_foto"], vf.NAO_VERIFICADO)

    def test_rota_de_outro_motorista_404(self):
        with self._modelo():
            r = self._enviar(headers=self._login("98765432100"))
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self._fotos_em_disco(), [])

    def test_mesmo_uuid_repetido_devolve_o_mesmo_corpo_sem_segunda_foto(self):
        from nucleo.test_validacao_fotos import _imagem
        with self._modelo():
            a = self._enviar(uuid="u-igual").get_json()
            b = self._enviar(foto=_imagem() + b"\x00", uuid="u-igual")
        self.assertEqual(b.status_code, 200)
        self.assertEqual(a["carregamento"], b.get_json()["carregamento"])
        self.assertEqual(len(self._fotos_em_disco()), 1)

    def test_desmarcado_sai_da_rota(self):
        p1, _ = self._paradas()
        with self._modelo():
            r = self._enviar(desmarcados=[{"parada_id": p1, "motivo": "Não coube"}])
        j = r.get_json()
        self.assertEqual(r.status_code, 200, j)
        self.assertEqual(j["carregamento"]["qtd_desmarcados"], 1)
        self.assertEqual(j["rota"]["total_paradas"], 1)

    def test_falha_da_vuupt_devolve_409_com_falhas(self):
        p1, _ = self._paradas()
        falso = _TirarFalso(falhas={p1: "Vuupt fora do ar"})
        with self._modelo(), mock.patch.object(carregamento, "_tirar_da_vuupt_ou_local", falso):
            r = self._enviar(desmarcados=[{"parada_id": p1, "motivo": "Não coube"}])
        j = r.get_json()
        self.assertEqual(r.status_code, 409, j)
        self.assertFalse(j["ok"])
        self.assertEqual(j["falhas"][0]["parada_id"], p1)
        self.assertIn("Vuupt fora do ar", j["erro"])
        self.assertEqual(j["removidos"], [])




    def _enviar_e_nada_gravado(self, esperado, **kw):
        with self._modelo() as modelo:
            r = self._enviar(**kw)
        self.assertEqual(r.status_code, esperado, r.get_json())
        modelo.assert_not_called()
        return r

    def test_uuid_vazio_recusa_sem_modelo_nem_foto(self):
        self._enviar_e_nada_gravado(400, uuid="")
        self.assertEqual(self._fotos_em_disco(), [])

    def test_outro_uuid_em_rota_ja_confirmada_recusa_sem_modelo_nem_foto(self):
        with self._modelo():
            self._enviar(uuid="u-1")
        antes = len(self._fotos_em_disco())
        self._enviar_e_nada_gravado(409, uuid="u-2")
        self.assertEqual(len(self._fotos_em_disco()), antes)

    def test_chave_desligada_recusa_sem_modelo_nem_foto(self):
        ponte_vuupt.definir_config(self._config(ativo=False))
        self._enviar_e_nada_gravado(409)
        self.assertEqual(self._fotos_em_disco(), [])

    def test_rota_em_rota_recusa_sem_modelo_nem_foto(self):
        conn = banco.conectar()
        conn.execute("UPDATE nucleo_rotas SET status = 'EM_ROTA' WHERE id = ?", (self.rota_id,))
        conn.commit()
        conn.close()
        self._enviar_e_nada_gravado(409)
        self.assertEqual(self._fotos_em_disco(), [])

    def test_desmarcados_malformado_devolve_400(self):
        import io
        from nucleo.test_validacao_fotos import _imagem
        p1, _ = self._paradas()
        for bruto in ("{nao-json", '{"a": 1}', "[1, 2]", json_sem_parada(p1)):
            dados = {"arquivo": (io.BytesIO(_imagem()), "foto.jpg"), "carregador": "Joao", "uuid": "m1",
                     "desmarcados": bruto, "tentativa": "1"}
            with self._modelo():
                r = self.cli.post(f"/api/rotas/{self.rota_id}/carregamento", headers=self.h, data=dados,
                                  content_type="multipart/form-data")
            self.assertEqual(r.status_code, 400, (bruto, r.get_json()))
            self.assertEqual(self._fotos_em_disco(), [])

    def test_tentativa_menor_que_1_vale_1(self):
        with self._modelo(eh_carga=False):
            r = self._enviar(tentativa=0)
        self.assertEqual(r.status_code, 409, r.get_json())   # com tentativa 0 nao liberaria por esgotar

    def test_cache_por_sha_nao_vale_de_outro_tipo(self):
        from nucleo import validacao_fotos as vf
        from nucleo.test_validacao_fotos import _imagem
        foto = _imagem()
        import hashlib
        sha = hashlib.sha256(foto).hexdigest()
        conn = banco.conectar()
        vf.registrar_validacao(conn, sha, vf.TIPO_CANHOTO, {"resultado": vf.REPROVADO, "motivo": "x"}, self.AGENT, "PS-1")
        conn.commit()
        conn.close()
        with self._modelo() as modelo:
            r = self._enviar(foto=foto)
        self.assertEqual(r.status_code, 200, r.get_json())
        modelo.assert_called_once()


class TestNaoCarregadosRecentes(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._patch = mock.patch.object(banco, "DB_PATH", Path(self._tmp.name) / "t.db")
        self._patch.start()
        self.conn = banco.conectar()

    def tearDown(self):
        self.conn.close()
        self._patch.stop()
        self._tmp.cleanup()

    def _item(self, service_id, removido_em, motivo="Não coube", obs=None):
        self.conn.execute(
            "INSERT INTO nucleo_carregamento_itens (rota_id, service_id, codigo, motivo, observacao, removido_em) "
            "VALUES (1, ?, 'PS-1', ?, ?, ?)", (service_id, motivo, obs, removido_em))
        self.conn.commit()

    def _antes(self, dias):
        return self.conn.execute(f"SELECT datetime('now','localtime','-{dias} days')").fetchone()[0]

    def test_so_removido_e_recente(self):
        self._item(1, self._antes(0))            # recente
        self._item(2, None)                      # carregado (nunca saiu)
        self._item(3, self._antes(5))            # antigo
        r = carregamento.nao_carregados_recentes(self.conn, [1, 2, 3, 4])
        self.assertEqual(list(r), [1])
        self.assertEqual(r[1]["motivo"], "Não coube")

    def test_mais_recente_por_service_id(self):
        self._item(1, self._antes(1), motivo="Outros", obs="velho")
        self._item(1, self._antes(0), motivo="Não Encontrado", obs="novo")
        r = carregamento.nao_carregados_recentes(self.conn, [1])
        self.assertEqual((r[1]["motivo"], r[1]["observacao"]), ("Não Encontrado", "novo"))
        self.assertIn("criado_em", r[1])

    def test_lista_vazia_e_tabela_ausente(self):
        self.assertEqual(carregamento.nao_carregados_recentes(self.conn, []), {})
        self.conn.execute("DROP TABLE nucleo_carregamento_itens")
        self.assertEqual(carregamento.nao_carregados_recentes(self.conn, [1]), {})


if __name__ == "__main__":
    unittest.main()
