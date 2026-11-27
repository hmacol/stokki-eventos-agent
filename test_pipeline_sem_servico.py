# -*- coding: utf-8 -*-
"""
Pipeline com planejamento.fonte_pool = stokki: o pedido nasce so no nucleo, sem
servico na Vuupt (plano 2026-11-26-pool-direto-da-stokki, Task 4). Rodar (da raiz):
py -3.11 -m unittest test_pipeline_sem_servico -v
"""
import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock

import pipeline
from nucleo import banco, pedidos, pool

CONFIG_STOKKI = {"planejamento": {"fonte_pool": "stokki"}}
MANHA = datetime(2026, 11, 9, 10, 0)       # antes do corte (14h15) -> 10/11
TARDE = datetime(2026, 11, 9, 15, 5)       # depois do corte -> 11/11


def _detalhe(situacao="Aguardando Transportador"):
    return {"destino": {"documento": "123", "nome": "DEST", "telefone": "11999990000"},
            "local_entrega": {}, "itens": [], "referencia": "NF1", "stkkc_id": 1,
            "transportadora": {}, "volumes": 1, "situacao": situacao}


def _res_end(tipo_transportadora=None):
    transp = {"tipo": tipo_transportadora, "nome": "CLIENTE RETIRA"} if tipo_transportadora else {}
    return mock.Mock(endereco={"logradouro": "RUA A 1", "bairro": "CENTRO", "cidade": "Sao Paulo",
                               "uf": "SP", "cep": "01001-000"}, horario_entrega=None,
                     fonte="destino", requer_revisao=False, observacao="", transportadora=transp)


class _Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name) / "t.db"
        self.detalhe = _detalhe()
        self.res_end = _res_end()
        patches = [
            mock.patch.object(banco, "DB_PATH", self.db),
            mock.patch("roteirizacao.registro_dia_fixo.DB_PATH", self.db),
            mock.patch.object(pipeline, "ja_confirmado_atribuido", return_value=False),
            mock.patch.object(pipeline, "marcar_atribuido"),
            mock.patch.object(pipeline.stokki_pedidos, "obter_detalhe", side_effect=lambda *a: self.detalhe),
            mock.patch.object(pipeline, "resolver_endereco_entrega", side_effect=lambda *a, **k: self.res_end),
            mock.patch.object(pipeline, "config_consulta", return_value={"ativo": False}),
            mock.patch.object(pipeline, "_buscar_dados_embarcador_banco", return_value={}),
            mock.patch.object(pipeline, "buscar_confirmacao", return_value=None),
            mock.patch.object(pipeline, "tem_agendamento", return_value=False),
            mock.patch.object(pipeline, "geocodificar", return_value=None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _vuupt(self, existente=None, acao="criado"):
        v = mock.Mock()
        v.STATUS_ATUALIZAVEL = "not_assigned"
        v.buscar_servico_por_code.return_value = existente
        v.skill_ids_por_nome.return_value = []
        v.criar_ou_atualizar_servico.return_value = ({"service": {"id": 5, "code": "#PS-1"}}, acao)
        return v

    def _processar(self, vuupt, inicio=MANHA, config=CONFIG_STOKKI, modo_teste=False, **kw):
        res = pipeline.processar_pedido(
            41679, "PS-1", sess_stokki=mock.Mock(), catalogo=mock.Mock(), vuupt=vuupt,
            anthropic_api_key="", modo_teste=modo_teste, mapa_horarios={"123": ("07:00", "10:00")},
            ajustes_manuais={}, inicio_rodada=inicio, config_app=config, **kw)
        self.assertIsNone(res["erro"])
        return res

    def _linha(self):
        return pedidos.buscar_pedido("PS-1")

    def _ja_no_nucleo_sem_servico(self):
        conn = banco.conectar()
        try:
            pedidos.upsert_pedido(conn, "PS-1", {"titulo": "PS-1 - X", "endereco": "RUA A 1"},
                                  banco.ORIGEM_PIPELINE)
            conn.commit()
        finally:
            conn.close()


class CaminhoSemServicoTest(_Base):
    def test_chave_ligada_nao_chama_vuupt(self):
        vuupt = self._vuupt()
        res = self._processar(vuupt)
        self.assertEqual(vuupt.mock_calls, [])          # nenhuma chamada (nem GET, nem skills)
        self.assertEqual(res["acao"], "gravado_nucleo")
        self.assertIsNone(res["vuupt_service_id"])
        linha = self._linha()
        self.assertIsNotNone(linha)
        self.assertIsNone(linha["vuupt_service_id"])
        self.assertEqual(linha["status"], banco.PEDIDO_ABERTO)
        self.assertEqual(linha["agendamento_inicio"], "2026-11-10 07:00:00")
        dados = json.loads(linha["dados_json"])
        self.assertEqual(dados["acao"], "nucleo_sem_servico")
        self.assertEqual(dados["payload"]["code"], "#PS-1")
        # entra no pool da fonte stokki com id sintetico
        itens = pool.listar_pool(incluir_sem_servico=True)
        self.assertEqual([i["code"] for i in itens], ["#PS-1"])
        self.assertTrue(pool.eh_sem_servico(itens[0]["id"]))

    def test_modo_teste_nao_grava_no_nucleo(self):
        vuupt = self._vuupt()
        res = self._processar(vuupt, modo_teste=True)
        self.assertEqual(res["acao"], "simulado")
        self.assertEqual(vuupt.mock_calls, [])
        self.assertIsNone(self._linha())

    def test_pedido_com_servico_no_nucleo_segue_caminho_antigo(self):
        conn = banco.conectar()
        try:
            pedidos.upsert_pedido(conn, "PS-1", {"titulo": "x"}, banco.ORIGEM_PIPELINE, vuupt_service_id=5)
            conn.commit()
        finally:
            conn.close()
        vuupt = self._vuupt({"id": 5, "code": "#PS-1", "status": "not_assigned"}, acao="atualizado")
        res = self._processar(vuupt)
        vuupt.buscar_servico_por_code.assert_called_once_with("PS-1")
        vuupt.criar_ou_atualizar_servico.assert_called_once()
        self.assertEqual(res["acao"], "atualizado")
        self.assertEqual(res["vuupt_service_id"], 5)

    def test_chave_desligada_igual_a_hoje(self):
        vuupt = self._vuupt()
        with mock.patch("nucleo.pedidos.registrar_importacao") as reg:
            res = self._processar(vuupt, config={})
        vuupt.buscar_servico_por_code.assert_called_once_with("PS-1")
        vuupt.skill_ids_por_nome.assert_called_once()
        vuupt.criar_ou_atualizar_servico.assert_called_once()
        self.assertEqual(res["acao"], "criado")
        self.assertEqual(res["vuupt_service_id"], 5)
        payload, resposta, acao = reg.call_args[0]
        self.assertEqual(acao, "criado")
        self.assertEqual(resposta, {"service": {"id": 5, "code": "#PS-1"}})

    def test_fila_freshhub_forca_caminho_antigo(self):
        vuupt = self._vuupt()
        res = self._processar(vuupt, forcar_caminho_vuupt=True)
        vuupt.criar_ou_atualizar_servico.assert_called_once()
        self.assertEqual(res["vuupt_service_id"], 5)


class MemoriaDoPedidoTest(_Base):
    def test_segunda_rodada_nao_recalcula_data_automatica(self):
        vuupt = self._vuupt()
        self._processar(vuupt, inicio=MANHA)
        self.assertEqual(self._linha()["agendamento_inicio"], "2026-11-10 07:00:00")
        # rodada pos-corte: pedido novo iria pra 11/11, mas este ja tem data automatica gravada
        res = self._processar(vuupt, inicio=TARDE)
        self.assertEqual(res["acao"], "gravado_nucleo")
        self.assertEqual(self._linha()["agendamento_inicio"], "2026-11-10 07:00:00")
        self.assertEqual(self._linha()["agendamento_fim"], "2026-11-10 10:00:00")
        self.assertEqual(vuupt.mock_calls, [])
        # a data e marcada AUTOMATICO (senao marcar_datas_automaticas a trata como do cliente -> dedicado)
        from roteirizacao import registro_dia_fixo as reg
        conn = reg.conectar(self.db)
        try:
            item = pool.pedido_para_servico(self._linha())
            self.assertTrue(reg.data_e_automatica(conn, item, date(2026, 11, 10)))
        finally:
            conn.close()

    def test_aviso_dia_fixo_nao_repete(self):
        vuupt = self._vuupt()
        regra = {"nome": "Sorocaba", "dias": [3]}
        ajuste = mock.Mock(return_value=(date(2026, 11, 12), regra))
        with mock.patch("roteirizacao.regioes_dia_fixo.ajustar_data_por_dia_fixo", ajuste), \
                mock.patch("roteirizacao.regioes_dia_fixo.descricao_dias", return_value="quintas"):
            primeira = self._processar(vuupt, inicio=MANHA)
            segunda = self._processar(vuupt, inicio=MANHA)
        self.assertEqual(primeira["ajuste_dia_fixo"]["data"], "2026-11-12")
        self.assertNotIn("ajuste_dia_fixo", segunda)
        self.assertEqual(self._linha()["agendamento_inicio"], "2026-11-12 07:00:00")
        self.assertEqual(vuupt.mock_calls, [])


class FecharSemServicoTest(_Base):
    def _eventos(self):
        conn = banco.conectar()
        try:
            return [json.loads(r[0]) for r in conn.execute(
                "SELECT dados_json FROM nucleo_eventos WHERE tipo = 'PEDIDO_CANCELADO'")]
        finally:
            conn.close()

    def test_cancelado_na_stokki_fecha_pedido_sem_servico(self):
        self._ja_no_nucleo_sem_servico()
        self.detalhe = _detalhe("Cancelado")
        vuupt = self._vuupt()
        res = self._processar(vuupt)
        self.assertEqual(res["acao"], "ignorado_cancelado_stokki")
        self.assertEqual(self._linha()["status"], banco.PEDIDO_CANCELADO)
        self.assertEqual(vuupt.mock_calls, [])
        self.assertEqual(len(self._eventos()), 1)
        self.assertEqual(pool.listar_pool(incluir_sem_servico=True), [])

    def test_cancelado_na_stokki_modo_teste_nao_fecha(self):
        self._ja_no_nucleo_sem_servico()
        self.detalhe = _detalhe("Cancelado")
        self._processar(self._vuupt(), modo_teste=True)
        self.assertEqual(self._linha()["status"], banco.PEDIDO_ABERTO)

    def test_retirada_sem_servico_fecha_no_nucleo(self):
        self._ja_no_nucleo_sem_servico()
        self.res_end = _res_end("RETIRADA")
        vuupt = self._vuupt()
        res = self._processar(vuupt)
        self.assertEqual(res["acao"], "ignorado_retirada")
        self.assertEqual(self._linha()["status"], banco.PEDIDO_CANCELADO)
        self.assertEqual(vuupt.mock_calls, [])
        self.assertEqual(pool.listar_pool(incluir_sem_servico=True), [])

    def test_retirada_que_nunca_entrou_nao_grava_nada(self):
        self.res_end = _res_end("RETIRADA")
        vuupt = self._vuupt()
        res = self._processar(vuupt)
        self.assertEqual(res["acao"], "ignorado_retirada")
        self.assertIsNone(self._linha())
        self.assertEqual(vuupt.mock_calls, [])

    def _parada_em_rascunhos(self):
        """Pedido sem servico com parada de id negativo num rascunho RASCUNHO e num OFERTADA (hoje)."""
        import sys
        painel = str(Path(pipeline.__file__).parent / "painel_agentes")
        if painel not in sys.path:
            sys.path.append(painel)
        import rascunhos_rota
        for p in (mock.patch.object(rascunhos_rota, "DB_PATH", self.db),
                  mock.patch.object(rascunhos_rota, "_recalcular_km_silencioso")):
            p.start()
            self.addCleanup(p.stop)
        self._ja_no_nucleo_sem_servico()
        neg = pedidos.id_sintetico(self._linha())
        ids = {}
        for status in (rascunhos_rota.STATUS_RASCUNHO, rascunhos_rota.STATUS_OFERTADA):
            rid = rascunhos_rota.criar_rascunho_vazio(date.today(), "L1", "p", "t", 1, None, "06:00")
            conn = rascunhos_rota._conectar()
            try:
                conn.execute("UPDATE rascunhos_rota SET status = ? WHERE id = ?", (status, rid))
                conn.execute("INSERT INTO rascunhos_parada (rascunho_id, ordem, service_id, codigo) "
                             "VALUES (?, 0, ?, 'PS-1')", (rid, neg))
                conn.commit()
            finally:
                conn.close()
            ids[status] = rid
        return rascunhos_rota, neg, ids

    def _paradas(self, rascunhos_rota, neg):
        conn = rascunhos_rota._conectar()
        try:
            return {r[0] for r in conn.execute(
                "SELECT r.status FROM rascunhos_parada p JOIN rascunhos_rota r ON r.id = p.rascunho_id "
                "WHERE p.service_id = ?", (neg,))}
        finally:
            conn.close()

    def test_cancelado_na_stokki_sem_servico_sai_do_rascunho(self):
        rr, neg, _ = self._parada_em_rascunhos()
        self.detalhe = _detalhe("Cancelado")
        self._processar(self._vuupt())
        self.assertEqual(self._linha()["status"], banco.PEDIDO_CANCELADO)
        # RASCUNHO perde a parada; OFERTADA nao e mexido (regra da materializacao, Task 5)
        self.assertEqual(self._paradas(rr, neg), {rr.STATUS_OFERTADA})

    def test_retirada_sem_servico_sai_do_rascunho(self):
        rr, neg, _ = self._parada_em_rascunhos()
        self.res_end = _res_end("RETIRADA")
        self._processar(self._vuupt())
        self.assertEqual(self._linha()["status"], banco.PEDIDO_CANCELADO)
        self.assertEqual(self._paradas(rr, neg), {rr.STATUS_OFERTADA})

    def test_modo_teste_nao_tira_do_rascunho(self):
        rr, neg, _ = self._parada_em_rascunhos()
        self.detalhe = _detalhe("Cancelado")
        self._processar(self._vuupt(), modo_teste=True)
        self.assertEqual(self._paradas(rr, neg), {rr.STATUS_RASCUNHO, rr.STATUS_OFERTADA})

    def test_cancelado_no_nucleo_e_aberto_na_stokki_pede_revisao(self):
        self._ja_no_nucleo_sem_servico()
        pedidos.cancelar_sem_servico("PS-1", "teste", banco.ORIGEM_PAINEL)
        vuupt = self._vuupt()
        res = self._processar(vuupt)
        self.assertEqual(res["acao"], "pulado_cancelado_vuupt")
        self.assertTrue(res["requer_revisao"])
        self.assertEqual(self._linha()["status"], banco.PEDIDO_CANCELADO)
        self.assertEqual(vuupt.mock_calls, [])


if __name__ == "__main__":
    unittest.main()
