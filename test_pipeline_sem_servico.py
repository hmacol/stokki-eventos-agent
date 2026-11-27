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

    def test_pedido_gravado_pelo_pipeline_materializa_com_o_payload_dele(self):
        # Task 5: o que o pipeline guardou (dados_json + colunas) e o que vai pra Vuupt no envio
        from nucleo import materializar
        vuupt = self._vuupt()
        self._processar(vuupt)
        vuupt.buscar_servico_por_code.return_value = None
        vuupt.skill_ids_por_nome.return_value = [55]
        res = materializar.materializar_servicos([{"code": "PS-1"}], vuupt, CONFIG_STOKKI)
        self.assertEqual(res.ok, {"PS-1": 5})
        payload = vuupt.criar_ou_atualizar_servico.call_args.args[0]
        self.assertEqual(payload["code"], "#PS-1")
        self.assertEqual(payload["scheduled_start"], "2026-11-10T07:00:00-03:00")
        self.assertEqual(payload["skills"], [{"id": 55}])
        self.assertFalse([k for k in payload if k.startswith("_")])
        self.assertEqual(self._linha()["vuupt_service_id"], 5)


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


class EdicaoLocalTest(_Base):
    def test_pipeline_nao_sobrescreve_edicao_local(self):
        # Task 6: o Planejamento reagendou e trocou o endereco do pedido sem servico; a rodada
        # seguinte do pipeline (Stokki com os dados de sempre) nao desfaz e a materializacao usa o editado
        from nucleo import materializar
        vuupt = self._vuupt()
        self._processar(vuupt)
        pedidos.editar_sem_servico(pedidos.id_sintetico(self._linha()), {
            "endereco": "RUA NOVA 2", "latitude": -23.6, "longitude": -46.7,
            "agendamento_inicio": "2026-11-20 08:00:00", "agendamento_fim": "2026-11-20 12:00:00"})
        res = self._processar(vuupt)
        self.assertEqual(res["acao"], "gravado_nucleo")
        self.assertEqual(vuupt.mock_calls, [])
        linha = self._linha()
        self.assertEqual((linha["endereco"], linha["latitude"], linha["longitude"]), ("RUA NOVA 2", -23.6, -46.7))
        self.assertEqual((linha["agendamento_inicio"], linha["agendamento_fim"]),
                         ("2026-11-20 08:00:00", "2026-11-20 12:00:00"))
        payload = materializar.montar_payload(linha, vuupt)
        self.assertEqual(payload["customer"]["address"], "RUA NOVA 2")
        self.assertEqual(payload["scheduled_start"], "2026-11-20T08:00:00-03:00")


    def test_edicao_local_sobrevive_ao_servico_voltar_a_not_assigned(self):
        # Fix round 1: materializa o pedido editado com o VuuptClient de verdade (HTTP mockado, fingerprint
        # real num banco temporario); o servico volta a not_assigned (envio falhou / rota cancelada) e a
        # rodada seguinte do pipeline (caminho antigo) nao pode mandar o payload da Stokki por cima
        import fingerprint_importacao
        from nucleo import materializar
        from nucleo.normalizacao import local_para_vuupt
        from vuupt_client import VuuptClient
        p = mock.patch.object(fingerprint_importacao, "DB_PATH", self.db)
        p.start()
        self.addCleanup(p.stop)
        self._processar(self._vuupt())
        sid = pedidos.id_sintetico(self._linha())
        pedidos.editar_sem_servico(sid, {"endereco": "RUA NOVA 2", "latitude": -23.6, "longitude": -46.7})
        antes = self._linha()

        vuupt = VuuptClient("tok")
        vuupt.session = mock.Mock(**{f"{m}.side_effect": AssertionError("rede") for m in
                                     ("get", "post", "put", "patch", "delete", "request")})
        vivo = {}
        servico = {"id": 5, "code": "#PS-1", "status": "not_assigned", "customer_id": 77, "sender_id": 11,
                   "scheduled_start": local_para_vuupt(antes["agendamento_inicio"]),
                   "scheduled_end": local_para_vuupt(antes["agendamento_fim"])}

        def criar(payload):
            vivo["s"] = servico
            return {"service": servico}
        metodos = {"buscar_servico_por_code": mock.Mock(side_effect=lambda code: vivo.get("s")),
                   "criar_servico": mock.Mock(side_effect=criar),
                   "atualizar_servico": mock.Mock(return_value={"service": servico}),
                   "resolver_customer_id": mock.Mock(return_value=77),
                   "skill_ids_por_nome": mock.Mock(return_value=[55])}
        for nome, m in metodos.items():
            setattr(vuupt, nome, m)

        res = materializar.materializar_servicos([{"id": sid}], vuupt, CONFIG_STOKKI)
        self.assertEqual((res.ok, res.falhas), ({"PS-1": 5}, {}))
        self.assertEqual(metodos["criar_servico"].call_args.args[0]["latitude"], -23.6)
        self.assertEqual(metodos["resolver_customer_id"].call_args.args[2], "RUA NOVA 2")

        # rodada seguinte, servico de volta a not_assigned na Vuupt
        res = self._processar(vuupt)
        self.assertEqual(res["acao"], "pulado_sem_alteracao")
        metodos["atualizar_servico"].assert_not_called()
        metodos["criar_servico"].assert_called_once()
        depois = self._linha()
        for coluna in ("endereco", "latitude", "longitude", "agendamento_inicio", "agendamento_fim"):
            self.assertEqual(depois[coluna], antes[coluna], coluna)


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
