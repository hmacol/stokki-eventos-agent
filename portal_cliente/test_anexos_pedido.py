# -*- coding: utf-8 -*-
"""
Documentos (PDF) anexados ao pedido no portal (Hugo, 11/11/2026): o sistema
sugere o pedido, o usuario confirma, e o anexo vira documento do romaneio
quando o envio ganha o codigo PS.

    py -3.11 -m unittest portal_cliente.test_anexos_pedido -v
"""
import functools
import io
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

from PIL import Image  # noqa: E402

import anexos_pedido as ax  # noqa: E402
import envio_pedidos as ep  # noqa: E402
from test_endereco_entrega_api import CLIENTE, CNPJ, _importar_app  # noqa: E402
from test_endereco_entrega_envio import _xml_nf  # noqa: E402


@functools.cache
def _pdf(cor: int = 255) -> bytes:
    """PDF valido de 1 pagina (imagem, sem texto); a cor muda o hash. Com
    cache: o PIL grava a hora de criacao, e o mesmo documento tem que sair
    com o mesmo conteudo."""
    saida = io.BytesIO()
    Image.new("RGB", (60, 80), (cor, cor, cor)).save(saida, format="PDF")
    return saida.getvalue()


PEDIDOS = [
    {"chave": "a", "numero_nf": "000123", "destinatario_doc": "11111111000111", "referencia": None},
    {"chave": "b", "numero_nf": "456", "destinatario_doc": "22222222000122", "referencia": None},
    {"chave": "c", "numero_nf": "789", "destinatario_doc": "22222222000122", "referencia": "PED-2024-X"},
]


class Sugestao(unittest.TestCase):
    def _s(self, **info):
        return ax.sugerir_pedido({"nome": "doc.pdf", **info}, PEDIDOS)[0]

    def test_numero_da_nf_lido_no_documento(self):
        self.assertEqual(self._s(numero_nf="123"), "a")

    def test_numero_da_nf_no_nome_do_arquivo(self):
        self.assertEqual(self._s(nome="boleto NF 000456 parcela 1.pdf"), "b")

    def test_referencia_da_planilha_no_nome(self):
        self.assertEqual(self._s(nome="laudo_ped-2024-x.pdf"), "c")

    def test_cnpj_so_casa_quando_um_pedido_tem_aquele_destinatario(self):
        self.assertEqual(self._s(cnpj="11.111.111/0001-11"), "a")
        self.assertIsNone(self._s(cnpj="22222222000122"))  # dois pedidos pro mesmo destinatario

    def test_sem_pista_fica_sem_pedido(self):
        self.assertIsNone(self._s())

    def test_nf_do_documento_vence_o_nome(self):
        self.assertEqual(self._s(nome="456.pdf", numero_nf="123"), "a")

    def test_unico_pedido_recebe_o_documento(self):
        self.assertEqual(ax.sugerir_pedido({"nome": "x.pdf"}, PEDIDOS[:1]), ("a", "único pedido"))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        raiz = Path(self.tmp.name)
        (raiz / "x" / "_temporarios").mkdir(parents=True)
        for nome, valor in (("DB_PATH", raiz / "d.db"), ("PASTA_XMLS", raiz / "x"), ("PASTA_TEMP", raiz / "x" / "_temporarios"),
                            ("_RAIZ", raiz)):
            p = mock.patch.object(ep, nome, valor)
            p.start()
            self.addCleanup(p.stop)
        self.raiz = raiz

    def _conn(self):
        conn = ep.conectar()
        self.addCleanup(conn.close)
        conn.execute("CREATE TABLE IF NOT EXISTS documentos_processados (hash_conteudo TEXT PRIMARY KEY, origem TEXT NOT NULL, "
                     "nome_arquivo TEXT, tipo TEXT, codigo_pedido TEXT, status TEXT NOT NULL, motivo TEXT, gcs_path TEXT, "
                     "processado_em TEXT NOT NULL, numero_parcela INTEGER, total_parcelas INTEGER, numero_nf TEXT, cnpj_contraparte TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS nucleo_pedidos (codigo TEXT PRIMARY KEY, status TEXT)")
        return conn

    def _envio(self, conn, chave: str, codigo=None, status="NA_FILA") -> dict:
        conn.execute("INSERT INTO portal_envios (cnpj_embarcador, chave_nfe, numero_nf, xml_path, status, codigo_pedido, criado_em, "
                     "atualizado_em) VALUES (?,?,?,?,?,?,?,?)", (CNPJ, chave, chave, "x.xml", status, codigo, "2026-11-11", "2026-11-11"))
        return dict(conn.execute("SELECT * FROM portal_envios WHERE id = last_insert_rowid()").fetchone())


class Gravacao(Base):
    def test_le_pdf_e_recusa_o_que_nao_e_pdf(self):
        info = ax.ler_pdf("boleto_123.pdf", _pdf())
        self.assertEqual((info["tipo"], info["paginas"]), ("Boleto", 1))
        with self.assertRaises(ep.ErroEnvio):
            ax.ler_pdf("nota.pdf", b"<xml/>")

    def test_anexo_so_vale_pro_romaneio_quando_o_envio_ganha_o_ps(self):
        conn = self._conn()
        envio = self._envio(conn, "1")
        previa = ax.guardar_previa("laudo.pdf", _pdf())
        ax.anexar(conn, envio, [{"token": previa["token"], "casado_por": "automatico"}], "cliente")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM documentos_processados").fetchone()[0], 0)
        self.assertFalse(list(ep.PASTA_TEMP.glob(previa["token"] + ".*")))  # temporario consumido

        conn.execute("UPDATE portal_envios SET codigo_pedido = 'PS-40001' WHERE id = ?", (envio["id"],))
        self.assertEqual(ax.registrar_no_romaneio(conn, envio["id"]), 1)
        doc = conn.execute("SELECT * FROM documentos_processados").fetchone()
        self.assertEqual((doc["origem"], doc["status"], doc["codigo_pedido"], doc["tipo"]), ("portal", "ENVIADO", "PS-40001", "Outro"))
        self.assertTrue((self.raiz / "x" / "_anexos" / doc["nome_arquivo"]).is_file())
        self.assertEqual(ax.registrar_no_romaneio(conn, envio["id"]), 0)  # nao registra duas vezes

    def test_envio_que_ja_tem_ps_registra_na_hora(self):
        conn = self._conn()
        envio = self._envio(conn, "1", codigo="#PS-7", status="CRIADO")
        ax.anexar(conn, envio, [{"token": ax.guardar_previa("boleto.pdf", _pdf())["token"]}], "cliente")
        doc = conn.execute("SELECT codigo_pedido, tipo FROM documentos_processados").fetchone()
        self.assertEqual(tuple(doc), ("PS-7", "Boleto"))

    def test_mesmo_documento_nao_entra_em_dois_pedidos(self):
        conn = self._conn()
        e1, e2 = self._envio(conn, "1"), self._envio(conn, "2")
        ax.anexar(conn, e1, [{"token": ax.guardar_previa("a.pdf", _pdf())["token"]}], "cliente")
        with self.assertRaises(ep.ErroEnvio):
            ax.anexar(conn, e2, [{"token": ax.guardar_previa("a.pdf", _pdf())["token"]}], "cliente")

    def test_nao_toma_documento_ja_enviado_de_outro_pedido(self):
        """Revisao de seguranca 11/11: arquivo identico ja ENVIADO a outro pedido
        (e-mail, outro cliente) continua do dono; so a linha sem dono e assumida."""
        conn = self._conn()
        envio = self._envio(conn, "1", codigo="PS-7", status="CRIADO")
        for cor, status, codigo, esperado in ((61, "ENVIADO", "PS-999", ("email", "PS-999")),
                                              (64, "REVISAO_MANUAL", "PS-999", ("email", "PS-999")),
                                              (62, "REVISAO_MANUAL", None, ("portal", "PS-7"))):
            previa = ax.guardar_previa(f"boleto{cor}.pdf", _pdf(cor))
            hash_ = ax._ler_temporario(previa["token"])[0]["hash"]
            conn.execute("INSERT INTO documentos_processados (hash_conteudo, origem, nome_arquivo, tipo, codigo_pedido, status, "
                         "processado_em) VALUES (?, 'email', 'b.pdf', 'Boleto', ?, ?, '2026-11-10')", (hash_, codigo, status))
            ax.anexar(conn, envio, [{"token": previa["token"]}], "cliente")
            linha = conn.execute("SELECT origem, codigo_pedido FROM documentos_processados WHERE hash_conteudo = ?", (hash_,)).fetchone()
            self.assertEqual(tuple(linha), esperado)

    def test_mesmo_arquivo_em_outro_embarcador_nao_avisa_nem_toma_o_registro(self):
        """Revisao de seguranca 11/11: a resposta nao muda por causa do arquivo de
        outro cliente (nada de sondar), e o registro do romaneio fica com o primeiro."""
        conn = self._conn()
        conn.execute("INSERT INTO portal_envios (cnpj_embarcador, chave_nfe, numero_nf, xml_path, status, codigo_pedido, criado_em, "
                     "atualizado_em) VALUES ('99999999000199', 'z', '4242', 'x.xml', 'CRIADO', 'PS-900', '2026-11-11', '2026-11-11')")
        alheio = dict(conn.execute("SELECT * FROM portal_envios WHERE chave_nfe = 'z'").fetchone())
        ax.anexar(conn, alheio, [{"token": ax.guardar_previa("a.pdf", _pdf(63))["token"]}], "cliente")
        meu = self._envio(conn, "1", codigo="PS-7", status="CRIADO")
        ax.anexar(conn, meu, [{"token": ax.guardar_previa("a.pdf", _pdf(63))["token"]}], "cliente")  # aceita, sem erro
        self.assertEqual([r[0] for r in conn.execute("SELECT codigo_pedido FROM documentos_processados")], ["PS-900"])

    def test_nao_sobrescreve_documento_que_veio_da_stokki(self):
        conn = self._conn()
        envio = self._envio(conn, "1", codigo="PS-7", status="CRIADO")
        previa = ax.guardar_previa("danfe.pdf", _pdf())
        hash_ = ax._ler_temporario(previa["token"])[0]["hash"]
        conn.execute("INSERT INTO documentos_processados (hash_conteudo, origem, nome_arquivo, tipo, codigo_pedido, status, processado_em) "
                     "VALUES (?, 'stokki', 'PS-7_DANFE.pdf', 'Nota Fiscal', 'PS-7', 'ENVIADO', '2026-11-10')", (hash_,))
        ax.anexar(conn, envio, [{"token": previa["token"]}], "cliente")
        self.assertEqual(conn.execute("SELECT origem FROM documentos_processados").fetchone()[0], "stokki")

    def test_remover_tira_do_pedido_e_do_romaneio(self):
        conn = self._conn()
        envio = self._envio(conn, "1", codigo="PS-7", status="CRIADO")
        ax.anexar(conn, envio, [{"token": ax.guardar_previa("laudo.pdf", _pdf())["token"]}], "cliente")
        linhas = [dict(envio)]
        ax.anotar(conn, linhas)
        anexo = ax.buscar(conn, envio["id"], linhas[0]["anexos"][0]["id"])
        assert anexo
        ax.remover(conn, anexo)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM documentos_processados").fetchone()[0], 0)
        self.assertFalse(ax.caminho(anexo).exists())

    def test_so_anexa_ate_a_rota_sair(self):
        conn = self._conn()
        aberto = self._envio(conn, "1", codigo="PS-1", status="CRIADO")
        em_rota = self._envio(conn, "2", codigo="PS-2", status="CRIADO")
        cancelado = self._envio(conn, "3", status="CANCELADO")
        reentrega = self._envio(conn, "4", codigo="PS-4", status="CRIADO")
        conn.executemany("INSERT INTO nucleo_pedidos VALUES (?, ?)",
                         [("PS-1", "ABERTO"), ("PS-2", "EM_ROTA"), ("PS-4", "INSUCESSO"), ("PS-4-R1", "ABERTO")])
        self.assertEqual([ax.pode_anexar(conn, e) for e in (aberto, em_rota, cancelado, reentrega)], [True, False, False, True])
        self.assertTrue(ax.pode_anexar(conn, self._envio(conn, "5")))  # ainda sem PS


class Rotas(Base):
    def setUp(self):
        super().setUp()
        self.app = app = _importar_app()
        patches = [
            mock.patch.object(ep, "validar_item", return_value={"ok": True, "erros": [], "avisos": [], "envio_existente": None}),
            mock.patch.object(ep, "config_stokki_cliente", return_value={"regra_xml": "nenhuma", "envio_ativo": True, "sender_id": 1,
                                                                        "client_id": 1}),
            mock.patch.object(ep, "fluxos_para_envio", return_value=(["armazenagem"], {"erro": None})),
            mock.patch.object(ep, "info_destinatarios", return_value={}),
            mock.patch.object(ep, "_tipo_transportadora", return_value=None),
            mock.patch.object(ep, "classificar_area_envio", return_value=None),
            mock.patch.object(ep, "avisos_fora_dia_fixo", return_value=[]),
            mock.patch.object(app.auth, "sessao_valida", lambda c, cnpj, v: dict(CLIENTE)),
            mock.patch.object(app, "_CONFIG", {}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        app.app.config["TESTING"] = True
        self.tc = app.app.test_client()
        with self.tc.session_transaction() as s:
            s["cnpj"] = CNPJ
            s["v"] = "x"
        self._conn().commit()

    def _analisar(self):
        arquivos = [(io.BytesIO(_xml_nf(5001)), "nf1.xml"), (io.BytesIO(_xml_nf(5002)), "nf2.xml"),
                    (io.BytesIO(_pdf(10)), "boleto nf 5002.pdf"), (io.BytesIO(_pdf(20)), "sem pista.pdf"),
                    (io.BytesIO(b"nao sou pdf"), "falso.pdf")]
        r = self.tc.post("/api/envios/analisar", data={"arquivos": arquivos}, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return r.get_json()

    def _itens(self, j):
        por_nf = {it["numero_nf"]: it["token"] for it in j["itens"]}
        anexos = {a["nome"]: a for a in j["anexos"]}
        return [{"token": por_nf["5001"], "horario_inicio": "08:00", "horario_fim": "12:00"},
                {"token": por_nf["5002"], "horario_inicio": "08:00", "horario_fim": "12:00",
                 "anexos": [{"token": anexos["boleto nf 5002.pdf"]["token"], "casado_por": "automatico"}]}]

    def test_previa_distribui_os_pdfs_e_rejeita_o_que_nao_e_pdf(self):
        j = self._analisar()
        por_nf = {it["numero_nf"]: it["token"] for it in j["itens"]}
        sugerido = {a["nome"]: a["pedido"] for a in j["anexos"]}
        self.assertEqual(sugerido, {"boleto nf 5002.pdf": por_nf["5002"], "sem pista.pdf": None})
        self.assertIn("falso.pdf", [r["arquivo"] for r in j["rejeitados"]])

    def test_confirmar_exige_a_conferencia_do_usuario(self):
        itens = self._itens(self._analisar())
        r = self.tc.post("/api/envios/confirmar", json={"itens": itens})
        self.assertEqual(r.status_code, 400)
        self.assertIn("pedido correto", r.get_json()["erro"])
        self.assertEqual(self._conn().execute("SELECT COUNT(*) FROM portal_envios").fetchone()[0], 0)

    def test_confirmar_grava_o_documento_no_pedido_escolhido(self):
        itens = self._itens(self._analisar())
        r = self.tc.post("/api/envios/confirmar", json={"itens": itens, "anexos_conferidos": True})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["anexos_falhas"], [])
        linhas = self._conn().execute("SELECT e.numero_nf, a.nome_original, a.casado_por, a.conferido_por FROM portal_envios_anexos a "
                                      "JOIN portal_envios e ON e.id = a.envio_id").fetchall()
        self.assertEqual([tuple(l) for l in linhas], [("5002", "boleto nf 5002.pdf", "automatico", "cliente")])
        lista = self.tc.get("/api/envios").get_json()["envios"]
        self.assertEqual({e["numero_nf"]: len(e["anexos"]) for e in lista}, {"5001": 0, "5002": 1})

    def test_anexar_depois_do_envio_pede_conferencia_e_respeita_a_rota(self):
        conn = self._conn()
        envio = self._envio(conn, "77", codigo="PS-77", status="CRIADO")
        conn.commit()
        ler = self.tc.post("/api/envios/anexos/ler", data={"arquivos": [(io.BytesIO(_pdf(30)), "laudo.pdf")],
                                                            "pedidos": '[{"chave": "%d", "numero_nf": "77"}]' % envio["id"]},
                           content_type="multipart/form-data").get_json()
        token = ler["anexos"][0]["token"]
        self.assertEqual(ler["anexos"][0]["pedido"], str(envio["id"]))
        url = f"/api/envios/{envio['id']}/anexos"
        self.assertEqual(self.tc.post(url, json={"anexos": [token]}).status_code, 400)
        self.assertEqual(self.tc.post(url, json={"anexos": [token], "conferido": True}).status_code, 200)
        anexo_id = conn.execute("SELECT id FROM portal_envios_anexos").fetchone()[0]
        baixado = self.tc.get(f"{url}/{anexo_id}")
        self.assertEqual((baixado.status_code, baixado.mimetype), (200, "application/pdf"))
        baixado.close()

        conn.execute("INSERT INTO nucleo_pedidos VALUES ('PS-77', 'EM_ROTA')")
        conn.commit()
        self.assertEqual(self.tc.post(f"{url}/{anexo_id}/remover", json={}).status_code, 409)
        outro = self.tc.post("/api/envios/anexos/ler", data={"arquivos": [(io.BytesIO(_pdf(40)), "b.pdf")]},
                             content_type="multipart/form-data").get_json()["anexos"][0]["token"]
        self.assertEqual(self.tc.post(url, json={"anexos": [outro], "conferido": True}).status_code, 409)

    def test_pedido_de_outro_embarcador_nao_aceita_anexo(self):
        conn = self._conn()
        conn.execute("INSERT INTO portal_envios (cnpj_embarcador, chave_nfe, xml_path, status, criado_em, atualizado_em) "
                     "VALUES ('99999999000199', 'z', 'x.xml', 'NA_FILA', '2026-11-11', '2026-11-11')")
        conn.commit()
        alheio = conn.execute("SELECT id FROM portal_envios").fetchone()[0]
        r = self.tc.post(f"/api/envios/{alheio}/anexos", json={"anexos": ["x" * 20], "conferido": True})
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
