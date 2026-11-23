# -*- coding: utf-8 -*-
"""
NF ao lado do PS no painel interno (decisao do Hugo, 23/11): sem NF = traco,
mais de uma NF = separadas por virgula, busca por NF onde ja busca por PS.
Fonte da NF: nucleo/nf_pedido (documentos_pedido + apoio documentos_processados).

    py -3.11 -m unittest test_nf_ao_lado_do_ps   (de dentro de painel_agentes/)
"""
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from nucleo import banco, tabela_pedidos as tp  # noqa: E402
import aviso_rota_cancelada as arc  # noqa: E402
import documentos_email as de  # noqa: E402
import nf_tela  # noqa: E402

HOJE = date.today().isoformat()


class _Base(unittest.TestCase):
    """dados.db temporario com 3 pedidos: PS-1 (uma NF), PS-2 (duas NFs) e PS-3 (sem NF)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._patch = mock.patch.object(banco, "DB_PATH", Path(self._tmp.name) / "t.db")
        self._patch.start()
        self.conn = banco.conectar()
        self.conn.execute("CREATE TABLE documentos_pedido (id_pedido, tipo_doc, status, numero_nf)")
        self.conn.execute("CREATE TABLE documentos_processados (hash_conteudo TEXT PRIMARY KEY, tipo TEXT, "
                          "codigo_pedido TEXT, status TEXT, numero_nf TEXT)")
        self.conn.executemany("INSERT INTO documentos_pedido VALUES (?,?,?,?)", [
            ("#PS-1", "nf", "casado", "111"), ("#PS-2", "nf", "casado", "222"), ("#PS-2", "nf", "baixado", "333")])
        for i, cod in enumerate(("PS-1", "PS-2", "PS-3"), 1):
            self.conn.execute(
                "INSERT INTO nucleo_pedidos (codigo, titulo, status, sender_id, remetente_nome, destinatario_nome, "
                "destinatario_codigo, endereco, caixas, criado_em_provedor, fluxo, zona) "
                "VALUES (?, ?, 'ABERTO', 10, 'EMB', 'DEST', '1', 'RUA', 1, ?, 'ENTREGA', 'CENTRO')",
                (cod, f"#{cod} - X / EMB / DEST", f"{HOJE} 08:00:00"))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self._patch.stop()
        self._tmp.cleanup()


class TabelaPedidosTest(_Base):
    def _nf(self, filtros=None):
        return {l["codigo"]: l["nf"] for l in tp.listar(filtros or {}, conn=self.conn)["linhas"]}

    def test_coluna_nf_com_uma_com_varias_e_sem(self):
        self.assertEqual(self._nf(), {"PS-1": "111", "PS-2": "222, 333", "PS-3": "—"})
        chaves = [c.chave for c in tp.COLUNAS]
        self.assertEqual(chaves[chaves.index("codigo") + 1], "nf")   # ao lado do PS

    def test_busca_livre_e_filtro_da_coluna_aceitam_nf(self):
        self.assertEqual(list(self._nf({"q": "333"})), ["PS-2"])
        self.assertEqual(list(self._nf({"colunas": {"nf": "111"}})), ["PS-1"])
        self.assertEqual(list(self._nf({"q": "PS-3"})), ["PS-3"])   # a busca por PS continua

    def test_csv_tem_a_coluna_nf_com_traco(self):
        linhas = tp.exportar_csv({}, ["codigo", "nf"], conn=self.conn).lstrip("﻿").splitlines()
        self.assertEqual(linhas[0].split(";")[:2], ["Código", "NF"])
        self.assertEqual(sorted(";".join(l.split(";")[:2]) for l in linhas[1:]),
                         ["PS-1;111", "PS-2;222, 333", "PS-3;—"])


class NfTelaTest(_Base):
    def test_codigos_por_nf(self):
        self.assertEqual(nf_tela.codigos_por_nf("333"), ["PS-2"])
        self.assertEqual(nf_tela.codigos_por_nf("12"), [])        # curto demais
        self.assertEqual(nf_tela.codigos_por_nf("PS-1"), [])      # so digitos
        self.assertEqual(nf_tela.codigos_por_nf("999"), [])

    def test_texto_ps_nf(self):
        self.assertEqual(nf_tela.ps_nf("PS-1", "111"), "PS-1 · NF 111")
        self.assertEqual(nf_tela.ps_nf("PS-3", ""), "PS-3 · NF —")


class EmailDocumentosTest(unittest.TestCase):
    def test_corpo_lista_ps_e_nf_e_assunto_ja_tinha(self):
        def arq(ref, nf):
            return {"ref": ref, "nome": f"NF_{nf}.pdf", "nf": nf, "tipo": "NF", "tamanho": 1}
        a1, a2, a3 = arq("h1", "111"), arq("h2", "222"), arq("h3", "333")
        pedidos = [{"codigo": "PS-1", "arquivos": [a1]}, {"codigo": "PS-2", "arquivos": [a2, a3]},
                   {"codigo": "PS-3", "arquivos": []}]
        corpo = de._corpo([a1, a2, a3], 1, 1, pedidos)
        self.assertIn("PS-1 &middot; NF 111", corpo)
        self.assertIn("PS-2 &middot; NF 222, 333", corpo)
        self.assertNotIn("PS-3", corpo)               # sem documento neste e-mail
        self.assertEqual(de.nf_do_pedido(pedidos[2]), "—")
        self.assertEqual(de.resumo([dict(pedidos[0], faltando=[])])["pedidos"][0]["nf"], "111")


class AvisoRotaCanceladaTest(_Base):
    PARADAS = [{"codigo": "PS-1", "sender_id": 1, "remetente_nome": "Alfa"},
               {"codigo": "PS-2", "sender_id": 1, "remetente_nome": "Alfa"},
               {"codigo": "PS-3", "sender_id": 2, "remetente_nome": "Beta", "numero_nf": "555"}]

    def test_textos_levam_ps_e_nf(self):
        emb = arc.agrupar_por_embarcador(self.PARADAS)
        self.assertEqual(emb[0]["nfs"], {"PS-1": "", "PS-2": ""})   # o preenchimento vem em avisar()
        emb[0]["nfs"] = {"PS-1": "111", "PS-2": "222, 333"}
        texto = arc.texto_embarcador({"data_alvo": HOJE}, "chuva", emb[0]["codigos"], emb[0]["nfs"])
        self.assertIn("PS-1 · NF 111, PS-2 · NF 222, 333", texto)
        sem = arc.texto_embarcador({"data_alvo": HOJE}, "chuva", ["PS-3"], {})
        self.assertIn("PS-3 · NF —", sem)
        grupo = arc.texto_grupo({"data_alvo": HOJE, "nome": "R"}, "chuva", "hugo",
                                [dict(emb[0], aviso="piloto"), dict(emb[1], aviso="piloto")])
        self.assertIn("PS-1 · NF 111", grupo)
        self.assertIn("PS-3 · NF 555", grupo)

    def test_avisar_completa_a_nf_pelo_banco_sem_enviar(self):
        capturado = {}
        with mock.patch.object(arc, "_avisar_embarcador", side_effect=lambda c, r, m, e, cfg, db: capturado.setdefault(
                e["nome"], dict(e)) and "mock"), \
                mock.patch.object(arc.ch, "conectar", side_effect=RuntimeError("sem chat")), \
                mock.patch.object(arc, "_postar_grupo", side_effect=lambda texto, db: capturado.update(grupo=texto)):
            arc.avisar({"id": 1, "data_alvo": HOJE, "paradas": [dict(p) for p in self.PARADAS]}, "chuva", "hugo", {})
        self.assertIn("PS-1 · NF 111", capturado["grupo"])
        self.assertIn("PS-2 · NF 222, 333", capturado["grupo"])
        self.assertIn("PS-3 · NF 555", capturado["grupo"])


class TorreEPedidosParadosTest(_Base):
    def test_torre_anota_chips_e_descricao(self):
        import torre_controle as tc
        rotas = [{"pedidos": [{"codigo": "PS-1"}, {"codigo": "PS-3"}], "paradas": [{"codigo": "PS-2"}]}]
        excecoes = [{"codigo": "PS-2", "descricao": "PS-2 — titulo"}, {"descricao": "sem pedido"}]
        tc._anotar_nf_torre(rotas, excecoes)
        self.assertEqual([p["nf"] for p in rotas[0]["pedidos"]], ["111", "—"])
        self.assertEqual(rotas[0]["paradas"][0]["nf"], "222, 333")
        self.assertEqual(excecoes[0]["descricao"], "PS-2 · NF 222, 333 — titulo")
        self.assertEqual(excecoes[1]["descricao"], "sem pedido")

    def test_pedidos_parados_pelo_ps_e_pela_propria_nf(self):
        import pedidos_parados_triagem as pp
        itens = [{"order_number": "PS-1"}, {"order_number": "PS-3"}, {"order_number": "98765"}]
        pp._anotar_nf(itens)
        self.assertEqual([i["nf"] for i in itens], ["111", "", "98765"])


class ColetasEConsultaTest(_Base):
    def test_coletas_completa_a_nf_da_folha(self):
        folha = [{"codigo": "PS-1", "nf": None}, {"codigo": "PS-2", "nf": "999"}, {"codigo": "PS-3", "nf": None}]
        nf_tela.completar_nf(folha)
        self.assertEqual([p["nf"] for p in folha], ["111", "999", ""])   # o template poe o traco


if __name__ == "__main__":
    unittest.main()
