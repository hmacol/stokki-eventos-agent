# -*- coding: utf-8 -*-
"""
test_cadastro_motorista_link.py

Processador do cadastro pelo link: agente e veículo na Vuupt, linha na
bd_motoristas, aviso no grupo e boas-vindas, sem duplicar nada em
retentativa. Vuupt falsa em memória; avisos como mock; SQLite temporário.
    py -3.11 -m unittest nucleo.test_cadastro_motorista_link -v
"""
import sys
import tempfile
import unittest
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))

from nucleo import banco, cadastro_motorista as cad, cadastro_motorista_link as proc  # noqa: E402
from nucleo.vuupt_agentes import ErroVuupt  # noqa: E402
from regras import motoristas_banco  # noqa: E402

AGORA = datetime(2026, 11, 2, 10, 0, 0)
CONFIG = {"vuupt_api": {"token": "x"}, "cadastro_motorista_link": {"ativo": True},
          "portal_cliente": {"chamados": {"url_painel": "https://painel.teste/painel/"}}}
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 50


def _cpf_com_dv(base9: str) -> str:
    d = base9
    for n in (9, 10):
        soma = sum(int(d[i]) * (n + 1 - i) for i in range(n))
        d += str((soma * 10 % 11) % 10)
    return d


CPF = _cpf_com_dv("012345678")   # começa com zero


def _dados(**extra):
    d = {
        "nome": "João Pedro Santos", "cpf": CPF, "data_nascimento": "1990-05-17", "telefone": "(11) 99876-5432",
        "chave_pix": "joao@exemplo.com", "emite_nf": True, "dias": ["SEGUNDA", "TERCA"], "zonas": ["ZONA SUL"],
        "posicao": "FIXO", "celular": "ANDROID", "tipo_veiculo": "fiorino", "placa": "abc-1d23",
        "veiculo_modelo": "Fiat Fiorino", "veiculo_ano": 2020, "refrigerado": False, "dono_veiculo": True,
    }
    d.update(extra)
    return d


class VuuptFalsa:
    """Guarda agentes/veículos criados; `falhas[metodo]` é uma fila de
    exceções levantadas nas próximas chamadas; `timeout_apos_post` registra
    o agente e levanta erro de rede (a Vuupt criou, a resposta se perdeu).
    E-mail é único (422 email); agente com `_oculto` não sai na lista
    (paginação truncada ou atraso da Vuupt)."""

    def __init__(self, agentes=None, veiculos=None):
        self.agentes = list(agentes or [])
        self.veiculos = list(veiculos or [])
        self.chamadas = Counter()
        self.posts_agente = []
        self.ligados = []
        self.apagados = []
        self.falhas = {}
        self.timeout_apos_post = False
        self._prox = 5001

    def _talvez_falhar(self, metodo):
        self.chamadas[metodo] += 1
        fila = self.falhas.get(metodo)
        if fila:
            raise fila.pop(0)

    def _novo_id(self):
        self._prox += 1
        return self._prox - 1

    def listar_agentes(self):
        self._talvez_falhar("listar_agentes")
        return [dict(a) for a in self.agentes if not a.get("_oculto")]

    def listar_veiculos(self):
        self._talvez_falhar("listar_veiculos")
        return [dict(v) for v in self.veiculos]

    def criar_agente(self, nome, email, senha, telefone):
        self.posts_agente.append({"nome": nome, "email": email, "senha": senha, "telefone": telefone})
        self._talvez_falhar("criar_agente")
        if any(str(a.get("email") or "").lower() == email.lower() for a in self.agentes):
            raise ErroVuupt("POST /users: HTTP 422", 422, '{"errors":{"email":["already taken"]}}')
        novo = {"id": self._novo_id(), "name": nome, "email": email, "phone_number": "+55" + telefone}
        self.agentes.append(novo)
        if self.timeout_apos_post:
            self.timeout_apos_post = False
            raise ErroVuupt("POST /users: Read timed out", None, "Read timed out")
        return novo["id"]

    def criar_veiculo(self, placa, base_operacional_id, agent_id):
        self._talvez_falhar("criar_veiculo")
        novo = {"id": self._novo_id(), "license_plate": placa, "operational_base_id": base_operacional_id}
        self.veiculos.append(novo)
        return novo["id"]

    def ligar_veiculo(self, agent_id, vehicle_id):
        self._talvez_falhar("ligar_veiculo")
        self.ligados.append((agent_id, vehicle_id))

    def apagar_agente(self, agent_id):
        self._talvez_falhar("apagar_agente")
        self.apagados.append(("agente", agent_id))

    def apagar_veiculo(self, vehicle_id):
        self._talvez_falhar("apagar_veiculo")
        # como a Vuupt real (provado 02/11): veículo ligado a agente que ainda existe -> 400
        if any(v == vehicle_id and ("agente", a) not in self.apagados for a, v in self.ligados):
            raise ErroVuupt(f"DELETE /vehicles/{vehicle_id}: HTTP 400", 400, "")
        self.apagados.append(("veiculo", vehicle_id))


class TestProcessador(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._patch = mock.patch.object(banco, "DB_PATH", Path(self._tmp.name) / "t.db")
        self._patch.start()
        self.conn = banco.conectar()
        self.vuupt = VuuptFalsa(veiculos=[{"id": 1, "license_plate": "XYZ9A99", "operational_base_id": 77}])
        self.grupo = mock.Mock(return_value="enviado")
        self.motorista = mock.Mock(return_value="enviado")

    def tearDown(self):
        self.conn.close()
        self._patch.stop()
        self._tmp.cleanup()

    def _enviado(self, **extra):
        r = cad.criar_cadastro_link(self.conn, _dados(**extra))
        for t in cad.DOCUMENTOS:
            cad.guardar_documento(self.conn, r["id"], r["chave_envio"], t, f"{t}.jpg", JPEG)
        cad.marcar_enviado(self.conn, r["id"], r["chave_envio"])
        return r["id"]

    def _processar(self, cid, agora=AGORA):
        return proc.processar(self.conn, CONFIG, cid, vuupt=self.vuupt, avisar_grupo=self.grupo,
                              avisar_motorista=self.motorista, agora=agora)

    def _retentar(self, agora=AGORA):
        return proc.retentar(self.conn, CONFIG, vuupt=self.vuupt, avisar_grupo=self.grupo,
                             avisar_motorista=self.motorista, agora=agora)

    def _linha(self, cid):
        return dict(self.conn.execute("SELECT * FROM motoristas_cadastros WHERE id = ?", (cid,)).fetchone())

    def _tabela(self, agent_id):
        return [r for r in motoristas_banco.listar(self.conn) if r["AGENT_ID_VUUPT"] == agent_id]

    def _textos_grupo(self):
        return [(c.args[1], c.args[2]) for c in self.grupo.call_args_list]

    def test_fluxo_feliz(self):
        cid = self._enviado()
        r = self._processar(cid)
        self.assertEqual(r, {"situacao": "concluido", "erro": None})
        self.assertEqual(len(self.vuupt.posts_agente), 1)
        post = self.vuupt.posts_agente[0]
        self.assertEqual(post["email"], "jps@freshhub.com.br")
        self.assertEqual(post["senha"], CPF[:6])
        self.assertEqual(self.vuupt.chamadas["criar_veiculo"], 1)
        l = self._linha(cid)
        self.assertEqual(self.vuupt.ligados, [(l["agent_id"], l["vehicle_id"])])
        self.assertEqual(l["vuupt_email"], "jps@freshhub.com.br")
        self.assertEqual(l["vuupt_senha_digitos"], 6)
        self.assertIsNotNone(l["concluido_em"])
        self.assertIsNone(l["processando_em"])
        linhas = self._tabela(l["agent_id"])
        self.assertEqual(len(linhas), 1)
        m = linhas[0]
        self.assertEqual(l["bd_motorista_id"], m["id"])
        self.assertEqual(m["ATIVO"], "SIM")
        self.assertEqual(m["APENAS_CARGA_SECA"], "SIM")
        self.assertIn(m["SEM_APP"], (None, "", "NAO"))
        self.assertEqual(m["POSICAO"], "ESPORADICO")
        self.assertEqual(l["posicao"], "FIXO")
        self.assertEqual((l["agente_criado"], l["vehicle_criado"]), (1, 1))
        self.assertEqual(m["CPF_MOTORISTA"], CPF)
        self.assertTrue(CPF.startswith("0"))
        self.assertEqual(m["VEHICLE_ID_VUUPT"], l["vehicle_id"])
        self.assertEqual(m["EMAIL_MOTORISTA"], "jps@freshhub.com.br")
        self.assertEqual(m["MAX_ROTAS_DIA"], 1)
        self.assertEqual(m["ACEITA_VIAGENS"], "NAO")
        self.assertEqual(m["DIAS_DISPONIVEIS"], "SEGUNDA,TERCA")
        self.assertEqual(m["ZONAS_PREFERIDAS"], "ZONA SUL")
        self.assertEqual(m["TIPO_VEICULO"], "FIORINO")
        self.assertEqual(self.grupo.call_count, 1)
        tipo, texto = self._textos_grupo()[0]
        self.assertEqual(tipo, "cadastro_link")
        self.assertNotIn("⚠️", texto)
        self.assertIn("FIXO", texto)
        self.assertIn("https://painel.teste/painel/motoristas", texto)
        self.assertEqual(self.grupo.call_args.args[3], f"cadastro_link:{cid}:inicio")
        self.assertEqual(self.motorista.call_count, 1)
        texto_mot = self.motorista.call_args.args[2]
        self.assertIn("jps@freshhub.com.br", texto_mot)
        self.assertIn(CPF[:6], texto_mot)
        self.assertEqual(l["boas_vindas_situacao"], "enviado")

    def test_duas_chamadas_um_agente(self):
        cid = self._enviado()
        self._processar(cid)
        self._processar(cid)
        self.assertEqual(len(self.vuupt.posts_agente), 1)
        self.assertEqual(self.grupo.call_count, 1)
        self.assertEqual(self.motorista.call_count, 1)

    def test_trava_ocupado(self):
        cid = self._enviado()
        self.conn.execute("UPDATE motoristas_cadastros SET processando_em = ? WHERE id = ?",
                          ((AGORA - timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%S"), cid))
        self.conn.commit()
        self.assertEqual(self._processar(cid)["situacao"], "ocupado")
        self.assertEqual(sum(self.vuupt.chamadas.values()), 0)
        self.conn.execute("UPDATE motoristas_cadastros SET processando_em = ? WHERE id = ?",
                          ((AGORA - timedelta(minutes=11)).strftime("%Y-%m-%d %H:%M:%S"), cid))
        self.conn.commit()
        self.assertEqual(self._processar(cid)["situacao"], "concluido")
        self.assertIsNone(self._linha(cid)["processando_em"])

    def test_veiculo_falha_e_retenta(self):
        cid = self._enviado()
        self.vuupt.falhas["criar_veiculo"] = [ErroVuupt("POST /vehicles: HTTP 500", 500, "erro")]
        r = self._processar(cid)
        self.assertEqual(r["situacao"], "pendente")
        self.assertIn("500", r["erro"])
        l = self._linha(cid)
        self.assertIsNotNone(l["agent_id"])
        self.assertIsNone(l["vehicle_id"])
        self.assertEqual(l["tentativas"], 1)
        self.assertIn("500", l["ultimo_erro"])
        self.assertIsNone(l["processando_em"])
        self.assertEqual(self.grupo.call_count, 1)
        self.assertIn("⚠️", self._textos_grupo()[0][1])
        self.motorista.assert_not_called()

        res = self._retentar()
        self.assertEqual([x["situacao"] for x in res], ["concluido"])
        self.assertEqual(len(self.vuupt.posts_agente), 1)
        self.assertEqual(self.vuupt.chamadas["criar_veiculo"], 2)
        self.assertIsNotNone(self._linha(cid)["concluido_em"])
        tipos = [t for t, _ in self._textos_grupo()]
        self.assertEqual(tipos, ["cadastro_link", "cadastro_link_concluido"])
        self.assertEqual(self.grupo.call_args.args[3], f"cadastro_link:{cid}:concluido")
        self.assertEqual(self._retentar(), [])
        self.assertEqual(self.grupo.call_count, 2)

    def test_ligar_falha_e_retenta(self):
        cid = self._enviado()
        self.vuupt.falhas["ligar_veiculo"] = [ErroVuupt("PUT /users: HTTP 502", 502, "")]
        self.assertEqual(self._processar(cid)["situacao"], "pendente")
        self.assertEqual(self._processar(cid)["situacao"], "concluido")
        self.assertEqual(self.vuupt.chamadas["criar_veiculo"], 1)
        l = self._linha(cid)
        self.assertEqual(self.vuupt.ligados, [(l["agent_id"], l["vehicle_id"])])

    def test_erro_que_nao_e_vuupt_vira_pendente(self):
        cid = self._enviado()
        self.vuupt.falhas["criar_veiculo"] = [ValueError("resposta não é JSON")]
        r = self._processar(cid)
        self.assertEqual(r["situacao"], "pendente")
        l = self._linha(cid)
        self.assertEqual(l["tentativas"], 1)
        self.assertIsNone(l["processando_em"])
        self.assertIn("JSON", l["ultimo_erro"])

    def test_timeout_depois_do_post(self):
        cid = self._enviado()
        self.vuupt.timeout_apos_post = True
        self.assertEqual(self._processar(cid)["situacao"], "pendente")
        self.assertIsNone(self._linha(cid)["agent_id"])
        self.assertEqual(self._processar(cid)["situacao"], "concluido")
        self.assertEqual(len(self.vuupt.posts_agente), 1)
        self.assertEqual(self._linha(cid)["agent_id"], self.vuupt.agentes[0]["id"])

    def test_timeout_acha_email_sem_diferenciar_maiusculas(self):
        cid = self._enviado()
        self.vuupt.timeout_apos_post = True
        self._processar(cid)
        self.vuupt.agentes[0]["email"] = "JPS@FreshHub.com.br"
        self.assertEqual(self._processar(cid)["situacao"], "concluido")
        self.assertEqual(len(self.vuupt.posts_agente), 1)

    def test_422_no_proprio_email_nao_cria_outro_agente(self):
        # caminho do revisor: POST criou, resposta perdeu, a lista não mostra o agente
        self.grupo.side_effect = ["falhou", "enviado"]
        cid = self._enviado()
        self.vuupt.timeout_apos_post = True
        self.assertEqual(self._processar(cid)["situacao"], "pendente")
        self.vuupt.agentes[0]["_oculto"] = True
        r = self._processar(cid)
        self.assertEqual(r["situacao"], "pendente")
        self.assertEqual([p["email"] for p in self.vuupt.posts_agente],
                         ["jps@freshhub.com.br", "jps@freshhub.com.br"])
        self.assertEqual(len(self.vuupt.agentes), 1)
        l = self._linha(cid)
        self.assertIsNone(l["agent_id"])
        self.assertEqual(l["vuupt_email"], "jps@freshhub.com.br")
        self.assertIn("jps@freshhub.com.br já existe na Vuupt", l["ultimo_erro"])
        self.assertIn("confira se o agente foi criado", l["ultimo_erro"])
        self.assertEqual(l["tentativas"], 2)
        self.assertIn("já existe na Vuupt", self._textos_grupo()[1][1])
        self.motorista.assert_not_called()

    def test_email_colisao_na_vuupt(self):
        cid = self._enviado()
        self.vuupt.falhas["criar_agente"] = [ErroVuupt("POST /users: HTTP 422", 422,
                                                       '{"errors":{"email":["already taken"]}}')]
        self.assertEqual(self._processar(cid)["situacao"], "concluido")
        self.assertEqual([p["email"] for p in self.vuupt.posts_agente],
                         ["jps@freshhub.com.br", "jps2@freshhub.com.br"])
        self.assertEqual(self._linha(cid)["vuupt_email"], "jps2@freshhub.com.br")

    def test_email_ocupado_por_outro_cadastro_e_pela_tabela(self):
        motoristas_banco.inserir(self.conn, {"AGENT_ID_VUUPT": 1, "NOME_MOTORISTA": "Outro",
                                             "EMAIL_MOTORISTA": "jps@freshhub.com.br"}, "teste")
        self.vuupt.agentes.append({"id": 2, "email": "jps2@freshhub.com.br", "deleted_at": "2026-01-01"})
        outro = self._enviado(cpf=_cpf_com_dv("111444777"), telefone="11911112222", placa="DEF1G23")
        self.conn.execute("UPDATE motoristas_cadastros SET vuupt_email = 'jps3@freshhub.com.br' WHERE id = ?", (outro,))
        self.conn.execute("UPDATE motoristas_cadastros SET concluido_em = '2026-11-01' WHERE id = ?", (outro,))
        self.conn.commit()
        cid = self._enviado()
        self._processar(cid)
        self.assertEqual(self.vuupt.posts_agente[0]["email"], "jps4@freshhub.com.br")

    def test_senha_recusada(self):
        cid = self._enviado()
        self.vuupt.falhas["criar_agente"] = [ErroVuupt("POST /users: HTTP 422", 422,
                                                       '{"errors":{"password":["min 8"]}}')]
        self.assertEqual(self._processar(cid)["situacao"], "concluido")
        self.assertEqual([p["senha"] for p in self.vuupt.posts_agente], [CPF[:6], CPF[:8]])
        self.assertEqual(self._linha(cid)["vuupt_senha_digitos"], 8)
        self.assertIn(f"Senha: {CPF[:8]}", self.motorista.call_args.args[2])

    def test_conflito_telefone(self):
        self.vuupt.agentes.append({"id": 321, "name": "Fulano", "email": "f@x.com", "phone_number": "+5511998765432"})
        cid = self._enviado()
        r = self._processar(cid)
        self.assertEqual(r["situacao"], "conflito")
        self.assertEqual(self.vuupt.posts_agente, [])
        l = self._linha(cid)
        self.assertEqual(l["conflito_agent_id"], 321)
        self.assertIsNone(l["processando_em"])
        self.assertEqual(self.grupo.call_count, 1)
        self.assertIn("telefone já é do agente 321", self._textos_grupo()[0][1])
        self.motorista.assert_not_called()
        self.assertEqual(self._retentar(), [])

        self.conn.execute("UPDATE motoristas_cadastros SET conflito_resolvido = 1 WHERE id = ?", (cid,))
        self.conn.commit()
        res = self._retentar()
        self.assertEqual([x["situacao"] for x in res], ["concluido"])
        self.assertEqual(len(self.vuupt.posts_agente), 1)
        self.assertEqual([t for t, _ in self._textos_grupo()], ["cadastro_link", "cadastro_link_concluido"])

    def _resolver(self, cid, usar, usuario="hugo"):
        return proc.resolver_conflito(self.conn, CONFIG, cid, usar, usuario, vuupt=self.vuupt, avisar_grupo=self.grupo,
                                      avisar_motorista=self.motorista, agora=AGORA)

    def test_resolver_conflito_usar_agente_existente(self):
        self.vuupt.agentes.append({"id": 321, "name": "Fulano", "email": "fu@freshhub.com.br", "phone_number": "+5511998765432"})
        cid = self._enviado()
        self.assertEqual(self._processar(cid)["situacao"], "conflito")
        self.assertEqual(self._resolver(cid, 321)["situacao"], "concluido")
        l = self._linha(cid)
        self.assertEqual((l["agent_id"], l["conflito_resolvido"], l["revisado_por"]), (321, 1, "hugo"))
        self.assertIsNotNone(l["revisado_em"])
        self.assertEqual((l["vuupt_email"], l["boas_vindas_situacao"]), ("fu@freshhub.com.br", "agente_existente"))
        self.assertEqual(self.vuupt.posts_agente, [])
        self.motorista.assert_not_called()
        self.assertEqual(len(self._tabela(321)), 1)

    def test_resolver_conflito_atualiza_linha_existente_da_tabela(self):
        motoristas_banco.inserir(self.conn, {"AGENT_ID_VUUPT": 444, "NOME_MOTORISTA": "Antigo", "ATIVO": "NAO", "POSICAO": "FIXO",
                                             "TELEFONE_MOTORISTA": "11998765432", "CPF_MOTORISTA": "11122233344", "VEHICLE_ID_VUUPT": 999}, "teste")
        self.vuupt.agentes.append({"id": 444, "name": "Antigo", "email": "antigo@freshhub.com.br"})
        cid = self._enviado()
        self.assertEqual(self._processar(cid)["situacao"], "conflito")
        self._resolver(cid, 444, "ana")
        linhas = self._tabela(444)
        self.assertEqual(len(linhas), 1)
        t = linhas[0]
        self.assertEqual((t["ATIVO"], t["POSICAO"], t["PLACA"], t["CPF_MOTORISTA"]), ("SIM", "ESPORADICO", "ABC1D23", "11122233344"))
        self.assertEqual(t["DIAS_DISPONIVEIS"], "SEGUNDA,TERCA")
        self.assertEqual(self._linha(cid)["bd_motorista_id"], t["id"])
        self.assertEqual(t["VEHICLE_ID_VUUPT"], self._linha(cid)["vehicle_id"])
        self.assertNotEqual(t["VEHICLE_ID_VUUPT"], 999)
        self.assertEqual(self.vuupt.posts_agente, [])

    def test_resolver_conflito_outra_pessoa_cria_agente_novo(self):
        self.vuupt.agentes.append({"id": 321, "name": "Fulano", "email": "f@x.com", "phone_number": "+5511998765432"})
        cid = self._enviado()
        self._processar(cid)
        self.assertEqual(self._resolver(cid, None)["situacao"], "concluido")
        self.assertEqual(len(self.vuupt.posts_agente), 1)
        self.assertEqual(self._linha(cid)["revisado_por"], "hugo")

    def test_resolver_email_ja_existente_usando_agente(self):
        self.vuupt.agentes.append({"id": 999, "name": "João Pedro Santos", "email": "jps@freshhub.com.br"})
        cid = self._enviado()
        self.conn.execute("UPDATE motoristas_cadastros SET ultimo_erro = 'agente: e-mail ja existe' WHERE id = ?", (cid,))
        self.conn.commit()
        self.assertEqual(self._resolver(cid, 999)["situacao"], "concluido")
        l = self._linha(cid)
        self.assertEqual((l["agent_id"], l["ultimo_erro"]), (999, None))
        self.assertEqual(self.vuupt.posts_agente, [])

    def test_resolver_nao_troca_agente_que_o_cadastro_ja_tem(self):
        cid = self._enviado()
        self.assertEqual(self._processar(cid)["situacao"], "concluido")
        proprio = self._linha(cid)["agent_id"]
        with self.assertRaises(cad.CadastroInvalido):
            self._resolver(cid, proprio + 1)
        self.assertEqual(self._linha(cid)["agent_id"], proprio)

    def test_resolver_cadastro_inexistente(self):
        with self.assertRaises(cad.CadastroInvalido):
            proc.resolver_conflito(self.conn, CONFIG, 9999, None, "hugo", vuupt=self.vuupt)

    def test_contar_pendentes_ignora_cadastro_do_link(self):
        r = cad.criar_cadastro_link(self.conn, _dados())
        for t in cad.DOCUMENTOS:
            cad.guardar_documento(self.conn, r["id"], r["chave_envio"], t, f"{t}.jpg", JPEG)
        self.assertEqual(cad.contar_pendentes(self.conn), 0)

    def test_conflito_telefone_na_tabela(self):
        motoristas_banco.inserir(self.conn, {"AGENT_ID_VUUPT": 444, "NOME_MOTORISTA": "Antigo",
                                             "TELEFONE_MOTORISTA": "11998765432"}, "teste")
        cid = self._enviado()
        self.assertEqual(self._processar(cid)["situacao"], "conflito")
        self.assertEqual(self._linha(cid)["conflito_agent_id"], 444)

    def test_agente_apagado_nao_da_conflito(self):
        self.vuupt.agentes.append({"id": 321, "email": "f@x.com", "phone_number": "+5511998765432",
                                   "deleted_at": "2026-01-01 00:00:00"})
        self.vuupt.agentes.append({"id": 322, "email": "g@x.com", "phone_number": ""})
        cid = self._enviado()
        self.assertEqual(self._processar(cid)["situacao"], "concluido")

    def _placa_de_outro_agente(self):
        self.vuupt.veiculos.append({"id": 900, "license_plate": "ABC-1D23", "operational_base_id": 77})
        self.vuupt.agentes.append({"id": 777, "name": "Dono", "email": "x@x.com", "phone_number": "+5511900001111",
                                   "vehicle_id": 900})

    def test_placa_de_veiculo_de_outro_agente_e_conflito(self):
        self._placa_de_outro_agente()
        cid = self._enviado()
        r = self._processar(cid)
        self.assertEqual(r["situacao"], "conflito")
        l = self._linha(cid)
        self.assertEqual(self.vuupt.posts_agente, [])   # nenhuma licença paga antes de resolver
        self.assertIsNone(l["agent_id"])
        self.assertIsNone(l["vehicle_id"])
        self.assertEqual((l["conflito_agent_id"], l["conflito_tipo"]), (777, "placa"))
        self.assertEqual(self.vuupt.ligados, [])
        self.assertIn("placa ABC1D23 já é do veículo do agente 777", self._textos_grupo()[0][1])
        self.motorista.assert_not_called()
        self.assertEqual(self._retentar(), [])

        # "É outra pessoa: criar agente novo e reaproveitar o veículo"
        self.assertEqual(self._resolver(cid, None)["situacao"], "concluido")
        l = self._linha(cid)
        self.assertEqual(l["vehicle_id"], 900)
        self.assertEqual((l["agente_criado"], l["vehicle_criado"]), (1, 0))
        self.assertEqual(self.vuupt.ligados, [(l["agent_id"], 900)])
        self.assertEqual(len(self.vuupt.posts_agente), 1)
        self.assertEqual(self.vuupt.chamadas["criar_veiculo"], 0)

    def test_placa_resolvida_com_o_dono_usa_o_agente_dele(self):
        self._placa_de_outro_agente()
        cid = self._enviado()
        self.assertEqual(self._processar(cid)["situacao"], "conflito")
        # "É o mesmo motorista: usar agente 777"
        self.assertEqual(self._resolver(cid, 777)["situacao"], "concluido")
        l = self._linha(cid)
        self.assertEqual((l["agent_id"], l["vehicle_id"], l["agente_criado"]), (777, 900, 0))
        self.assertEqual(self.vuupt.posts_agente, [])
        self.assertEqual(self.vuupt.chamadas["criar_veiculo"], 0)
        self.assertEqual(self.vuupt.ligados, [(777, 900)])
        self.motorista.assert_not_called()

    def test_placa_ligada_a_outro_agente_entre_as_etapas(self):
        cid = self._enviado()
        criar = self.vuupt.criar_agente

        def criar_e_alguem_pega_a_placa(*a):
            novo = criar(*a)
            self._placa_de_outro_agente()
            return novo
        self.vuupt.criar_agente = criar_e_alguem_pega_a_placa
        self.assertEqual(self._processar(cid)["situacao"], "conflito")
        l = self._linha(cid)
        self.assertIsNotNone(l["agent_id"])
        self.assertIsNone(l["vehicle_id"])
        self.assertEqual((l["conflito_agent_id"], l["conflito_tipo"]), (777, "placa"))
        self.assertEqual(self.vuupt.ligados, [])

    def test_resolver_recusa_agente_inexistente_ou_apagado(self):
        self.vuupt.agentes.append({"id": 321, "name": "Fulano", "email": "f@x.com", "phone_number": "+5511998765432"})
        self.vuupt.agentes.append({"id": 654, "name": "Apagado", "email": "a@x.com", "deleted_at": "2026-01-01"})
        cid = self._enviado()
        self.assertEqual(self._processar(cid)["situacao"], "conflito")
        antes = self._linha(cid)
        for agent_id in (12345, 654):
            with self.assertRaises(cad.CadastroInvalido) as ctx:
                self._resolver(cid, agent_id)
            self.assertEqual(ctx.exception.codigo, 400)
        l = self._linha(cid)
        self.assertEqual((l["agent_id"], l["conflito_resolvido"], l["revisado_por"], l["boas_vindas_situacao"]),
                         (None, 0, antes["revisado_por"], None))
        self.assertEqual(self.vuupt.posts_agente, [])

    def test_agente_da_vuupt(self):
        self.vuupt.agentes.append({"id": 321, "name": "Fulano", "email": "f@x.com", "phone_number": "+5511998765432"})
        self.vuupt.agentes.append({"id": 654, "name": "Apagado", "deleted_at": "2026-01-01"})
        self.assertEqual(proc.agente_da_vuupt(CONFIG, 321, vuupt=self.vuupt),
                         {"id": 321, "nome": "Fulano", "telefone": "+5511998765432"})
        self.assertIsNone(proc.agente_da_vuupt(CONFIG, 654, vuupt=self.vuupt))
        self.assertIsNone(proc.agente_da_vuupt(CONFIG, 1, vuupt=self.vuupt))

    def test_resolver_durante_processamento_e_409(self):
        self.vuupt.agentes.append({"id": 321, "name": "Fulano", "email": "f@x.com", "phone_number": "+5511998765432"})
        cid = self._enviado()
        self._processar(cid)
        self.conn.execute("UPDATE motoristas_cadastros SET processando_em = ? WHERE id = ?",
                          ((AGORA - timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%S"), cid))
        self.conn.commit()
        with self.assertRaises(cad.CadastroInvalido) as ctx:
            self._resolver(cid, 321)
        self.assertEqual(ctx.exception.codigo, 409)
        self.assertIn("em processamento", ctx.exception.mensagem)
        self.assertIsNone(self._linha(cid)["agent_id"])

    def test_resolver_exige_cadastro_enviado(self):
        rascunho = cad.criar_cadastro_link(self.conn, _dados())["id"]
        with self.assertRaises(cad.CadastroInvalido) as ctx:
            self._resolver(rascunho, None)
        self.assertEqual(ctx.exception.codigo, 404)
        self.assertEqual(self._linha(rascunho)["conflito_resolvido"], 0)

    def test_telefone_resolvido_nao_libera_placa_de_outro_agente(self):
        self.vuupt.agentes.append({"id": 321, "email": "f@x.com", "phone_number": "+5511998765432"})
        self.vuupt.veiculos.append({"id": 900, "license_plate": "ABC1D23"})
        self.vuupt.agentes.append({"id": 777, "email": "x@x.com", "vehicle_id": 900})
        cid = self._enviado()
        self.assertEqual(self._processar(cid)["situacao"], "conflito")
        self.conn.execute("UPDATE motoristas_cadastros SET conflito_resolvido = 1 WHERE id = ?", (cid,))
        self.conn.commit()
        self.assertEqual(self._processar(cid)["situacao"], "conflito")
        l = self._linha(cid)
        self.assertEqual((l["conflito_agent_id"], l["conflito_resolvido"]), (777, 0))

    def test_placa_de_agente_apagado_reaproveita(self):
        self.vuupt.veiculos.append({"id": 900, "license_plate": "ABC1D23"})
        self.vuupt.agentes.append({"id": 777, "email": "x@x.com", "vehicle_id": 900, "deleted_at": "2026-01-01"})
        cid = self._enviado()
        self.assertEqual(self._processar(cid)["situacao"], "concluido")
        self.assertEqual(self._linha(cid)["vehicle_id"], 900)

    def test_placa_existente_reaproveita(self):
        self.vuupt.veiculos.append({"id": 900, "license_plate": "ABC-1D23", "operational_base_id": 77})
        cid = self._enviado()
        self.assertEqual(self._processar(cid)["situacao"], "concluido")
        self.assertEqual(self.vuupt.chamadas["criar_veiculo"], 0)
        self.assertEqual(self._linha(cid)["vehicle_id"], 900)

    def test_iphone(self):
        cid = self._enviado(celular="IPHONE")
        self.assertEqual(self._processar(cid)["situacao"], "concluido")
        m = self._tabela(self._linha(cid)["agent_id"])[0]
        self.assertEqual(m["SEM_APP"], "SIM")
        self.assertIn("iPhone", self.motorista.call_args.args[2])
        self.assertIn("iPhone", self._textos_grupo()[0][1])

    def test_refrigerado_nao_e_apenas_seco(self):
        cid = self._enviado(refrigerado=True, temp_min_c="-5")
        self._processar(cid)
        self.assertEqual(self._tabela(self._linha(cid)["agent_id"])[0]["APENAS_CARGA_SECA"], "NAO")

    def test_tabela_ja_tem_agente(self):
        existente = motoristas_banco.inserir(self.conn, {"AGENT_ID_VUUPT": 5001, "NOME_MOTORISTA": "X",
                                                         "TELEFONE_MOTORISTA": "11911112222"}, "teste")
        cid = self._enviado()
        self.assertEqual(self._processar(cid)["situacao"], "concluido")
        self.assertEqual(self._linha(cid)["agent_id"], 5001)
        self.assertEqual(len(self._tabela(5001)), 1)
        self.assertEqual(self._linha(cid)["bd_motorista_id"], existente)

    def test_boas_vindas_falhou_retenta(self):
        self.motorista.side_effect = ["falhou", "enviado"]
        cid = self._enviado()
        r = self._processar(cid)
        self.assertEqual(r["situacao"], "pendente")
        self.assertEqual(self._linha(cid)["boas_vindas_situacao"], "falhou")
        self.assertEqual(self._linha(cid)["tentativas"], 1)
        self.assertEqual([x["situacao"] for x in self._retentar()], ["concluido"])
        self.assertEqual(self.motorista.call_count, 2)
        self.assertEqual(self.grupo.call_count, 1)

    def test_boas_vindas_nao_enviado_retenta(self):
        self.motorista.side_effect = ["nao_enviado", "enviado"]
        cid = self._enviado()
        self.assertEqual(self._processar(cid)["situacao"], "pendente")
        self.assertEqual(self._linha(cid)["tentativas"], 1)
        self.assertEqual([x["situacao"] for x in self._retentar()], ["concluido"])
        self.assertEqual(self.motorista.call_count, 2)

    def test_boas_vindas_finais_concluem(self):
        casos = [("desligado", "111444777", "AAA1A11", "11900000001"),
                 ("modo_teste", "222555888", "BBB2B22", "11900000002"),
                 ("numero_sem_whatsapp", "333666999", "CCC3C33", "11900000003")]
        for situacao, base, placa, telefone in casos:
            self.motorista.return_value = situacao
            cid = self._enviado(cpf=_cpf_com_dv(base), placa=placa, telefone=telefone)
            self.assertEqual(self._processar(cid)["situacao"], "concluido", situacao)

    def test_grupo_desligado_conta_como_avisado(self):
        self.grupo.return_value = "desligado"
        self.motorista.return_value = "desligado"
        cid = self._enviado()
        self.assertEqual(self._processar(cid)["situacao"], "concluido")
        self.assertIsNotNone(self._linha(cid)["aviso_grupo_em"])

    def test_grupo_nao_enviado_tenta_de_novo(self):
        self.grupo.side_effect = ["nao_enviado", "enviado"]
        cid = self._enviado()
        r = self._processar(cid)
        self.assertEqual(r, {"situacao": "pendente", "erro": "aviso do grupo: nao_enviado"})
        l = self._linha(cid)
        self.assertIsNone(l["aviso_grupo_em"])
        self.assertIsNone(l["concluido_em"])
        self.assertEqual(l["tentativas"], 1)
        self.assertEqual(self.motorista.call_count, 1)   # o login do motorista não espera o grupo
        self.assertEqual([x["situacao"] for x in self._retentar()], ["concluido"])
        self.assertIsNotNone(self._linha(cid)["aviso_grupo_em"])
        self.assertEqual(self.grupo.call_count, 2)
        self.assertEqual(self.motorista.call_count, 1)

    def test_aviso_de_concluido_nao_enviado_tenta_de_novo(self):
        self.grupo.side_effect = ["enviado", "nao_enviado", "enviado"]
        self.vuupt.falhas["criar_veiculo"] = [ErroVuupt("HTTP 500", 500, "")]
        cid = self._enviado()
        self._processar(cid)
        self.assertEqual(self._processar(cid)["situacao"], "pendente")
        self.assertIsNone(self._linha(cid)["aviso_concluido_em"])
        self.assertEqual(self._processar(cid)["situacao"], "concluido")
        self.assertIsNotNone(self._linha(cid)["aviso_concluido_em"])
        self.assertEqual([t for t, _ in self._textos_grupo()],
                         ["cadastro_link", "cadastro_link_concluido", "cadastro_link_concluido"])

    def test_grupo_falhou_tenta_de_novo(self):
        self.grupo.side_effect = ["falhou", "enviado", "enviado"]
        self.vuupt.falhas["criar_veiculo"] = [ErroVuupt("HTTP 500", 500, "")]
        cid = self._enviado()
        self._processar(cid)
        self.assertIsNone(self._linha(cid)["aviso_grupo_em"])
        self._processar(cid)
        self.assertIsNotNone(self._linha(cid)["aviso_grupo_em"])

    def test_retentar_segue_depois_de_erro_fora_das_etapas(self):
        primeiro = self._enviado(cpf=_cpf_com_dv("111444777"), telefone="11911112222", placa="DEF1G23")
        segundo = self._enviado()
        self.grupo.side_effect = [RuntimeError("gateway quebrou"), "enviado"]
        res = self._retentar()
        self.assertEqual([(x["id"], x["situacao"]) for x in res], [(primeiro, "pendente"), (segundo, "concluido")])
        l = self._linha(primeiro)
        self.assertEqual(l["tentativas"], 1)
        self.assertIn("gateway quebrou", l["ultimo_erro"])
        self.assertIsNone(l["processando_em"])

    def test_trava_de_outra_rodada_nao_e_solta(self):
        cid = self._enviado()
        outra = (AGORA + timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")

        def outra_rodada_assume(*a):
            self.conn.execute("UPDATE motoristas_cadastros SET processando_em = ? WHERE id = ?", (outra, cid))
            self.conn.commit()
            return "enviado"
        self.grupo.side_effect = outra_rodada_assume
        self._processar(cid)
        self.assertEqual(self._linha(cid)["processando_em"], outra)

    def test_ignorado_rascunho_concluido_e_app(self):
        rascunho = cad.criar_cadastro_link(self.conn, _dados(cpf=_cpf_com_dv("111444777")))["id"]
        self.assertEqual(self._processar(rascunho), {"situacao": "ignorado", "erro": None})
        app = cad.criar_cadastro(self.conn, {**_dados(cpf=_cpf_com_dv("222555888")), "email": None})["id"]
        self.conn.execute("UPDATE motoristas_cadastros SET enviado_em = '2026-11-01 10:00:00' WHERE id = ?", (app,))
        self.conn.commit()
        self.assertEqual(self._processar(app)["situacao"], "ignorado")
        cid = self._enviado()
        self._processar(cid)
        concluido = self._linha(cid)["concluido_em"]
        self.assertEqual(self._processar(cid, agora=AGORA + timedelta(hours=1))["situacao"], "ignorado")
        self.assertEqual(self._linha(cid)["concluido_em"], concluido)
        self.assertEqual(self._processar(99999)["situacao"], "ignorado")
        self.assertEqual(len(self.vuupt.posts_agente), 1)

    def _desfazer(self, cid, apagar, usuario="hugo"):
        return proc.desfazer_cadastro_link(self.conn, CONFIG, cid, usuario, apagar_na_vuupt=apagar, vuupt=self.vuupt,
                                           agora=AGORA)

    def test_desfazer_inativa_libera_cpf_e_mantem_na_vuupt(self):
        cid = self._enviado()
        self._processar(cid)
        l = self._linha(cid)
        r = self._desfazer(cid, False)
        self.assertEqual(r["situacao"], "desfeito")
        self.assertIsNone(r["erro"])
        m = self._tabela(l["agent_id"])[0]
        self.assertEqual((m["ATIVO"], m["CPF_MOTORISTA"]), ("NAO", None))
        origens = {x[0] for x in self.conn.execute("SELECT origem FROM bd_motoristas_alteracoes WHERE motorista_id = ? "
                                                   "AND campo IN ('ATIVO', 'CPF_MOTORISTA')", (m["id"],))}
        self.assertEqual(origens, {"link-desfeito:hugo"})
        d = self._linha(cid)
        self.assertEqual((d["status"], d["motivo"], d["revisado_por"]), ("RECUSADO", "desfeito no painel", "hugo"))
        self.assertEqual(d["concluido_em"], l["concluido_em"])
        self.assertEqual(self.vuupt.apagados, [])
        # o CPF volta a poder se cadastrar
        novo = cad.criar_cadastro_link(self.conn, _dados())
        self.assertIsNotNone(novo["id"])

    def test_desfazer_apaga_na_vuupt_o_que_criou(self):
        cid = self._enviado()
        self._processar(cid)
        l = self._linha(cid)
        r = self._desfazer(cid, True)
        self.assertEqual(self.vuupt.apagados, [("agente", l["agent_id"]), ("veiculo", l["vehicle_id"])])
        self.assertEqual(len(r["apagados"]), 2)
        d = self._linha(cid)
        self.assertEqual((d["agente_criado"], d["vehicle_criado"]), (0, 0))
        self._desfazer(cid, True)   # de novo: nada a apagar
        self.assertEqual(len(self.vuupt.apagados), 2)

    def test_desfazer_nunca_apaga_agente_reaproveitado(self):
        self.vuupt.agentes.append({"id": 321, "name": "Fulano", "email": "fu@freshhub.com.br", "phone_number": "+5511998765432"})
        self.vuupt.veiculos.append({"id": 900, "license_plate": "ABC1D23"})
        cid = self._enviado()
        self._processar(cid)
        self._resolver(cid, 321)
        self._desfazer(cid, True)
        self.assertEqual(self.vuupt.apagados, [])

    def test_desfazer_erro_da_vuupt_mantem_o_resto(self):
        cid = self._enviado()
        self._processar(cid)
        self.vuupt.falhas["apagar_veiculo"] = [ErroVuupt("DELETE /vehicles: HTTP 500", 500, "")]
        r = self._desfazer(cid, True)
        self.assertIn("veículo", r["erro"])
        self.assertIn("500", r["erro"])
        d = self._linha(cid)
        self.assertEqual((d["status"], d["vehicle_criado"], d["agente_criado"]), ("RECUSADO", 1, 0))
        self.assertEqual(self._tabela(d["agent_id"])[0]["ATIVO"], "NAO")
        self._desfazer(cid, True)   # o que ficou sai na 2ª vez
        self.assertEqual([t for t, _ in self.vuupt.apagados], ["agente", "veiculo"])

    def test_desfazer_pendente_sai_do_retentar(self):
        cid = self._enviado()
        self.vuupt.falhas["criar_veiculo"] = [ErroVuupt("HTTP 500", 500, "")]
        self._processar(cid)
        self._desfazer(cid, False)
        self.assertEqual(self._retentar(), [])
        self.assertEqual(self._processar(cid)["situacao"], "ignorado")

    def test_desfazer_durante_processamento_e_409_e_app_404(self):
        cid = self._enviado()
        self.conn.execute("UPDATE motoristas_cadastros SET processando_em = ? WHERE id = ?",
                          ((AGORA - timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%S"), cid))
        self.conn.commit()
        with self.assertRaises(cad.CadastroInvalido) as ctx:
            self._desfazer(cid, False)
        self.assertEqual(ctx.exception.codigo, 409)
        self.assertEqual(self._linha(cid)["status"], "APROVADO")
        app = cad.criar_cadastro(self.conn, {**_dados(cpf=_cpf_com_dv("222555888")), "email": None})["id"]
        with self.assertRaises(cad.CadastroInvalido) as ctx:
            self._desfazer(app, False)
        self.assertEqual(ctx.exception.codigo, 404)

    def test_retentar_filtros(self):
        nao_enviado = cad.criar_cadastro_link(self.conn, _dados(cpf=_cpf_com_dv("111444777")))["id"]
        cansado = self._enviado(cpf=_cpf_com_dv("222555888"), telefone="11911112222", placa="DEF1G23")
        self.conn.execute("UPDATE motoristas_cadastros SET tentativas = 20 WHERE id = ?", (cansado,))
        self.conn.commit()
        cid = self._enviado()
        res = self._retentar()
        self.assertEqual([x["id"] for x in res], [cid])
        self.assertIsNone(self._linha(nao_enviado)["agent_id"])

    # ── Revisão de segurança 02/11 (A1, M1) ──────────────────────────────────

    def test_processar_sem_forcar_ignora_esgotado_e_conflito_em_aberto(self):
        """A1: processar vindo da API não pula os filtros do retentar."""
        cid = self._enviado()
        self.conn.execute("UPDATE motoristas_cadastros SET tentativas = 20 WHERE id = ?", (cid,))
        self.conn.commit()
        self.assertEqual(self._processar(cid), {"situacao": "ignorado", "erro": None})
        self.conn.execute("UPDATE motoristas_cadastros SET tentativas = 0, conflito_agent_id = 321, "
                          "conflito_resolvido = 0 WHERE id = ?", (cid,))
        self.conn.commit()
        self.assertEqual(self._processar(cid), {"situacao": "ignorado", "erro": None})
        self.assertEqual(sum(self.vuupt.chamadas.values()), 0)   # nem listar_agentes
        self.assertIsNone(self._linha(cid)["processando_em"])
        self.grupo.assert_not_called()

    def test_forcar_destrava_esgotado(self):
        """O "Tentar de novo" do painel passa forcar=True."""
        cid = self._enviado()
        self.conn.execute("UPDATE motoristas_cadastros SET tentativas = 20 WHERE id = ?", (cid,))
        self.conn.commit()
        r = proc.processar(self.conn, CONFIG, cid, vuupt=self.vuupt, avisar_grupo=self.grupo,
                           avisar_motorista=self.motorista, agora=AGORA, forcar=True)
        self.assertEqual(r["situacao"], "concluido")

    def test_resolver_destrava_conflito_com_tentativas_esgotadas(self):
        self.vuupt.agentes.append({"id": 321, "name": "Fulano", "email": "f@x.com", "phone_number": "+5511998765432"})
        cid = self._enviado()
        self.assertEqual(self._processar(cid)["situacao"], "conflito")
        self.conn.execute("UPDATE motoristas_cadastros SET tentativas = 20 WHERE id = ?", (cid,))
        self.conn.commit()
        self.assertEqual(self._resolver(cid, None)["situacao"], "concluido")

    def test_retentar_desligado_nao_faz_nada(self):
        """M1(b): ativo desligado é o botão de parada também pro timer."""
        cid = self._enviado()
        desligado = {**CONFIG, "cadastro_motorista_link": {"ativo": False}}
        with self.assertLogs("nucleo.cadastro_motorista_link", "INFO") as logs:
            self.assertEqual(proc.retentar(self.conn, desligado, vuupt=self.vuupt, avisar_grupo=self.grupo,
                                           avisar_motorista=self.motorista, agora=AGORA), [])
        self.assertEqual(len(logs.output), 1)
        self.assertIn("desligado", logs.output[0])
        sem_chave = {k: v for k, v in CONFIG.items() if k != "cadastro_motorista_link"}
        self.assertEqual(proc.retentar(self.conn, sem_chave, vuupt=self.vuupt), [])
        self.assertEqual(sum(self.vuupt.chamadas.values()), 0)
        self.assertIsNone(self._linha(cid)["agent_id"])

    def test_teto_diario_de_agentes_novos(self):
        """M1(a): passou do teto, não faz POST; pendência no grupo; no outro dia segue."""
        cfg = {**CONFIG, "cadastro_motorista_link": {"ativo": True, "max_agentes_dia": 1}}
        primeiro = self._enviado()
        segundo = self._enviado(cpf=_cpf_com_dv("222555888"), telefone="11911112222", placa="DEF1G23")
        kw = dict(vuupt=self.vuupt, avisar_grupo=self.grupo, avisar_motorista=self.motorista, agora=AGORA)
        self.assertEqual(proc.processar(self.conn, cfg, primeiro, **kw)["situacao"], "concluido")
        self.assertEqual(self._linha(primeiro)["agente_criado_em"], "2026-11-02 10:00:00")
        self.grupo.reset_mock()
        r = proc.processar(self.conn, cfg, segundo, **kw)
        self.assertEqual(r["situacao"], "pendente")
        self.assertIn(proc.MSG_LIMITE_AGENTES, r["erro"])
        self.assertEqual(len(self.vuupt.posts_agente), 1)
        l = self._linha(segundo)
        self.assertIn(proc.MSG_LIMITE_AGENTES, l["ultimo_erro"])
        self.assertEqual(l["tentativas"], 1)
        self.assertIsNone(l["agent_id"])
        self.assertIn(proc.MSG_LIMITE_AGENTES, self._textos_grupo()[0][1])
        self.motorista.assert_called_once()   # só as boas-vindas do primeiro
        amanha = dict(kw, agora=AGORA + timedelta(days=1))
        self.assertEqual(proc.processar(self.conn, cfg, segundo, **amanha)["situacao"], "concluido")
        self.assertEqual(len(self.vuupt.posts_agente), 2)

    def test_teto_padrao_e_10_e_conta_so_criados_pelo_link(self):
        for i in range(proc.MAX_AGENTES_DIA):
            self.conn.execute("INSERT INTO motoristas_cadastros (cpf, nome, chave_envio, status, criado_em, origem, "
                              "agente_criado, agente_criado_em) VALUES (?, 'X Y', ?, 'APROVADO', ?, 'link', 1, ?)",
                              (f"{i:011d}", f"k{i}", "2026-11-02 08:00:00", "2026-11-02 08:00:00"))
        self.conn.commit()
        cid = self._enviado()
        self.assertEqual(self._processar(cid)["situacao"], "pendente")
        self.assertEqual(self.vuupt.posts_agente, [])
        # agente apagado no "Desfazer" (agente_criado = 0) libera a vaga
        self.conn.execute("UPDATE motoristas_cadastros SET agente_criado = 0 WHERE cpf = '00000000000'")
        self.conn.commit()
        self.assertEqual(self._processar(cid)["situacao"], "concluido")


class TestMain(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._patch = mock.patch.object(banco, "DB_PATH", Path(self._tmp.name) / "t.db")
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def test_modo_teste_nao_grava_nada(self):
        conn = banco.conectar()
        r = cad.criar_cadastro_link(conn, _dados())
        for t in cad.DOCUMENTOS:
            cad.guardar_documento(conn, r["id"], r["chave_envio"], t, f"{t}.jpg", JPEG)
        cad.marcar_enviado(conn, r["id"], r["chave_envio"])
        conn.close()
        with mock.patch.object(proc, "_carregar_config", return_value=dict(CONFIG)), \
                mock.patch.object(proc, "VuuptAgentes", side_effect=AssertionError("Vuupt real")):
            self.assertEqual(proc.main(["--retentar", "--modo-teste"]), 0)
        conn = banco.conectar()
        try:
            l = dict(conn.execute("SELECT * FROM motoristas_cadastros WHERE id = ?", (r["id"],)).fetchone())
            self.assertIsNone(l["agent_id"])
            self.assertIsNone(l["concluido_em"])
            self.assertEqual(motoristas_banco.listar(conn), [])
        finally:
            conn.close()
        self.assertFalse(motoristas_banco.caminho_espelho().exists())

    def test_id_com_link_desligado_nao_processa(self):
        desligado = {**CONFIG, "cadastro_motorista_link": {"ativo": False}}
        with mock.patch.object(proc, "_carregar_config", return_value=desligado), \
                mock.patch.object(proc, "processar", side_effect=AssertionError("processou")):
            self.assertEqual(proc.main(["--id", "1"]), 0)


if __name__ == "__main__":
    unittest.main()
