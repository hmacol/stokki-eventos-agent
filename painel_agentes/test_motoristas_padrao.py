# -*- coding: utf-8 -*-
"""
Tela /motoristas, padrão único de cadastro (16/11/2026): selos de pendência
por pessoa, ficha, ações da linha (ativo, PIN, excluir, link) e o bloco
WhatsApp (grupos e envio do link de atualização). Banco temporário, sem rede.

    cd painel_agentes && py -3.11 -m unittest test_motoristas_padrao -v
"""
import builtins
import hashlib
import importlib.util
import io
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

_CONFIG_FALSO = "painel_agentes:\n  usuario: u\n  senha: s\n  secret_key: chave-de-teste\n"
_open_real = builtins.open


def _open_sem_config(arq, *a, **kw):
    # O worktree nao tem config.yaml (segredo): o painel so precisa dele pra subir.
    if Path(str(arq)).name == "config.yaml" and not Path(str(arq)).exists():
        return io.StringIO(_CONFIG_FALSO)
    return _open_real(arq, *a, **kw)


_spec = importlib.util.spec_from_file_location("painel_agentes_app", _AQUI / "painel_agentes.py")
painel_agentes = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = painel_agentes
with mock.patch.object(builtins, "open", _open_sem_config):
    _spec.loader.exec_module(painel_agentes)

import acesso_telas  # noqa: E402
import motoristas as mot  # noqa: E402
from nucleo import atualizacao_motorista as atu, auth_motorista as auth, banco  # noqa: E402
from nucleo import grupos_motoristas as grupos  # noqa: E402
from regras import motoristas_banco as mb  # noqa: E402

ORIGEM = {"Origin": "http://localhost", "Referer": "http://localhost/"}
CPF_A = "52998224725"
CPF_B = "11144477735"


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        db = Path(self._tmp.name) / "t.db"
        for alvo in (banco, acesso_telas):
            p = mock.patch.object(alvo, "DB_PATH", db)
            p.start()
            self.addCleanup(p.stop)
        self.config = {"painel_agentes": {"usuario": "u", "senha": "s"},
                       "motoristas": {"planilha": str(Path(self._tmp.name) / "BD.xlsx")}}
        p = mock.patch.object(painel_agentes, "_carregar_config", side_effect=lambda: self.config)
        p.start()
        self.addCleanup(p.stop)
        painel_agentes.app.config["TESTING"] = True
        self.cliente = painel_agentes.app.test_client()
        self.conn = banco.conectar()
        self.addCleanup(self.conn.close)
        # Ana: duas linhas (dois carros), mesmo CPF = uma pessoa; Bia: sem CPF; Caio: inativo
        self.ana1 = self._linha("Ana Souza", CPF_A, 11, PLACA="AAA1A11")
        self.ana2 = self._linha("Ana Souza", CPF_A, 12, PLACA="AAA2A22")
        self.bia = self._linha("Bia Lima", None, 21)
        self.caio = self._linha("Caio Inativo", CPF_B, 31, ATIVO="NAO")

    def _linha(self, nome, cpf, agent, **extra):
        linha = {c: None for c in mb.COLUNAS}
        linha.update(AGENT_ID_VUUPT=agent, NOME_MOTORISTA=nome, CPF_MOTORISTA=cpf, ATIVO="SIM",
                     ACEITA_VIAGENS="SIM", ZONAS_PREFERIDAS="ZONA SUL", DIAS_DISPONIVEIS="SEGUNDA",
                     MAX_ROTAS_DIA=1, TELEFONE_MOTORISTA="11998765432")
        linha.update(extra)
        return mb.inserir(self.conn, linha, "teste")

    def _logar(self, nivel):
        with self.cliente.session_transaction() as sess:
            sess["nivel_acesso"] = nivel
            sess["usuario"] = "hugo"

    def _post(self, url, body=None):
        return self.cliente.post(url, json=body or {}, headers=ORIGEM)

    def _get(self, url):
        # fetch() GET da mesma origem manda Referer (não Origin)
        return self.cliente.get(url, headers={"Referer": "http://localhost/motoristas"})

    def _linha_da_tela(self, dados, id_):
        return next(m for m in dados["motoristas"] if m["id"] == id_)


class TestPagina(_Base):
    def test_pagina_traz_pendencias_e_total_por_pessoa(self):
        self.conn.execute("INSERT INTO nucleo_rotas (data_rota, agent_id) VALUES (?, 11)", (date.today().isoformat(),))
        self.conn.execute("INSERT INTO nucleo_rotas (data_rota, agent_id) VALUES (?, 11)", (date.today().isoformat(),))
        self.conn.execute("INSERT INTO motoristas_grupos_whatsapp (bd_motorista_id, grupo_id, grupo_nome, como) "
                          "VALUES (?, 'g1@g.us', 'FRESHLOG + ANA - FIORINO', 'telefone')", (self.ana1,))
        # ficha antiga sem vínculo: a carga da tela liga pelo agent_id
        cid = self.conn.execute("INSERT INTO motoristas_cadastros (cpf, nome, chave_envio, status, origem, agent_id, "
                                "criado_em) VALUES (?, 'Ana', 'k', 'APROVADO', 'app', 12, ?)",
                                (CPF_A, banco.agora())).lastrowid
        self.conn.commit()
        atu.gerar_link(self.conn, self.ana1, "hugo")

        dados = mot.dados_pagina_motoristas()

        self.assertEqual(dados["total_pendentes"], 2)   # Ana (uma pessoa, duas linhas) + Bia; Caio inativo
        a1, a2 = self._linha_da_tela(dados, self.ana1), self._linha_da_tela(dados, self.ana2)
        self.assertEqual((a1["principal_id"], a2["principal_id"]), (self.ana1, self.ana1))
        self.assertTrue(a1["tem_pendencia"] and a2["tem_pendencia"])
        self.assertEqual(a1["pendencias"]["documentos"], ["cnh", "crlv", "selfie", "antt", "residencia"])
        self.assertEqual((a1["rotas_30d"], a2["rotas_30d"]), (2, 2))   # por pessoa: igual nas duas linhas
        self.assertEqual(a1["grupo_nome"], "FRESHLOG + ANA - FIORINO")
        self.assertEqual(a1["estado_link"], "enviado")
        self.assertTrue(a1["tem_ficha"])
        self.assertFalse(a1["tem_login"])
        bia = self._linha_da_tela(dados, self.bia)
        self.assertTrue(bia["pendencias"]["falta_cpf"])
        caio = self._linha_da_tela(dados, self.caio)
        self.assertEqual((caio["pendencias"], caio["tem_pendencia"]), ({}, False))
        vinculo = self.conn.execute("SELECT bd_motorista_id FROM motoristas_cadastros WHERE id = ?", (cid,)).fetchone()[0]
        self.assertEqual(vinculo, self.ana1)

        self._logar("operador")
        html = self.cliente.get("/motoristas").get_data(as_text=True)
        self.assertIn("2 motoristas com pendência", html)
        self.assertIn('<span class="badge badge-erro">Residência</span>', html)
        self.assertIn("falta CPF para enviar no grupo", html)
        self.assertIn("link enviado", html)
        self.assertIn("Rotas 30 d", html)

    def test_falha_no_calculo_de_pendencias_nao_derruba_a_tela(self):
        self._logar("total")
        with mock.patch.object(atu, "pendencias", side_effect=RuntimeError("boom")):
            r = self.cliente.get("/motoristas")
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn("Ana Souza", html)
        self.assertNotIn("Falha ao carregar motoristas", html)

    def test_nivel_leitura_nao_ve_botoes(self):
        auth.criar_ou_atualizar_motorista(self.conn, CPF_B, "Caio Inativo", pin="123456", agent_id=31)
        self._logar("leitura")
        html = self.cliente.get("/motoristas").get_data(as_text=True)
        self.assertIn("Ana Souza", html)
        for escondido in ("Resetar PIN", "Excluir", "Ver ficha", "Copiar link", "Identificar grupos"):
            self.assertNotIn(escondido, html)
        self._logar("operador")
        html = self.cliente.get("/motoristas").get_data(as_text=True)
        self.assertIn(">Resetar PIN</button>", html)     # Caio tem login
        self.assertIn("Ver ficha", html)
        self.assertNotIn(">Excluir</button>", html)
        self.assertNotIn("Identificar grupos", html)
        self._logar("total")
        html = self.cliente.get("/motoristas").get_data(as_text=True)
        self.assertIn(">Excluir</button>", html)
        self.assertIn("Identificar grupos", html)
        self.assertIn('title="falta o CPF"', html)        # Bia: sem CPF, botão desabilitado
        self.assertIn("Criar acesso ao app", html)        # Ana: CPF sem login


class TestAcoes(_Base):
    def test_operador_nao_exclui_403(self):
        self._logar("operador")
        self.assertEqual(self.cliente.delete(f"/api/motoristas/{self.bia}", headers=ORIGEM).status_code, 403)
        self.assertTrue(any(l["id"] == self.bia for l in mb.listar(self.conn)))
        self._logar("total")
        r = self.cliente.delete(f"/api/motoristas/{self.bia}", headers=ORIGEM)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertFalse(any(l["id"] == self.bia for l in mb.listar(self.conn)))

    def test_resetar_pin_devolve_pin_uma_vez(self):
        self._logar("operador")
        r = self._post(f"/api/motoristas/{self.ana1}/resetar-pin")
        self.assertEqual(r.status_code, 200, r.get_json())
        pin = r.get_json()["pin"]
        self.assertRegex(pin, r"^\d{6}$")
        login = auth.buscar_motorista(self.conn, CPF_A)
        self.assertEqual(login["trocar_pin"], 1)
        self.assertNotIn(pin, (login["pin_hash"], login["pin_salt"]))
        historico = [dict(h) for h in self.conn.execute("SELECT * FROM bd_motoristas_alteracoes")]
        self.assertFalse(any(pin in str(v) for h in historico for v in h.values()))
        r = self._post(f"/api/motoristas/{self.bia}/resetar-pin")
        self.assertEqual((r.status_code, r.get_json()["erro"]), (400, "falta o CPF"))
        self._logar("leitura")
        self.assertIn(self._post(f"/api/motoristas/{self.ana1}/resetar-pin").status_code, (302, 403))

    def test_inativar_pelo_atalho(self):
        self._logar("operador")
        r = self._post(f"/api/motoristas/{self.bia}/ativo", {"ativo": False})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(next(l for l in mb.listar(self.conn) if l["id"] == self.bia)["ATIVO"], "NAO")


class TestLinkEFicha(_Base):
    def test_link_atualizacao_devolve_url(self):
        self._logar("operador")
        r = self._post(f"/api/motoristas/{self.ana2}/link-atualizacao")
        self.assertEqual(r.status_code, 200, r.get_json())
        d = r.get_json()
        prefixo = "https://app.freshhub.com.br/motorista/atualizar/"
        self.assertTrue(d["url"].startswith(prefixo), d["url"])
        self.assertFalse(d["pagina_ligada"])
        token = d["url"][len(prefixo):]
        row = self.conn.execute("SELECT bd_motorista_id, token_hash FROM motoristas_links_atualizacao").fetchone()
        self.assertEqual((row[0], row[1]), (self.ana1, hashlib.sha256(token.encode()).hexdigest()))
        html = self.cliente.get("/motoristas").get_data(as_text=True)
        self.assertNotIn(token, html)

        self.config["atualizacao_motoristas"] = {"ativo": True}
        self.assertTrue(self._post(f"/api/motoristas/{self.ana1}/link-atualizacao").get_json()["pagina_ligada"])
        self.assertEqual(self._post("/api/motoristas/9999/link-atualizacao").status_code, 404)
        r = self._post(f"/api/motoristas/{self.caio}/link-atualizacao")   # inativo
        self.assertEqual((r.status_code, r.get_json()["erro"]), (409, "Motorista inativo: reative antes de gerar o link."))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM motoristas_links_atualizacao WHERE bd_motorista_id = ?",
                                           (self.caio,)).fetchone()[0], 0)
        self._logar("leitura")
        self.assertIn(self._post(f"/api/motoristas/{self.ana1}/link-atualizacao").status_code, (302, 403))

    def test_validade_malformada_no_config_vale_15_dias(self):
        self._logar("operador")
        for valor in ("abc", None, 0, -3, [1]):
            self.config["atualizacao_motoristas"] = {"validade_dias": valor}
            r = self._post(f"/api/motoristas/{self.ana1}/link-atualizacao")
            self.assertEqual(r.status_code, 200, valor)
            self.assertEqual(r.get_json()["expira_em"][:10], (date.today() + timedelta(days=15)).isoformat(), valor)
        self.config["atualizacao_motoristas"] = {"validade_dias": "7"}
        r = self._post(f"/api/motoristas/{self.ana1}/link-atualizacao")
        self.assertEqual(r.get_json()["expira_em"][:10], (date.today() + timedelta(days=7)).isoformat())

    def test_ficha_traz_pendencias_e_esconde_pix_de_quem_nao_e_total(self):
        self.conn.execute("INSERT INTO motoristas_cadastros (cpf, nome, chave_envio, status, origem, bd_motorista_id, "
                          "chave_pix, data_nascimento, vuupt_email, vuupt_senha_digitos, criado_em) VALUES (?, 'Ana', 'k', "
                          "'APROVADO', 'app', ?, 'pix-da-ana', '1990-01-02', 'ana@freshhub.com.br', 6, ?)",
                          (CPF_A, self.ana1, banco.agora()))
        self.conn.commit()
        self._logar("total")
        d = self._get(f"/api/motoristas/{self.ana2}/ficha").get_json()
        self.assertEqual(d["ficha"]["chave_pix"], "pix-da-ana")
        for fora in ("chave_envio", "vuupt_senha_digitos", "vuupt_email", "motivo", "cnh_arquivo"):   # lista fechada
            self.assertNotIn(fora, d["ficha"])
        self.assertFalse(d["ficha"]["tem_cnh"])
        self.assertIn("cnh", d["pendencias"]["documentos"])
        self._logar("operador")
        d = self._get(f"/api/motoristas/{self.ana1}/ficha").get_json()
        self.assertNotIn("chave_pix", d["ficha"])
        self.assertNotIn("data_nascimento", d["ficha"])
        for fora in ("vuupt_email", "vuupt_senha_digitos", "motivo", "cnh_arquivo"):
            self.assertNotIn(fora, d["ficha"])
        self.assertEqual(d["ficha"]["cpf"], CPF_A)
        self.assertFalse(d["documentos_visiveis"])
        d = self._get(f"/api/motoristas/{self.bia}/ficha").get_json()
        self.assertIsNone(d["ficha"])
        self.assertTrue(d["pendencias"]["falta_cpf"])
        self.assertEqual(self._get("/api/motoristas/9999/ficha").status_code, 404)
        self.assertEqual(self.cliente.get(f"/api/motoristas/{self.ana1}/ficha").status_code, 403)   # sem Referer
        self._logar("leitura")
        self.assertIn(self._get(f"/api/motoristas/{self.ana1}/ficha").status_code, (302, 403))

    def test_ficha_mostra_o_pix_vigente_do_login(self):
        """C1: PIX trocado no app ("Meus dados") depois da aprovação: a ficha mostra o do login."""
        self.conn.execute("INSERT INTO motoristas_cadastros (cpf, nome, chave_envio, status, origem, bd_motorista_id, "
                          "chave_pix, criado_em) VALUES (?, 'Ana', 'k', 'APROVADO', 'app', ?, 'pix-A', ?)",
                          (CPF_A, self.ana1, banco.agora()))
        self.conn.commit()
        auth.criar_ou_atualizar_motorista(self.conn, CPF_A, "Ana Souza", pin="246802", agent_id=11)
        self.conn.execute("UPDATE motoristas SET chave_pix = 'pix-B' WHERE cpf = ?", (CPF_A,))
        self.conn.commit()
        self._logar("total")
        self.assertEqual(self._get(f"/api/motoristas/{self.ana1}/ficha").get_json()["ficha"]["chave_pix"], "pix-B")
        self._logar("operador")
        self.assertNotIn("chave_pix", self._get(f"/api/motoristas/{self.ana1}/ficha").get_json()["ficha"])

    def test_carro_2_sem_cpf_entra_na_pessoa_e_mostra_os_documentos_dela(self):
        """C2: linha sem CPF no mesmo agente da Ana (caso "CARRO 2"): mesma pessoa, mesma ficha."""
        carro2 = self._linha("ANA CARRO 2", None, 11)
        cid = self.conn.execute("INSERT INTO motoristas_cadastros (cpf, nome, chave_envio, status, origem, agent_id, "
                                "criado_em) VALUES (?, 'Ana', 'k', 'APROVADO', 'app', 11, ?)",
                                (CPF_A, banco.agora())).lastrowid
        self.conn.commit()
        dados = mot.dados_pagina_motoristas()
        self.assertEqual(self._linha_da_tela(dados, carro2)["principal_id"], self.ana1)
        self.assertEqual(self.conn.execute("SELECT bd_motorista_id FROM motoristas_cadastros WHERE id = ?",
                                           (cid,)).fetchone()[0], self.ana1)
        self._logar("total")
        self.assertEqual(self._get(f"/api/motoristas/{carro2}/ficha").get_json()["ficha"]["id"], cid)


class TestUrlAppMotorista(unittest.TestCase):
    def test_deriva_do_url_painel(self):
        self.assertEqual(mot.url_app_motorista({}), "https://app.freshhub.com.br/motorista")
        cfg = lambda u: {"portal_cliente": {"chamados": {"url_painel": u}}}  # noqa: E731
        self.assertEqual(mot.url_app_motorista(cfg("https://homolog.freshhub.com.br/painel/")),
                         "https://homolog.freshhub.com.br/motorista")
        self.assertEqual(mot.url_app_motorista(cfg("https://outro.exemplo/painel-x")),
                         "https://app.freshhub.com.br/motorista")


class TestWhatsApp(_Base):
    def setUp(self):
        super().setUp()
        self.conn.execute("INSERT INTO motoristas_grupos_whatsapp (bd_motorista_id, grupo_id, grupo_nome, como) "
                          "VALUES (?, 'g1@g.us', 'FRESHLOG + ANA - FIORINO', 'telefone')", (self.ana1,))
        self.conn.commit()

    def test_envio_links_so_total(self):
        for nivel in ("operador", "logistica", "leitura"):
            self._logar(nivel)
            self.assertIn(self._get("/api/motoristas/envio-links/candidatos").status_code, (302, 403), nivel)
            with mock.patch.object(grupos, "enviar_lote") as lote:
                self.assertIn(self._post("/api/motoristas/envio-links", {"principal_ids": [self.ana1]}).status_code,
                              (302, 403), nivel)
            lote.assert_not_called()
            for url in ("/api/motoristas/grupos/sugestoes", "/api/motoristas/grupos/previa-renomear"):
                self.assertIn(self._get(url).status_code, (302, 403), (nivel, url))
            for url in ("/api/motoristas/grupos/confirmar", "/api/motoristas/grupos/renomear"):
                self.assertIn(self._post(url, {}).status_code, (302, 403), (nivel, url))

        self._logar("total")
        with mock.patch.object(grupos, "enviar_lote") as lote:
            r = self._post("/api/motoristas/envio-links", {"principal_ids": [self.ana1]})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["erro"], "A página de atualização está desligada (atualizacao_motoristas.ativo). "
                                               "Ligue antes de enviar links.")
        lote.assert_not_called()

        self.config["atualizacao_motoristas"] = {"ativo": True, "forcar_destino_whatsapp": ""}
        with mock.patch.object(grupos, "enviar_lote",
                               return_value=[{"principal_id": self.ana1, "situacao": "enviado"}]) as lote:
            r = self._post("/api/motoristas/envio-links", {"principal_ids": [self.ana1]})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["resultado"], [{"principal_id": self.ana1, "situacao": "enviado"}])
        args = lote.call_args.args
        self.assertEqual((args[2], args[3], args[4]), ([self.ana1], "hugo", "https://app.freshhub.com.br/motorista"))
        self.assertIn("forcar_destino_whatsapp", args[1]["atualizacao_motoristas"])   # config inteiro
        self.config["atualizacao_motoristas"]["validade_dias"] = "quinze"
        with mock.patch.object(grupos, "enviar_lote", return_value=[]) as lote:
            self.assertEqual(self._post("/api/motoristas/envio-links", {"principal_ids": [self.ana1]}).status_code, 200)
        self.assertEqual(lote.call_args.args[1]["atualizacao_motoristas"]["validade_dias"], 15)
        self.assertEqual(self._post("/api/motoristas/envio-links", {"principal_ids": "x"}).status_code, 400)

    def test_candidatos_trazem_o_modo_sem_o_numero(self):
        self._logar("total")
        wa = {"ativo": True, "base_url": "http://gw.invalido", "api_key": "k", "sessao": "s"}
        casos = (({}, wa, "desligado"),
                 ({"forcar_destino_whatsapp": ""}, wa, "desligado"),                     # página desligada
                 ({"ativo": True}, wa, "desligado"),                                     # chave ausente
                 ({"ativo": True, "forcar_destino_whatsapp": ""}, {}, "desligado"),      # WhatsApp desligado
                 ({"ativo": True, "forcar_destino_whatsapp": "5511912345678"}, wa, "piloto"),
                 ({"ativo": True, "forcar_destino_whatsapp": ""}, wa, "real"))
        for cfg_atu, cfg_wa, modo in casos:
            self.config["atualizacao_motoristas"] = cfg_atu
            self.config["whatsapp_notificacoes"] = cfg_wa
            r = self._get("/api/motoristas/envio-links/candidatos")
            self.assertEqual(r.status_code, 200)
            d = r.get_json()
            self.assertEqual(d["modo"], modo, cfg_atu)
            self.assertNotIn("5511912345678", r.get_data(as_text=True))
        self.assertEqual([c["principal_id"] for c in d["candidatos"]], [self.ana1])

    def test_grupos_sugestoes_confirmar_e_previa(self):
        self._logar("total")
        sugestao = {"erro": None, "pessoas": [], "grupos": [], "grupos_sem_leitura": 3}
        with mock.patch.object(grupos, "sugerir", return_value=sugestao) as sug:
            r = self._get("/api/motoristas/grupos/sugestoes")
        self.assertEqual(r.get_json(), sugestao)
        self.assertEqual(sug.call_args.args[1], {})   # whatsapp_notificacoes ausente
        r = self._post("/api/motoristas/grupos/confirmar", {"itens": [
            {"principal_id": self.bia, "grupo_id": "g2@g.us", "grupo_nome": "FRESH + BIA", "como": "nome"},
            {"principal_id": self.ana1, "grupo_id": "", "grupo_nome": "", "como": ""}]})
        self.assertEqual(r.status_code, 200, r.get_json())
        vinculos = {r[0]: (r[1], r[2]) for r in self.conn.execute(
            "SELECT bd_motorista_id, grupo_id, confirmado_por FROM motoristas_grupos_whatsapp")}
        self.assertEqual(vinculos, {self.bia: ("g2@g.us", "hugo")})
        self.assertEqual(self._post("/api/motoristas/grupos/confirmar", {"itens": [{"grupo_id": "x"}]}).status_code, 400)
        antes = list(self.conn.execute("SELECT * FROM motoristas_grupos_whatsapp ORDER BY bd_motorista_id"))
        ok = {"principal_id": self.caio, "grupo_id": "g3@g.us", "grupo_nome": "FRESH + CAIO", "como": "telefone"}
        for ruim, trecho in (({**ok, "principal_id": self.ana2}, str(self.ana2)),     # linha que não é a principal
                             ({**ok, "principal_id": 9999}, "9999"),                 # id que não existe
                             ({**ok, "como": "chute"}, "como")):
            r = self._post("/api/motoristas/grupos/confirmar", {"itens": [ok, ruim]})
            self.assertEqual(r.status_code, 400, ruim)
            self.assertIn(trecho, r.get_json()["erro"])
            self.assertEqual(list(self.conn.execute("SELECT * FROM motoristas_grupos_whatsapp ORDER BY bd_motorista_id")),
                             antes)   # nada gravado, nem o item bom

        with mock.patch.object(grupos, "previa_renomear", return_value=[]):
            self.assertFalse(self._get("/api/motoristas/grupos/previa-renomear").get_json()["ligado"])
            self.config["atualizacao_motoristas"] = {"renomear_grupos": True}
            self.assertTrue(self._get("/api/motoristas/grupos/previa-renomear").get_json()["ligado"])
        with mock.patch.object(grupos, "renomear", return_value=[{"principal_id": self.bia, "situacao": "renomeado"}]) as ren:
            r = self._post("/api/motoristas/grupos/renomear", {"principal_ids": [self.bia]})
        self.assertEqual(r.get_json()["resultado"][0]["situacao"], "renomeado")
        self.assertEqual(ren.call_args.args[2:4], ("hugo", [self.bia]))

    def test_previa_renomear_traz_o_modo(self):
        """I1: o botão só habilita em "real"; piloto ou canal desligado só mostram a prévia."""
        self._logar("total")
        wa = {"ativo": True, "base_url": "http://gw.invalido", "api_key": "k", "sessao": "s"}
        casos = (({}, wa, "desligado"),
                 ({"renomear_grupos": True}, wa, "desligado"),                                    # chave ausente
                 ({"renomear_grupos": True, "forcar_destino_whatsapp": ""}, {}, "desligado"),     # WhatsApp desligado
                 ({"renomear_grupos": True, "forcar_destino_whatsapp": "5511912345678"}, wa, "piloto"),
                 ({"renomear_grupos": True, "forcar_destino_whatsapp": ""}, wa, "real"))
        for cfg_atu, cfg_wa, modo in casos:
            self.config["atualizacao_motoristas"] = cfg_atu
            self.config["whatsapp_notificacoes"] = cfg_wa
            with mock.patch.object(grupos, "previa_renomear", return_value=[]):
                r = self._get("/api/motoristas/grupos/previa-renomear")
            self.assertEqual(r.get_json()["modo"], modo, cfg_atu)
            self.assertNotIn("5511912345678", r.get_data(as_text=True))

    def test_tela_tem_os_avisos_do_lote_e_da_renomeacao(self):
        """M7 e I1: textos da janela (o JS só monta com el(), sem innerHTML)."""
        self._logar("total")
        html = self.cliente.get("/motoristas").get_data(as_text=True)
        for texto in ("Leva cerca de 20 segundos por mensagem. Não feche a página.",
                      "Piloto: a renomeação só mostra a prévia.",
                      "RENOMEAÇÃO REAL: os grupos mudam de nome para todos os participantes.",
                      'piloto: "piloto: nada renomeado (só a prévia)"'):
            self.assertIn(texto, html)


if __name__ == "__main__":
    unittest.main()
