# -*- coding: utf-8 -*-
"""
test_grupos_motoristas.py

Grupos do WhatsApp dos motoristas: sugerir, confirmar, prévia e renomear.
Gateway sempre injetado (nenhuma chamada de rede). SQLite temporário.
    py -3.11 -m unittest nucleo.test_grupos_motoristas -v
"""
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))

from nucleo import atualizacao_motorista as atu, banco, grupos_motoristas as gm  # noqa: E402

CFG = {"base_url": "http://x/api", "api_key": "k", "sessao": "s"}
# renomeacao REAL: chave ligada, canal do WhatsApp ligado e envio real (forcar_destino_whatsapp presente e vazio)
CONFIG_LIGADO = {"whatsapp_notificacoes": dict(CFG, ativo=True),
                 "atualizacao_motoristas": {"renomear_grupos": True, "forcar_destino_whatsapp": ""}}

GRUPOS = {
    "g-ew": "FRESHLOG + EWERTON - REFRIGERADO",
    "g-ro1": "FRESHLOG + RODRIGO - Fio Refrigerado",
    "g-ro2": "FRESHLOG + RODRIGO - FIO Refrig",
    "g-rf": "FRESHLOG + RENATO FORTE - FIO-REFRIG",
    "g-re": "FRESHLOG + RENATO - CARRO REFRIGERADO",
    "g-je": "FRESHLOG + JESSÉ/WESLEI - Van -15",
    "g-sa": "FRESH + SAMUEL - FIO -10",
    "g-zw": "​FRESHLOG + IAGO - Fiorino -15",
    "g-min": " freshlog + marcos - fiorino ",
    "g-car": " FreshLog Carregamento ",
    "g-vv": "Vida Veg & Freshlog",
    "g-rt": "Rotas Freshlog",
}


def _listar(grupos=GRUPOS):
    return lambda cfg: [{"id": i, "name": n} for i, n in grupos.items()]


def _grupo_de(participantes=None, extra=None):
    """participantes: {grupo_id: [numeros]}; extra: {grupo_id: campos que sobrescrevem}"""
    participantes = participantes or {}
    extra = extra or {}

    def _g(cfg, gid):
        dados = {"id": gid, "name": GRUPOS.get(gid), "locked": False,
                 "participants": [{"id": f"{n}@c.us", "number": n, "isAdmin": False}
                                  for n in participantes.get(gid, [])]}
        dados.update(extra.get(gid, {}))
        return dados
    return _g


class TestGruposMotoristas(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._patch = mock.patch.object(banco, "DB_PATH", Path(self._tmp.name) / "t.db")
        self._patch.start()
        self.conn = banco.conectar()

    def tearDown(self):
        self.conn.close()
        self._patch.stop()
        self._tmp.cleanup()

    def _mot(self, nome, telefone=None, cpf=None):
        agora = banco.agora()
        c = self.conn.execute(
            "INSERT INTO bd_motoristas (NOME_MOTORISTA, TELEFONE_MOTORISTA, CPF_MOTORISTA, ATIVO, criado_em, atualizado_em) "
            "VALUES (?, ?, ?, 'SIM', ?, ?)", (nome, telefone, cpf, agora, agora))
        self.conn.commit()
        return c.lastrowid

    def _tabela(self):
        return [dict(r) for r in self.conn.execute("SELECT * FROM motoristas_grupos_whatsapp ORDER BY bd_motorista_id")]

    def _pessoa(self, res, pid):
        return next(p for p in res["pessoas"] if p["principal_id"] == pid)

    def test_sugere_por_telefone_antes_de_nome(self):
        m = self._mot("Ewerton de Paula", "(11) 99999-8888")
        res = gm.sugerir(self.conn, CFG, listar=_listar(),
                         grupo=_grupo_de({"g-sa": ["5511999998888"]}))
        sug = self._pessoa(res, m)["sugestoes"]
        self.assertEqual([(s["grupo_id"], s["como"]) for s in sug], [("g-sa", "telefone"), ("g-ew", "nome")])
        self.assertEqual(sug[0]["grupo_nome"], GRUPOS["g-sa"])

    def test_dois_rodrigos_viram_sugestao_fraca_para_os_dois_grupos(self):
        a = self._mot("Rodrigo Alves")
        b = self._mot("Rodrigo Lima")
        res = gm.sugerir(self.conn, CFG, listar=_listar(), grupo=_grupo_de())
        for pid in (a, b):
            sug = self._pessoa(res, pid)["sugestoes"]
            self.assertEqual({(s["grupo_id"], s["como"]) for s in sug},
                             {("g-ro1", "nome"), ("g-ro2", "nome")})

    def test_renato_e_renato_forte_e_nome_com_barra(self):
        r = self._mot("Renato Silva")
        j = self._mot("Jesse Santos")
        res = gm.sugerir(self.conn, CFG, listar=_listar(), grupo=_grupo_de())
        self.assertEqual({s["grupo_id"] for s in self._pessoa(res, r)["sugestoes"]}, {"g-rf", "g-re"})
        self.assertEqual([s["grupo_id"] for s in self._pessoa(res, j)["sugestoes"]], ["g-je"])

    def test_ignora_grupo_fora_do_padrao(self):
        self._mot("Vida Veg")
        res = gm.sugerir(self.conn, CFG, listar=_listar(), grupo=_grupo_de())
        ids = {g["grupo_id"] for g in res["grupos"]}
        self.assertNotIn("g-vv", ids)
        self.assertNotIn("g-rt", ids)
        self.assertNotIn("g-car", ids)
        self.assertIn("g-ew", ids)
        self.assertEqual(self._pessoa(res, 1)["sugestoes"], [])

    def test_prefixo_com_caractere_invisivel_e_minusculas(self):
        i = self._mot("Iago Souza")
        m = self._mot("Marcos Dias")
        res = gm.sugerir(self.conn, CFG, listar=_listar(), grupo=_grupo_de())
        self.assertEqual([s["grupo_id"] for s in self._pessoa(res, i)["sugestoes"]], ["g-zw"])
        self.assertEqual([s["grupo_id"] for s in self._pessoa(res, m)["sugestoes"]], ["g-min"])

    def test_gateway_fora_devolve_erro_e_nao_grava(self):
        self._mot("Ewerton")
        res = gm.sugerir(self.conn, CFG, listar=lambda cfg: None, grupo=_grupo_de())
        self.assertTrue(res["erro"])
        self.assertEqual(res["pessoas"], [])
        self.assertEqual(self._tabela(), [])
        # leitura de um grupo vazia nao derruba
        m = self._mot("Samuel", "11999998888")
        res = gm.sugerir(self.conn, CFG, listar=_listar(), grupo=lambda cfg, gid: {})
        self.assertIsNone(res["erro"])
        self.assertEqual([s["como"] for s in self._pessoa(res, m)["sugestoes"]], ["nome"])
        res = gm.sugerir(self.conn, CFG, listar=_listar(), grupo=lambda cfg, gid: None)
        self.assertIsNone(res["erro"])

    def test_confirmar_upsert_e_remover(self):
        m = self._mot("Ewerton")
        gm.confirmar(self.conn, m, "g-ew", GRUPOS["g-ew"], "telefone", "ana")
        gm.confirmar(self.conn, m, "g-ro1", GRUPOS["g-ro1"], "nome", "bia")
        t = self._tabela()
        self.assertEqual(len(t), 1)
        self.assertEqual((t[0]["grupo_id"], t[0]["como"], t[0]["confirmado_por"]), ("g-ro1", "nome", "bia"))
        self.assertTrue(t[0]["confirmado_em"])
        res = gm.sugerir(self.conn, CFG, listar=_listar(), grupo=_grupo_de())
        self.assertEqual(self._pessoa(res, m)["confirmado"]["grupo_id"], "g-ro1")
        gm.confirmar(self.conn, m, "", "", "", "bia")
        self.assertEqual(self._tabela(), [])

    def test_nome_novo(self):
        self.assertEqual(gm.nome_novo("FRESHLOG + EWERTON - REFRIGERADO", "Ewerton de Paula Abreu"),
                         "FRESHLOG + EWERTON DE PAULA ABREU - REFRIGERADO")
        self.assertEqual(gm.nome_novo("FRESH + SAMUEL - FIO -10", "Samuel Souza"),
                         "FRESHLOG + SAMUEL SOUZA - FIO -10")
        self.assertEqual(gm.nome_novo("FRESHLOG + SAMUEL", "Samuel Souza"), "FRESHLOG + SAMUEL SOUZA")
        self.assertEqual(gm.nome_novo("​ freshlog + iago - Fiorino -15", "Iago Reis"),
                         "FRESHLOG + IAGO REIS - Fiorino -15")
        longo = gm.nome_novo("FRESHLOG + X - " + "v" * 200, "Nome Completo")
        self.assertEqual(len(longo), 100)
        self.assertTrue(longo.startswith("FRESHLOG + NOME COMPLETO - v"))
        self.assertEqual(len(gm.nome_novo("FRESHLOG + X", "N" * 200)), 100)

    def _confirma(self, pid, gid):
        gm.confirmar(self.conn, pid, gid, GRUPOS[gid], "nome", "ana")

    def test_previa_marca_dupla_e_ja_no_padrao(self):
        ew = self._mot("Ewerton de Paula")
        sa = self._mot("Samuel Souza")
        ja = self._mot("Iago Reis")
        j1 = self._mot("Jesse Santos")
        j2 = self._mot("Weslei Santos")
        self._mot("Sem vinculo")
        self._confirma(ew, "g-ew")
        self._confirma(sa, "g-sa")
        self._confirma(j1, "g-je")
        self._confirma(j2, "g-je")
        gm.confirmar(self.conn, ja, "g-ja", "FRESHLOG + IAGO REIS - Fiorino", "nome", "ana")
        grupos = _grupo_de(extra={"g-ja": {"name": "FRESHLOG + IAGO REIS - Fiorino"}})
        previa = {p["principal_id"]: p for p in gm.previa_renomear(self.conn, CFG, grupo=grupos)}
        self.assertEqual(len(previa), 5)
        self.assertEqual(previa[ew]["situacao"], "renomear")
        self.assertEqual(previa[ew]["atual"], GRUPOS["g-ew"])
        self.assertEqual(previa[ew]["novo"], "FRESHLOG + EWERTON DE PAULA - REFRIGERADO")
        self.assertEqual(previa[sa]["novo"], "FRESHLOG + SAMUEL SOUZA - FIO -10")
        self.assertEqual(previa[ja]["situacao"], "ja_no_padrao")
        self.assertEqual(previa[j1]["situacao"], "dupla")
        self.assertEqual(previa[j2]["situacao"], "dupla")

    def test_previa_locked_sem_permissao_erro_e_admin(self):
        a = self._mot("Ewerton de Paula")
        b = self._mot("Samuel Souza")
        c = self._mot("Jesse Santos")
        self._confirma(a, "g-ew")
        self._confirma(b, "g-sa")
        self._confirma(c, "g-je")
        admin = [{"id": "5511911112222@c.us", "number": "5511911112222", "isAdmin": True}]
        extra = {"g-ew": {"locked": True},
                 "g-je": {"locked": True, "participants": admin}}

        def _g(cfg, gid):
            return None if gid == "g-sa" else _grupo_de(extra=extra)(cfg, gid)
        # sem o numero conectado nao da pra saber se ele e admin: trancado vai pra lista, com a observacao
        sem = {p["principal_id"]: p for p in gm.previa_renomear(self.conn, CFG, grupo=_g)}
        self.assertEqual({k: p["situacao"] for k, p in sem.items()}, {a: "renomear", b: "erro", c: "renomear"})
        self.assertEqual(sem[a]["observacao"], "grupo trancado: só funciona se o número conectado for administrador")
        self.assertIsNone(sem[b]["observacao"])
        com = {p["principal_id"]: p["situacao"]
               for p in gm.previa_renomear(self.conn, CFG, "(11) 91111-2222", grupo=_g)}
        self.assertEqual(com, {a: "sem_permissao", b: "erro", c: "renomear"})

    def test_renomear_desligado_nao_chama_gateway(self):
        a = self._mot("Ewerton de Paula")
        self._confirma(a, "g-ew")
        chamada = mock.Mock()
        for config in ({"whatsapp_notificacoes": CFG},
                       {"whatsapp_notificacoes": CFG, "atualizacao_motoristas": {"renomear_grupos": False}}):
            res = gm.renomear(self.conn, config, "ana", [a], grupo=_grupo_de(), renomear_grupo=chamada,
                              dormir=lambda s: None)
            self.assertEqual([(r["principal_id"], r["situacao"]) for r in res], [(a, "desligado")])
        chamada.assert_not_called()

    def test_modo_teste_devolve_previa_sem_chamar_gateway(self):
        a = self._mot("Ewerton de Paula")
        self._confirma(a, "g-ew")
        chamada = mock.Mock()
        res = gm.renomear(self.conn, CONFIG_LIGADO, "ana", [a], modo_teste=True, grupo=_grupo_de(),
                          renomear_grupo=chamada, dormir=lambda s: None)
        self.assertEqual(res[0]["situacao"], "renomear")
        chamada.assert_not_called()
        self.assertIsNone(self._tabela()[0]["renomeado_em"])

    def test_renomear_guarda_nome_anterior(self):
        a = self._mot("Ewerton de Paula")
        b = self._mot("Samuel Souza")
        self._confirma(a, "g-ew")
        self._confirma(b, "g-sa")
        chamadas, pausas = [], []
        res = gm.renomear(self.conn, CONFIG_LIGADO, "ana", [a, b], grupo=_grupo_de(),
                          renomear_grupo=lambda cfg, gid, nome: chamadas.append((gid, nome)) or (True, None),
                          dormir=pausas.append)
        self.assertEqual(chamadas, [("g-ew", "FRESHLOG + EWERTON DE PAULA - REFRIGERADO"),
                                    ("g-sa", "FRESHLOG + SAMUEL SOUZA - FIO -10")])
        self.assertEqual(pausas, [3])
        self.assertEqual([r["resultado"] for r in res], ["renomeado", "renomeado"])
        t = {r["bd_motorista_id"]: r for r in self._tabela()}
        self.assertEqual(t[a]["nome_anterior"], GRUPOS["g-ew"])
        self.assertEqual(t[a]["grupo_nome"], "FRESHLOG + EWERTON DE PAULA - REFRIGERADO")
        self.assertTrue(t[a]["renomeado_em"])

    def test_renomear_so_os_pedidos_e_so_situacao_renomear(self):
        a = self._mot("Ewerton de Paula")
        b = self._mot("Samuel Souza")
        self._confirma(a, "g-ew")
        self._confirma(b, "g-sa")
        chamada = mock.Mock(return_value=(True, None))
        res = gm.renomear(self.conn, CONFIG_LIGADO, "ana", [a], grupo=_grupo_de(extra={"g-ew": {"locked": True}}),
                          renomear_grupo=chamada, dormir=lambda s: None, numero_proprio="5511900000000")
        chamada.assert_not_called()
        self.assertEqual([(r["principal_id"], r["resultado"]) for r in res], [(a, "sem_permissao")])

    def test_renomear_falha_no_meio_mantem_os_feitos(self):
        a = self._mot("Ewerton de Paula")
        b = self._mot("Samuel Souza")
        c = self._mot("Iago Reis")
        for pid, gid in ((a, "g-ew"), (b, "g-sa"), (c, "g-zw")):
            self._confirma(pid, gid)

        def _fn(cfg, gid, nome):
            return (False, "HTTP 500") if gid == "g-sa" else (True, None)
        res = gm.renomear(self.conn, CONFIG_LIGADO, "ana", [a, b, c], grupo=_grupo_de(), renomear_grupo=_fn,
                          dormir=lambda s: None)
        self.assertEqual([r["resultado"] for r in res], ["renomeado", "falhou", "renomeado"])
        self.assertEqual(res[1]["erro"], "HTTP 500")
        t = {r["bd_motorista_id"]: r for r in self._tabela()}
        self.assertTrue(t[a]["renomeado_em"])
        self.assertIsNone(t[b]["renomeado_em"])
        self.assertEqual(t[b]["grupo_nome"], GRUPOS["g-sa"])
        self.assertTrue(t[c]["renomeado_em"])

    def test_renomear_503_nome_mudou_registra_como_renomeado(self):
        a = self._mot("Ewerton de Paula")
        self._confirma(a, "g-ew")
        novo = "FRESHLOG + EWERTON DE PAULA - REFRIGERADO"
        lidos = iter([GRUPOS["g-ew"], novo])  # previa, depois a releitura
        res = gm.renomear(self.conn, CONFIG_LIGADO, "ana", [a],
                          grupo=lambda cfg, gid: {"id": gid, "name": next(lidos), "locked": False, "participants": []},
                          renomear_grupo=lambda cfg, gid, nome: (False, "incerto"), dormir=lambda s: None)
        self.assertEqual((res[0]["situacao"], res[0]["resultado"]), ("renomeado", "renomeado"))
        t = self._tabela()[0]
        self.assertEqual((t["nome_anterior"], t["grupo_nome"]), (GRUPOS["g-ew"], novo))
        self.assertTrue(t["renomeado_em"])

    def test_renomear_503_nome_igual_fica_incerto_e_nao_grava(self):
        a = self._mot("Ewerton de Paula")
        self._confirma(a, "g-ew")
        res = gm.renomear(self.conn, CONFIG_LIGADO, "ana", [a], grupo=_grupo_de(),
                          renomear_grupo=lambda cfg, gid, nome: (False, "incerto"), dormir=lambda s: None)
        self.assertEqual((res[0]["situacao"], res[0]["resultado"]), ("incerto", "incerto"))
        t = self._tabela()[0]
        self.assertIsNone(t["renomeado_em"])
        self.assertIsNone(t["nome_anterior"])
        self.assertEqual(t["grupo_nome"], GRUPOS["g-ew"])

    def test_renomear_403_reporta_sem_permissao(self):
        a = self._mot("Ewerton de Paula")
        self._confirma(a, "g-ew")
        res = gm.renomear(self.conn, CONFIG_LIGADO, "ana", [a], grupo=_grupo_de(),
                          renomear_grupo=lambda cfg, gid, nome: (False, "sem_permissao"), dormir=lambda s: None)
        self.assertEqual((res[0]["situacao"], res[0]["resultado"]), ("sem_permissao", "sem_permissao"))
        self.assertIsNone(self._tabela()[0]["renomeado_em"])

    def test_nome_novo_limite_de_100_com_base_de_96_a_100(self):
        for tam in range(96, 101):
            nome = "N" * (tam - len("FRESHLOG + "))
            novo = gm.nome_novo("FRESHLOG + X - VEICULO LONGO", nome)
            self.assertLessEqual(len(novo), 100, tam)
            self.assertTrue(novo.startswith("FRESHLOG + " + nome), tam)
        self.assertEqual(gm.nome_novo("FRESHLOG + X - VEICULO", "N" * 87), "FRESHLOG + " + "N" * 87)  # base 98
        self.assertEqual(len(gm.nome_novo("FRESHLOG + X - " + "v" * 50, "N" * 80)), 100)

    def test_previa_nome_vazio_do_motorista_e_erro(self):
        a = self._mot("")
        self._confirma(a, "g-ew")
        self.assertEqual(gm.previa_renomear(self.conn, CFG, grupo=_grupo_de())[0]["situacao"], "erro")

    def test_renomear_pedido_sem_grupo_confirmado_volta_como_sem_grupo(self):
        a = self._mot("Ewerton de Paula")
        b = self._mot("Samuel")
        self._confirma(a, "g-ew")
        res = gm.renomear(self.conn, CONFIG_LIGADO, "ana", [a, b], grupo=_grupo_de(),
                          renomear_grupo=lambda cfg, gid, nome: (True, None), dormir=lambda s: None)
        self.assertEqual({r["principal_id"]: r["situacao"] for r in res}, {a: "renomeado", b: "sem_grupo"})

    def test_item_malformado_do_gateway_e_ignorado_e_grupos_sem_leitura_contados(self):
        m = self._mot("Samuel", "11999998888")
        lista = [None, "texto", 5] + [{"id": i, "name": n} for i, n in GRUPOS.items()]
        res = gm.sugerir(self.conn, CFG, listar=lambda cfg: lista, grupo=lambda cfg, gid: None)
        self.assertIsNone(res["erro"])
        self.assertEqual(res["grupos_sem_leitura"], len(res["grupos"]))
        self.assertGreater(res["grupos_sem_leitura"], 0)
        self.assertEqual([s["como"] for s in self._pessoa(res, m)["sugestoes"]], ["nome"])
        ok = gm.sugerir(self.conn, CFG, listar=_listar(), grupo=_grupo_de())
        self.assertEqual(ok["grupos_sem_leitura"], 0)

    def test_telefone_de_10_digitos_bate(self):
        m = self._mot("Fulano", "(11) 9999-8888")
        res = gm.sugerir(self.conn, CFG, listar=_listar(), grupo=_grupo_de({"g-sa": ["551199998888"]}))
        self.assertEqual([(s["grupo_id"], s["como"]) for s in self._pessoa(res, m)["sugestoes"]],
                         [("g-sa", "telefone")])

    def test_reconfirmar_mesmo_grupo_preserva_renomeacao_e_trocar_zera(self):
        a = self._mot("Ewerton de Paula")
        self._confirma(a, "g-ew")
        gm.renomear(self.conn, CONFIG_LIGADO, "ana", [a], grupo=_grupo_de(),
                    renomear_grupo=lambda cfg, gid, nome: (True, None), dormir=lambda s: None)
        gm.confirmar(self.conn, a, "g-ew", "novo nome", "nome", "bia")
        t = self._tabela()[0]
        self.assertEqual(t["nome_anterior"], GRUPOS["g-ew"])
        self.assertTrue(t["renomeado_em"])
        gm.confirmar(self.conn, a, "g-sa", GRUPOS["g-sa"], "nome", "bia")
        t = self._tabela()[0]
        self.assertIsNone(t["nome_anterior"])
        self.assertIsNone(t["renomeado_em"])

    # ── Revisao final do ramo ──

    def test_modo_renomear(self):
        wa = dict(CFG, ativo=True)
        casos = (({"whatsapp_notificacoes": wa}, "desligado"),
                 ({"whatsapp_notificacoes": wa, "atualizacao_motoristas": {"renomear_grupos": "false",
                                                                           "forcar_destino_whatsapp": ""}}, "desligado"),
                 ({"whatsapp_notificacoes": wa, "atualizacao_motoristas": {"renomear_grupos": True}}, "desligado"),
                 ({"whatsapp_notificacoes": CFG, "atualizacao_motoristas": {"renomear_grupos": True,
                                                                            "forcar_destino_whatsapp": ""}}, "desligado"),
                 ({"whatsapp_notificacoes": wa, "atualizacao_motoristas": {"renomear_grupos": True,
                                                                           "forcar_destino_whatsapp": "5511900000000"}},
                  "piloto"),
                 (CONFIG_LIGADO, "real"))
        for config, modo in casos:
            self.assertEqual(gm.modo_renomear(config), modo, config)

    def test_renomear_em_piloto_ou_canal_desligado_nao_chama_o_gateway(self):
        """I1: so renomeia de verdade com a chave ligada, o canal ligado e o envio real."""
        a = self._mot("Ewerton de Paula")
        self._confirma(a, "g-ew")
        piloto = {"whatsapp_notificacoes": dict(CFG, ativo=True),
                  "atualizacao_motoristas": {"renomear_grupos": True, "forcar_destino_whatsapp": "5511900000000"}}
        canal_off = {"whatsapp_notificacoes": CFG,
                     "atualizacao_motoristas": {"renomear_grupos": True, "forcar_destino_whatsapp": ""}}
        sem_chave = {"whatsapp_notificacoes": dict(CFG, ativo=True), "atualizacao_motoristas": {"renomear_grupos": True}}
        for config, situacao in ((piloto, "piloto"), (canal_off, "desligado"), (sem_chave, "desligado")):
            chamada, leitura = mock.Mock(return_value=(True, None)), mock.Mock()
            res = gm.renomear(self.conn, config, "ana", [a], grupo=leitura, renomear_grupo=chamada,
                              dormir=lambda s: None)
            self.assertEqual([(r["principal_id"], r["situacao"], r["resultado"]) for r in res], [(a, situacao, situacao)])
            chamada.assert_not_called()
            leitura.assert_not_called()
        self.assertIsNone(self._tabela()[0]["renomeado_em"])

    def test_renomear_grupo_trancado_sem_numero_proprio_tenta_e_403_vira_sem_permissao(self):
        """I2: trancado nao e mais pulado sem o numero conectado; o gateway decide (403 = nao e admin)."""
        a = self._mot("Ewerton de Paula")
        b = self._mot("Samuel Souza")
        self._confirma(a, "g-ew")
        self._confirma(b, "g-sa")
        grupos = _grupo_de(extra={"g-ew": {"locked": True}, "g-sa": {"locked": True}})
        res = gm.renomear(self.conn, CONFIG_LIGADO, "ana", [a, b], grupo=grupos,
                          renomear_grupo=lambda cfg, gid, nome: (True, None) if gid == "g-ew" else (False, "sem_permissao"),
                          dormir=lambda s: None)
        self.assertEqual([(r["principal_id"], r["resultado"]) for r in res], [(a, "renomeado"), (b, "sem_permissao")])

    def test_segunda_renomeacao_preserva_o_primeiro_nome_anterior(self):
        """M9: nome_anterior guarda o nome de antes da primeira renomeacao (pra poder voltar)."""
        a = self._mot("Ewerton de Paula")
        self._confirma(a, "g-ew")
        nomes = {"g-ew": GRUPOS["g-ew"]}

        def _g(cfg, gid):
            return {"id": gid, "name": nomes[gid], "locked": False, "participants": []}

        def _ren(cfg, gid, nome):
            nomes[gid] = nome
            return True, None
        gm.renomear(self.conn, CONFIG_LIGADO, "ana", [a], grupo=_g, renomear_grupo=_ren, dormir=lambda s: None)
        self.conn.execute("UPDATE bd_motoristas SET NOME_MOTORISTA = 'Ewerton de Paula Abreu' WHERE id = ?", (a,))
        self.conn.commit()
        res = gm.renomear(self.conn, CONFIG_LIGADO, "ana", [a], grupo=_g, renomear_grupo=_ren, dormir=lambda s: None)
        self.assertEqual(res[0]["resultado"], "renomeado")
        t = self._tabela()[0]
        self.assertEqual((t["nome_anterior"], t["grupo_nome"]),
                         (GRUPOS["g-ew"], "FRESHLOG + EWERTON DE PAULA ABREU - REFRIGERADO"))

    def test_vinculo_guardado_na_linha_que_deixou_de_ser_a_principal(self):
        """C2 (d): a linha principal mudou (linha sem CPF de menor id entrou na pessoa); o vinculo antigo vale."""
        agora = banco.agora()
        ins = ("INSERT INTO bd_motoristas (NOME_MOTORISTA, CPF_MOTORISTA, AGENT_ID_VUUPT, ATIVO, criado_em, atualizado_em) "
               "VALUES (?, ?, 1, 'SIM', ?, ?)")
        carro2 = self.conn.execute(ins, ("Ewerton de Paula", None, agora, agora)).lastrowid
        real = self.conn.execute(ins, ("Ewerton de Paula", "52998224725", agora, agora)).lastrowid
        self.conn.execute("INSERT INTO motoristas_grupos_whatsapp (bd_motorista_id, grupo_id, grupo_nome, como) "
                          "VALUES (?, 'g-ew', ?, 'telefone')", (real, GRUPOS["g-ew"]))
        self.conn.commit()
        self.assertEqual(next(p for p in atu.pessoas(self.conn) if real in p["ids"])["principal_id"], carro2)

        self.assertEqual([c["principal_id"] for c in gm.candidatos_envio(self.conn)], [carro2])
        res = gm.sugerir(self.conn, CFG, listar=_listar(), grupo=_grupo_de())
        self.assertEqual(self._pessoa(res, carro2)["confirmado"]["grupo_id"], "g-ew")
        previa = gm.previa_renomear(self.conn, CFG, grupo=_grupo_de())
        self.assertEqual([(p["principal_id"], p["situacao"]) for p in previa], [(carro2, "renomear")])
        res = gm.renomear(self.conn, CONFIG_LIGADO, "ana", [carro2], grupo=_grupo_de(),
                          renomear_grupo=lambda cfg, gid, nome: (True, None), dormir=lambda s: None)
        self.assertEqual(res[0]["resultado"], "renomeado")
        self.assertTrue(self._tabela()[0]["renomeado_em"])
        # confirmar de novo pela principal nao deixa dois vinculos pra mesma pessoa
        gm.confirmar(self.conn, carro2, "g-ew", GRUPOS["g-ew"], "manual", "bia")
        t = self._tabela()
        self.assertEqual([(r["bd_motorista_id"], r["grupo_id"]) for r in t], [(carro2, "g-ew")])
        self.assertTrue(t[0]["renomeado_em"])   # mesmo grupo: a renomeacao fica


HOJE = date(2026, 11, 16)
_WA = {"ativo": True, "base_url": "http://x/api", "api_key": "k", "sessao": "s", "grupo_id": "g@g.us"}
CFG_REAL = {"whatsapp_notificacoes": _WA, "atualizacao_motoristas": {"forcar_destino_whatsapp": ""}}
CFG_PILOTO = {"whatsapp_notificacoes": _WA, "atualizacao_motoristas": {"forcar_destino_whatsapp": "5511900000000"}}
CFG_DESLIGADO = {"whatsapp_notificacoes": _WA, "atualizacao_motoristas": {}}


class TestEnvioLink(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._patch = mock.patch.object(banco, "DB_PATH", Path(self._tmp.name) / "t.db")
        self._patch.start()
        self.conn = banco.conectar()

    def tearDown(self):
        self.conn.close()
        self._patch.stop()
        self._tmp.cleanup()

    def _mot(self, nome, cpf="12345678901", agent=None, grupo="g1@g.us", rotas=0, ativo="SIM"):
        agora = banco.agora()
        pid = self.conn.execute(
            "INSERT INTO bd_motoristas (NOME_MOTORISTA, CPF_MOTORISTA, AGENT_ID_VUUPT, ATIVO, criado_em, atualizado_em) "
            "VALUES (?, ?, ?, ?, ?, ?)", (nome, cpf, agent, ativo, agora, agora)).lastrowid
        for i in range(rotas):
            self.conn.execute("INSERT INTO nucleo_rotas (data_rota, agent_id) VALUES (?, ?)",
                              ((HOJE - timedelta(days=i)).isoformat(), agent))
        if grupo:
            gm.confirmar(self.conn, pid, grupo, "FRESHLOG + " + nome, "telefone", "t")
        self.conn.commit()
        return pid

    def _ids(self):
        return [c["principal_id"] for c in gm.candidatos_envio(self.conn, HOJE)]

    def _link(self, pid):
        return self.conn.execute("SELECT * FROM motoristas_links_atualizacao WHERE bd_motorista_id = ? "
                                 "ORDER BY id DESC LIMIT 1", (pid,)).fetchone()

    def _avisar(self, situacoes):
        fila = list(situacoes)
        chamadas = []

        def avisar(link_id, grupo_id, grupo_nome, texto, config, modo_teste=False, **kw):
            chamadas.append({"link_id": link_id, "grupo_id": grupo_id, "texto": texto})
            return fila.pop(0), False
        avisar.chamadas = chamadas
        return avisar

    def test_rotas_30d_so_conta_a_janela(self):
        self._mot("Ana", agent=7, rotas=3)
        self.conn.execute("INSERT INTO nucleo_rotas (data_rota, agent_id) VALUES ('2026-09-01', 7)")
        self.conn.execute("INSERT INTO nucleo_rotas (data_rota, agent_id) VALUES ('2026-11-20', 7)")
        # 30 dias = hoje e os 29 anteriores: 18/10 entra, 17/10 nao
        self.conn.execute("INSERT INTO nucleo_rotas (data_rota, agent_id) VALUES ('2026-10-18', 8)")
        self.conn.execute("INSERT INTO nucleo_rotas (data_rota, agent_id) VALUES ('2026-10-17', 8)")
        self.assertEqual(gm.rotas_30d(self.conn, HOJE), {7: 3, 8: 1})

    def test_candidatos_ordenados_por_rotas_e_sem_cpf_fica_fora(self):
        pouco = self._mot("Bia", agent=1, rotas=1)
        muito = self._mot("Caio", cpf="22222222222", agent=2, rotas=5)
        empate = self._mot("Zeca", cpf="33333333333", agent=3, rotas=1)
        self._mot("Sem Cpf", cpf=None, agent=4, rotas=9)
        self._mot("Sem Grupo", cpf="44444444444", agent=5, grupo=None, rotas=9)
        self._mot("Inativo", cpf="55555555555", agent=6, ativo="NAO", rotas=9)
        res = gm.candidatos_envio(self.conn, HOJE)
        self.assertEqual([c["principal_id"] for c in res], [muito, pouco, empate])
        self.assertEqual(res[0]["rotas_30d"], 5)
        self.assertEqual(res[0]["grupo_nome"], "FRESHLOG + Caio")
        self.assertTrue(res[0]["pendencias"]["documentos"])

    def test_candidato_com_link_em_aberto_fica_fora_mas_link_piloto_nao_conta(self):
        a = self._mot("Ana", agent=1)
        b = self._mot("Bia", cpf="22222222222", agent=2)
        atu.gerar_link(self.conn, a, "t")
        atu.gerar_link(self.conn, b, "t", piloto=True)
        self.assertEqual(self._ids(), [b])

    def test_lote_interrompido_no_teto(self):
        ids = [self._mot("Ana", agent=1, rotas=3), self._mot("Bia", cpf="22222222222", agent=2, rotas=2),
               self._mot("Caio", cpf="33333333333", agent=3, rotas=1)]
        avisar = self._avisar(["enviado", "teto"])
        res = gm.enviar_lote(self.conn, CFG_REAL, ids, "t", "https://x/app", avisar=avisar)
        self.assertEqual([r["situacao"] for r in res], ["enviado", "teto", "nao_tentado"])
        self.assertEqual(len(avisar.chamadas), 2)
        self.assertIsNotNone(atu.link_em_aberto(self.conn, ids[0]))
        self.assertIsNone(atu.link_em_aberto(self.conn, ids[1]))
        self.assertIsNone(atu.link_em_aberto(self.conn, ids[2]))
        self.assertIsNone(self._link(ids[2]))
        self.assertEqual(self._link(ids[1])["envio_situacao"], "teto")
        self.assertIsNone(self._link(ids[1])["enviado_em"])

    def test_lote_grava_destino_e_texto_com_link_do_token(self):
        pid = self._mot("ana maria", agent=1)
        avisar = self._avisar(["enviado"])
        gm.enviar_lote(self.conn, CFG_REAL, [pid], "t", "https://x/app/", avisar=avisar)
        row = self._link(pid)
        self.assertEqual(row["envio_destino"], "g1@g.us")
        self.assertEqual(row["envio_situacao"], "enviado")
        self.assertTrue(row["enviado_em"])
        self.assertEqual(row["piloto"], 0)
        texto = avisar.chamadas[0]["texto"]
        self.assertTrue(texto.startswith("Olá, Ana! "))
        self.assertIn("https://x/app/atualizar/", texto)
        self.assertEqual(avisar.chamadas[0]["link_id"], row["id"])

    def test_lote_piloto_gera_link_piloto_e_nao_tira_do_proximo_lote(self):
        pid = self._mot("Ana", agent=1)
        gm.enviar_lote(self.conn, CFG_PILOTO, [pid], "t", "https://x", avisar=self._avisar(["enviado"]))
        row = self._link(pid)
        self.assertEqual((row["piloto"], row["envio_destino"]), (1, "5511900000000@c.us"))
        self.assertEqual(self._ids(), [pid])

    def test_lote_falha_cancela_o_link_gerado(self):
        a = self._mot("Ana", agent=1, rotas=2)
        b = self._mot("Bia", cpf="22222222222", agent=2, rotas=1)
        res = gm.enviar_lote(self.conn, CFG_REAL, [a, b], "t", "https://x",
                             avisar=self._avisar(["falhou", "enviado"]))
        self.assertEqual([r["situacao"] for r in res], ["falhou", "enviado"])
        self.assertIsNone(atu.link_em_aberto(self.conn, a))
        self.assertIsNotNone(self._link(a)["cancelado_em"])
        self.assertIsNotNone(atu.link_em_aberto(self.conn, b))

    def test_lote_modo_teste_nao_deixa_link_vivo_nem_enviado_em(self):
        pid = self._mot("Ana", agent=1)
        res = gm.enviar_lote(self.conn, CFG_REAL, [pid], "t", "https://x", modo_teste=True,
                             avisar=self._avisar(["modo_teste"]))
        self.assertEqual(res, [{"principal_id": pid, "situacao": "modo_teste"}])
        row = self._link(pid)
        self.assertEqual((row["piloto"], row["envio_situacao"], row["enviado_em"]), (1, "modo_teste", None))
        self.assertIsNotNone(row["cancelado_em"])
        self.assertIsNone(atu.link_em_aberto(self.conn, pid))
        self.assertEqual(self._ids(), [pid])
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM motoristas_links_atualizacao WHERE enviado_em IS NOT NULL").fetchone())

    def test_envio_desligado_nao_gera_nem_cancela_link(self):
        pid = self._mot("Ana", agent=1)
        outro = self._mot("Bia", cpf="22222222222", agent=2)
        aberto = atu.gerar_link(self.conn, outro, "t")
        for config in (CFG_DESLIGADO, {"whatsapp_notificacoes": dict(_WA, ativo=False),
                                       "atualizacao_motoristas": {"forcar_destino_whatsapp": ""}}):
            avisar = self._avisar([])
            res = gm.enviar_lote(self.conn, config, [pid, outro], "t", "https://x", avisar=avisar)
            self.assertEqual([r["situacao"] for r in res], ["desligado", "desligado"])
            self.assertEqual(avisar.chamadas, [])
        self.assertIsNone(self._link(pid))
        self.assertEqual(atu.link_em_aberto(self.conn, outro)["id"], aberto["id"])

    def test_erro_no_meio_cancela_o_link_e_o_lote_segue(self):
        a = self._mot("Ana", agent=1, rotas=2)
        b = self._mot("Bia", cpf="22222222222", agent=2, rotas=1)
        c = self._mot("Caio", cpf="33333333333", agent=3)
        chamadas = []

        def avisar(link_id, grupo_id, grupo_nome, texto, config, modo_teste=False, **kw):
            chamadas.append(link_id)
            if len(chamadas) == 2:
                raise RuntimeError("boom")
            return "enviado", False
        with self.assertLogs("nucleo.grupos_motoristas", level="ERROR"):
            res = gm.enviar_lote(self.conn, CFG_REAL, [a, b, c], "t", "https://x", avisar=avisar)
        self.assertEqual([r["situacao"] for r in res], ["enviado", "falhou", "enviado"])
        self.assertIsNone(atu.link_em_aberto(self.conn, b))
        self.assertIsNotNone(self._link(b)["cancelado_em"])
        self.assertIsNotNone(atu.link_em_aberto(self.conn, a))
        self.assertIsNotNone(atu.link_em_aberto(self.conn, c))

    def test_segundo_lote_seguido_sai_nao_elegivel_e_preserva_o_primeiro_link(self):
        pid = self._mot("Ana", agent=1)
        gm.enviar_lote(self.conn, CFG_REAL, [pid], "t", "https://x", avisar=self._avisar(["enviado"]))
        primeiro = atu.link_em_aberto(self.conn, pid)["id"]
        avisar = self._avisar([])
        res = gm.enviar_lote(self.conn, CFG_REAL, [pid], "t", "https://x", avisar=avisar)
        self.assertEqual(res, [{"principal_id": pid, "situacao": "nao_elegivel"}])
        self.assertEqual(atu.link_em_aberto(self.conn, pid)["id"], primeiro)
        self.assertEqual(avisar.chamadas, [])

    def test_elegibilidade_conferida_na_hora_de_cada_pessoa(self):
        a = self._mot("Ana", agent=1, rotas=2)
        b = self._mot("Bia", cpf="22222222222", agent=2, rotas=1)
        feito = []

        def avisar(link_id, grupo_id, grupo_nome, texto, config, modo_teste=False, **kw):
            if not feito:  # "outro usuario" gera o link da Bia enquanto a Ana e enviada
                feito.append(atu.gerar_link(self.conn, b, "outro")["id"])
            return "enviado", False
        res = gm.enviar_lote(self.conn, CFG_REAL, [a, b], "t", "https://x", avisar=avisar)
        self.assertEqual([r["situacao"] for r in res], ["enviado", "nao_elegivel"])
        self.assertEqual(atu.link_em_aberto(self.conn, b)["id"], feito[0])

    def test_canal_em_pausa_interrompe_o_lote(self):
        ids = [self._mot("Ana", agent=1, rotas=2), self._mot("Bia", cpf="22222222222", agent=2, rotas=1)]
        res = gm.enviar_lote(self.conn, CFG_REAL, ids, "t", "https://x", avisar=self._avisar(["canal_em_pausa"]))
        self.assertEqual([r["situacao"] for r in res], ["canal_em_pausa", "nao_tentado"])

    def test_nao_candidato_sai_nao_elegivel_sem_gerar_link(self):
        pid = self._mot("Sem Grupo", agent=1, grupo=None)
        avisar = self._avisar([])
        res = gm.enviar_lote(self.conn, CFG_REAL, [pid, 9999], "t", "https://x", avisar=avisar)
        self.assertEqual([r["situacao"] for r in res], ["nao_elegivel", "nao_elegivel"])
        self.assertEqual(avisar.chamadas, [])
        self.assertIsNone(self._link(pid))

    def test_dupla_no_mesmo_grupo_recebe_duas_mensagens(self):
        a = self._mot("Jesse Santos", agent=1, grupo="dupla@g.us", rotas=2)
        b = self._mot("Weslei Lima", cpf="22222222222", agent=2, grupo="dupla@g.us", rotas=1)
        avisar = self._avisar(["enviado", "enviado"])
        gm.enviar_lote(self.conn, CFG_REAL, [a, b], "t", "https://x", avisar=avisar)
        self.assertEqual([c["grupo_id"] for c in avisar.chamadas], ["dupla@g.us", "dupla@g.us"])
        self.assertTrue(avisar.chamadas[0]["texto"].startswith("Olá, Jesse!"))
        self.assertTrue(avisar.chamadas[1]["texto"].startswith("Olá, Weslei!"))
        self.assertNotEqual(avisar.chamadas[0]["link_id"], avisar.chamadas[1]["link_id"])

    def test_lote_com_vinculo_na_linha_que_deixou_de_ser_a_principal(self):
        """C2 (d): o lote acha o grupo pela pessoa, nao so pelo id exato da principal."""
        agora = banco.agora()
        carro2 = self.conn.execute(
            "INSERT INTO bd_motoristas (NOME_MOTORISTA, AGENT_ID_VUUPT, ATIVO, criado_em, atualizado_em) "
            "VALUES ('Ana Carro 2', 1, 'SIM', ?, ?)", (agora, agora)).lastrowid
        real = self._mot("Ana", agent=1, grupo="g-ana@g.us")
        avisar = self._avisar(["enviado"])
        res = gm.enviar_lote(self.conn, CFG_REAL, [carro2], "t", "https://x", avisar=avisar)
        self.assertEqual(res, [{"principal_id": carro2, "situacao": "enviado"}])
        self.assertEqual(avisar.chamadas[0]["grupo_id"], "g-ana@g.us")
        self.assertIsNotNone(atu.link_em_aberto(self.conn, real))

if __name__ == "__main__":
    unittest.main()
