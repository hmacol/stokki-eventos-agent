# -*- coding: utf-8 -*-
"""
Rotas do painel da chegada (Receber) e trava da conferencia sem checklist.

    py -3.11 -m unittest test_wms_chegada_tela   (de dentro de painel_agentes/)

Nenhum e-mail sai: as funcoes de wms_chegada_avisos sao trocadas por mock.
"""
import io
import uuid
import sys
import unittest
from pathlib import Path
from unittest import mock

_AQUI = Path(__file__).parent
for _p in (_AQUI.parent, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import test_wms_doca_tela as doca  # noqa: E402
import wms  # noqa: E402
import wms_caixas  # noqa: E402
import wms_chegada  # noqa: E402
import wms_chegada_avisos  # noqa: E402
import wms_doca  # noqa: E402
from test_fila_etiquetas_tela import painel_agentes  # noqa: E402

ORIGEM = doca.ORIGEM
OK_CLIENTE = [{"enviado": True}]


def _png():
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), (200, 10, 10)).save(buf, format="PNG")
    return buf.getvalue()


class Base(doca.Base):
    def setUp(self):
        super().setUp()
        self.cfg["portal_cliente"] = {"secret_key": "segredo-de-teste"}
        self.etapas()
        self.entrar("total")
        self.operador()
        self.pasta = Path(wms.DB_PATH).parent / "chegadas"
        for nome, retorno in (("avisar_escritorio", {"enviado": True}),
                              ("avisar_cliente_chegada", OK_CLIENTE),
                              ("pedir_liberacao", {"enviado": True, "motivo": "", "destinos": ["a@b.c"]})):
            p = mock.patch.object(wms_chegada_avisos, nome, return_value=retorno)
            setattr(self, "m_" + nome, p.start())
            self.addCleanup(p.stop)

    def receb(self, id_stokki=1, embarcador="EMB", produto=1):
        conn = wms_caixas.conectar()
        cur = conn.execute(
            "INSERT INTO wms_recebimentos (id_stokki, stkkc_id, embarcador, lido_em, atualizado_em) "
            "VALUES (?, ?, ?, '2026-11-22', '2026-11-22')", (id_stokki, "E1", embarcador))
        rid = cur.lastrowid
        conn.execute("INSERT INTO wms_recebimento_itens (recebimento_id, linha, qtd_embalagem, produto_id) "
                     "VALUES (?, 1, 1, ?)", (rid, produto))
        conn.commit()
        conn.close()
        return rid

    def produto_frio(self, classe="REFRIGERADO"):
        conn = wms_caixas.conectar()
        conn.execute("UPDATE wms_produtos SET temperatura = ? WHERE id = 1", (classe,))
        conn.commit()
        conn.close()

    def abrir(self, rid, uuid=doca.UUID):
        r = self.enviar("/api/wms/chegadas", {"uuid": uuid, "recebimento_ids": [rid]})
        self.assertEqual(r.status_code, 200, r.get_json())
        return r.get_json()["chegada"]["id"]

    def foto(self, cid, tipo):
        return self.cliente.post(f"/api/wms/chegadas/{cid}/fotos", headers=ORIGEM,
                                 data={"tipo": tipo, "foto": (io.BytesIO(_png()), "f.png")},
                                 content_type="multipart/form-data")

    def dados(self, **mud):
        d = {"motorista": "Jose Silva", "placa": "ABC1D23", "integra": True, "observacao": "",
             "temperaturas": {"REFRIGERADO": "5"}}
        d.update(mud)
        return d

    def chegada_pronta(self, rid, **mud):
        cid = self.abrir(rid)
        for tipo in ("CAMINHAO_ABERTO", "CARGA", "TERMOMETRO", "AVARIA"):
            self.assertEqual(self.foto(cid, tipo).status_code, 200)
        r = self.enviar(f"/api/wms/chegadas/{cid}/concluir", self.dados(**mud))
        self.assertEqual(r.status_code, 200, r.get_json())
        return cid, r.get_json()

    def aguardando(self):
        self.produto_frio()
        rid = self.receb()
        cid, r = self.chegada_pronta(rid, temperaturas={"REFRIGERADO": "12"})
        self.assertEqual(r["chegada"]["estado"], "AGUARDANDO_ESCRITORIO")
        return rid, cid


class Lista(Base):
    def test_lista_marca_chegada_estado(self):
        rid = self.receb()
        rec = self.cliente.get("/api/wms/recebimentos").get_json()["recebimentos"][0]
        self.assertEqual((rec["usa_etapas"], rec["chegada_estado"], rec["chegada_id"]), (True, "SEM_CHEGADA", None))
        cid = self.abrir(rid)
        rec = self.cliente.get(f"/api/wms/recebimentos/{rid}").get_json()["recebimento"]
        self.assertEqual((rec["usa_etapas"], rec["chegada_estado"], rec["chegada_id"]), (True, "RASCUNHO", cid))


class Trava(Base):
    MSG = "Faça o Receber (checklist da chegada) antes de conferir."

    def tres_rotas(self, rid):
        return [
            self.enviar("/api/wms/caixas/C0000001/ativar", {"recebimento_id": rid}),
            self.enviar(f"/api/wms/recebimentos/{rid}/itens/1/enderecar", {"qtd": 1, "uuid": "x"}),
            self.enviar(f"/api/wms/recebimentos/{rid}/encerrar", {}),
        ]

    def test_conferir_sem_chegada_e_barrado(self):
        rid = self.receb()
        for r in self.tres_rotas(rid):
            self.assertEqual(r.status_code, 400)
            self.assertEqual(r.get_json()["erro"], self.MSG)

    def test_conferir_com_chegada_liberada_passa(self):
        rid = self.receb()
        self.chegada_pronta(rid)
        self.assertEqual(self.tres_rotas(rid)[0].status_code, 400)  # caixa inexistente: passou da trava
        for r in self.tres_rotas(rid):
            self.assertNotEqual((r.get_json() or {}).get("erro"), self.MSG)
        r = self.enviar(f"/api/wms/recebimentos/{rid}/itens/1/enderecar", {"qtd": 1, "uuid": "x1"})
        self.assertEqual(r.status_code, 200, r.get_json())

    def test_embarcador_sem_etapas_nao_tem_trava(self):
        self.cfg["wms"]["pilotos"] = []
        rid = self.receb()
        for r in self.tres_rotas(rid):
            self.assertNotEqual((r.get_json() or {}).get("erro"), self.MSG)
        rec = self.cliente.get(f"/api/wms/recebimentos/{rid}").get_json()["recebimento"]
        self.assertFalse(rec["usa_etapas"])


class EntradaNaDoca(Base):
    """ENTRADA por quantidade na doca (/movimentos) so pela conferencia de recebimento liberado."""
    def saldo_doca(self):
        conn = wms.conectar()
        try:
            return sum(s["quantidade"] for s in wms.saldos_produto(conn, 1) if s["posicao"] == "DOC1-P1")
        finally:
            conn.close()

    def entrada(self, rota="/api/wms/movimentos", **mud):
        m = {"uuid": "x" + uuid.uuid4().hex, "tipo": "ENTRADA", "produto_id": 1,
             "lote": "L1", "quantidade": 3, "destino": "DOC1-P1"}
        m.update(mud)
        if rota.endswith("lote"):
            return self.enviar(rota, {"movimentos": [m]})
        return self.enviar(rota, m)

    def setUp(self):
        super().setUp()
        self.cfg["wms"]["pilotos"] = [{"id": 48, "nome": "X", "etapas": True}]   # sem caixas: entrada por quantidade
        conn = wms.conectar()
        wms_doca.garantir_doca(conn)
        wms.criar_area(conn, "C1", "CONTAINER", "Container 1")
        wms.criar_posicao(conn, doca.POS)
        conn.close()

    def test_entrada_na_doca_sem_recebimento_e_barrada(self):
        r = self.entrada()
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["erro"], "Entrada na doca só pela conferência de um recebimento.")
        self.assertEqual(self.saldo_doca(), 0)

    def test_entrada_na_doca_sem_chegada_liberada_e_barrada(self):
        rid = self.receb()
        r = self.entrada(recebimento_id=rid)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["erro"], Trava.MSG)
        self.assertEqual(self.saldo_doca(), 0)

    def test_entrada_na_doca_com_chegada_liberada_passa(self):
        rid = self.receb()
        self.chegada_pronta(rid)
        r = self.entrada(recebimento_id=rid)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(self.saldo_doca(), 3)

    def test_entrada_na_doca_de_produto_de_outro_recebimento_e_barrada(self):
        rid = self.receb()
        self.chegada_pronta(rid)
        conn = wms.conectar()
        conn.execute("INSERT INTO wms_produtos (id, stokki_id, sku, descricao, embarcador, embarcador_id, unidade, "
                     "atualizado_em) VALUES (2, 901, 'X2', 'OUTRO', 'EMB', 48, 'UN', '2026-09-21 10:00:00')")
        conn.commit()
        conn.close()
        r = self.entrada(recebimento_id=rid, produto_id=2)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["erro"], "Esse produto não é deste recebimento.")

    def test_entrada_avulsa_fora_da_doca_continua_livre(self):
        r = self.entrada(destino=doca.POS)
        self.assertEqual(r.status_code, 200, r.get_json())

    def test_lote_com_entrada_na_doca_respeita_a_trava(self):
        rid = self.receb()
        r = self.entrada("/api/wms/movimentos/lote", recebimento_id=rid)
        res = r.get_json()["resultados"][0]
        self.assertFalse(res["ok"])
        self.assertEqual(res["erro"], Trava.MSG)
        self.assertEqual(self.saldo_doca(), 0)


class Fluxo(Base):
    def test_fluxo_completo_na_faixa(self):
        self.produto_frio()
        rid = self.receb()
        cid, r = self.chegada_pronta(rid)
        self.assertEqual(r["chegada"]["estado"], "LIBERADA")
        self.m_avisar_cliente_chegada.assert_called_once()
        self.m_avisar_escritorio.assert_not_called()
        self.assertEqual(r["avisos"], {"escritorio": None, "cliente": OK_CLIENTE})
        det = self.cliente.get(f"/api/wms/chegadas/{cid}").get_json()
        self.assertEqual(det["faixas"], {"REFRIGERADO": [0.0, 7.0]})
        self.assertEqual(det["classes"], ["REFRIGERADO"])

    def test_fora_da_faixa_avisa_escritorio_e_nao_o_cliente(self):
        self.aguardando()
        self.m_avisar_cliente_chegada.assert_not_called()
        self.assertEqual(self.m_avisar_escritorio.call_args.args[3], "TEMPERATURA")

    def test_avaria_avisa_escritorio(self):
        rid = self.receb()
        self.chegada_pronta(rid, integra=False, observacao="caixas amassadas")
        self.assertEqual(self.m_avisar_escritorio.call_args.args[3], "AVARIA")

    def test_falha_de_aviso_nao_desfaz(self):
        self.m_avisar_cliente_chegada.side_effect = RuntimeError("smtp")
        rid = self.receb()
        cid, r = self.chegada_pronta(rid)
        self.assertEqual(r["chegada"]["estado"], "LIBERADA")
        self.assertEqual(r["avisos"]["cliente"], None)

    def test_falha_do_aviso_do_escritorio_nao_desfaz(self):
        self.produto_frio()
        self.m_avisar_escritorio.side_effect = RuntimeError("smtp")
        rid = self.receb()
        cid, r = self.chegada_pronta(rid, temperaturas={"REFRIGERADO": "12"})
        self.assertEqual(r["chegada"]["estado"], "AGUARDANDO_ESCRITORIO")
        self.assertEqual(r["avisos"]["escritorio"], {"TEMPERATURA": None})

    def test_foto_e_remocao_depois_de_concluir_400(self):
        cid, _ = self.chegada_pronta(self.receb())
        self.assertEqual(self.foto(cid, "CARGA").status_code, 400)
        fid = self.cliente.get(f"/api/wms/chegadas/{cid}").get_json()["chegada"]["fotos"][0]["id"]
        r = self.cliente.delete(f"/api/wms/chegadas/{cid}/fotos/{fid}", headers=ORIGEM)
        self.assertEqual(r.status_code, 400)

    def test_concluir_invalido_400(self):
        cid = self.abrir(self.receb())
        r = self.enviar(f"/api/wms/chegadas/{cid}/concluir", {})
        self.assertEqual(r.status_code, 400)
        self.assertIn("erro", r.get_json())

    def test_foto_grande_demais_400(self):
        cid = self.abrir(self.receb())
        with mock.patch.object(wms_chegada, "MAX_FOTO_BYTES", 10):
            r = self.foto(cid, "CARGA")
        self.assertEqual(r.status_code, 400)

    def test_remover_foto(self):
        cid = self.abrir(self.receb())
        fid = self.foto(cid, "CARGA").get_json()["foto"]["id"]
        r = self.cliente.delete(f"/api/wms/chegadas/{cid}/fotos/{fid}", headers=ORIGEM)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.cliente.get(f"/api/wms/chegadas/{cid}").get_json()["chegada"]["fotos"], [])

    def test_relatorio_pdf(self):
        cid, _ = self.chegada_pronta(self.receb())
        r = self.cliente.get(f"/wms/chegadas/{cid}/relatorio.pdf")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content_type, "application/pdf")
        self.assertTrue(r.data.startswith(b"%PDF"))

    def test_foto_servida_e_de_outra_chegada_404(self):
        a = self.abrir(self.receb(1), uuid="u-a")
        b = self.abrir(self.receb(2), uuid="u-b")
        fid = self.foto(a, "CARGA").get_json()["foto"]["id"]
        ok = self.cliente.get(f"/wms/chegadas/{a}/foto/{fid}")
        ok.close()
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(self.cliente.get(f"/wms/chegadas/{b}/foto/{fid}").status_code, 404)


class Escritorio(Base):
    def test_galpao_nao_decide(self):
        rid, cid = self.aguardando()
        self.entrar("galpao")
        for rota, corpo in (("pedir-liberacao", {}), ("liberar-por-fora", {"texto": "x" * 12}),
                            ("nao-receber", {"motivo": "temperatura"})):
            r = self.enviar(f"/api/wms/chegadas/{cid}/{rota}", corpo)
            self.assertEqual(r.status_code, 403, rota)

    def test_pedir_liberacao_devolve_link(self):
        rid, cid = self.aguardando()
        r = self.enviar(f"/api/wms/chegadas/{cid}/pedir-liberacao", {})
        self.assertEqual(r.status_code, 200, r.get_json())
        corpo = r.get_json()
        self.assertIn("/liberacao/", corpo["link"])
        self.assertTrue(corpo["envio"]["enviado"])
        self.assertEqual(self.m_pedir_liberacao.call_args.args[3], corpo["link"])

    def test_pedir_liberacao_fora_do_estado_400(self):
        cid = self.abrir(self.receb())
        r = self.enviar(f"/api/wms/chegadas/{cid}/pedir-liberacao", {})
        self.assertEqual(r.status_code, 400)

    def test_liberar_por_fora_libera_e_avisa_cliente(self):
        rid, cid = self.aguardando()
        r = self.enviar(f"/api/wms/chegadas/{cid}/liberar-por-fora", {"texto": "Cliente autorizou por telefone"})
        self.assertEqual(r.status_code, 200, r.get_json())
        ch = r.get_json()["chegada"]
        self.assertEqual((ch["estado"], ch["liberacao_nome"]), ("LIBERADA", "teste"))
        self.m_avisar_cliente_chegada.assert_called_once()

    def test_nao_receber(self):
        rid, cid = self.aguardando()
        r = self.enviar(f"/api/wms/chegadas/{cid}/nao-receber", {"motivo": "carga quente"})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["chegada"]["estado"], "NAO_RECEBIDA")
        self.m_avisar_cliente_chegada.assert_not_called()

    def test_erro_de_regra_vira_400(self):
        rid, cid = self.aguardando()
        r = self.enviar(f"/api/wms/chegadas/{cid}/nao-receber", {"motivo": ""})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["erro"], "Informe o motivo.")


class TelasNoHtml(Base):
    """Marcadores das telas Receber no HTML do /wms (nao ha harness de JS)."""

    def test_marcadores(self):
        self.entrar("galpao")
        h = self.cliente.get("/wms").get_data(as_text=True)
        for m in ("TELAS.receberChegada", "TELAS.chegada1", "TELAS.chegada2", "TELAS.chegadaAguardando",
                  "Pedir liberação ao cliente", 'capture="environment"', "Relatório de Recebimento (PDF)",
                  "outro embarcador: faça um Receber separado", "Sem rede: a foto não foi enviada. Tente de novo."):
            self.assertIn(m, h)


if __name__ == "__main__":
    unittest.main()
