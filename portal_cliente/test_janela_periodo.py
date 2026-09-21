# -*- coding: utf-8 -*-
"""
Testes da janela corrida do acompanhamento (pedido do Hugo, 21/09: "o
quadro de pedidos corrido, com historico a partir de uma semana, similar
ao que temos na torre" -- ficou em 14 dias, a mesma janela da Torre).

    py -3.11 -m unittest portal_cliente.test_janela_periodo
"""
import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import dados_cliente as dc  # noqa: E402

HOJE = date(2026, 9, 21)
REMETENTE = 100


def _servico(codigo, status="done", status_done=None, sender_id=REMETENTE, completed_at=None):
    return {
        "id": abs(hash(codigo)) % 100000, "code": codigo, "sender_id": sender_id,
        "status": status, "status_done": status_done, "dimension_3": 3,
        "address": "Rua X 10, Sao Paulo - SP", "completed_at": completed_at,
        "customer": {"data": {"name": "DESTINO " + codigo}},
    }


def _rota(rota_id, dia_local: date, hora_local="06:00", servicos=None):
    """Rota como a Vuupt devolve: start_at em UTC SEM fuso (06:00 em SP
    chega como 09:00). Ver nucleo/normalizacao.vuupt_para_local."""
    h, m = (int(x) for x in hora_local.split(":"))
    utc = datetime.combine(dia_local, datetime.min.time()).replace(hour=h, minute=m) + timedelta(hours=3)
    return {
        "id": rota_id, "name": f"Planejamento - {dia_local.strftime('%d/%m/%Y')} - #{rota_id}",
        "status": "finished", "agent_id": None, "start_at": utc.strftime("%Y-%m-%d %H:%M:%S"),
        "services": {"data": servicos or []},
    }


class DiaLocalDaRotaTest(unittest.TestCase):
    """start_at vem em UTC sem fuso -- armadilha conhecida do projeto."""

    def test_rota_da_manha_fica_no_proprio_dia(self):
        self.assertEqual(dc.dia_local_da_rota(_rota(1, HOJE, "06:00")), HOJE)

    def test_rota_das_22h_nao_escorrega_pro_dia_seguinte(self):
        self.assertEqual(dc.dia_local_da_rota(_rota(1, HOJE, "22:00")), HOJE)

    def test_start_at_ausente_nao_quebra(self):
        self.assertIsNone(dc.dia_local_da_rota({"id": 1}))


class LinhasDasRotasTest(unittest.TestCase):
    def test_linha_carrega_a_data_local_da_rota(self):
        rota = _rota(7, date(2026, 9, 15), servicos=[_servico("#PS-1")])
        linhas = dc.linhas_das_rotas([rota], REMETENTE, {}, {})
        self.assertEqual(linhas[0]["data"], "2026-09-15")
        self.assertEqual(linhas[0]["data_rotulo"], "ter 15/09")

    def test_hora_de_saida_aparece_em_hora_local(self):
        # 06:00 em SP chega da Vuupt como 09:00 -- o cliente tem que ler 06:00.
        rota = _rota(7, HOJE, "06:00", servicos=[_servico("#PS-1", status="assigned")])
        linhas = dc.linhas_das_rotas([rota], REMETENTE, {}, {})
        self.assertIn("saída 06:00", linhas[0]["detalhe"])


class _JanelaBase(unittest.TestCase):
    """Monta a janela sem rede: as coletas da Vuupt/banco viram mocks."""

    def setUp(self):
        dc.limpar_caches()
        self.rotas = []
        self.sem_rota = []
        self.futuros = []
        patches = [
            mock.patch.object(dc, "carregar_config", return_value={"vuupt_api": {"token": "x"}}),
            mock.patch.object(dc, "VuuptClient", mock.Mock()),
            mock.patch.object(dc, "_catalogo_motoristas", return_value={}),
            mock.patch.object(dc, "nf_por_codigo", return_value={}),
            mock.patch.object(dc, "_coletar_atencao", return_value=[]),
            mock.patch.object(dc, "_historico", return_value=None),
            mock.patch.object(dc, "buscar_rotas_do_periodo", side_effect=self._buscar),
            mock.patch.object(dc, "_coletar_sem_rota", side_effect=lambda *a, **k: (self.sem_rota, self.futuros)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.buscas = []

    def _buscar(self, token, de, ate):
        self.buscas.append((de, ate))
        return [r for r in self.rotas if de <= dc.dia_local_da_rota(r) <= ate]

    def montar(self, dias=dc.DIAS_JANELA, forcar=False):
        return dc.montar_janela(REMETENTE, dias=dias, forcar=forcar, hoje=HOJE)


class JanelaTest(_JanelaBase):
    def setUp(self):
        super().setUp()
        self.rotas = [
            _rota(1, HOJE, servicos=[_servico("#PS-10", status="on_route", status_done=None)]),
            _rota(2, HOJE - timedelta(days=1), servicos=[_servico("#PS-9", completed_at="2026-09-20 14:00:00")]),
            _rota(3, HOJE - timedelta(days=13), servicos=[_servico("#PS-1")]),
            _rota(4, HOJE - timedelta(days=14), servicos=[_servico("#PS-0")]),   # fora da janela
        ]

    def test_traz_os_14_dias_e_para_no_limite(self):
        d = self.montar()
        codigos = [p["codigo"] for p in d["pedidos"]]
        self.assertIn("#PS-1", codigos)
        self.assertNotIn("#PS-0", codigos)
        self.assertEqual(self.buscas[0], (HOJE - timedelta(days=13), HOJE))

    def test_mais_recente_primeiro(self):
        datas = [p["data"] for p in self.montar()["pedidos"]]
        self.assertEqual(datas, sorted(datas, reverse=True))

    def test_kpis_sao_so_de_hoje(self):
        d = self.montar()
        self.assertEqual(d["kpis"]["total"], 1)         # só o #PS-10 é de hoje
        self.assertEqual(d["kpis"]["em_rota"], 1)
        self.assertEqual(d["kpis"]["entregues"], 0)     # os entregues são de dias passados

    def test_periodo_resume_a_janela_inteira(self):
        p = self.montar()["periodo"]
        self.assertEqual(p["dias"], 14)
        self.assertEqual(p["de"], (HOJE - timedelta(days=13)).isoformat())
        self.assertEqual(p["ate"], HOJE.isoformat())
        self.assertEqual(p["total"], 3)
        self.assertEqual(p["entregues"], 2)

    def test_pedido_sem_rota_conta_como_de_hoje(self):
        self.sem_rota = [dc._linha(_servico("#PS-50", status="not_assigned"), "aguardando_saida", {})]
        d = self.montar()
        linha = next(p for p in d["pedidos"] if p["codigo"] == "#PS-50")
        self.assertEqual(linha["data"], HOJE.isoformat())
        self.assertEqual(d["kpis"]["aguardando_saida"], 1)

    def test_agendado_futuro_fica_fora_da_lista_corrida(self):
        amanha = HOJE + timedelta(days=1)
        self.futuros = [dc._linha(_servico("#PS-60", status="not_assigned"), "agendado", {},
                                  agendado_para=amanha.isoformat())]
        d = self.montar()
        self.assertNotIn("#PS-60", [p["codigo"] for p in d["pedidos"]])
        self.assertEqual([p["codigo"] for p in d["agendados_futuros"]], ["#PS-60"])

    def test_dias_diferentes_nao_se_misturam_no_cache(self):
        self.assertEqual(self.montar(dias=7)["periodo"]["dias"], 7)
        self.assertEqual(self.montar(dias=14)["periodo"]["dias"], 14)


class CacheRotasTest(_JanelaBase):
    def setUp(self):
        super().setUp()
        self.rotas = [_rota(1, HOJE, servicos=[_servico("#PS-10")]),
                      _rota(2, HOJE - timedelta(days=5), servicos=[_servico("#PS-5")])]

    def test_segunda_carga_dentro_dos_5_min_nao_rebusca_nada(self):
        self.montar()
        self.montar()
        self.assertEqual(len(self.buscas), 1)

    def test_passados_os_5_min_so_hoje_e_rebuscado(self):
        # Dia que já passou vale 1 h em cache: a carga automática de 5 em 5
        # min só vai à VUUPT buscar o dia corrente.
        self.montar()
        dc._envelhecer_caches(dc.CACHE_DIA_SEGUNDOS + 1)
        self.montar()
        self.assertEqual(self.buscas[-1], (HOJE, HOJE))

    def test_passada_1_h_a_janela_inteira_e_rebuscada(self):
        self.montar()
        dc._envelhecer_caches(dc.CACHE_ROTAS_PASSADO_SEGUNDOS + 1)
        self.montar()
        self.assertEqual(self.buscas[-1], (HOJE - timedelta(days=13), HOJE))

    def test_atualizar_agora_rebusca_a_janela_inteira(self):
        self.montar()
        self.montar(forcar=True)
        self.assertEqual(self.buscas[-1], (HOJE - timedelta(days=13), HOJE))


if __name__ == "__main__":
    unittest.main()
