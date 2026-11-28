# -*- coding: utf-8 -*-
"""
test_atualizacao_motorista.py

Base da atualização cadastral: pessoas (linhas da bd_motoristas por CPF),
ficha vigente e pendências; link pessoal (gerar, abrir com CPF, gravar,
concluir e repassar). SQLite temporário.
    py -3.11 -m unittest nucleo.test_atualizacao_motorista -v
"""
import hashlib
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))

from nucleo import atualizacao_motorista as atu, banco, cadastro_motorista as cad  # noqa: E402
from nucleo import cadastro_motorista_link as cml  # noqa: E402
from regras import motoristas_banco  # noqa: E402


def _cpf(base9: str) -> str:
    d = [int(c) for c in base9]
    for n in (9, 10):
        d.append(sum(d[i] * (n + 1 - i) for i in range(n)) * 10 % 11 % 10)
    return "".join(map(str, d))


CPF_X = "52998224725"
CPF_Y = _cpf("111444777")
CPF_Z = _cpf("123456789")
JPG = b"\xff\xd8\xff\xe0" + b"0" * 50


def _dados_ficha(**extra):
    d = {"nome": "Fulano de Tal", "data_nascimento": "1990-05-17", "telefone": "(11) 99876-5432",
         "email": "Fulano@Exemplo.com", "chave_pix": "pix-novo@exemplo.com", "tipo_veiculo": "vuc",
         "placa": "abc-1d23", "veiculo_modelo": "Fiat Fiorino", "veiculo_ano": 2020, "refrigerado": False,
         # nada disto pode passar pelo link
         "zonas": ["ZONA SUL"], "dias": ["SEGUNDA"], "posicao": "FIXO", "celular": "IPHONE",
         "apenas_carga_seca": True}
    d.update(extra)
    return d


class TestAtualizacaoMotorista(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._patch = mock.patch.object(banco, "DB_PATH", Path(self._tmp.name) / "t.db")
        self._patch.start()
        self.conn = banco.conectar()

    def tearDown(self):
        self.conn.close()
        self._patch.stop()
        self._tmp.cleanup()

    def _linha(self, nome="Fulano", cpf=None, agent=None, ativo="SIM", **extra):
        agora = banco.agora()
        cols = {"NOME_MOTORISTA": nome, "CPF_MOTORISTA": cpf, "AGENT_ID_VUUPT": agent, "ATIVO": ativo,
                "criado_em": agora, "atualizado_em": agora}
        cols.update(extra)
        c = self.conn.execute(
            f"INSERT INTO bd_motoristas ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
            list(cols.values()))
        self.conn.commit()
        return c.lastrowid

    def _ficha(self, status="APROVADO", origem="app", agent_id=None, bd_id=None, **extra):
        cols = {"cpf": CPF_X, "nome": "Fulano", "chave_envio": "k", "status": status, "origem": origem,
                "agent_id": agent_id, "bd_motorista_id": bd_id, "criado_em": banco.agora()}
        cols.update(extra)
        c = self.conn.execute(
            f"INSERT INTO motoristas_cadastros ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
            list(cols.values()))
        self.conn.commit()
        return c.lastrowid

    def _pessoa(self, principal_id):
        return next(p for p in atu.pessoas(self.conn) if p["principal_id"] == principal_id)

    def test_pessoas_junta_mesmo_cpf_e_mesmo_agent_sem_cpf(self):
        a = self._linha("A", cpf=CPF_X, agent=1)
        b = self._linha("B", cpf="529.982.247-25", agent=2)
        c = self._linha("C", agent=3)
        d = self._linha("D", agent=3, ativo="NAO")
        ps = atu.pessoas(self.conn)
        self.assertEqual(len(ps), 2)
        p1 = next(p for p in ps if p["principal_id"] == a)
        p2 = next(p for p in ps if p["principal_id"] == c)
        self.assertEqual(p1["ids"], [a, b])
        self.assertEqual(p1["agent_ids"], [1, 2])
        self.assertEqual(p2["ids"], [c, d])
        self.assertEqual(p2["agent_ids"], [3])
        self.assertTrue(p2["ativo"])
        self.assertEqual(p1["cpf"], CPF_X)

    def test_vincular_fichas_do_app_por_agent_id_e_idempotente(self):
        lid = self._linha(agent=7)
        fid = self._ficha(agent_id=7)
        self.assertEqual(atu.vincular_fichas(self.conn), 1)
        self.assertEqual(self.conn.execute("SELECT bd_motorista_id FROM motoristas_cadastros WHERE id=?",
                                           (fid,)).fetchone()[0], lid)
        self.assertEqual(atu.vincular_fichas(self.conn), 0)

    def test_ficha_vigente_e_a_aprovada_mais_recente(self):
        lid = self._linha(agent=7)
        self._ficha(agent_id=7, bd_id=lid, nome="Antiga")
        f2 = self._ficha(agent_id=7, bd_id=lid, nome="Nova")
        self._ficha(status="PENDENTE", agent_id=7, bd_id=lid, nome="Pendente")
        v = atu.ficha_vigente(self.conn, lid)
        self.assertEqual(v["id"], f2)

    def test_pendencias_sem_ficha_lista_os_cinco_documentos(self):
        lid = self._linha()
        pend = atu.pendencias(self.conn, self._pessoa(lid))
        self.assertEqual(pend["documentos"], ["cnh", "crlv", "selfie", "antt", "residencia"])

    def test_pendencias_ficha_do_app_falta_selfie_antt_residencia_e_nascimento(self):
        lid = self._linha(cpf=CPF_X, agent=7, TELEFONE_MOTORISTA="11998765432", PLACA="ABC1D23")
        self._ficha(agent_id=7, bd_id=lid, cnh_arquivo="a.jpg", crlv_arquivo="b.jpg",
                    chave_pix="x@y.com", veiculo_modelo="Fiorino", veiculo_ano=2020)
        pend = atu.pendencias(self.conn, self._pessoa(lid))
        self.assertEqual(pend["documentos"], ["selfie", "antt", "residencia"])
        self.assertEqual(pend["campos"], ["nascimento"])
        self.assertFalse(pend["falta_cpf"])

    def test_pendencias_iguais_nas_duas_linhas_da_pessoa(self):
        a = self._linha("A", cpf=CPF_X, agent=1)
        b = self._linha("B", cpf=CPF_X, agent=2, TELEFONE_MOTORISTA="11998765432", PLACA="ABC1D23")
        self._ficha(agent_id=2, bd_id=b, cnh_arquivo="a.jpg")
        pessoa = self._pessoa(a)
        self.assertEqual(pessoa["ids"], [a, b])
        pend = atu.pendencias(self.conn, pessoa)
        self.assertNotIn("telefone", pend["campos"])   # vem da linha B
        self.assertNotIn("placa", pend["campos"])
        self.assertEqual(pend["documentos"], ["crlv", "selfie", "antt", "residencia"])
        invertida = dict(pessoa, linhas=list(reversed(pessoa["linhas"])))
        self.assertEqual(atu.pendencias(self.conn, invertida), pend)

    def test_pix_do_login_conta(self):
        lid = self._linha(cpf=CPF_X, agent=7)
        self._ficha(agent_id=7, bd_id=lid)
        self.assertIn("PIX", atu.pendencias(self.conn, self._pessoa(lid))["campos"])
        self.conn.execute("INSERT INTO motoristas (cpf, nome, pin_hash, pin_salt, chave_pix, criado_em, atualizado_em) "
                          "VALUES (?, 'F', 'h', 's', 'pix@x.com', ?, ?)", (CPF_X, banco.agora(), banco.agora()))
        self.conn.commit()
        self.assertNotIn("PIX", atu.pendencias(self.conn, self._pessoa(lid))["campos"])

    def test_veiculo_diferente(self):
        lid = self._linha(agent=7, TIPO_VEICULO="FIORINO")
        self._ficha(agent_id=7, bd_id=lid, tipo_veiculo="fiorino")
        self.assertFalse(atu.pendencias(self.conn, self._pessoa(lid))["veiculo_diferente"])
        self.conn.execute("UPDATE motoristas_cadastros SET tipo_veiculo = 'vuc'")
        self.conn.commit()
        self.assertTrue(atu.pendencias(self.conn, self._pessoa(lid))["veiculo_diferente"])

    # ── Link pessoal ──────────────────────────────────────────────────────

    def _link_row(self, link_id):
        return dict(self.conn.execute("SELECT * FROM motoristas_links_atualizacao WHERE id = ?",
                                      (link_id,)).fetchone())

    def _cadastro_row(self, cadastro_id):
        return dict(self.conn.execute("SELECT * FROM motoristas_cadastros WHERE id = ?", (cadastro_id,)).fetchone())

    def _login(self, cpf, agent_id=None, chave_pix=None):
        self.conn.execute("INSERT INTO motoristas (cpf, nome, pin_hash, pin_salt, agent_id, chave_pix, criado_em, "
                          "atualizado_em) VALUES (?, 'F', 'h', 's', ?, ?, ?, ?)",
                          (cpf, agent_id, chave_pix, banco.agora(), banco.agora()))
        self.conn.commit()

    def _docs(self, r, tipos):
        for t in tipos:
            cad.guardar_documento(self.conn, r["cadastro_id"], r["chave_envio"], t, f"{t}.jpg", JPG)

    def _pronto(self, lid, cpf=CPF_X, docs=("cnh", "crlv", "selfie"), **dados):
        """Gera, abre, preenche e manda os documentos. Devolve (token, abrir)."""
        token = atu.gerar_link(self.conn, lid, "hugo")["token"]
        r = atu.abrir(self.conn, token, cpf)
        atu.gravar(self.conn, token, r["chave_envio"], _dados_ficha(**dados))
        self._docs(r, docs)
        return token, r

    def test_gerar_guarda_so_o_hash_e_cancela_o_anterior(self):
        lid = self._linha(cpf=CPF_X)
        agora = datetime(2026, 11, 16, 10, 0, 0)
        l1 = atu.gerar_link(self.conn, lid, "hugo", agora=agora)
        self.assertGreaterEqual(len(l1["token"]), 43)
        row1 = self._link_row(l1["id"])
        self.assertEqual(row1["token_hash"], hashlib.sha256(l1["token"].encode()).hexdigest())
        self.assertNotIn(l1["token"], [str(v) for v in row1.values()])
        self.assertEqual((row1["bd_motorista_id"], row1["criado_por"]), (lid, "hugo"))
        self.assertEqual(l1["expira_em"], "2026-12-01 10:00:00")
        self.assertEqual(row1["expira_em"], "2026-12-01 10:00:00")

        l2 = atu.gerar_link(self.conn, lid, "hugo", agora=agora)
        self.assertIsNotNone(self._link_row(l1["id"])["cancelado_em"])
        self.assertIsNone(self._link_row(l2["id"])["cancelado_em"])
        self.assertEqual(atu.link_em_aberto(self.conn, lid, agora=agora)["id"], l2["id"])
        with self.assertRaises(atu.LinkInvalido) as cx:
            atu.abrir(self.conn, l1["token"], CPF_X, agora=agora)
        self.assertEqual(cx.exception.codigo, 404)

    def test_link_em_aberto_e_estado_link(self):
        lid = self._linha(cpf=CPF_X)
        agora = datetime.now()
        self.assertIsNone(atu.estado_link(self.conn, lid))
        atu.gerar_link(self.conn, lid, "hugo", piloto=True)
        self.assertIsNone(atu.link_em_aberto(self.conn, lid))   # piloto não conta como enviado
        self.assertIsNone(atu.estado_link(self.conn, lid))
        link = atu.gerar_link(self.conn, lid, "hugo")
        self.assertEqual(atu.estado_link(self.conn, lid), "enviado")
        atu.abrir(self.conn, link["token"], CPF_X)
        self.assertEqual(atu.estado_link(self.conn, lid), "aberto")
        depois = agora + timedelta(days=16)
        self.assertEqual(atu.estado_link(self.conn, lid, agora=depois), "vencido")
        self.assertIsNone(atu.link_em_aberto(self.conn, lid, agora=depois))
        self.assertEqual(atu.link_em_aberto(self.conn, lid)["id"], link["id"])

    def test_abrir_cpf_errado_cinco_vezes_cancela(self):
        lid = self._linha(cpf=CPF_X)
        token = atu.gerar_link(self.conn, lid, "hugo")["token"]
        for _ in range(4):
            with self.assertRaises(atu.LinkInvalido) as cx:
                atu.abrir(self.conn, token, CPF_Y)
            self.assertEqual(cx.exception.codigo, 403)
        with self.assertRaises(atu.LinkInvalido) as cx:
            atu.abrir(self.conn, token, CPF_Y)
        self.assertEqual(cx.exception.codigo, 429)
        row = self.conn.execute("SELECT * FROM motoristas_links_atualizacao").fetchone()
        self.assertEqual(row["tentativas_cpf"], 5)
        self.assertIsNotNone(row["cancelado_em"])
        with self.assertRaises(atu.LinkInvalido) as cx:   # cancelado: nem o CPF certo abre
            atu.abrir(self.conn, token, CPF_X)
        self.assertEqual(cx.exception.codigo, 404)

    def test_abrir_linha_sem_cpf_aceita_cpf_valido_e_recusa_o_de_outro_motorista(self):
        lid = self._linha("Sem CPF", agent=7)
        self._linha("Outro", cpf=CPF_Y, agent=8)
        self._login(CPF_Z, agent_id=99)
        link = atu.gerar_link(self.conn, lid, "hugo")
        for cpf, tentativas in ((CPF_Y, 1), (CPF_Z, 2)):
            with self.assertRaises(atu.LinkInvalido) as cx:
                atu.abrir(self.conn, link["token"], cpf)
            self.assertEqual(cx.exception.codigo, 409)
            row = self._link_row(link["id"])
            self.assertEqual(row["tentativas_cpf"], tentativas)
            self.assertIsNone(row["cadastro_id"])
            self.assertIsNone(row["aberto_em"])
            self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM motoristas_cadastros").fetchone()[0], 0)
        with self.assertRaises(atu.LinkInvalido) as cx:
            atu.abrir(self.conn, link["token"], "12345678900")   # dígito verificador errado
        self.assertEqual(cx.exception.codigo, 403)

        r = atu.abrir(self.conn, link["token"], "529.982.247-25")
        self.assertEqual(r["dados"]["cpf"], CPF_X)
        self.assertIsNone(self.conn.execute("SELECT CPF_MOTORISTA FROM bd_motoristas WHERE id = ?",
                                            (lid,)).fetchone()[0])   # só vai pra tabela no concluir
        with self.assertRaises(atu.LinkInvalido) as cx:   # depois de aberto, o CPF fica o do rascunho
            atu.abrir(self.conn, link["token"], _cpf("987654321"))
        self.assertEqual(cx.exception.codigo, 403)
        self.assertEqual(atu.abrir(self.conn, link["token"], CPF_X)["cadastro_id"], r["cadastro_id"])

    def test_abrir_linha_sem_cpf_com_login_exige_o_cpf_do_login(self):
        lid = self._linha("Sem CPF", agent=7)
        self._login(CPF_X, agent_id=7)
        token = atu.gerar_link(self.conn, lid, "hugo")["token"]
        with self.assertRaises(atu.LinkInvalido) as cx:
            atu.abrir(self.conn, token, CPF_Y)
        self.assertEqual(cx.exception.codigo, 403)
        self.assertEqual(atu.abrir(self.conn, token, CPF_X)["dados"]["cpf"], CPF_X)

    def test_abrir_duas_vezes_devolve_o_mesmo_rascunho(self):
        lid = self._linha("Fulano de Tal", cpf=CPF_X, agent=7, TELEFONE_MOTORISTA="11998765432",
                          PLACA="ABC1D23", TIPO_VEICULO="FIORINO", EMAIL_MOTORISTA="f@x.com")
        link = atu.gerar_link(self.conn, lid, "hugo")
        r1 = atu.abrir(self.conn, link["token"], "529.982.247-25")
        r2 = atu.abrir(self.conn, link["token"], CPF_X)
        self.assertEqual((r1["cadastro_id"], r1["chave_envio"]), (r2["cadastro_id"], r2["chave_envio"]))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM motoristas_cadastros").fetchone()[0], 1)
        c = self._cadastro_row(r1["cadastro_id"])
        self.assertEqual((c["origem"], c["status"], c["bd_motorista_id"]), ("atualizacao", "PENDENTE", lid))
        self.assertEqual((r1["dados"]["nome"], r1["dados"]["telefone"], r1["dados"]["placa"],
                          r1["dados"]["tipo_veiculo"], r1["dados"]["email"]),
                         ("Fulano de Tal", "11998765432", "ABC1D23", "FIORINO", "f@x.com"))
        self.assertEqual(r1["documentos"], {t: False for t in cad.DOCUMENTOS})
        row = self._link_row(link["id"])
        self.assertEqual(row["cadastro_id"], r1["cadastro_id"])
        self.assertIsNotNone(row["aberto_em"])

    def test_abrir_traz_documentos_da_ficha_vigente(self):
        lid = self._linha(cpf=CPF_X, agent=7)
        fid = self._ficha(agent_id=7, bd_id=lid, cnh_arquivo="cnh.jpg", crlv_arquivo="crlv.pdf",
                          residencia_arquivo="residencia.png",   # sem arquivo no disco: não conta
                          chave_pix="antigo@pix", data_nascimento="1990-05-17", veiculo_modelo="Fiorino",
                          veiculo_ano=2019)
        pasta = cad.pasta_cadastros() / str(fid)
        pasta.mkdir(parents=True)
        (pasta / "cnh.jpg").write_bytes(JPG)
        (pasta / "crlv.pdf").write_bytes(b"%PDF-1.4\n")
        r = atu.abrir(self.conn, atu.gerar_link(self.conn, lid, "hugo")["token"], CPF_X)
        self.assertEqual(r["documentos"], {"cnh": True, "crlv": True, "selfie": False, "antt": False,
                                           "residencia": False})
        self.assertEqual((cad.pasta_cadastros() / str(r["cadastro_id"]) / "cnh.jpg").read_bytes(), JPG)
        self.assertEqual((r["dados"]["chave_pix"], r["dados"]["data_nascimento"], r["dados"]["veiculo_modelo"],
                          r["dados"]["veiculo_ano"]), ("antigo@pix", "1990-05-17", "Fiorino", 2019))
        self.assertTrue((pasta / "cnh.jpg").is_file())   # a ficha antiga fica como estava
        self.assertEqual(self._cadastro_row(fid)["status"], "APROVADO")

    def test_abrir_vencido_410(self):
        lid = self._linha(cpf=CPF_X)
        token = atu.gerar_link(self.conn, lid, "hugo", agora=datetime(2026, 1, 1, 8, 0, 0))["token"]
        with self.assertRaises(atu.LinkInvalido) as cx:
            atu.abrir(self.conn, token, CPF_X, agora=datetime(2026, 1, 16, 8, 0, 1))
        self.assertEqual(cx.exception.codigo, 410)
        atu.abrir(self.conn, token, CPF_X, agora=datetime(2026, 1, 16, 7, 59, 0))
        with self.assertRaises(atu.LinkInvalido) as cx:
            atu.abrir(self.conn, "nao-existe", CPF_X)
        self.assertEqual(cx.exception.codigo, 404)

    def test_gravar_valida_e_ignora_zonas_dias_posicao(self):
        lid = self._linha(cpf=CPF_X)
        token = atu.gerar_link(self.conn, lid, "hugo")["token"]
        r = atu.abrir(self.conn, token, CPF_X)
        with self.assertRaises(cad.CadastroInvalido):
            atu.gravar(self.conn, token, r["chave_envio"], _dados_ficha(veiculo_ano=None))
        with self.assertRaises(cad.CadastroInvalido):
            atu.gravar(self.conn, token, r["chave_envio"], _dados_ficha(email="sem-arroba"))
        with self.assertRaises(atu.LinkInvalido) as cx:
            atu.gravar(self.conn, token, "chave-errada", _dados_ficha())
        self.assertEqual(cx.exception.codigo, 404)
        g = atu.gravar(self.conn, token, r["chave_envio"], _dados_ficha())
        self.assertEqual((g["dados"]["telefone"], g["dados"]["placa"], g["dados"]["tipo_veiculo"],
                          g["dados"]["email"]), ("11998765432", "ABC1D23", "VUC", "fulano@exemplo.com"))
        c = self._cadastro_row(r["cadastro_id"])
        self.assertEqual((c["zonas"], c["dias"], c["posicao"], c["celular"]), (None, None, None, None))

    def test_exigir_link_vivo_do_rascunho(self):
        lid = self._linha(cpf=CPF_X)
        l1 = atu.gerar_link(self.conn, lid, "hugo")
        cid = atu.abrir(self.conn, l1["token"], CPF_X)["cadastro_id"]
        atu.exigir_link_vivo_do_rascunho(self.conn, cid)   # vivo: não levanta
        with self.assertRaises(atu.LinkInvalido) as cx:   # vencido
            atu.exigir_link_vivo_do_rascunho(self.conn, cid, agora=datetime.now() + timedelta(days=16))
        self.assertEqual(cx.exception.codigo, 410)
        with self.assertRaises(atu.LinkInvalido) as cx:   # rascunho sem link dono
            atu.exigir_link_vivo_do_rascunho(self.conn, self._ficha(status="PENDENTE", origem="atualizacao"))
        self.assertEqual(cx.exception.codigo, 404)
        atu.gerar_link(self.conn, lid, "hugo")   # cancela o l1
        with self.assertRaises(atu.LinkInvalido) as cx:
            atu.exigir_link_vivo_do_rascunho(self.conn, cid)
        self.assertEqual(cx.exception.codigo, 404)
        l3 = atu.gerar_link(self.conn, lid, "hugo")
        token, r = self._pronto(lid)
        self.assertNotEqual(l3["token"], token)
        atu.concluir(self.conn, {}, token, r["chave_envio"])
        with self.assertRaises(atu.LinkInvalido) as cx:   # concluído
            atu.exigir_link_vivo_do_rascunho(self.conn, r["cadastro_id"])
        self.assertEqual(cx.exception.codigo, 410)

    def test_concluir_sem_selfie_recusa(self):
        lid = self._linha(cpf=CPF_X)
        token, r = self._pronto(lid, docs=("cnh", "crlv", "antt", "residencia"))
        with self.assertRaises(cad.CadastroInvalido):
            atu.concluir(self.conn, {}, token, r["chave_envio"])
        self.assertEqual(self._cadastro_row(r["cadastro_id"])["status"], "PENDENTE")
        self.assertIsNone(self.conn.execute("SELECT concluido_em FROM motoristas_links_atualizacao").fetchone()[0])

    def test_concluir_sem_antt_e_residencia_aceita_e_lista_pendentes(self):
        lid = self._linha(cpf=CPF_X)
        token, r = self._pronto(lid)
        self.assertEqual(atu.concluir(self.conn, {}, token, r["chave_envio"]),
                         {"ok": True, "pendentes": ["antt", "residencia"], "pix_em_analise": False})
        c = self._cadastro_row(r["cadastro_id"])
        self.assertEqual(c["status"], "APROVADO")
        self.assertIsNotNone(c["enviado_em"])
        self.assertIsNotNone(c["concluido_em"])
        self.assertIsNotNone(self.conn.execute("SELECT concluido_em FROM motoristas_links_atualizacao").fetchone()[0])
        self.assertEqual(atu.ficha_vigente(self.conn, lid)["id"], r["cadastro_id"])
        self.assertEqual(atu.estado_link(self.conn, lid), "concluido")
        self.assertIsNone(atu.link_em_aberto(self.conn, lid))
        with self.assertRaises(atu.LinkInvalido) as cx:
            atu.abrir(self.conn, token, CPF_X)
        self.assertEqual(cx.exception.codigo, 410)
        with self.assertRaises(atu.LinkInvalido) as cx:
            atu.gravar(self.conn, token, r["chave_envio"], _dados_ficha())
        self.assertEqual(cx.exception.codigo, 410)

    def test_concluir_repassa_telefone_email_cpf_e_pix_mas_nao_tipo_veiculo_nem_zonas(self):
        lid = self._linha("Fulano", agent=7, TIPO_VEICULO="FIORINO", ZONAS_PREFERIDAS="ZONA NORTE",
                          DIAS_DISPONIVEIS="TERCA", POSICAO="ESPORADICO", APENAS_CARGA_SECA="NAO",
                          TELEFONE_MOTORISTA="11911112222")
        self._login(CPF_X, agent_id=7, chave_pix="pix-antigo")
        token, r = self._pronto(lid)
        atu.concluir(self.conn, {}, token, r["chave_envio"])

        linha = dict(self.conn.execute("SELECT * FROM bd_motoristas WHERE id = ?", (lid,)).fetchone())
        self.assertEqual((linha["CPF_MOTORISTA"], linha["TELEFONE_MOTORISTA"], linha["EMAIL_MOTORISTA"],
                          linha["PLACA"]), (CPF_X, "11998765432", "fulano@exemplo.com", "ABC1D23"))
        self.assertEqual((linha["TIPO_VEICULO"], linha["ZONAS_PREFERIDAS"], linha["DIAS_DISPONIVEIS"],
                          linha["POSICAO"], linha["APENAS_CARGA_SECA"], linha["NOME_MOTORISTA"]),
                         ("FIORINO", "ZONA NORTE", "TERCA", "ESPORADICO", "NAO", "Fulano"))
        hist = self.conn.execute("SELECT campo, origem FROM bd_motoristas_alteracoes WHERE motorista_id = ? "
                                 "AND campo != '*criado*'", (lid,)).fetchall()
        self.assertEqual({h["campo"] for h in hist},
                         {"CPF_MOTORISTA", "TELEFONE_MOTORISTA", "EMAIL_MOTORISTA", "PLACA"})
        self.assertEqual({h["origem"] for h in hist}, {"link_atualizacao"})

        login = dict(self.conn.execute("SELECT * FROM motoristas WHERE cpf = ?", (CPF_X,)).fetchone())
        self.assertEqual(login["chave_pix"], "pix-antigo")   # troca de PIX espera a equipe
        self.assertEqual(login["telefone"], "11998765432")   # mesmo repasse da edição pelo painel
        pessoa = next(p for p in atu.pessoas(self.conn) if lid in p["ids"])
        self.assertEqual(atu.pix_pendente(self.conn, pessoa)["novo"], "pix-novo@exemplo.com")
        atu.decidir_pix(self.conn, lid, True, "hugo")
        alt = self.conn.execute("SELECT valor_antigo, valor_novo FROM motoristas_alteracoes "
                                "WHERE cpf = ? AND campo = 'chave_pix'", (CPF_X,)).fetchone()
        self.assertEqual(tuple(alt), ("pix-antigo", "pix-novo@exemplo.com"))
        self.assertTrue(motoristas_banco.caminho_espelho().is_file())
        self.assertTrue(atu.pendencias(self.conn, self._pessoa(lid))["veiculo_diferente"])

    def test_concluir_pessoa_com_duas_linhas_nao_repassa_placa(self):
        a = self._linha("A", cpf=CPF_X, agent=1, PLACA="AAA1A11")
        b = self._linha("B", cpf=CPF_X, agent=2, PLACA="BBB2B22")
        token, r = self._pronto(a)
        atu.concluir(self.conn, {}, token, r["chave_envio"])
        linhas = {l["id"]: l for l in motoristas_banco.listar(self.conn)}
        self.assertEqual((linhas[a]["PLACA"], linhas[b]["PLACA"]), ("AAA1A11", "BBB2B22"))
        for lid in (a, b):
            self.assertEqual((linhas[lid]["TELEFONE_MOTORISTA"], linhas[lid]["EMAIL_MOTORISTA"]),
                             ("11998765432", "fulano@exemplo.com"))

    def test_concluir_duas_vezes_nao_duplica(self):
        lid = self._linha(agent=7)
        self._login(CPF_X, agent_id=7, chave_pix="pix-antigo")
        token, r = self._pronto(lid)
        primeiro = atu.concluir(self.conn, {}, token, r["chave_envio"])
        hist = [tuple(h) for h in self.conn.execute("SELECT * FROM bd_motoristas_alteracoes ORDER BY id")]
        alts = [tuple(h) for h in self.conn.execute("SELECT * FROM motoristas_alteracoes ORDER BY id")]
        self.assertEqual(atu.concluir(self.conn, {}, token, r["chave_envio"]), primeiro)
        self.assertEqual([tuple(h) for h in self.conn.execute("SELECT * FROM bd_motoristas_alteracoes ORDER BY id")],
                         hist)
        self.assertEqual([tuple(h) for h in self.conn.execute("SELECT * FROM motoristas_alteracoes ORDER BY id")],
                         alts)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM motoristas_cadastros WHERE status = 'APROVADO' "
                                           "AND bd_motorista_id = ?", (lid,)).fetchone()[0], 1)
        with self.assertRaises(atu.LinkInvalido) as cx:
            atu.concluir(self.conn, {}, token, "chave-errada")
        self.assertEqual(cx.exception.codigo, 404)

    # ── Revisão, rodada 1 ─────────────────────────────────────────────────

    def test_concluir_cpf_que_virou_de_outro_motorista_409(self):
        lid = self._linha("Sem CPF", agent=7)
        token, r = self._pronto(lid)   # aberto com CPF_X, livre naquela hora
        outro = self._linha("Outro", cpf=CPF_X, agent=8)
        for _ in range(2):
            with self.assertRaises(atu.LinkInvalido) as cx:
                atu.concluir(self.conn, {}, token, r["chave_envio"])
            self.assertEqual(cx.exception.codigo, 409)
            self.assertEqual(self._cadastro_row(r["cadastro_id"])["status"], "PENDENTE")
            self.assertIsNone(self.conn.execute("SELECT concluido_em FROM motoristas_links_atualizacao").fetchone()[0])
            self.assertIsNone(self.conn.execute("SELECT CPF_MOTORISTA FROM bd_motoristas WHERE id = ?",
                                                (lid,)).fetchone()[0])
            self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM bd_motoristas_alteracoes WHERE motorista_id = ? "
                                               "AND campo != '*criado*'", (lid,)).fetchone()[0], 0)
            # 2ª volta: o CPF agora é de um login de outro agente (e não de linha da tabela)
            self.conn.execute("DELETE FROM bd_motoristas WHERE id = ?", (outro,))
            self.conn.commit()
            if not self.conn.execute("SELECT 1 FROM motoristas WHERE cpf = ?", (CPF_X,)).fetchone():
                self._login(CPF_X, agent_id=99, chave_pix="pix-do-outro")
        self.assertEqual(self.conn.execute("SELECT chave_pix FROM motoristas WHERE cpf = ?",
                                           (CPF_X,)).fetchone()[0], "pix-do-outro")

    def test_cpf_preenchido_pela_equipe_depois_de_aberto(self):
        lid = self._linha("Sem CPF", agent=7)
        token, r = self._pronto(lid, cpf=CPF_Y)
        motoristas_banco.atualizar(self.conn, lid, {"CPF_MOTORISTA": CPF_X}, "painel:hugo")
        self._login(CPF_Y, agent_id=99, chave_pix="pix-do-y")
        self._login(CPF_X, agent_id=7, chave_pix="pix-antigo")
        with self.assertRaises(atu.LinkInvalido) as cx:   # rascunho ainda com Y, tabela agora com X
            atu.concluir(self.conn, {}, token, r["chave_envio"])
        self.assertEqual(cx.exception.codigo, 409)
        with self.assertRaises(atu.LinkInvalido) as cx:
            atu.abrir(self.conn, token, CPF_Y)
        self.assertEqual(cx.exception.codigo, 403)
        r2 = atu.abrir(self.conn, token, CPF_X)
        self.assertEqual((r2["cadastro_id"], r2["dados"]["cpf"]), (r["cadastro_id"], CPF_X))
        atu.concluir(self.conn, {}, token, r["chave_envio"])
        pix = dict(self.conn.execute("SELECT cpf, chave_pix FROM motoristas").fetchall())
        # trocar PIX que já existia espera a aprovação da equipe; o de Y nunca é tocado
        self.assertEqual(pix, {CPF_X: "pix-antigo", CPF_Y: "pix-do-y"})

    def test_dois_links_disputando_o_mesmo_cpf(self):
        p = self._linha("P", agent=7)
        q = self._linha("Q", agent=8)
        tp = atu.gerar_link(self.conn, p, "hugo")["token"]
        tq = atu.gerar_link(self.conn, q, "hugo")["token"]
        atu.abrir(self.conn, tp, CPF_X)
        with self.assertRaises(atu.LinkInvalido) as cx:
            atu.abrir(self.conn, tq, CPF_X)
        self.assertEqual(cx.exception.codigo, 409)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM motoristas_cadastros").fetchone()[0], 1)
        atu.gerar_link(self.conn, p, "hugo")   # link de P cancelado: X volta a ficar livre
        self.assertEqual(atu.abrir(self.conn, tq, CPF_X)["dados"]["cpf"], CPF_X)

    def test_link_piloto_nao_cancela_o_real_e_o_real_cancela_o_piloto(self):
        lid = self._linha(cpf=CPF_X)
        real = atu.gerar_link(self.conn, lid, "hugo")
        piloto = atu.gerar_link(self.conn, lid, "hugo", piloto=True)
        self.assertIsNone(self._link_row(real["id"])["cancelado_em"])
        self.assertEqual(atu.link_em_aberto(self.conn, lid)["id"], real["id"])
        piloto2 = atu.gerar_link(self.conn, lid, "hugo", piloto=True)
        self.assertIsNotNone(self._link_row(piloto["id"])["cancelado_em"])
        self.assertIsNone(self._link_row(real["id"])["cancelado_em"])
        atu.gerar_link(self.conn, lid, "hugo")
        self.assertIsNotNone(self._link_row(piloto2["id"])["cancelado_em"])
        self.assertIsNotNone(self._link_row(real["id"])["cancelado_em"])

    def test_concluir_linha_com_cpf_sem_digitos_repassa_o_resto(self):
        a = self._linha("A", cpf="N/A", agent=7)
        b = self._linha("B", agent=7)
        token, r = self._pronto(a)
        atu.concluir(self.conn, {}, token, r["chave_envio"])
        linhas = {l["id"]: l for l in motoristas_banco.listar(self.conn)}
        self.assertEqual((linhas[a]["CPF_MOTORISTA"], linhas[a]["TELEFONE_MOTORISTA"]), ("N/A", "11998765432"))
        self.assertEqual((linhas[b]["CPF_MOTORISTA"], linhas[b]["TELEFONE_MOTORISTA"]), (CPF_X, "11998765432"))

    def test_concluir_linha_que_falha_nao_impede_as_outras(self):
        a = self._linha("A", agent=7)
        b = self._linha("B", agent=7)
        token, r = self._pronto(a)
        original = motoristas_banco.atualizar

        def falha_em_a(conn, mid, campos, origem):
            if mid == a:
                raise RuntimeError("lock")
            return original(conn, mid, campos, origem)

        with mock.patch.object(motoristas_banco, "atualizar", side_effect=falha_em_a), \
                self.assertLogs(atu.logger, "ERROR"):
            self.assertTrue(atu.concluir(self.conn, {}, token, r["chave_envio"])["ok"])
        linhas = {l["id"]: l for l in motoristas_banco.listar(self.conn)}
        self.assertIsNone(linhas[a]["TELEFONE_MOTORISTA"])
        self.assertEqual(linhas[b]["TELEFONE_MOTORISTA"], "11998765432")
        self.assertEqual(self._cadastro_row(r["cadastro_id"])["status"], "APROVADO")

    def test_concluir_sem_gravar_repassa_valores_normalizados(self):
        lid = self._linha("Fulano de Tal", cpf=CPF_X, agent=7, TELEFONE_MOTORISTA="(11) 99876-5432",
                          EMAIL_MOTORISTA=" F@X.COM ", PLACA="abc-1d23")
        self._ficha(agent_id=7, bd_id=lid, data_nascimento="1990-05-17", chave_pix="p@x.com",
                    veiculo_modelo="Fiorino", veiculo_ano=2019, tipo_veiculo="FIORINO")
        token = atu.gerar_link(self.conn, lid, "hugo")["token"]
        r = atu.abrir(self.conn, token, CPF_X)
        self._docs(r, ("cnh", "crlv", "selfie"))
        atu.concluir(self.conn, {}, token, r["chave_envio"])
        linha = dict(self.conn.execute("SELECT * FROM bd_motoristas WHERE id = ?", (lid,)).fetchone())
        self.assertEqual((linha["TELEFONE_MOTORISTA"], linha["EMAIL_MOTORISTA"], linha["PLACA"]),
                         ("11998765432", "f@x.com", "ABC1D23"))
        c = self._cadastro_row(r["cadastro_id"])
        self.assertEqual((c["telefone"], c["email"], c["placa"]), ("11998765432", "f@x.com", "ABC1D23"))

    def test_concluir_link_vencido_410_e_cancelado_404(self):
        lid = self._linha(cpf=CPF_X)
        token, r = self._pronto(lid)
        with self.assertRaises(atu.LinkInvalido) as cx:
            atu.concluir(self.conn, {}, token, r["chave_envio"], agora=datetime.now() + timedelta(days=16))
        self.assertEqual(cx.exception.codigo, 410)
        atu.gerar_link(self.conn, lid, "hugo")   # cancela o anterior
        with self.assertRaises(atu.LinkInvalido) as cx:
            atu.gravar(self.conn, token, r["chave_envio"], _dados_ficha())
        self.assertEqual(cx.exception.codigo, 404)
        with self.assertRaises(atu.LinkInvalido) as cx:
            atu.concluir(self.conn, {}, token, r["chave_envio"])
        self.assertEqual(cx.exception.codigo, 404)

    def test_processador_do_link_e_fila_do_app_ignoram_atualizacao(self):
        lid = self._linha(cpf=CPF_X)
        token, r = self._pronto(lid)
        self.assertTrue(cml._ignorar(cad.buscar_cadastro(self.conn, r["cadastro_id"])))
        self.assertNotIn(r["cadastro_id"], [c["id"] for c in cad.listar_pendentes(self.conn)])
        atu.concluir(self.conn, {}, token, r["chave_envio"])
        self.assertTrue(cml._ignorar(cad.buscar_cadastro(self.conn, r["cadastro_id"])))

    # ── Revisão final do ramo ─────────────────────────────────────────────

    def test_pix_trocado_no_app_depois_da_aprovacao_continua_o_do_login(self):
        """C1: o PIX vigente é o do login do app; a ficha é só o reserva."""
        lid = self._linha("Fulano de Tal", cpf=CPF_X, agent=7)
        self._ficha(agent_id=7, bd_id=lid, chave_pix="pix-A")
        self._login(CPF_X, agent_id=7, chave_pix="pix-B")   # trocado em "Meus dados" depois da aprovação
        token = atu.gerar_link(self.conn, lid, "hugo")["token"]
        r = atu.abrir(self.conn, token, CPF_X)
        self.assertEqual(r["dados"]["chave_pix"], "pix-B")
        self.assertEqual(atu.pix_vigente(self.conn, self._pessoa(lid), atu.ficha_vigente(self.conn, lid)), "pix-B")
        atu.gravar(self.conn, token, r["chave_envio"], _dados_ficha(chave_pix=r["dados"]["chave_pix"]))
        self._docs(r, ("cnh", "crlv", "selfie"))
        atu.concluir(self.conn, {}, token, r["chave_envio"])
        self.assertEqual(self.conn.execute("SELECT chave_pix FROM motoristas WHERE cpf = ?", (CPF_X,)).fetchone()[0],
                         "pix-B")
        # sem login, a ficha vale como reserva
        lid2 = self._linha("Outro", cpf=CPF_Y, agent=8)
        self._ficha(agent_id=8, bd_id=lid2, cpf=CPF_Y, chave_pix="pix-da-ficha")
        r2 = atu.abrir(self.conn, atu.gerar_link(self.conn, lid2, "hugo")["token"], CPF_Y)
        self.assertEqual(r2["dados"]["chave_pix"], "pix-da-ficha")

    def test_linha_sem_cpf_do_mesmo_agente_entra_na_pessoa_com_cpf(self):
        """C2 (b): "CARRO 2" sem CPF no agente de uma pessoa com CPF = a mesma pessoa."""
        a = self._linha("Fulano", cpf=CPF_X, agent=1)
        b = self._linha("FULANO CARRO 2", agent=1)
        ps = atu.pessoas(self.conn)
        self.assertEqual(len(ps), 1)
        self.assertEqual((ps[0]["principal_id"], ps[0]["ids"], ps[0]["cpf"], ps[0]["agent_ids"]),
                         (a, [a, b], CPF_X, [1]))

    def test_linha_sem_cpf_com_duas_pessoas_com_cpf_no_agente_fica_sozinha(self):
        """C2 (b): dupla (dois CPFs) no mesmo agente: a linha sem CPF não é de ninguém."""
        x = self._linha("X", cpf=CPF_X, agent=1)
        y = self._linha("Y", cpf=CPF_Y, agent=1)
        z = self._linha("Z sem CPF", agent=1)
        ps = {p["principal_id"]: p for p in atu.pessoas(self.conn)}
        self.assertEqual(sorted(ps), [x, y, z])
        self.assertEqual([ps[i]["ids"] for i in (x, y, z)], [[x], [y], [z]])
        # a ficha de Y vai pra Y (pelo CPF), nunca pra X nem pra linha sem CPF
        fy = self._ficha(agent_id=1, cpf=CPF_Y)
        self.assertEqual(atu.vincular_fichas(self.conn), 1)
        self.assertEqual(self._cadastro_row(fy)["bd_motorista_id"], y)

    def test_vincular_ficha_do_app_vai_pra_linha_com_cpf_e_nao_pro_carro_2(self):
        """C2 (a): ficha aprovada do app com CPF X casa com a linha de CPF X."""
        a = self._linha("Fulano", cpf=CPF_X, agent=1)
        self._linha("FULANO CARRO 2", agent=1)
        fid = self._ficha(agent_id=1, cpf=CPF_X, cnh_arquivo="cnh.jpg")
        pasta = cad.pasta_cadastros() / str(fid)
        pasta.mkdir(parents=True)
        (pasta / "cnh.jpg").write_bytes(JPG)
        atu.vincular_fichas(self.conn)
        self.assertEqual(self._cadastro_row(fid)["bd_motorista_id"], a)
        self.assertNotIn("cnh", atu.pendencias(self.conn, self._pessoa(a))["documentos"])

    def test_vincular_por_cpf_quando_o_agente_nao_casa(self):
        lid = self._linha(cpf=CPF_X, agent=5)
        fid = self._ficha(agent_id=9, cpf=CPF_X)
        self.assertEqual(atu.vincular_fichas(self.conn), 1)
        self.assertEqual(self._cadastro_row(fid)["bd_motorista_id"], lid)

    def test_vincular_por_agente_nao_liga_ficha_de_outro_cpf(self):
        """C2 (a): o agente só casa quando nenhum CPF desmente (ficha de Y nunca vai pra X)."""
        self._linha("X", cpf=CPF_X, agent=1)
        fy = self._ficha(agent_id=1, cpf=CPF_Y)
        self.assertEqual(atu.vincular_fichas(self.conn), 0)
        self.assertIsNone(self._cadastro_row(fy)["bd_motorista_id"])

    def test_vincular_por_agente_com_dois_donos_nao_liga(self):
        """C2 (a): agente de mais de uma pessoa não decide."""
        self._linha("X", cpf=CPF_X, agent=1)
        self._linha("Y", cpf=CPF_Y, agent=1)
        fz = self._ficha(agent_id=1, cpf=CPF_Z)
        self.assertEqual(atu.vincular_fichas(self.conn), 0)
        self.assertIsNone(self._cadastro_row(fz)["bd_motorista_id"])

    def test_pessoa_sem_cpf_nem_agente(self):
        a = self._linha("Sozinho")
        b = self._linha("Outro sozinho")
        ps = {p["principal_id"]: p for p in atu.pessoas(self.conn)}
        self.assertEqual(sorted(ps), [a, b])
        self.assertEqual((ps[a]["ids"], ps[a]["agent_ids"], ps[a]["cpf"]), ([a], [], None))
        self._ficha(cpf=CPF_Y)   # sem agente e CPF que ninguém tem: não liga
        self.assertEqual(atu.vincular_fichas(self.conn), 0)
        pend = atu.pendencias(self.conn, ps[a])
        self.assertTrue(pend["falta_cpf"])
        self.assertIn("CPF", pend["campos"])

    def test_falta_cpf_olha_so_a_tabela(self):
        """M4: CPF só na ficha ainda deixa a pessoa fora do lote do grupo: o selo tem de aparecer."""
        lid = self._linha("Sem CPF", agent=7)
        self._ficha(agent_id=7, bd_id=lid, cpf=CPF_X)
        pend = atu.pendencias(self.conn, self._pessoa(lid))
        self.assertTrue(pend["falta_cpf"])
        self.assertNotIn("CPF", pend["campos"])   # a ficha completa o dado; o envio no grupo é que exige a tabela

    def test_link_e_estado_acham_por_qualquer_id_da_pessoa(self):
        """C2 (d): link guardado na linha que deixou de ser a principal continua achado e é cancelado."""
        a = self._linha("FULANO CARRO 2", agent=1)   # menor id: vira a principal quando as duas se juntam
        b = self._linha("Fulano", cpf=CPF_X, agent=1)
        agora = banco.agora()
        velho = self.conn.execute(
            "INSERT INTO motoristas_links_atualizacao (bd_motorista_id, token_hash, criado_em, expira_em) "
            "VALUES (?, 'h-velho', ?, '2999-01-01 00:00:00')", (b, agora)).lastrowid
        self.conn.commit()
        self.assertEqual(self._pessoa(a)["ids"], [a, b])
        self.assertEqual(self._pessoa(a)["nome"], "Fulano")   # o nome vem da linha com CPF (vai pro grupo)
        self.assertEqual(atu.link_em_aberto(self.conn, a)["id"], velho)
        self.assertEqual(atu.estado_link(self.conn, a), "enviado")
        novo = atu.gerar_link(self.conn, a, "hugo")
        self.assertIsNotNone(self._link_row(velho)["cancelado_em"])
        self.assertEqual(atu.link_em_aberto(self.conn, a)["id"], novo["id"])

    def test_concluir_com_rascunho_substituido_410(self):
        """M1: o rascunho virou SUBSTITUIDO (o que o criar_cadastro_link faz com PENDENTE do mesmo CPF)."""
        lid = self._linha(cpf=CPF_X)
        token, r = self._pronto(lid)
        self.conn.execute("UPDATE motoristas_cadastros SET status = ? WHERE id = ?",
                          (cad.STATUS_SUBSTITUIDO, r["cadastro_id"]))
        self.conn.commit()
        with self.assertRaises(atu.LinkInvalido) as cx:
            atu.concluir(self.conn, {}, token, r["chave_envio"])
        self.assertEqual(cx.exception.codigo, 410)
        self.assertIsNone(self.conn.execute("SELECT concluido_em FROM motoristas_links_atualizacao").fetchone()[0])

    def test_repasse_que_falha_no_meio_nao_deixa_escrita_pela_metade(self):
        """M5: a linha que falha desfaz o que escreveu antes de seguir pras outras."""
        a = self._linha("A", agent=7)
        b = self._linha("B", agent=7)
        token, r = self._pronto(a)
        original = motoristas_banco.atualizar

        def meio_e_falha(conn, mid, campos, origem):
            if mid == a:
                conn.execute("UPDATE bd_motoristas SET PLACA = 'XXX9X99' WHERE id = ?", (a,))
                raise RuntimeError("lock")
            return original(conn, mid, campos, origem)

        with mock.patch.object(motoristas_banco, "atualizar", side_effect=meio_e_falha), \
                self.assertLogs(atu.logger, "ERROR"):
            atu.concluir(self.conn, {}, token, r["chave_envio"])
        linhas = {l["id"]: l for l in motoristas_banco.listar(self.conn)}
        self.assertIsNone(linhas[a]["PLACA"])
        self.assertEqual(linhas[b]["TELEFONE_MOTORISTA"], "11998765432")

    def test_link_novo_substitui_o_rascunho_do_anterior_e_apaga_a_pasta(self):
        """M6: rascunho de link cancelado não fica PENDENTE (seguraria o CPF) nem com cópia no disco."""
        lid = self._linha(cpf=CPF_X)
        l1 = atu.gerar_link(self.conn, lid, "hugo")
        r = atu.abrir(self.conn, l1["token"], CPF_X)
        self._docs(r, ("cnh",))
        pasta = cad.pasta_cadastros() / str(r["cadastro_id"])
        self.assertTrue(pasta.is_dir())
        atu.gerar_link(self.conn, lid, "hugo")
        self.assertEqual(self._cadastro_row(r["cadastro_id"])["status"], cad.STATUS_SUBSTITUIDO)
        self.assertFalse(pasta.exists())
        self.assertIsNone(self.conn.execute("SELECT 1 FROM motoristas_cadastros WHERE cpf = ? AND status = ?",
                                            (CPF_X, cad.STATUS_PENDENTE)).fetchone())

    def test_cinco_erros_de_cpf_substituem_o_rascunho(self):
        """M6: o link cancelado pelas tentativas também solta o rascunho."""
        lid = self._linha(cpf=CPF_X)
        token = atu.gerar_link(self.conn, lid, "hugo")["token"]
        r = atu.abrir(self.conn, token, CPF_X)
        self._docs(r, ("cnh",))
        for _ in range(atu.MAX_TENTATIVAS_CPF):
            with self.assertRaises(atu.LinkInvalido):
                atu.abrir(self.conn, token, CPF_Y)
        self.assertEqual(self._cadastro_row(r["cadastro_id"])["status"], cad.STATUS_SUBSTITUIDO)
        self.assertFalse((cad.pasta_cadastros() / str(r["cadastro_id"])).exists())
        # ficha de outra origem nunca é tocada
        outra = self._ficha(status="PENDENTE", origem="app", cpf=CPF_Z)
        atu.gerar_link(self.conn, lid, "hugo")
        self.assertEqual(self._cadastro_row(outra)["status"], "PENDENTE")

    # ── Correção, rodada 2 ────────────────────────────────────────────────

    def _pix_login(self, cpf=CPF_X):
        row = self.conn.execute("SELECT chave_pix FROM motoristas WHERE cpf = ?", (cpf,)).fetchone()
        return row[0] if row else None

    def _enviar_sem_mexer_no_pix(self, token, r):
        atu.gravar(self.conn, token, r["chave_envio"], _dados_ficha(chave_pix=r["dados"]["chave_pix"]))
        self._docs(r, ("cnh", "crlv", "selfie"))
        atu.concluir(self.conn, {}, token, r["chave_envio"])

    def test_pix_trocado_no_app_durante_o_link_nao_volta_ao_do_rascunho(self):
        """S1: rascunho criado com B; no meio do link ele troca pra C no app; Enviar sem mexer = fica C."""
        lid = self._linha("Fulano de Tal", cpf=CPF_X, agent=7)
        self._ficha(agent_id=7, bd_id=lid, chave_pix="pix-A")
        self._login(CPF_X, agent_id=7, chave_pix="pix-B")
        token = atu.gerar_link(self.conn, lid, "hugo")["token"]
        self.assertEqual(atu.abrir(self.conn, token, CPF_X)["dados"]["chave_pix"], "pix-B")
        self.conn.execute("UPDATE motoristas SET chave_pix = 'pix-C' WHERE cpf = ?", (CPF_X,))   # "Meus dados"
        self.conn.commit()
        r = atu.abrir(self.conn, token, CPF_X)   # mesmo rascunho, ainda com B
        self.assertEqual(r["dados"]["chave_pix"], "pix-B")
        self._enviar_sem_mexer_no_pix(token, r)
        self.assertEqual(self._pix_login(), "pix-C")
        self.assertEqual(self._cadastro_row(r["cadastro_id"])["chave_pix"], "pix-C")   # ficha igual ao login

    def test_login_criado_durante_o_link_nao_recebe_o_pix_da_ficha_antiga(self):
        """S2: sem login no abrir (rascunho com A da ficha); depois cria o login com C; Enviar sem mexer = fica C."""
        lid = self._linha("Fulano de Tal", cpf=CPF_X, agent=7)
        self._ficha(agent_id=7, bd_id=lid, chave_pix="pix-A")
        token = atu.gerar_link(self.conn, lid, "hugo")["token"]
        r = atu.abrir(self.conn, token, CPF_X)
        self.assertEqual(r["dados"]["chave_pix"], "pix-A")
        self._login(CPF_X, agent_id=7, chave_pix="pix-C")
        self._enviar_sem_mexer_no_pix(token, r)
        self.assertEqual(self._pix_login(), "pix-C")
        self.assertEqual(self._cadastro_row(r["cadastro_id"])["chave_pix"], "pix-C")

    def _troca_pedida(self):
        """Login com pix-C, motorista digita pix-D no link. Devolve (lid, token, abrir, resultado)."""
        lid = self._linha("Fulano de Tal", cpf=CPF_X, agent=7)
        self._ficha(agent_id=7, bd_id=lid, chave_pix="pix-A")
        self._login(CPF_X, agent_id=7, chave_pix="pix-B")
        token = atu.gerar_link(self.conn, lid, "hugo")["token"]
        r = atu.abrir(self.conn, token, CPF_X)
        self.conn.execute("UPDATE motoristas SET chave_pix = 'pix-C' WHERE cpf = ?", (CPF_X,))
        self.conn.commit()
        atu.gravar(self.conn, token, r["chave_envio"], _dados_ficha(chave_pix="pix-D"))
        self._docs(r, ("cnh", "crlv", "selfie"))
        return lid, token, r, atu.concluir(self.conn, {}, token, r["chave_envio"])

    def _pessoa(self, lid):
        return next(p for p in atu.pessoas(self.conn) if lid in p["ids"])

    def test_pix_digitado_no_formulario_espera_a_aprovacao(self):
        lid, token, r, fim = self._troca_pedida()
        self.assertTrue(fim["pix_em_analise"])
        self.assertTrue(atu.concluir(self.conn, {}, token, r["chave_envio"])["pix_em_analise"])   # repetir não muda
        self.assertEqual(self._pix_login(), "pix-C")
        self.assertEqual(self._cadastro_row(r["cadastro_id"])["chave_pix"], "pix-C")
        self.assertEqual(atu.pix_pendente(self.conn, self._pessoa(lid))["novo"], "pix-D")
        self.assertTrue(atu.pendencias(self.conn, self._pessoa(lid))["pix_pendente"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM motoristas_alteracoes WHERE campo = 'chave_pix'")
                         .fetchone()[0], 0)

    def test_aprovar_a_troca_grava_no_login_e_na_ficha(self):
        lid, token, r, _ = self._troca_pedida()
        self.assertEqual(atu.decidir_pix(self.conn, lid, True, "hugo"), {"ok": True, "aprovado": True})
        self.assertEqual(self._pix_login(), "pix-D")
        self.assertEqual(self._cadastro_row(r["cadastro_id"])["chave_pix"], "pix-D")
        alt = self.conn.execute("SELECT valor_antigo, valor_novo FROM motoristas_alteracoes WHERE campo = 'chave_pix'").fetchone()
        self.assertEqual(tuple(alt), ("pix-C", "pix-D"))
        self.assertIsNone(atu.pix_pendente(self.conn, self._pessoa(lid)))
        with self.assertRaises(atu.LinkInvalido) as cx:   # segundo clique não repete
            atu.decidir_pix(self.conn, lid, True, "hugo")
        self.assertEqual(cx.exception.codigo, 404)

    def test_recusar_a_troca_mantem_o_pix_atual(self):
        lid, token, r, _ = self._troca_pedida()
        self.assertEqual(atu.decidir_pix(self.conn, lid, False, "hugo"), {"ok": True, "aprovado": False})
        self.assertEqual(self._pix_login(), "pix-C")
        self.assertEqual(self._cadastro_row(r["cadastro_id"])["chave_pix"], "pix-C")
        self.assertIsNone(atu.pix_pendente(self.conn, self._pessoa(lid)))
        self.assertFalse(atu.pendencias(self.conn, self._pessoa(lid))["pix_pendente"])

    def test_sem_login_trocar_o_pix_da_ficha_antiga_tambem_espera(self):
        lid = self._linha("Fulano de Tal", cpf=CPF_X, agent=7)
        self._ficha(agent_id=7, bd_id=lid, chave_pix="pix-A")
        token = atu.gerar_link(self.conn, lid, "hugo")["token"]
        r = atu.abrir(self.conn, token, CPF_X)
        atu.gravar(self.conn, token, r["chave_envio"], _dados_ficha(chave_pix="pix-D"))
        self._docs(r, ("cnh", "crlv", "selfie"))
        self.assertTrue(atu.concluir(self.conn, {}, token, r["chave_envio"])["pix_em_analise"])
        self.assertEqual(self._cadastro_row(r["cadastro_id"])["chave_pix"], "pix-A")
        atu.decidir_pix(self.conn, lid, True, "hugo")
        self.assertEqual(self._cadastro_row(r["cadastro_id"])["chave_pix"], "pix-D")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM motoristas").fetchone()[0], 0)

    def test_primeiro_pix_entra_direto(self):
        lid = self._linha("Fulano de Tal", cpf=CPF_X, agent=7)
        self._login(CPF_X, agent_id=7)   # login sem PIX
        token = atu.gerar_link(self.conn, lid, "hugo")["token"]
        r = atu.abrir(self.conn, token, CPF_X)
        atu.gravar(self.conn, token, r["chave_envio"], _dados_ficha(chave_pix="pix-D"))
        self._docs(r, ("cnh", "crlv", "selfie"))
        self.assertFalse(atu.concluir(self.conn, {}, token, r["chave_envio"])["pix_em_analise"])
        self.assertEqual(self._pix_login(), "pix-D")
        self.assertIsNone(atu.pix_pendente(self.conn, self._pessoa(lid)))

    def test_sem_login_a_ficha_fica_com_o_do_formulario_e_nao_cria_login(self):
        lid = self._linha("Fulano de Tal", cpf=CPF_X, agent=7)
        self._ficha(agent_id=7, bd_id=lid, chave_pix="pix-A")
        token = atu.gerar_link(self.conn, lid, "hugo")["token"]
        r = atu.abrir(self.conn, token, CPF_X)
        self._enviar_sem_mexer_no_pix(token, r)
        self.assertEqual(self._cadastro_row(r["cadastro_id"])["chave_pix"], "pix-A")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM motoristas").fetchone()[0], 0)

    def test_pix_inicial_nao_sai_na_resposta(self):
        lid = self._linha(cpf=CPF_X)
        self._login(CPF_X, chave_pix="pix-B")
        token = atu.gerar_link(self.conn, lid, "hugo")["token"]
        r = atu.abrir(self.conn, token, CPF_X)
        self.assertNotIn("pix_inicial", str(r))
        self.assertEqual(self.conn.execute("SELECT pix_inicial FROM motoristas_links_atualizacao").fetchone()[0],
                         "pix-B")

    def test_ficha_vigente_de_pessoa_com_cpf_ignora_ficha_de_outro_cpf(self):
        """Rodada 2, item 3: ficha de outro CPF ligada a uma linha da pessoa não vira a vigente."""
        lid = self._linha(cpf=CPF_X, agent=7)
        minha = self._ficha(agent_id=7, bd_id=lid, cpf=CPF_X, cnh_arquivo="cnh.jpg")
        self._ficha(agent_id=7, bd_id=lid, cpf=CPF_Y)   # mais nova, de outro CPF
        self.assertEqual(atu.ficha_vigente(self.conn, lid)["id"], minha)
        # pessoa sem CPF: como antes, a aprovada mais recente
        sem = self._linha("Sem CPF", agent=9)
        self._ficha(agent_id=9, bd_id=sem, cpf=CPF_Y)
        ultima = self._ficha(agent_id=9, bd_id=sem, cpf=CPF_Z)
        self.assertEqual(atu.ficha_vigente(self.conn, sem)["id"], ultima)

    # ── Link único de PIX (/motorista/pix) ────────────────────────────────

    NASC = "1990-05-17"

    def _motorista_com_nascimento(self, nome="Fulano", cpf=None, agent=7, nasc="1990-05-17", **extra):
        """Motorista ativo + ficha aprovada com data de nascimento (o 2o fator do link de PIX)."""
        lid = self._linha(nome, cpf=cpf or CPF_X, agent=agent, **extra)
        self._ficha(agent_id=agent, bd_id=lid, cpf=cpf or CPF_X, data_nascimento=nasc)
        return lid

    def _pedidos_pix(self):
        return [dict(r) for r in self.conn.execute("SELECT * FROM motoristas_pix_pedidos ORDER BY id")]

    def test_pedir_pix_registra_pendente_e_nao_troca_na_hora(self):
        lid = self._motorista_com_nascimento("Fulano de Tal")
        self._login(CPF_X, agent_id=7, chave_pix="pix-B")
        r = atu.pedir_pix(self.conn, "529.982.247-25", " pix-D ", "17/05/1990", ip="1.2.3.4")
        self.assertEqual(r, {"ok": True})
        (p,) = self._pedidos_pix()
        self.assertEqual((p["cpf"], p["pix_novo"], p["pix_anterior"], p["status"], p["bd_motorista_id"], p["ip_origem"]),
                         (CPF_X, "pix-D", "pix-B", "PENDENTE", lid, "1.2.3.4"))
        self.assertEqual(self._pix_login(), "pix-B")
        self.assertEqual(atu.pix_pendente(self.conn, self._pessoa(lid))["novo"], "pix-D")
        self.assertTrue(atu.pendencias(self.conn, self._pessoa(lid))["pix_pendente"])

    def test_pedir_pix_recusa_so_cpf_invalido_e_responde_igual_nos_demais_casos(self):
        self._motorista_com_nascimento("Ativo")
        self._motorista_com_nascimento("Inativo", cpf=CPF_Y, agent=8, ativo="NAO")
        self._login(CPF_X, agent_id=7, chave_pix="pix-B")
        for cpf, pix, nasc in (("123", "p", self.NASC), (CPF_X, "  ", self.NASC),
                               (CPF_X, "p", ""), (CPF_X, "p", "31/02/1990"), (CPF_X, "p", "lixo")):
            with self.assertRaises(cad.CadastroInvalido) as cx:
                atu.pedir_pix(self.conn, cpf, pix, nasc)
            self.assertEqual(cx.exception.codigo, 400)
        # desconhecido, inativo e mesma chave: resposta idêntica e nada registrado (sem enumeração)
        for cpf, pix in ((CPF_Z, "p"), (CPF_Y, "p"), (CPF_X, "PIX-b")):
            self.assertEqual(atu.pedir_pix(self.conn, cpf, pix, self.NASC), {"ok": True})
        self.assertEqual(self._pedidos_pix(), [])

    def test_pedir_pix_de_novo_substitui_o_anterior_e_tem_freios_por_cpf_e_por_ip(self):
        self._motorista_com_nascimento()
        atu.pedir_pix(self.conn, CPF_X, "pix-1", self.NASC, ip="9.9.9.9")
        atu.pedir_pix(self.conn, CPF_X, "pix-2", self.NASC, ip="9.9.9.9")
        self.assertEqual([p["status"] for p in self._pedidos_pix()], ["SUBSTITUIDO", "PENDENTE"])
        atu.pedir_pix(self.conn, CPF_X, "pix-3", self.NASC, ip="9.9.9.9")
        # 4o do mesmo CPF na hora: mesma resposta do sucesso, sem gravar (nao vira oracle de CPF)
        self.assertEqual(atu.pedir_pix(self.conn, CPF_X, "pix-4", self.NASC, ip="8.8.8.8"), {"ok": True})
        self.assertEqual(len(self._pedidos_pix()), 3)
        self.conn.execute("UPDATE motoristas_pix_pedidos SET cpf = '0'")   # IP: 3 gravados, completa o teto
        for _ in range(atu.MAX_PEDIDOS_PIX_IP_HORA - 3):
            self.conn.execute("INSERT INTO motoristas_pix_pedidos (cpf, pix_novo, criado_em, ip_origem) VALUES ('0','x',?,'9.9.9.9')",
                              (banco.agora(),))
        self.conn.commit()
        self.assertEqual(atu.pedir_pix(self.conn, CPF_X, "pix-z", self.NASC, ip="9.9.9.9"), {"ok": True})
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM motoristas_pix_pedidos WHERE cpf = ?", (CPF_X,)).fetchone()[0], 0)

    def test_aprovar_pedido_do_link_unico_grava_no_login_e_fecha_o_pedido(self):
        lid = self._motorista_com_nascimento()
        self._login(CPF_X, agent_id=7, chave_pix="pix-B")
        atu.pedir_pix(self.conn, CPF_X, "pix-D", self.NASC)
        self.assertEqual(atu.decidir_pix(self.conn, lid, True, "hugo"), {"ok": True, "aprovado": True})
        self.assertEqual(self._pix_login(), "pix-D")
        p = self._pedidos_pix()[0]
        self.assertEqual((p["status"], p["decidido_por"]), ("APROVADO", "hugo"))
        self.assertIsNone(atu.pix_pendente(self.conn, self._pessoa(lid)))
        with self.assertRaises(atu.LinkInvalido):
            atu.decidir_pix(self.conn, lid, True, "hugo")

    def test_recusar_pedido_do_link_unico_mantem_o_pix(self):
        lid = self._motorista_com_nascimento()
        self._login(CPF_X, agent_id=7, chave_pix="pix-B")
        atu.pedir_pix(self.conn, CPF_X, "pix-D", self.NASC)
        atu.decidir_pix(self.conn, lid, False, "hugo")
        self.assertEqual(self._pix_login(), "pix-B")
        self.assertEqual(self._pedidos_pix()[0]["status"], "RECUSADO")

    def test_pedir_pix_so_grava_se_a_data_de_nascimento_bate_com_a_ficha(self):
        self._motorista_com_nascimento()
        self._login(CPF_X, agent_id=7, chave_pix="pix-B")
        # data errada: resposta igual ao sucesso, nada gravado
        self.assertEqual(atu.pedir_pix(self.conn, CPF_X, "pix-D", "1990-05-18"), {"ok": True})
        self.assertEqual(self._pedidos_pix(), [])
        # formatos AAAA-MM-DD e DD/MM/AAAA valem
        atu.pedir_pix(self.conn, CPF_X, "pix-D", "1990-05-17")
        atu.pedir_pix(self.conn, CPF_X, "pix-E", "17/05/1990")
        self.assertEqual([p["status"] for p in self._pedidos_pix()], ["SUBSTITUIDO", "PENDENTE"])

    def test_pedir_pix_sem_data_na_ficha_ou_sem_ficha_nao_grava(self):
        lid = self._linha("Sem ficha", cpf=CPF_X, agent=7)
        self.assertEqual(atu.pedir_pix(self.conn, CPF_X, "pix-D", self.NASC), {"ok": True})
        self._ficha(agent_id=7, bd_id=lid, cpf=CPF_X)   # ficha aprovada, mas sem data_nascimento
        self.assertEqual(atu.pedir_pix(self.conn, CPF_X, "pix-D", self.NASC), {"ok": True})
        self.assertEqual(self._pedidos_pix(), [])

    def test_pedido_com_data_errada_nao_substitui_o_pendente_do_motorista(self):
        self._motorista_com_nascimento()
        atu.pedir_pix(self.conn, CPF_X, "pix-do-motorista", self.NASC)
        atu.pedir_pix(self.conn, CPF_X, "pix-do-atacante", "2000-01-01")
        (p,) = self._pedidos_pix()
        self.assertEqual((p["pix_novo"], p["status"]), ("pix-do-motorista", "PENDENTE"))


if __name__ == "__main__":
    unittest.main()
