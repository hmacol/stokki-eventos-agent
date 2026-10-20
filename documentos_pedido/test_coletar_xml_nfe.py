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
    def _estado(self, situacao, tentativas, dias_atras=0):
        return {"situacao": situacao, "tentativas": tentativas,
                "ultima_em": (AGORA - timedelta(days=dias_atras)).strftime("%Y-%m-%d %H:%M:%S")}

    def test_politica_de_nova_tentativa(self):
        casos = [
            (None, False, True),
            (self._estado("OK", 1), True, False),
            (self._estado("ERRO", 4), False, True),
            (self._estado("ERRO", 5), False, False),
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


class PaginaFalsa:
    def __init__(self, falhas=()):
        self.visitas, self.falhas = [], set(falhas)

    def goto(self, url, **kw):
        self.visitas.append(url)
        if url.rsplit("/", 1)[-1] in self.falhas:
            raise TimeoutError(f"Timeout 30000ms exceeded navigating to {url}")

    def wait_for_timeout(self, ms):
        pass


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


if __name__ == "__main__":
    unittest.main()
