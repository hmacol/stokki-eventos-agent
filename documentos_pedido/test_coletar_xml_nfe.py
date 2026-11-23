# -*- coding: utf-8 -*-
"""
Rodar:  py -3.11 -m unittest documentos_pedido.test_coletar_xml_nfe
"""
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

_RAIZ = Path(__file__).parent.parent
for _p in (_RAIZ, _RAIZ / "documentos_pedido"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import coletar_xml_nfe as col  # noqa: E402

AGORA = datetime(2026, 10, 20, 23, 30)
DESDE = date(2026, 7, 15)


def _banco() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        create table nucleo_pedidos (codigo text primary key, status text, remetente_nome text,
            criado_em_provedor text);
        create table nucleo_rotas (id integer primary key, data_rota text);
        create table nucleo_paradas (id integer primary key, rota_id int, codigo text);
        create table portal_envios (id integer primary key, codigo_pedido text, chave_nfe text, status text);
    """)
    col.garantir_tabela(conn)
    return conn


def _ped(conn, codigo, status="ENTREGUE", criado="2026-08-10 10:00:00", remetente="EMBARCADOR X"):
    conn.execute("insert into nucleo_pedidos values (?, ?, ?, ?)", (codigo, status, remetente, criado))


class TestSelecao(unittest.TestCase):
    def test_seleciona_ativos_primeiro_depois_mais_novos(self):
        conn = _banco()
        _ped(conn, "PS-1", criado="2026-08-01 10:00:00")
        _ped(conn, "PS-2", criado="2026-09-01 10:00:00")
        _ped(conn, "PS-3", status="ABERTO", criado="2026-07-20 10:00:00")
        _ped(conn, "PS-4", status="EM_ROTA", criado="2026-07-21 10:00:00")
        self.assertEqual(col.selecionar(conn, DESDE, AGORA, 10), ["PS-4", "PS-3", "PS-2", "PS-1"])

    def test_reentrega_vira_um_codigo_base(self):
        conn = _banco()
        _ped(conn, "PS-10", status="INSUCESSO")
        _ped(conn, "PS-10-R1", status="ABERTO")
        self.assertEqual(col.selecionar(conn, DESDE, AGORA, 10), ["PS-10"])

    def test_fora_do_periodo_so_entra_com_parada_no_periodo(self):
        conn = _banco()
        _ped(conn, "PS-5", criado="2026-06-01 10:00:00")
        _ped(conn, "PS-6", criado="2026-06-02 10:00:00")
        conn.execute("insert into nucleo_rotas values (1, '2026-07-20')")
        conn.execute("insert into nucleo_paradas values (1, 1, 'PS-6')")
        self.assertEqual(col.selecionar(conn, DESDE, AGORA, 10), ["PS-6"])

    def test_pula_portal_com_chave_real_mas_nao_placeholder(self):
        conn = _banco()
        _ped(conn, "PS-7")
        _ped(conn, "PS-8")
        conn.execute("insert into portal_envios values (1, 'PS-7', ?, 'CRIADO')", ("3" * 44,))
        conn.execute("insert into portal_envios values (2, 'PS-8', ?, 'CRIADO')", ("99" + "1" * 42,))
        self.assertEqual(col.selecionar(conn, DESDE, AGORA, 10), ["PS-8"])

    def test_pula_embarcador_sem_nf(self):
        conn = _banco()
        _ped(conn, "PS-9", remetente="Padrão Puro Alimentos")
        _ped(conn, "PS-11", remetente="PEDRAMOURA LTDA")
        _ped(conn, "PS-12", remetente="Laticínios Dourado")
        self.assertEqual(col.selecionar(conn, DESDE, AGORA, 10), ["PS-12"])

    def test_respeita_limite(self):
        conn = _banco()
        for i in range(1, 6):
            _ped(conn, f"PS-{i}", criado=f"2026-08-0{i} 10:00:00")
        self.assertEqual(col.selecionar(conn, DESDE, AGORA, 2), ["PS-5", "PS-4"])

    def test_estado_ok_sai_da_selecao(self):
        conn = _banco()
        _ped(conn, "PS-1")
        col.registrar(conn, "PS-1", col.Situacao.OK, AGORA, chave="3" * 44)
        self.assertEqual(col.selecionar(conn, DESDE, AGORA, 10), [])


class TestPolitica(unittest.TestCase):
    def _estado(self, situacao, tentativas, dias_atras=0, erros=0):
        return {"situacao": situacao, "tentativas": tentativas, "erros": erros,
                "ultima_em": (AGORA - timedelta(days=dias_atras)).strftime("%Y-%m-%d %H:%M:%S")}

    def test_politica_de_nova_tentativa(self):
        casos = [
            (None, False, True),
            (self._estado("OK", 1), True, False),
            (self._estado("ERRO", 4, erros=4), False, True),
            (self._estado("ERRO", 5, erros=5), False, False),
            (self._estado("ERRO", 7, erros=1), True, True),        # revisao: 6 noites SEM_XML + 1 erro
            (self._estado("SEM_XML", 9), True, True),              # ativo: toda noite
            (self._estado("PLACEHOLDER", 1, dias_atras=7), False, True),
            (self._estado("PLACEHOLDER", 1, dias_atras=6), False, False),
            (self._estado("SEM_XML", 2, dias_atras=30), False, True),
            (self._estado("SEM_XML", 3, dias_atras=30), False, False),
        ]
        for estado, ativo, esperado in casos:
            with self.subTest(estado=estado, ativo=ativo):
                self.assertIs(col.deve_tentar(estado, ativo, AGORA), esperado)

    def test_registrar_conta_tentativas_e_corta_erro(self):
        conn = _banco()
        col.registrar(conn, "PS-1", col.Situacao.ERRO, AGORA, erro="x" * 500)
        col.registrar(conn, "PS-1", col.Situacao.SEM_XML, AGORA + timedelta(days=1))
        linha = conn.execute("select * from xml_stokki_coleta where codigo = 'PS-1'").fetchone()
        self.assertEqual((linha["situacao"], linha["tentativas"]), ("SEM_XML", 2))
        self.assertEqual(linha["primeira_em"], "2026-10-20 23:30:00")
        self.assertIsNone(linha["erro"])
        col.registrar(conn, "PS-2", col.Situacao.ERRO, AGORA, erro="x" * 500)
        erro = conn.execute("select erro from xml_stokki_coleta where codigo = 'PS-2'").fetchone()[0]
        self.assertEqual(len(erro), 200)


class TestEscrita(unittest.TestCase):
    def test_escrita_atomica_nao_deixa_xml_parcial(self):
        with tempfile.TemporaryDirectory() as tmp:
            pasta = Path(tmp)
            with patch.object(col.os, "replace", side_effect=OSError("disco")):
                with self.assertRaises(OSError):
                    col.salvar_xml(b"<nfe>1</nfe>", "PS-1", pasta)
            self.assertEqual([p.name for p in pasta.iterdir() if p.suffix == ".xml"], [])
            final = col.salvar_xml(b"<nfe>2</nfe>", "PS-1", pasta)
            self.assertEqual((final.name, final.read_bytes()), ("PS-1.xml", b"<nfe>2</nfe>"))
            col.salvar_xml(b"<nfe>3</nfe>", "PS-1", pasta)
            self.assertEqual(final.read_bytes(), b"<nfe>3</nfe>")
            self.assertEqual(sorted(p.name for p in pasta.iterdir()), ["PS-1.xml"])


XML_REAL = (b'<nfeProc xmlns="http://www.portalfiscal.inf.br/nfe"><NFe><infNFe Id="NFe'
            + b"3" * 44 + b'"></infNFe></NFe></nfeProc>')


class Resposta:
    def __init__(self, status=200):
        self.status = status


class PaginaFalsa:
    """Pagina do pedido na area /provider/ (a aba #document sempre existe na pagina boa)."""
    def __init__(self, falhas=(), status=200, url="https://freshlog.stokki.com.br/pt-br/provider/x"):
        self.visitas, self.falhas, self.status, self.url = [], set(falhas), status, url

    def goto(self, url, **kw):
        self.visitas.append(url)
        if url.rsplit("/", 1)[-1] in self.falhas:
            raise TimeoutError(f"Timeout 30000ms exceeded navigating to {url}")
        return Resposta(self.status)

    def wait_for_timeout(self, ms):
        pass

    def query_selector(self, sel):
        return object() if sel == "#document" and self.status < 400 else None

    def query_selector_all(self, sel):
        return []


class TestColeta(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pasta = Path(self.tmp.name) / "xml"
        self.conn = _banco()
        self.renovar = patch.object(col.sessao_uso, "renovar").start()
        self.addCleanup(patch.stopall)
        self.addCleanup(self.tmp.cleanup)
        self.respostas: dict[str, tuple] = {}  # id -> (bytes do xml ou None, href do campo proprio)

        def baixar(page, codigo):
            conteudo, _ = self.respostas.get(codigo.split("-")[1], (None, None))
            if conteudo is None:
                return None
            p = Path(self.tmp.name) / f"{codigo}_NFe.xml"
            p.write_bytes(conteudo)
            return p

        def link(page):
            ultimo = page.visitas[-1].rsplit("/", 1)[-1]
            return self.respostas.get(ultimo, (None, None))[1]

        patch.object(col.sd, "baixar_xml_nfe", side_effect=baixar).start()
        patch.object(col.sd, "localizar_link_xml_nfe", side_effect=link).start()

    def _coletar(self, codigos, page=None, relogio=None, gravar_estado=True):
        horas = iter([AGORA + timedelta(minutes=i) for i in range(100)])
        return col.coletar(self.conn, codigos, page or PaginaFalsa(), ate=AGORA + timedelta(hours=4),
                           pausa_s=0, relogio=relogio or (lambda: next(horas)), pasta=self.pasta,
                           gravar_estado=gravar_estado)

    def _estado(self, codigo):
        return self.conn.execute("select situacao, chave from xml_stokki_coleta where codigo = ?",
                                 (codigo,)).fetchone()

    def test_xml_real_salvo_e_estado_ok(self):
        self.respostas["1"] = (XML_REAL, "/xml/nfe/abc")
        r = self._coletar(["PS-1"])
        self.assertEqual((r["OK"], r["parou_por"]), (1, "fim"))
        self.assertEqual((self.pasta / "PS-1.xml").read_bytes(), XML_REAL)
        self.assertEqual(tuple(self._estado("PS-1")), ("OK", "3" * 44))

    def test_placeholder_sem_anexo_vira_placeholder(self):
        self.respostas["2"] = (None, "/xml/nfe/" + "99" + "1" * 42 + ".xml")
        self._coletar(["PS-2"])
        self.assertEqual(self._estado("PS-2")["situacao"], "PLACEHOLDER")
        self.assertFalse((self.pasta / "PS-2.xml").exists())

    def test_sem_link_vira_sem_xml(self):
        self._coletar(["PS-3"])
        self.assertEqual(self._estado("PS-3")["situacao"], "SEM_XML")

    def test_para_no_horario_limite(self):
        horas = iter([AGORA, AGORA + timedelta(hours=1), AGORA + timedelta(hours=5)])
        r = self._coletar(["PS-1", "PS-2", "PS-3"], relogio=lambda: next(horas))
        self.assertEqual((r["parou_por"], r["SEM_XML"]), ("horario", 2))
        self.assertIsNone(self._estado("PS-3"))

    def test_tres_erros_seguidos_param_a_noite(self):
        page = PaginaFalsa(falhas={"1", "2", "3"})
        r = self._coletar(["PS-1", "PS-2", "PS-3", "PS-4"], page=page)
        self.assertEqual((r["parou_por"], r["ERRO"]), ("erros", 3))
        self.assertEqual([self._estado(c)["situacao"] for c in ("PS-1", "PS-2", "PS-3")], ["ERRO"] * 3)
        self.assertEqual(len(page.visitas), 3)

    def test_erro_isolado_nao_para(self):
        r = self._coletar(["PS-1", "PS-2", "PS-3"], page=PaginaFalsa(falhas={"2"}))
        self.assertEqual((r["parou_por"], r["ERRO"], r["SEM_XML"]), ("fim", 1, 2))

    def test_modo_teste_nao_grava_estado(self):
        self.respostas["1"] = (XML_REAL, "/xml/nfe/abc")
        self._coletar(["PS-1"], gravar_estado=False)
        self.assertIsNone(self._estado("PS-1"))
        self.assertTrue((self.pasta / "PS-1.xml").exists())

    def test_renova_a_trava_durante_a_noite(self):
        self._coletar([f"PS-{i}" for i in range(1, 52)])
        self.assertEqual(self.renovar.call_count, 2)


class TestMain(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(patch.stopall)
        patch.object(col, "DB_PATH", Path(self.tmp.name) / "dados.db").start()
        self.navegador = patch.object(col, "_navegador").start()
        patch.object(col, "_carregar_config", return_value={}).start()
        patch.object(col.sessao_uso, "liberar").start()

    def test_sem_trava_nao_faz_login(self):
        patch.object(col.sessao_uso, "adquirir", return_value=False).start()
        patch.object(col.sessao_uso, "em_uso", return_value="expedir_pedidos:123").start()
        with self.assertLogs(col.logger, "WARNING") as log:
            self.assertEqual(col.main(["--somente", "PS-1"]), 0)
        self.navegador.assert_not_called()
        self.assertIn("expedir_pedidos:123", "\n".join(log.output))

    def test_tres_erros_seguidos_saem_com_1(self):
        patch.object(col.sessao_uso, "adquirir", return_value="provider").start()
        patch.object(col, "coletar", return_value={"parou_por": "erros"}).start()
        self.assertEqual(col.main(["--somente", "PS-1"]), 1)
        col.sessao_uso.liberar.assert_called_once_with(col.DONO_TRAVA)

    def test_pega_a_trava_da_provider(self):
        adquirir = patch.object(col.sessao_uso, "adquirir", return_value="provider").start()
        patch.object(col, "coletar", return_value={"parou_por": "fim"}).start()
        self.assertEqual(col.main(["--somente", "PS-1"]), 0)
        self.assertEqual(adquirir.call_args.kwargs["conta"], col.contas.PROVIDER)
        self.assertEqual(adquirir.call_args.args[0], "coletar-xml-nfe")


class _El:
    def __init__(self, href):
        self.href = href

    def get_attribute(self, nome):
        return self.href

    def query_selector(self, sel):
        return self if sel == "a.btn" else None


def _href(caminho: bytes, base: str = "https://freshlog.stokki.com.br/pt-br/xml/nfe/") -> str:
    import base64
    return base + base64.b64encode(caminho).decode()


class PaginaComLink(PaginaFalsa):
    """Link /xml/nfe/ real na aba Documentos; o fetch devolve `corpo` (None = excecao de rede)."""
    def __init__(self, corpo, links=None):
        super().__init__()
        self.corpo = corpo
        self.links = links or [_href(b"users/a/" + b"3" * 44 + b".xml")]

    def query_selector_all(self, sel):
        return [_El(h) for h in self.links]

    def evaluate(self, js, url):
        import base64
        if self.corpo is None:
            raise RuntimeError("net::ERR_CONNECTION_RESET")
        return base64.b64encode(self.corpo).decode()


class TestRevisao(unittest.TestCase):
    """Revisao independente do 0.4b (20/10), com as funcoes REAIS do stokki_documentos."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(patch.stopall)
        self.dl = Path(self.tmp.name) / "dl"
        patch.object(col.sd, "PASTA_TEMP_DOWNLOADS", self.dl).start()
        patch.object(col.sessao_uso, "renovar").start()
        self.conn = _banco()

    def _coletar(self, page, n):
        return col.coletar(self.conn, [f"PS-{i}" for i in range(1, n + 1)], page, ate=AGORA + timedelta(hours=4),
                           pausa_s=0, relogio=lambda: AGORA, pasta=Path(self.tmp.name) / "xml")

    def test_403_conta_como_erro_e_para_a_noite(self):
        r = self._coletar(PaginaFalsa(status=403), 10)
        self.assertEqual((r["ERRO"], r["SEM_XML"], r["parou_por"]), (3, 0, "erros"))

    def test_redirecionado_pro_login_conta_como_erro(self):
        r = self._coletar(PaginaFalsa(url="https://freshlog.stokki.com.br/pt-br/login"), 5)
        self.assertEqual((r["ERRO"], r["parou_por"]), (3, "erros"))

    def test_pedido_inexistente_500_e_sem_xml_e_nao_para(self):
        r = self._coletar(PaginaFalsa(status=500), 5)
        self.assertEqual((r["SEM_XML"], r["ERRO"], r["parou_por"]), (5, 0, "fim"))

    def test_download_html_de_bloqueio_conta_como_erro(self):
        r = self._coletar(PaginaComLink(b"<html>Acesso automatizado nao e permitido</html>"), 4)
        self.assertEqual((r["ERRO"], r["PLACEHOLDER"], r["parou_por"]), (3, 0, "erros"))

    def test_download_que_falha_com_link_real_conta_como_erro(self):
        r = self._coletar(PaginaComLink(None), 4)
        self.assertEqual((r["ERRO"], r["SEM_XML"]), (3, 0))

    def test_erro_isolado_nao_abandona_pedido_ativo(self):
        for _ in range(6):
            col.registrar(self.conn, "PS-1", col.Situacao.SEM_XML, AGORA)
        col.registrar(self.conn, "PS-1", col.Situacao.ERRO, AGORA, erro="Timeout")
        _ped(self.conn, "PS-1", status="ABERTO")
        self.assertEqual(col.selecionar(self.conn, DESDE, AGORA, 10), ["PS-1"])

    def test_cinco_erros_seguidos_abandonam(self):
        _ped(self.conn, "PS-1", status="ABERTO")
        for _ in range(5):
            col.registrar(self.conn, "PS-1", col.Situacao.ERRO, AGORA, erro="Timeout")
        self.assertEqual(col.selecionar(self.conn, DESDE, AGORA, 10), [])

    def test_anexos_baixados_nao_ficam_na_pasta_compartilhada(self):
        xml = (b'<nfeProc xmlns="http://www.portalfiscal.inf.br/nfe"><NFe><infNFe Id="NFe'
               + b"3" * 44 + b'"></infNFe></NFe></nfeProc>')
        links = [_href(b"users/a/99" + b"1" * 42 + b".xml"),
                 _href(b"users/a/nota.xml", "https://freshlog.stokki.com.br/pt-br/document/download/document/")]
        r = self._coletar(PaginaComLink(xml, links), 3)
        self.assertEqual(r["OK"], 3)
        self.assertEqual(sorted(p.name for p in self.dl.iterdir()) if self.dl.exists() else [], [])


class TestJanela(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(patch.stopall)
        patch.object(col, "DB_PATH", Path(self.tmp.name) / "dados.db").start()
        patch.object(col, "_carregar_config", return_value={}).start()
        self.adquirir = patch.object(col.sessao_uso, "adquirir", return_value=False).start()

    def test_fora_da_janela_sem_somente_nao_roda(self):
        conn = sqlite3.connect(col.DB_PATH)
        conn.executescript("create table nucleo_pedidos (codigo text, status text, remetente_nome text, "
                           "criado_em_provedor text); create table nucleo_rotas (id int, data_rota text); "
                           "create table nucleo_paradas (id int, rota_id int, codigo text); "
                           "create table portal_envios (id int, codigo_pedido text, chave_nfe text, status text);"
                           "insert into nucleo_pedidos values ('PS-1', 'ABERTO', 'X', '2026-10-10 10:00:00');")
        conn.close()
        for hora in (datetime(2026, 10, 21, 4, 0), datetime(2026, 10, 21, 14, 0), datetime(2026, 10, 21, 22, 59)):
            with self.subTest(hora=hora), patch.object(col, "_agora", return_value=hora):
                self.assertEqual(col.main([]), 0)
        self.adquirir.assert_not_called()

    def test_dentro_da_janela_ou_com_somente_roda(self):
        for hora, argv in ((datetime(2026, 10, 21, 23, 30), []), (datetime(2026, 10, 21, 1, 0), []),
                           (datetime(2026, 10, 21, 14, 0), ["--somente", "PS-1"])):
            with self.subTest(hora=hora), patch.object(col, "_agora", return_value=hora):
                col.main(argv or ["--somente", "PS-1"])
        self.assertEqual(self.adquirir.call_count, 3)

    def test_somente_vira_codigo_base(self):
        with patch.object(col, "_agora", return_value=datetime(2026, 10, 21, 14, 0)), \
                patch.object(col, "coletar", return_value={"parou_por": "fim"}) as coletar, \
                patch.object(col, "_navegador"):
            self.adquirir.return_value = "provider"
            patch.object(col.sessao_uso, "liberar").start()
            col.main(["--somente", "ps-10-r1, PS-11"])
        self.assertEqual(coletar.call_args.args[1], ["PS-10", "PS-11"])


if __name__ == "__main__":
    unittest.main()
