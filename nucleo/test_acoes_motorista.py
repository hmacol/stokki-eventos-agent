# -*- coding: utf-8 -*-
"""
test_acoes_motorista.py

Ações da tela /motoristas: inativar/reativar, excluir e resetar PIN.
SQLite e pasta de cadastros temporários; nada de rede.
    py -3.11 -m unittest nucleo.test_acoes_motorista -v
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))

from nucleo import acoes_motorista as acoes, auth_motorista as auth, banco, cadastro_motorista as cad  # noqa: E402
from regras import motoristas_banco as mb  # noqa: E402

CPF = "52998224725"


def _linha(**kw):
    base = {c: None for c in mb.COLUNAS}
    base.update(kw)
    return base


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.dir = Path(self._tmp.name)
        self._patch = mock.patch.object(banco, "DB_PATH", self.dir / "t.db")
        self._patch.start()
        self.conn = banco.conectar(self.dir / "t.db")
        self.config = {}

    def tearDown(self):
        self._patch.stop()
        self.conn.close()
        self._tmp.cleanup()

    def _motorista(self, agent=10, cpf=CPF, ativo="SIM", nome="Ana"):
        return mb.inserir(self.conn, _linha(AGENT_ID_VUUPT=agent, NOME_MOTORISTA=nome, ATIVO=ativo,
                                            CPF_MOTORISTA=cpf), "teste")

    def _login(self, cpf=CPF, **kw):
        return auth.criar_ou_atualizar_motorista(self.conn, cpf, "Ana", "246802", **kw)

    def _link(self, bd_id, concluido=False):
        self.conn.execute("INSERT INTO motoristas_links_atualizacao (bd_motorista_id, token_hash, criado_em, expira_em, "
                          "concluido_em) VALUES (?, ?, '2026-11-01 10:00:00', '2999-01-01 00:00:00', ?)",
                          (bd_id, f"h{bd_id}{concluido}", "2026-11-02 10:00:00" if concluido else None))
        self.conn.commit()

    def _ficha(self, bd_id, status=cad.STATUS_APROVADO):
        cur = self.conn.execute("INSERT INTO motoristas_cadastros (cpf, nome, chave_envio, status, criado_em, "
                                "bd_motorista_id) VALUES (?, 'Ana', 'k', ?, '2026-11-01 10:00:00', ?)",
                                (CPF, status, bd_id))
        self.conn.commit()
        pasta = cad.pasta_cadastros() / str(cur.lastrowid)
        pasta.mkdir(parents=True)
        (pasta / "cnh.jpg").write_bytes(b"x")
        return cur.lastrowid, pasta

    def _rota(self, agent):
        self.conn.execute("INSERT INTO nucleo_rotas (data_rota, agent_id) VALUES ('2026-11-10', ?)", (agent,))
        self.conn.commit()

    def _cancelado(self, bd_id):
        return self.conn.execute("SELECT cancelado_em FROM motoristas_links_atualizacao WHERE bd_motorista_id = ?",
                                 (bd_id,)).fetchone()["cancelado_em"]


class TestRotasDoAgente(_Base):
    def test_soma_as_tabelas_com_o_agente(self):
        self._rota(10)
        self._rota(10)
        self._rota(11)
        self.assertEqual(acoes.rotas_do_agente(self.conn, 10), 2)

    def test_rotas_do_agente_tabela_ausente_conta_zero(self):
        # nenhuma das quatro tabelas existe
        self.conn.execute("DROP TABLE IF EXISTS nucleo_rotas")
        self.conn.execute("DROP TABLE IF EXISTS rascunhos_rota")
        self.conn.execute("DROP TABLE IF EXISTS baixas_sem_app")
        self.conn.execute("DROP TABLE IF EXISTS rotas")
        self.assertEqual(acoes.rotas_do_agente(self.conn, 10), 0)


class TestGuardaDeRotas(_Base):
    def _recria(self, tabela, ddl):
        self.conn.execute(f"DROP TABLE IF EXISTS {tabela}")
        self.conn.execute(ddl)
        self.conn.commit()

    def test_recusa_vem_do_rascunho_e_da_baixa(self):
        mid = self._motorista()
        self._recria("rascunhos_rota", "CREATE TABLE rascunhos_rota (id INTEGER PRIMARY KEY, agent_id INTEGER)")
        self._recria("baixas_sem_app", "CREATE TABLE baixas_sem_app (id INTEGER PRIMARY KEY, agent_id INTEGER)")
        self.conn.execute("INSERT INTO rascunhos_rota (agent_id) VALUES (10)")
        self.conn.commit()
        with self.assertRaises(acoes.AcaoRecusada) as e:
            acoes.excluir(self.conn, self.config, mid, "hugo")
        self.assertEqual(e.exception.codigo, 409)
        self.conn.execute("DELETE FROM rascunhos_rota")
        self.conn.execute("INSERT INTO baixas_sem_app (agent_id) VALUES (10)")
        self.conn.commit()
        with self.assertRaises(acoes.AcaoRecusada):
            acoes.excluir(self.conn, self.config, mid, "hugo")
        self.assertEqual(len(mb.listar(self.conn)), 1)

    def test_tabela_conhecida_sem_agent_id_recusa(self):
        mid = self._motorista()
        self._recria("rascunhos_rota", "CREATE TABLE rascunhos_rota (id INTEGER PRIMARY KEY)")
        with self.assertRaises(acoes.AcaoRecusada) as e:
            acoes.excluir(self.conn, self.config, mid, "hugo")
        self.assertIn("rascunhos_rota", e.exception.mensagem)
        self.assertEqual(len(mb.listar(self.conn)), 1)

    def test_rotas_antiga_conta_por_cpf(self):
        mid = self._motorista()
        self._recria("rotas", "CREATE TABLE rotas (id INTEGER PRIMARY KEY, motorista_cpf TEXT)")
        self.assertEqual(acoes.rotas_do_agente(self.conn, 10), 0)
        self.conn.execute("INSERT INTO rotas (motorista_cpf) VALUES (?)", (CPF,))
        self.conn.commit()
        self.assertEqual(acoes.rotas_do_agente(self.conn, 10, CPF), 1)
        with self.assertRaises(acoes.AcaoRecusada):
            acoes.excluir(self.conn, self.config, mid, "hugo")


class TestAtivo(_Base):
    def test_inativar_cancela_link_em_aberto(self):
        mid = self._motorista()
        self._link(mid)
        acoes.definir_ativo(self.conn, self.config, mid, False, "hugo")
        self.assertFalse(mb.listar(self.conn)[0]["ATIVO"] == "SIM")
        self.assertIsNotNone(self._cancelado(mid))

    def test_inativar_uma_de_duas_linhas_mantem_o_link(self):
        a = self._motorista(agent=10)
        self._motorista(agent=11)
        self._link(a)
        acoes.definir_ativo(self.conn, self.config, a, False, "hugo")
        self.assertIsNone(self._cancelado(a))

    def test_inativar_substitui_o_rascunho_do_link_e_apaga_a_pasta(self):
        """M6: link cancelado pela inativação não deixa rascunho PENDENTE (segura o CPF) nem cópia no disco."""
        from nucleo import atualizacao_motorista as atu
        mid = self._motorista()
        r = atu.abrir(self.conn, atu.gerar_link(self.conn, mid, "hugo")["token"], CPF)
        pasta = cad.pasta_cadastros() / str(r["cadastro_id"])
        pasta.mkdir(parents=True)
        (pasta / "cnh.jpg").write_bytes(b"x")
        acoes.definir_ativo(self.conn, self.config, mid, False, "hugo")
        self.assertEqual(self.conn.execute("SELECT status FROM motoristas_cadastros WHERE id = ?",
                                           (r["cadastro_id"],)).fetchone()[0], cad.STATUS_SUBSTITUIDO)
        self.assertFalse(pasta.exists())

    def test_reativar_marca_ativo(self):
        mid = self._motorista(ativo="NAO")
        acoes.definir_ativo(self.conn, self.config, mid, True, "hugo")
        self.assertEqual(mb.listar(self.conn)[0]["ATIVO"], "SIM")


class TestExcluir(_Base):
    def test_excluir_com_rota_recusa_409_e_nao_apaga(self):
        mid = self._motorista()
        self._rota(10)
        with self.assertRaises(acoes.AcaoRecusada) as e:
            acoes.excluir(self.conn, self.config, mid, "hugo")
        self.assertEqual(e.exception.codigo, 409)
        self.assertIn("1 rotas no histórico", e.exception.mensagem)
        self.assertEqual(len(mb.listar(self.conn)), 1)

    def test_excluir_sem_rota_apaga_linha_login_fichas_e_arquivos(self):
        mid = self._motorista()
        self._login()
        self._link(mid)
        self.conn.execute("INSERT INTO motoristas_grupos_whatsapp (bd_motorista_id, grupo_id, como) "
                          "VALUES (?, 'g@g.us', 'manual')", (mid,))
        self.conn.commit()
        ficha, pasta = self._ficha(mid)
        r = acoes.excluir(self.conn, self.config, mid, "hugo")
        self.assertEqual(r, {"ok": True, "login_apagado": True, "fichas": 1})
        self.assertEqual(mb.listar(self.conn), [])
        self.assertIsNone(auth.buscar_motorista(self.conn, CPF))
        self.assertIsNotNone(self._cancelado(mid))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM motoristas_grupos_whatsapp").fetchone()[0], 0)
        row = self.conn.execute("SELECT status, motivo FROM motoristas_cadastros WHERE id = ?", (ficha,)).fetchone()
        self.assertEqual((row["status"], row["motivo"]), (cad.STATUS_RECUSADO, "excluído no painel"))
        self.assertFalse(pasta.exists())

    def test_excluir_uma_de_duas_linhas_mantem_login_e_ficha(self):
        a = self._motorista(agent=10)
        b = self._motorista(agent=11)
        self._login()
        ficha, pasta = self._ficha(a)
        r = acoes.excluir(self.conn, self.config, a, "hugo")
        self.assertEqual(r, {"ok": True, "login_apagado": False, "fichas": 0})
        self.assertEqual([x["id"] for x in mb.listar(self.conn)], [b])
        self.assertIsNotNone(auth.buscar_motorista(self.conn, CPF))
        row = self.conn.execute("SELECT status, bd_motorista_id FROM motoristas_cadastros WHERE id = ?",
                                (ficha,)).fetchone()
        self.assertEqual((row["status"], row["bd_motorista_id"]), (cad.STATUS_APROVADO, b))
        self.assertTrue(pasta.exists())

    def _estado(self):
        q = lambda sql: [tuple(r) for r in self.conn.execute(sql)]
        return (q("SELECT * FROM bd_motoristas"), q("SELECT cpf, pin_hash FROM motoristas"),
                q("SELECT id, bd_motorista_id, cancelado_em FROM motoristas_links_atualizacao"),
                q("SELECT * FROM motoristas_grupos_whatsapp"),
                q("SELECT id, status, motivo, bd_motorista_id FROM motoristas_cadastros"),
                q("SELECT * FROM bd_motoristas_alteracoes"))

    def test_excluir_falha_no_meio_nao_deixa_meia_exclusao(self):
        mid = self._motorista()
        self._login()
        self._link(mid)
        self.conn.execute("INSERT INTO motoristas_grupos_whatsapp (bd_motorista_id, grupo_id, como) "
                          "VALUES (?, 'g@g.us', 'manual')", (mid,))
        self.conn.commit()
        _, pasta = self._ficha(mid)
        antes = self._estado()
        with mock.patch.object(acoes.cad, "STATUS_RECUSADO", None):   # NOT NULL: o UPDATE das fichas estoura
            with self.assertRaises(Exception):
                acoes.excluir(self.conn, self.config, mid, "hugo")
        self.assertEqual(self._estado(), antes)
        self.assertTrue(pasta.exists())

    def test_excluir_nao_chama_vuupt(self):
        mid = self._motorista()
        with mock.patch("requests.sessions.Session.request") as req:
            acoes.excluir(self.conn, self.config, mid, "hugo")
        req.assert_not_called()

    def test_excluir_id_inexistente(self):
        from regras.cadastro_motoristas import MotoristaNaoEncontrado
        with self.assertRaises(MotoristaNaoEncontrado):
            acoes.excluir(self.conn, self.config, 999, "hugo")

    # ── Revisão final do ramo ──

    def _ficha_de(self, bd_id, cpf, agent_id=None, origem="app", **extra):
        cols = {"cpf": cpf, "nome": "Ana", "chave_envio": "k", "status": cad.STATUS_APROVADO, "origem": origem,
                "criado_em": "2026-11-01 10:00:00", "bd_motorista_id": bd_id, "agent_id": agent_id}
        cols.update(extra)
        cur = self.conn.execute(f"INSERT INTO motoristas_cadastros ({', '.join(cols)}) "
                                f"VALUES ({', '.join('?' * len(cols))})", list(cols.values()))
        self.conn.commit()
        pasta = cad.pasta_cadastros() / str(cur.lastrowid)
        pasta.mkdir(parents=True)
        (pasta / "cnh.jpg").write_bytes(b"x")
        return cur.lastrowid, pasta

    def _status(self, ficha_id):
        return self.conn.execute("SELECT status FROM motoristas_cadastros WHERE id = ?", (ficha_id,)).fetchone()[0]

    def test_excluir_carro_2_sem_cpf_mantem_a_ficha_do_motorista(self):
        """C2: linha com CPF + "CARRO 2" sem CPF no mesmo agente; excluir a "duplicada" não apaga nada dele."""
        from nucleo import atualizacao_motorista as atu
        real = self._motorista(agent=10, cpf=CPF)
        carro2 = self._motorista(agent=10, cpf=None, nome="ANA CARRO 2")
        ficha, pasta = self._ficha_de(None, CPF, agent_id=10)   # aprovada pelo app, sem vínculo
        atu.vincular_fichas(self.conn)
        r = acoes.excluir(self.conn, self.config, carro2, "hugo")
        self.assertEqual((r["fichas"], r["login_apagado"]), (0, False))
        self.assertEqual(self._status(ficha), cad.STATUS_APROVADO)
        self.assertEqual(self.conn.execute("SELECT bd_motorista_id FROM motoristas_cadastros WHERE id = ?",
                                           (ficha,)).fetchone()[0], real)
        self.assertTrue(pasta.exists())

    def test_excluir_nunca_recusa_ficha_de_outro_cpf(self):
        """C2 (c): só as fichas do CPF da pessoa removida."""
        mid = self._motorista(agent=10, cpf=CPF)
        minha, pasta_minha = self._ficha_de(mid, CPF)
        outra, pasta_outra = self._ficha_de(mid, "11144477735")
        r = acoes.excluir(self.conn, self.config, mid, "hugo")
        self.assertEqual(r["fichas"], 1)
        self.assertEqual((self._status(minha), self._status(outra)), (cad.STATUS_RECUSADO, cad.STATUS_APROVADO))
        self.assertFalse(pasta_minha.exists())
        self.assertTrue(pasta_outra.exists())

    def test_excluir_solta_a_ficha_de_outro_cpf_pra_religar_pelo_cpf(self):
        """Rodada 2, item 2: a ficha de outro CPF não fica presa na linha apagada; o vincular religa pelo CPF."""
        from nucleo import atualizacao_motorista as atu
        mid = self._motorista(agent=10, cpf=CPF)
        dono = self._motorista(agent=20, cpf="11144477735", nome="Outro")
        outra, pasta_outra = self._ficha_de(mid, "11144477735")
        acoes.excluir(self.conn, self.config, mid, "hugo")
        row = self.conn.execute("SELECT status, bd_motorista_id FROM motoristas_cadastros WHERE id = ?",
                                (outra,)).fetchone()
        self.assertEqual((row["status"], row["bd_motorista_id"]), (cad.STATUS_APROVADO, None))
        self.assertTrue(pasta_outra.exists())
        atu.vincular_fichas(self.conn)
        self.assertEqual(self.conn.execute("SELECT bd_motorista_id FROM motoristas_cadastros WHERE id = ?",
                                           (outra,)).fetchone()[0], dono)

    def test_excluir_pessoa_sem_cpf_so_recusa_ficha_sem_outra_linha_dona(self):
        """C2 (c): pessoa sem CPF: ficha cujo CPF é de outra linha da tabela fica como está."""
        mid = self._motorista(agent=10, cpf=None)
        self._motorista(agent=20, cpf="11144477735", nome="Outro")
        sem_dono, pasta_sem_dono = self._ficha_de(mid, CPF)
        de_outro, pasta_de_outro = self._ficha_de(mid, "11144477735")
        acoes.excluir(self.conn, self.config, mid, "hugo")
        self.assertEqual((self._status(sem_dono), self._status(de_outro)), (cad.STATUS_RECUSADO, cad.STATUS_APROVADO))
        self.assertFalse(pasta_sem_dono.exists())
        self.assertTrue(pasta_de_outro.exists())

    def test_excluir_encerra_ficha_do_link_pro_retentar_nao_pegar(self):
        """M2: ficha de origem link recusada na exclusão ganha concluido_em (o retentar não filtra status)."""
        from nucleo import cadastro_motorista_link as cml
        mid = self._motorista()
        ficha, _ = self._ficha_de(mid, CPF, origem=cad.ORIGEM_LINK, enviado_em="2026-11-01 10:05:00")
        acoes.excluir(self.conn, self.config, mid, "hugo")
        self.assertIsNotNone(self.conn.execute("SELECT concluido_em FROM motoristas_cadastros WHERE id = ?",
                                               (ficha,)).fetchone()[0])
        with mock.patch.object(cml, "processar", return_value={"situacao": "concluido", "erro": None}) as proc:
            self.assertEqual(cml.retentar(self.conn, {"cadastro_motorista_link": {"ativo": True}}), [])
        proc.assert_not_called()


class TestResetarPin(_Base):
    def test_resetar_pin_marca_trocar_pin_e_derruba_sessao(self):
        mid = self._motorista()
        login = self._login()
        tokens = auth.emitir_tokens("segredo", login)
        auth.motorista_do_token_acesso("segredo", tokens["acesso"], self.conn)   # valida antes
        r = acoes.resetar_pin(self.conn, mid, "hugo")
        self.assertFalse(r["criado"])
        self.assertRegex(r["pin"], r"^\d{6}$")
        self.assertEqual(r["cpf"], CPF)
        self.assertEqual(auth.buscar_motorista(self.conn, CPF)["trocar_pin"], 1)
        auth.autenticar(self.conn, CPF, r["pin"])
        with self.assertRaises(auth.AutenticacaoInvalida):
            auth.motorista_do_token_acesso("segredo", tokens["acesso"], self.conn)

    def test_resetar_pin_preserva_perfil_agent_e_ve_financeiro(self):
        mid = self._motorista()
        self._login(perfil=auth.PERFIL_TESTE, agent_id=77)
        self.conn.execute("UPDATE motoristas SET ve_financeiro = 0 WHERE cpf = ?", (CPF,))
        self.conn.commit()
        acoes.resetar_pin(self.conn, mid, "hugo")
        m = auth.buscar_motorista(self.conn, CPF)
        self.assertEqual((m["perfil"], m["agent_id"], m["ve_financeiro"]), ("TESTE", 77, 0))

    def test_resetar_pin_sem_login_cria(self):
        mid = self._motorista()
        r = acoes.resetar_pin(self.conn, mid, "hugo")
        self.assertTrue(r["criado"])
        m = auth.buscar_motorista(self.conn, CPF)
        self.assertEqual((m["nome"], m["agent_id"], m["trocar_pin"]), ("Ana", 10, 1))

    def test_resetar_pin_sem_cpf_recusa(self):
        mid = self._motorista(cpf=None)
        with self.assertRaises(acoes.AcaoRecusada) as e:
            acoes.resetar_pin(self.conn, mid, "hugo")
        self.assertEqual((e.exception.codigo, e.exception.mensagem), (400, "falta o CPF"))

    def test_resetar_pin_nao_grava_pin_no_historico(self):
        mid = self._motorista()
        r = acoes.resetar_pin(self.conn, mid, "hugo")
        rows = self.conn.execute("SELECT * FROM bd_motoristas_alteracoes WHERE campo = 'PIN_RESETADO'").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["origem"], "painel:hugo")
        self.assertNotIn(r["pin"], "".join(str(v) for v in tuple(rows[0])))
        self.assertIsNone(rows[0]["valor_novo"])


if __name__ == "__main__":
    unittest.main()
