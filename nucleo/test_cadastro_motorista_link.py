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
CONFIG = {"vuupt_api": {"token": "x"},
          "portal_cliente": {"chamados": {"url_painel": "https://painel.teste/painel/"}}}


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
            cad.guardar_documento(self.conn, r["id"], r["chave_envio"], t, f"{t}.jpg", b"x" * 50)
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
        self.assertEqual(m["POSICAO"], "FIXO")
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
        motoristas_banco.inserir(self.conn, {"AGENT_ID_VUUPT": 444, "NOME_MOTORISTA": "Antigo", "ATIVO": "NAO",
                                             "TELEFONE_MOTORISTA": "11998765432", "CPF_MOTORISTA": "11122233344"}, "teste")
        cid = self._enviado()
        self.assertEqual(self._processar(cid)["situacao"], "conflito")
        self._resolver(cid, 444, "ana")
        linhas = self._tabela(444)
        self.assertEqual(len(linhas), 1)
        t = linhas[0]
        self.assertEqual((t["ATIVO"], t["POSICAO"], t["PLACA"], t["CPF_MOTORISTA"]), ("SIM", "FIXO", "ABC1D23", "11122233344"))
        self.assertEqual(t["DIAS_DISPONIVEIS"], "SEGUNDA,TERCA")
        self.assertEqual(self._linha(cid)["bd_motorista_id"], t["id"])
        self.assertEqual(self.vuupt.posts_agente, [])

    def test_resolver_conflito_outra_pessoa_cria_agente_novo(self):
        self.vuupt.agentes.append({"id": 321, "name": "Fulano", "email": "f@x.com", "phone_number": "+5511998765432"})
        cid = self._enviado()
        self._processar(cid)
        self.assertEqual(self._resolver(cid, None)["situacao"], "concluido")
        self.assertEqual(len(self.vuupt.posts_agente), 1)
        self.assertEqual(self._linha(cid)["revisado_por"], "hugo")

    def test_resolver_email_ja_existente_usando_agente(self):
        cid = self._enviado()
        self.conn.execute("UPDATE motoristas_cadastros SET ultimo_erro = 'agente: e-mail ja existe' WHERE id = ?", (cid,))
        self.conn.commit()
        self.assertEqual(self._resolver(cid, 999)["situacao"], "concluido")
        l = self._linha(cid)
        self.assertEqual((l["agent_id"], l["ultimo_erro"]), (999, None))
        self.assertEqual(self.vuupt.posts_agente, [])

    def test_resolver_cadastro_inexistente(self):
        with self.assertRaises(cad.CadastroInvalido):
            proc.resolver_conflito(self.conn, CONFIG, 9999, None, "hugo", vuupt=self.vuupt)

    def test_contar_pendentes_ignora_cadastro_do_link(self):
        r = cad.criar_cadastro_link(self.conn, _dados())
        for t in cad.DOCUMENTOS:
            cad.guardar_documento(self.conn, r["id"], r["chave_envio"], t, f"{t}.jpg", b"x" * 50)
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

    def test_placa_de_veiculo_de_outro_agente_e_conflito(self):
        self.vuupt.veiculos.append({"id": 900, "license_plate": "ABC-1D23", "operational_base_id": 77})
        self.vuupt.agentes.append({"id": 777, "email": "x@x.com", "phone_number": "+5511900001111", "vehicle_id": 900})
        cid = self._enviado()
        r = self._processar(cid)
        self.assertEqual(r["situacao"], "conflito")
        l = self._linha(cid)
        self.assertIsNotNone(l["agent_id"])
        self.assertIsNone(l["vehicle_id"])
        self.assertEqual(l["conflito_agent_id"], 777)
        self.assertEqual(self.vuupt.ligados, [])
        self.assertEqual(self._tabela(l["agent_id"]), [])
        self.assertIn("placa ABC1D23 já é do veículo do agente 777", self._textos_grupo()[0][1])
        self.motorista.assert_not_called()
        self.assertEqual(self._retentar(), [])

        self.conn.execute("UPDATE motoristas_cadastros SET conflito_resolvido = 1 WHERE id = ?", (cid,))
        self.conn.commit()
        self.assertEqual([x["situacao"] for x in self._retentar()], ["concluido"])
        l = self._linha(cid)
        self.assertEqual(l["vehicle_id"], 900)
        self.assertEqual(self.vuupt.ligados, [(l["agent_id"], 900)])
        self.assertEqual(len(self.vuupt.posts_agente), 1)
        self.assertEqual(self.vuupt.chamadas["criar_veiculo"], 0)

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

    def test_retentar_filtros(self):
        nao_enviado = cad.criar_cadastro_link(self.conn, _dados(cpf=_cpf_com_dv("111444777")))["id"]
        cansado = self._enviado(cpf=_cpf_com_dv("222555888"), telefone="11911112222", placa="DEF1G23")
        self.conn.execute("UPDATE motoristas_cadastros SET tentativas = 20 WHERE id = ?", (cansado,))
        self.conn.commit()
        cid = self._enviado()
        res = self._retentar()
        self.assertEqual([x["id"] for x in res], [cid])
        self.assertIsNone(self._linha(nao_enviado)["agent_id"])


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
            cad.guardar_documento(conn, r["id"], r["chave_envio"], t, f"{t}.jpg", b"x" * 50)
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


if __name__ == "__main__":
    unittest.main()
