# -*- coding: utf-8 -*-
"""
test_cadastro_link.py

Cadastro do motorista novo pelo link público: campos novos, CPF com dígito
verificador, 5 documentos, substituição de pendente e envio final.
SQLite temporário.
    py -3.11 -m unittest nucleo.test_cadastro_link -v
"""
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))

from nucleo import auth_motorista as auth, banco, cadastro_motorista as cad  # noqa: E402
from regras import motoristas_banco  # noqa: E402

CPF_OK = "52998224725"
CABECALHOS = {"jpg": b"\xff\xd8\xff\xe0", "png": b"\x89PNG\r\n\x1a\n", "pdf": b"%PDF-1.4\n",
              "heic": b"\x00\x00\x00\x18ftypheic", "webp": b"RIFF\x00\x00\x00\x00WEBPVP8 "}


def _arq(nome: str, n: int = 50) -> bytes:
    """Conteúdo com o cabeçalho do tipo pela extensão do nome."""
    return CABECALHOS[nome.rsplit(".", 1)[-1]] + b"0" * n


def _dados(**extra):
    d = {
        "nome": "João Pedro Santos", "cpf": CPF_OK, "data_nascimento": "1990-05-17", "telefone": "(11) 99876-5432",
        "chave_pix": "joao@exemplo.com", "emite_nf": True, "dias": ["SEGUNDA", "TERCA"], "zonas": ["ZONA SUL"],
        "posicao": "FIXO", "celular": "ANDROID", "tipo_veiculo": "fiorino", "placa": "abc-1d23",
        "veiculo_modelo": "Fiat Fiorino", "veiculo_ano": 2020, "refrigerado": False, "dono_veiculo": True,
    }
    d.update(extra)
    return d


class TestCadastroLink(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._patch = mock.patch.object(banco, "DB_PATH", Path(self._tmp.name) / "t.db")
        self._patch.start()
        self.conn = banco.conectar()

    def tearDown(self):
        self.conn.close()
        self._patch.stop()
        self._tmp.cleanup()

    def _linha(self, cid):
        return dict(self.conn.execute("SELECT * FROM motoristas_cadastros WHERE id = ?", (cid,)).fetchone())

    def _completar(self, r):
        for t, nome in (("cnh", "cnh.jpg"), ("crlv", "crlv.pdf"), ("selfie", "selfie.heic"),
                        ("antt", "antt.pdf"), ("residencia", "res.png")):
            cad.guardar_documento(self.conn, r["id"], r["chave_envio"], t, nome, _arq(nome))

    def test_cpf_valido(self):
        self.assertTrue(cad.cpf_valido("52998224725"))
        self.assertTrue(cad.cpf_valido("529.982.247-25"))
        self.assertFalse(cad.cpf_valido("52998224724"))
        self.assertFalse(cad.cpf_valido("11111111111"))
        self.assertFalse(cad.cpf_valido("123"))

    def test_cria_link_completo(self):
        r = cad.criar_cadastro_link(self.conn, _dados())
        l = self._linha(r["id"])
        self.assertEqual(l["origem"], "link")
        self.assertEqual(l["status"], "PENDENTE")
        self.assertEqual(l["data_nascimento"], "1990-05-17")
        self.assertIsNone(l["temp_min_c"])
        self.assertEqual(l["emite_nf"], 1)
        self.assertEqual(l["celular"], "ANDROID")
        self.assertEqual(l["placa"], "ABC1D23")
        self.assertEqual(l["telefone"], "11998765432")
        self.assertTrue(r["chave_envio"])

    def test_aceita_data_dd_mm_aaaa(self):
        r = cad.criar_cadastro_link(self.conn, _dados(data_nascimento="17/05/1990"))
        self.assertEqual(self._linha(r["id"])["data_nascimento"], "1990-05-17")

    def test_refrigerado_exige_temperatura(self):
        with self.assertRaises(cad.CadastroInvalido):
            cad.criar_cadastro_link(self.conn, _dados(refrigerado=True))
        r = cad.criar_cadastro_link(self.conn, _dados(refrigerado=True, temp_min_c=-18))
        l = self._linha(r["id"])
        self.assertEqual(l["temp_min_c"], -18.0)
        self.assertEqual(l["refrigerado"], 1)
        with self.assertRaises(cad.CadastroInvalido):
            cad.criar_cadastro_link(self.conn, _dados(cpf="11144477735", refrigerado=True, temp_min_c=-40))

    def test_recusa_quem_ja_e_motorista(self):
        motoristas_banco.inserir(self.conn, {"AGENT_ID_VUUPT": 1, "NOME_MOTORISTA": "X", "CPF_MOTORISTA": CPF_OK}, "teste")
        with self.assertRaises(cad.CadastroInvalido) as cx:
            cad.criar_cadastro_link(self.conn, _dados())
        self.assertEqual(cx.exception.codigo, 409)
        self.assertIn("já é motorista da Fresh Log", cx.exception.mensagem)

    def test_recusa_login_existente_e_aprovado(self):
        auth.criar_ou_atualizar_motorista(self.conn, CPF_OK, "Fulano", "482913", agent_id=7)
        with self.assertRaises(cad.CadastroInvalido) as cx:
            cad.criar_cadastro_link(self.conn, _dados())
        self.assertEqual(cx.exception.codigo, 409)
        self.assertIn("já é motorista da Fresh Log", cx.exception.mensagem)

        outro = "11144477735"
        r = cad.criar_cadastro_link(self.conn, _dados(cpf=outro))
        self.conn.execute("UPDATE motoristas_cadastros SET status = 'APROVADO' WHERE id = ?", (r["id"],))
        self.conn.commit()
        with self.assertRaises(cad.CadastroInvalido) as cx:
            cad.criar_cadastro_link(self.conn, _dados(cpf=outro))
        self.assertEqual(cx.exception.codigo, 409)
        self.assertIn("já é motorista da Fresh Log", cx.exception.mensagem)

    def test_recusado_nao_bloqueia(self):
        r = cad.criar_cadastro_link(self.conn, _dados())
        self.conn.execute("UPDATE motoristas_cadastros SET status = 'RECUSADO' WHERE id = ?", (r["id"],))
        self.conn.commit()
        r2 = cad.criar_cadastro_link(self.conn, _dados())
        self.assertNotEqual(r["id"], r2["id"])
        self.assertEqual(self._linha(r["id"])["status"], "RECUSADO")

    def test_pendente_vira_substituido(self):
        # pendente do app e pendente do link: os dois são substituídos
        app = cad.criar_cadastro(self.conn, {
            "cpf": CPF_OK, "nome": "Maria da Silva", "telefone": "11998765432", "chave_pix": "x@y.com",
            "placa": "ABC1D23", "tipo_veiculo": "fiorino", "zonas": ["ZONA SUL"], "dias": ["SEGUNDA"]})
        r1 = cad.criar_cadastro_link(self.conn, _dados())
        self.assertEqual(self._linha(app["id"])["status"], cad.STATUS_SUBSTITUIDO)
        r2 = cad.criar_cadastro_link(self.conn, _dados())
        self.assertEqual(self._linha(r1["id"])["status"], "SUBSTITUIDO")
        self.assertEqual(self._linha(r2["id"])["status"], "PENDENTE")

    def test_validacoes(self):
        hoje = date.today()
        menor = date(hoje.year - 17, hoje.month, 1).isoformat()
        casos = [
            {"data_nascimento": menor},
            {"data_nascimento": "nao-e-data"},
            {"veiculo_ano": 1979},
            {"veiculo_ano": hoje.year + 2},
            {"veiculo_modelo": ""},
            {"posicao": "QUALQUER"},
            {"celular": "NOKIA"},
            {"cpf": "52998224724"},
        ]
        for extra in casos:
            with self.subTest(extra=extra), self.assertRaises(cad.CadastroInvalido):
                cad.criar_cadastro_link(self.conn, _dados(**extra))
        cad.criar_cadastro_link(self.conn, _dados(veiculo_ano=hoje.year + 1))

    def test_documentos_link(self):
        r = cad.criar_cadastro_link(self.conn, _dados())
        with self.assertRaises(cad.CadastroInvalido):
            cad.guardar_documento(self.conn, r["id"], r["chave_envio"], "cnh", "cnh.exe", b"x")
        c = None
        for t, nome in (("selfie", "selfie.heic"), ("antt", "antt.pdf"), ("cnh", "cnh.jpg"), ("crlv", "c.png")):
            c = cad.guardar_documento(self.conn, r["id"], r["chave_envio"], t, nome, _arq(nome))
        self.assertTrue(c["tem_selfie"] and c["tem_antt"])
        self.assertFalse(c["completo"])
        c = cad.guardar_documento(self.conn, r["id"], r["chave_envio"], "residencia", "r.pdf", _arq("r.pdf"))
        self.assertTrue(c["completo"])

    def test_completo_do_app_segue_cnh_e_crlv(self):
        app = cad.criar_cadastro(self.conn, {
            "cpf": CPF_OK, "nome": "Maria da Silva", "telefone": "11998765432", "chave_pix": "x@y.com",
            "placa": "ABC1D23", "tipo_veiculo": "fiorino", "zonas": ["ZONA SUL"], "dias": ["SEGUNDA"]})
        cad.guardar_documento(self.conn, app["id"], app["chave_envio"], "cnh", "a.jpg", _arq("a.jpg"))
        c = cad.guardar_documento(self.conn, app["id"], app["chave_envio"], "crlv", "b.jpg", _arq("b.jpg"))
        self.assertTrue(c["completo"])

    def test_marcar_enviado(self):
        r = cad.criar_cadastro_link(self.conn, _dados())
        with self.assertRaises(cad.CadastroInvalido):
            cad.marcar_enviado(self.conn, r["id"], r["chave_envio"])
        self._completar(r)
        with self.assertRaises(cad.CadastroInvalido) as cx:
            cad.marcar_enviado(self.conn, r["id"], "chave-errada")
        self.assertEqual(cx.exception.codigo, 404)
        c = cad.marcar_enviado(self.conn, r["id"], r["chave_envio"])
        l = self._linha(r["id"])
        self.assertEqual(l["status"], "APROVADO")
        self.assertEqual(l["revisado_por"], "link")
        self.assertTrue(l["enviado_em"])
        self.assertTrue(c["enviado_em"])
        antes = l["enviado_em"]
        cad.marcar_enviado(self.conn, r["id"], r["chave_envio"])
        self.assertEqual(self._linha(r["id"])["enviado_em"], antes)

    # ── Revisão de segurança 02/11 (A1, M1, M2, B3) ─────────────────────────

    def test_registrar_envio_diz_se_gravou_agora(self):
        r = cad.criar_cadastro_link(self.conn, _dados())
        self._completar(r)
        c, gravou = cad.registrar_envio(self.conn, r["id"], r["chave_envio"])
        self.assertTrue(gravou)
        self.assertTrue(c["enviado_em"])
        c2, gravou2 = cad.registrar_envio(self.conn, r["id"], r["chave_envio"])
        self.assertFalse(gravou2)
        self.assertEqual(c2["enviado_em"], c["enviado_em"])

    def test_tipo_do_arquivo_pelos_bytes(self):
        for ext, cab in CABECALHOS.items():
            self.assertEqual(cad.tipo_do_arquivo(cab + b"0" * 20), ext)
        for marca in (b"heix", b"mif1", b"msf1", b"hevc"):
            self.assertEqual(cad.tipo_do_arquivo(b"\x00\x00\x00\x18ftyp" + marca + b"0" * 8), "heic")
        for ruim in (b"x" * 50, b"GIF89a" + b"0" * 20, b"\x00\x00\x00\x18ftypisom" + b"0" * 8,
                     b"RIFF\x00\x00\x00\x00WAVE" + b"0" * 8, b"MZ\x90\x00", b"\xff\xd8"):
            self.assertIsNone(cad.tipo_do_arquivo(ruim), ruim)

    def test_documento_que_nao_e_foto_nem_pdf_400(self):
        r = cad.criar_cadastro_link(self.conn, _dados())
        with self.assertRaises(cad.CadastroInvalido) as cx:
            cad.guardar_documento(self.conn, r["id"], r["chave_envio"], "cnh", "cnh.jpg", b"x" * 50)
        self.assertEqual((cx.exception.codigo, cx.exception.mensagem), (400, "Arquivo não parece uma foto ou PDF."))
        self.assertIsNone(self._linha(r["id"])["cnh_arquivo"])
        self.assertFalse((cad.pasta_cadastros() / str(r["id"])).exists())

    def test_reenvio_apaga_o_arquivo_antigo_do_tipo(self):
        r = cad.criar_cadastro_link(self.conn, _dados())
        pasta = cad.pasta_cadastros() / str(r["id"])
        for nome in ("cnh.jpg", "cnh.png", "cnh.pdf", "cnh.webp"):
            cad.guardar_documento(self.conn, r["id"], r["chave_envio"], "cnh", nome, _arq(nome))
        cad.guardar_documento(self.conn, r["id"], r["chave_envio"], "crlv", "crlv.jpg", _arq("crlv.jpg"))
        self.assertEqual(sorted(p.name for p in pasta.iterdir()), ["cnh.webp", "crlv.jpg"])
        self.assertEqual(self._linha(r["id"])["cnh_arquivo"], "cnh.webp")
        # o nome segue o conteúdo: HEIC chamado .jpg (iPhone) fica .heic
        cad.guardar_documento(self.conn, r["id"], r["chave_envio"], "cnh", "IMG_1.jpg", _arq("x.heic"))
        self.assertEqual(sorted(p.name for p in pasta.iterdir()), ["cnh.heic", "crlv.jpg"])
        self.assertEqual(self._linha(r["id"])["cnh_arquivo"], "cnh.heic")
        self.assertEqual(cad.caminho_documento(self.conn, r["id"], "cnh"), pasta / "cnh.heic")

    def test_nome_ate_120_caracteres(self):
        with self.assertRaises(cad.CadastroInvalido):
            cad.criar_cadastro_link(self.conn, _dados(nome="Jo " + "a" * 118))
        r = cad.criar_cadastro_link(self.conn, _dados(nome="Jo " + "a" * 117))
        self.assertEqual(len(self._linha(r["id"])["nome"]), 120)

    def _cpf(self, i):
        d = f"{i:09d}"
        for n in (9, 10):
            soma = sum(int(d[k]) * (n + 1 - k) for k in range(n))
            d += str((soma * 10 % 11) % 10)
        return d

    def test_freio_do_link_no_banco_sobrevive_a_restart(self):
        """M1(c): 5 por IP por hora, contado em motoristas_cadastros.ip_origem."""
        for i in range(cad.LIMITE_LINK_IP_HORA):
            r = cad.criar_cadastro_link(self.conn, _dados(cpf=self._cpf(i + 1)), ip="1.2.3.4")
        self.assertEqual(self._linha(r["id"])["ip_origem"], "1.2.3.4")
        # "restart": conexão nova, nada em memória
        self.conn.close()
        self.conn = banco.conectar()
        with self.assertRaises(cad.CadastroInvalido) as cx:
            cad.criar_cadastro_link(self.conn, _dados(cpf=self._cpf(50)), ip="1.2.3.4")
        self.assertEqual(cx.exception.codigo, 429)
        self.assertFalse(self.conn.in_transaction)
        cad.criar_cadastro_link(self.conn, _dados(cpf=self._cpf(51)), ip="5.6.7.8")
        # passou uma hora: o IP volta a poder
        self.conn.execute("UPDATE motoristas_cadastros SET criado_em = datetime(criado_em, '-61 minutes') "
                          "WHERE ip_origem = '1.2.3.4'")
        self.conn.commit()
        cad.criar_cadastro_link(self.conn, _dados(cpf=self._cpf(52)), ip="1.2.3.4")

    def test_freio_do_link_40_por_dia_no_total(self):
        agora = banco.agora()
        for i in range(cad.LIMITE_LINK_DIA):
            self.conn.execute("INSERT INTO motoristas_cadastros (cpf, nome, chave_envio, status, criado_em, origem, ip_origem) "
                              "VALUES (?, 'X Y', ?, 'SUBSTITUIDO', ?, 'link', ?)", (f"{i:011d}", f"k{i}", agora, f"10.0.0.{i}"))
        # cadastro do app não conta no teto do link
        self.conn.execute("INSERT INTO motoristas_cadastros (cpf, nome, chave_envio, status, criado_em, origem) "
                          "VALUES ('99999999999', 'A B', 'kapp', 'PENDENTE', ?, 'app')", (agora,))
        self.conn.commit()
        with self.assertRaises(cad.CadastroInvalido) as cx:
            cad.criar_cadastro_link(self.conn, _dados(), ip="9.9.9.9")
        self.assertEqual(cx.exception.codigo, 429)
        self.conn.execute("UPDATE motoristas_cadastros SET criado_em = datetime(criado_em, '-25 hours') WHERE id = 1")
        self.conn.commit()
        cad.criar_cadastro_link(self.conn, _dados(), ip="9.9.9.9")

    def test_erro_de_validacao_nao_gasta_o_freio(self):
        for _ in range(cad.LIMITE_LINK_IP_HORA + 2):
            with self.assertRaises(cad.CadastroInvalido):
                cad.criar_cadastro_link(self.conn, _dados(cpf="11111111111"), ip="1.2.3.4")
        cad.criar_cadastro_link(self.conn, _dados(), ip="1.2.3.4")

    def test_situacao_link(self):
        r = cad.criar_cadastro_link(self.conn, _dados())
        s = cad.situacao_link(self.conn, r["id"], r["chave_envio"])
        self.assertEqual(s["primeiro_nome"], "João")
        self.assertEqual(set(s["documentos"]), set(cad.DOCUMENTOS))
        self.assertFalse(any(s["documentos"].values()))
        self.assertFalse(s["enviado"])
        self.assertEqual(s["status"], cad.STATUS_PENDENTE)
        with self.assertRaises(cad.CadastroInvalido):
            cad.situacao_link(self.conn, r["id"], "errada")
        cad.criar_cadastro_link(self.conn, _dados())   # refez o formulário: o anterior é substituído
        self.assertEqual(cad.situacao_link(self.conn, r["id"], r["chave_envio"])["status"], cad.STATUS_SUBSTITUIDO)

    def test_situacao_resultado_pra_tela_final(self):
        # Hugo, 02/11: "liberado" só quando entrou na tabela; conflito ou pendência = "em avaliação"
        r = cad.criar_cadastro_link(self.conn, _dados())
        sit = lambda: cad.situacao_link(self.conn, r["id"], r["chave_envio"])["resultado"]
        self.assertIsNone(sit())   # ainda não enviou
        self.conn.execute("UPDATE motoristas_cadastros SET enviado_em = ? WHERE id = ?", (banco.agora(), r["id"]))
        self.conn.commit()
        self.assertEqual(sit(), "processando")
        self.conn.execute("UPDATE motoristas_cadastros SET conflito_agent_id = 321 WHERE id = ?", (r["id"],))
        self.conn.commit()
        self.assertEqual(sit(), "em_avaliacao")
        self.conn.execute("UPDATE motoristas_cadastros SET conflito_resolvido = 1, ultimo_erro = 'Vuupt 500', "
                          "tentativas = 1 WHERE id = ?", (r["id"],))
        self.conn.commit()
        self.assertEqual(sit(), "em_avaliacao")
        self.conn.execute("UPDATE motoristas_cadastros SET bd_motorista_id = 7, ultimo_erro = NULL WHERE id = ?", (r["id"],))
        self.conn.commit()
        self.assertEqual(sit(), "liberado")

    def test_pendentes_do_app_sem_link(self):
        cad.criar_cadastro_link(self.conn, _dados())
        self.assertEqual(cad.listar_pendentes(self.conn), [])


if __name__ == "__main__":
    unittest.main()
