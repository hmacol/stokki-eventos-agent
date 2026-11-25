# -*- coding: utf-8 -*-
"""
Demandas de compra (Hugo, 19/11/2026): regras, fluxo de aprovacao (desligado
por padrao), aviso em simulacao e rotas da tela /compras.

    py -3.11 -m unittest test_compras   (de dentro de painel_agentes/)
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

import acesso_telas  # noqa: E402
import compras as cp  # noqa: E402
from test_pagamentos_pontuais import painel_agentes  # noqa: E402  (importa o painel sem config.yaml)

ORIGEM = {"Origin": "http://localhost"}
CFG_DESLIGADO = {"painel_agentes": {"usuario": "u_teste", "senha": "s_teste"}}
CFG_APROVACAO = {**CFG_DESLIGADO, "compras": {"aprovacao_ativa": True}}
CFG_LIGADO = {**CFG_DESLIGADO, "compras": {"ativo": True}}
PEDIDO = {"item": "Fita larga", "quantidade": "10 rolos", "motivo": "Acabando no expedição", "prioridade": "NORMAL"}


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "dados.db"
        for alvo, attr in ((cp, "DB_PATH"), (acesso_telas, "DB_PATH")):
            p = mock.patch.object(alvo, attr, self.db)
            p.start()
            self.addCleanup(p.stop)
        self.conn = cp.conectar()
        self.addCleanup(self.conn.close)

    def criar(self, config=None, **kw):
        return cp.criar(self.conn, {**PEDIDO, **kw}, "Valeria", config)


class TestRegras(Base):
    def test_criar_vai_direto_para_pendente_com_aprovacao_desligada(self):
        d = cp.obter(self.conn, self.criar())
        self.assertEqual(d["situacao"], cp.PENDENTE)
        self.assertEqual((d["item"], d["criado_por"]), ("Fita larga", "Valeria"))

    def test_campos_obrigatorios_e_prioridade(self):
        for extra in ({"item": ""}, {"quantidade": " "}, {"motivo": ""}, {"prioridade": "MEIA"}, {"item": "x" * 121}):
            with self.assertRaises(cp.ErroCompra, msg=str(extra)):
                self.criar(**extra)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM compras_demandas").fetchone()[0], 0)

    def test_lista_por_prioridade_depois_mais_antiga(self):
        ids = {p: self.criar(item=p, prioridade=p) for p in ("BAIXA", "NORMAL", "URGENTE", "ALTA")}
        outro_normal = self.criar(item="Normal2", prioridade="NORMAL")
        ordem = [d["id"] for d in cp.listar(self.conn, cp.PENDENTE)]
        self.assertEqual(ordem, [ids["URGENTE"], ids["ALTA"], ids["NORMAL"], outro_normal, ids["BAIXA"]])

    def test_marcar_comprada_guarda_os_dados(self):
        i = self.criar()
        cp.marcar_comprada(self.conn, i, {"data_compra": "2026-11-19", "fornecedor": "Kalunga", "valor": "89,90",
                                          "compra_obs": "NF 123"}, "Vivian")
        d = cp.obter(self.conn, i)
        self.assertEqual((d["situacao"], d["comprado_por"], d["data_compra"], d["fornecedor"], d["valor"], d["compra_obs"]),
                         (cp.COMPRADA, "Vivian", "2026-11-19", "Kalunga", 89.9, "NF 123"))
        self.assertEqual([x["id"] for x in cp.listar(self.conn, cp.PENDENTE)], [])
        with self.assertRaises(cp.ErroCompra):                      # nao compra duas vezes
            cp.marcar_comprada(self.conn, i, {"data_compra": "2026-11-19"}, "Vivian")

    def test_compra_exige_data_valida_e_valor_certo(self):
        i = self.criar()
        for dados in ({}, {"data_compra": "19/11/2026"}, {"data_compra": "2026-11-19", "valor": "abc"},
                      {"data_compra": "2026-11-19", "valor": "0"}, {"data_compra": "2026-11-19", "valor": "-5"}):
            with self.assertRaises(cp.ErroCompra, msg=str(dados)):
                cp.marcar_comprada(self.conn, i, dados, "Vivian")
        self.assertEqual(cp.obter(self.conn, i)["situacao"], cp.PENDENTE)
        cp.marcar_comprada(self.conn, i, {"data_compra": "2026-11-19"}, "Vivian")   # fornecedor e valor sao opcionais
        self.assertIsNone(cp.obter(self.conn, i)["valor"])

    def test_cancelar_pede_motivo_e_so_antes_da_compra(self):
        i = self.criar()
        with self.assertRaises(cp.ErroCompra):
            cp.cancelar(self.conn, i, "", "Valeria")
        cp.cancelar(self.conn, i, "Já tinha em estoque", "Valeria")
        self.assertEqual(cp.obter(self.conn, i)["situacao"], cp.CANCELADA)
        with self.assertRaises(cp.ErroCompra):
            cp.marcar_comprada(self.conn, i, {"data_compra": "2026-11-19"}, "Vivian")


class TestAprovacao(Base):
    def test_desligada_nao_tem_etapa(self):
        self.assertEqual(cp.obter(self.conn, self.criar(CFG_DESLIGADO))["situacao"], cp.PENDENTE)

    def test_ligada_nasce_aguardando_e_nao_pode_comprar(self):
        i = self.criar(CFG_APROVACAO)
        self.assertEqual(cp.obter(self.conn, i)["situacao"], cp.AGUARDANDO_APROVACAO)
        with self.assertRaises(cp.ErroCompra):
            cp.marcar_comprada(self.conn, i, {"data_compra": "2026-11-19"}, "Vivian")
        cp.aprovar(self.conn, i, "Hugo")
        d = cp.obter(self.conn, i)
        self.assertEqual((d["situacao"], d["aprovado_por"]), (cp.PENDENTE, "Hugo"))
        cp.marcar_comprada(self.conn, i, {"data_compra": "2026-11-19"}, "Vivian")
        self.assertEqual(cp.obter(self.conn, i)["situacao"], cp.COMPRADA)

    def test_recusar_exige_motivo_e_encerra(self):
        i = self.criar(CFG_APROVACAO)
        with self.assertRaises(cp.ErroCompra):
            cp.recusar(self.conn, i, "", "Hugo")
        cp.recusar(self.conn, i, "Não é prioridade", "Hugo")
        d = cp.obter(self.conn, i)
        self.assertEqual((d["situacao"], d["recusado_por"], d["motivo_recusa"]), (cp.RECUSADA, "Hugo", "Não é prioridade"))
        with self.assertRaises(cp.ErroCompra):
            cp.aprovar(self.conn, i, "Hugo")

    def test_nao_aprova_o_que_ja_esta_pendente(self):
        with self.assertRaises(cp.ErroCompra):
            cp.aprovar(self.conn, self.criar(), "Hugo")

    def test_ligar_depois_nao_mexe_nas_pendentes_antigas(self):
        antiga = self.criar(CFG_DESLIGADO)
        self.criar(CFG_APROVACAO)
        self.assertEqual(cp.obter(self.conn, antiga)["situacao"], cp.PENDENTE)


class TestAviso(Base):
    def test_desligado_fica_simulado_e_nao_despacha(self):
        i = self.criar()
        despachar = mock.Mock()
        with mock.patch("notificar_whatsapp.despachar", despachar):
            aviso = cp.disparar_aviso(self.conn, i, "criacao", CFG_DESLIGADO)
        self.assertEqual(aviso["situacao"], cp.AVISO_SIMULADO)
        self.assertIn("Fita larga", aviso["texto"])
        despachar.assert_not_called()

    def test_ligado_envia_ao_grupo_achado_pelo_nome(self):
        i = self.criar()
        cp.preparar_aviso(self.conn, i, "criacao", CFG_LIGADO)
        despachar = mock.Mock(return_value="enviado")
        grupos = lambda cfg: [{"id": "g-outro", "name": "Outro"}, {"id": "g-adm", "name": "Freshlog Adm / Financeiro / RH"}]
        self.assertEqual(cp.enviar_aviso(CFG_LIGADO, i, self.db, despachar, grupos), "enviado")
        self.assertEqual(despachar.call_args.kwargs["grupo_id"], "g-adm")
        self.assertEqual(cp.obter(self.conn, i)["aviso_situacao"], "enviado")

    def test_grupo_nao_achado_ou_repetido_nao_envia(self):
        i = self.criar()
        cp.preparar_aviso(self.conn, i, "criacao", CFG_LIGADO)
        despachar = mock.Mock()
        grupos = lambda cfg: [{"id": "a", "name": "Freshlog Adm/Financeiro/RH"}, {"id": "b", "name": "Freshlog Adm/Financeiro/RH"}]
        self.assertEqual(cp.enviar_aviso(CFG_LIGADO, i, self.db, despachar, grupos), "nao_enviado")
        despachar.assert_not_called()

    def test_textos(self):
        i = self.criar(CFG_APROVACAO)
        self.assertIn("aguardando aprovação", cp.TEXTOS["criacao"](cp.obter(self.conn, i)))
        cp.aprovar(self.conn, i, "Hugo")
        self.assertIn("pode comprar", cp.TEXTOS["aprovacao"](cp.obter(self.conn, i)))
        cp.marcar_comprada(self.conn, i, {"data_compra": "2026-11-19", "valor": "1234,5"}, "Vivian")
        texto = cp.TEXTOS["compra"](cp.obter(self.conn, i))
        self.assertIn("19/11/2026", texto)
        self.assertIn("R$ 1.234,50", texto)


class TestTela(Base):
    def setUp(self):
        super().setUp()
        self.cfg = CFG_DESLIGADO
        p = mock.patch.object(painel_agentes, "_carregar_config", side_effect=lambda: self.cfg)
        p.start()
        self.addCleanup(p.stop)
        mock.patch("notificar_whatsapp.despachar", side_effect=AssertionError("envio real nos testes")).start()
        self.addCleanup(mock.patch.stopall)
        painel_agentes.app.config["TESTING"] = True
        self.cliente = painel_agentes.app.test_client()

    def logar(self, nivel="total", usuario="hugo"):
        with self.cliente.session_transaction() as s:
            s["nivel_acesso"] = nivel
            s["usuario"] = usuario

    def test_so_total_entra_por_nivel(self):
        for nivel, esperado in (("total", 200), ("operador", 403), ("leitura", 403), ("atendimento", 403),
                                ("logistica", 403), ("galpao", 403)):
            self.logar(nivel)
            self.assertEqual(self.cliente.get("/compras").status_code, esperado, nivel)

    def test_criar_aparecer_e_marcar_comprada(self):
        self.logar()
        r = self.cliente.post("/api/compras", json=PEDIDO, headers=ORIGEM)
        self.assertEqual(r.status_code, 200, r.get_json())
        j = r.get_json()
        self.assertEqual((j["demanda"]["situacao"], j["aviso"]["situacao"]), ("PENDENTE", "simulado"))
        html = self.cliente.get("/compras").get_data(as_text=True)
        self.assertIn("Fita larga", html)
        self.assertIn("Modo simulação", html)

        r = self.cliente.post(f"/api/compras/{j['id']}/comprar", json={}, headers=ORIGEM)
        self.assertEqual(r.status_code, 400)                        # sem data
        r = self.cliente.post(f"/api/compras/{j['id']}/comprar", headers=ORIGEM,
                              json={"data_compra": "2026-11-19", "fornecedor": "Kalunga", "valor": "50"})
        self.assertEqual(r.get_json()["demanda"]["situacao"], "COMPRADA")
        self.assertIn("Kalunga", self.cliente.get("/compras?situacao=COMPRADA").get_data(as_text=True))
        self.assertNotIn("Fita larga", self.cliente.get("/compras").get_data(as_text=True))

    def test_validacao_e_origem(self):
        self.logar()
        self.assertEqual(self.cliente.post("/api/compras", json={"item": "x"}, headers=ORIGEM).status_code, 400)
        self.assertEqual(self.cliente.post("/api/compras", json=PEDIDO).status_code, 403)

    def test_aprovar_e_recusar_so_pelo_total(self):
        self.cfg = CFG_APROVACAO
        self.logar()
        i = self.cliente.post("/api/compras", json=PEDIDO, headers=ORIGEM).get_json()["id"]
        j = cp.obter(self.conn, i)
        self.assertEqual(j["situacao"], cp.AGUARDANDO_APROVACAO)
        with mock.patch.object(painel_agentes, "_pode_aprovar_compras", return_value=False):
            self.assertEqual(self.cliente.post(f"/api/compras/{i}/aprovar", headers=ORIGEM).status_code, 403)
            self.assertEqual(self.cliente.post(f"/api/compras/{i}/recusar", json={"motivo": "x"}, headers=ORIGEM).status_code, 403)
        r = self.cliente.post(f"/api/compras/{i}/aprovar", headers=ORIGEM)
        self.assertEqual(r.get_json()["demanda"]["situacao"], "PENDENTE")
        self.assertEqual(r.get_json()["demanda"]["aprovado_por"], "hugo")

    def test_tela_liberada_por_acesso_personalizado(self):
        # Vivian: login nominal de nivel operador com a tela "Compras" liberada em /acessos
        self.assertIn("compras_tela", [t[0] for t in acesso_telas.TELAS])
        self.assertTrue(any(__import__("re").search(p, "/compras") for t in acesso_telas.TELAS
                            if t[0] == "compras_tela" for p in t[3]))


if __name__ == "__main__":
    unittest.main()
