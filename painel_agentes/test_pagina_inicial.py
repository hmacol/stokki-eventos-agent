# -*- coding: utf-8 -*-
"""
test_pagina_inicial.py

Montagem dos dados da pagina inicial do painel (/inicio), por nivel de
acesso. As tres fontes (contadores do menu, snapshot da Torre e execucoes
dos agentes) entram por parametro, entao nada aqui toca VUUPT, Stokki ou
o banco.

Rodar (da raiz):
    py -3.11 -m unittest painel_agentes.test_pagina_inicial -v
"""
import sys
import unittest
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import pagina_inicial  # noqa: E402


def _url(rota, **kwargs):
    if kwargs:
        return f"/painel/{rota}/{kwargs['execucao_id']}"
    return f"/painel/{rota}"


NIVEIS = {
    "torre": ("total", "operador", "leitura", "atendimento"),
    "pedidos_parados": ("total", "operador", "leitura", "atendimento"),
    "pedagios": ("total", "operador", "leitura"),
    "canhotos": ("total", "operador", "leitura"),
    "atendimento": ("total", "operador", "atendimento"),
}


class TestPendenciasPorNivel(unittest.TestCase):

    def _chaves(self, nivel, contadores=None):
        cartoes = pagina_inicial.montar_pendencias(nivel, contadores or {}, NIVEIS, _url)
        return [c["chave"] for c in cartoes]

    def test_total_ve_os_cinco_cartoes(self):
        self.assertEqual(set(self._chaves("total")),
                         {"torre", "pedidos_parados", "atendimento", "pedagios", "canhotos"})

    def test_leitura_nao_ve_atendimento(self):
        self.assertNotIn("atendimento", self._chaves("leitura"))
        self.assertIn("pedagios", self._chaves("leitura"))

    def test_atendimento_nao_ve_pedagios_nem_canhotos(self):
        chaves = self._chaves("atendimento")
        self.assertEqual(set(chaves), {"atendimento", "torre", "pedidos_parados"})

    def test_expedicao_nao_ve_nada(self):
        self.assertEqual(self._chaves("expedicao"), [])

    def test_cartao_traz_rotulo_url_e_numeros(self):
        cartoes = pagina_inicial.montar_pendencias(
            "total", {"torre": {"qtd": 3, "criticas": 1}}, NIVEIS, _url)
        torre = next(c for c in cartoes if c["chave"] == "torre")
        self.assertEqual(torre["rotulo"], "Torre de Controle")
        self.assertEqual(torre["url"], "/painel/torre")
        self.assertEqual((torre["qtd"], torre["criticas"]), (3, 1))

    def test_contador_sem_leitura_vira_qtd_nula(self):
        cartoes = pagina_inicial.montar_pendencias("total", {}, NIVEIS, _url)
        torre = next(c for c in cartoes if c["chave"] == "torre")
        self.assertIsNone(torre["qtd"])
        self.assertEqual(torre["criticas"], 0)


class TestOrdemDosCartoes(unittest.TestCase):

    def test_criticas_depois_pendentes_depois_zerados(self):
        contadores = {
            "torre": {"qtd": 2, "criticas": 0},
            "pedidos_parados": {"qtd": 0, "criticas": 0},
            "atendimento": {"qtd": 4, "criticas": 0},
            "pedagios": {"qtd": 1, "criticas": 1},
            "canhotos": {"qtd": 0, "criticas": 0},
        }
        chaves = [c["chave"] for c in pagina_inicial.montar_pendencias("total", contadores, NIVEIS, _url)]
        self.assertEqual(chaves[0], "pedagios")
        self.assertEqual(set(chaves[1:3]), {"torre", "atendimento"})
        self.assertEqual(set(chaves[3:]), {"pedidos_parados", "canhotos"})

    def test_sem_leitura_fica_entre_pendentes_e_zerados(self):
        contadores = {"torre": {"qtd": 0, "criticas": 0}, "atendimento": {"qtd": 1, "criticas": 0}}
        chaves = [c["chave"] for c in pagina_inicial.montar_pendencias("total", contadores, NIVEIS, _url)]
        self.assertEqual(chaves[0], "atendimento")
        self.assertEqual(chaves[-1], "torre")

    def test_nivel_atendimento_desempata_pelo_atendimento(self):
        contadores = {"torre": {"qtd": 2, "criticas": 0}, "atendimento": {"qtd": 2, "criticas": 0},
                      "pedidos_parados": {"qtd": 2, "criticas": 0}}
        chaves = [c["chave"] for c in pagina_inicial.montar_pendencias("atendimento", contadores, NIVEIS, _url)]
        self.assertEqual(chaves[0], "atendimento")

    def test_nivel_total_desempata_pela_torre(self):
        contadores = {"torre": {"qtd": 2, "criticas": 0}, "atendimento": {"qtd": 2, "criticas": 0}}
        chaves = [c["chave"] for c in pagina_inicial.montar_pendencias("total", contadores, NIVEIS, _url)]
        self.assertEqual(chaves[0], "torre")


class TestOperacaoDoDia(unittest.TestCase):

    def test_sem_snapshot_vira_none(self):
        self.assertIsNone(pagina_inicial.montar_operacao(None))

    def test_snapshot_vira_rotas_e_pedidos(self):
        snap = {
            "gerado_em": "14:32:10",
            "rotas_resumo": {"concluidas": 4, "em_andamento": 6, "nao_iniciadas": 1, "atrasadas": 2},
            "pedidos": {"total": 118, "entregues": 71, "insucessos": 3, "sem_rota": 5},
        }
        op = pagina_inicial.montar_operacao(snap)
        self.assertEqual(op["gerado_em"], "14:32")
        self.assertEqual(op["rotas"]["atrasadas"], 2)
        self.assertEqual(op["pedidos"]["sem_rota"], 5)


class TestRotinas(unittest.TestCase):
    AGORA = datetime(2026, 9, 30, 10, 0, 0)

    def _exec(self, id_, status, iniciado, finalizado=None, nome="Executar Tudo"):
        return {"id": id_, "agente_id": "x", "agente_nome": nome, "status": status,
                "iniciado_em": iniciado, "finalizado_em": finalizado, "modo_teste": 0}

    def test_rodando_agora_aparece_com_desde_quando(self):
        execs = [self._exec(9, "RODANDO", "2026-09-30 09:58:00")]
        r = pagina_inicial.montar_rotinas(execs, _url, agora=self.AGORA)
        self.assertEqual(r["rodando"], [{"nome": "Executar Tudo", "iniciado_em": "09:58"}])
        self.assertEqual(r["falhas"], [])

    def test_so_falhas_das_ultimas_24h(self):
        execs = [
            self._exec(3, "ERRO", "2026-09-30 08:00:00", "2026-09-30 08:05:00"),
            self._exec(2, "SUCESSO", "2026-09-30 07:00:00", "2026-09-30 07:05:00"),
            self._exec(1, "TIMEOUT", "2026-09-28 08:00:00", "2026-09-28 09:00:00"),
        ]
        r = pagina_inicial.montar_rotinas(execs, _url, agora=self.AGORA)
        self.assertEqual([f["status"] for f in r["falhas"]], ["ERRO"])
        self.assertEqual(r["falhas"][0]["url"], "/painel/execucao/3")
        self.assertEqual(r["falhas"][0]["quando"], "30/09 08:05")

    def test_no_maximo_cinco_falhas(self):
        execs = [self._exec(i, "INTERROMPIDO", "2026-09-30 09:00:00", "2026-09-30 09:01:00") for i in range(9, 0, -1)]
        r = pagina_inicial.montar_rotinas(execs, _url, agora=self.AGORA)
        self.assertEqual(len(r["falhas"]), 5)

    def test_na_fila_nao_e_falha_nem_rodando(self):
        execs = [self._exec(1, "NA_FILA", "2026-09-30 09:59:00")]
        r = pagina_inicial.montar_rotinas(execs, _url, agora=self.AGORA)
        self.assertEqual(r, {"rodando": [], "falhas": []})


class TestMontarDados(unittest.TestCase):

    def _dados(self, nivel):
        return pagina_inicial.montar_dados(
            nivel, _url,
            contadores=lambda n: {"torre": {"qtd": 1, "criticas": 0}},
            niveis_por_contador=NIVEIS,
            snapshot=lambda d: None,
            execucoes=lambda: [],
        )

    def test_virada_de_mes_so_na_ultima_semana(self):
        kw = dict(contadores=lambda n: {}, niveis_por_contador={}, snapshot=lambda d: None, execucoes=lambda: [])
        meio = pagina_inicial.montar_dados("total", lambda r: "/" + r, hoje=date(2026, 10, 7), **kw)
        self.assertIsNone(meio["virada_mes"])
        fim = pagina_inicial.montar_dados("leitura", lambda r: "/" + r, hoje=date(2026, 10, 28), **kw)
        self.assertEqual(fim["virada_mes"]["titulo_mes"], "Novembro de 2026")
        self.assertEqual(fim["virada_mes"]["dias_para_virar"], 4)
        self.assertTrue(fim["virada_mes"]["url"].endswith("?mes=2026-11"))

    def test_rotinas_so_para_total(self):
        self.assertIn("rotinas", self._dados("total"))
        for nivel in ("operador", "leitura", "atendimento"):
            self.assertNotIn("rotinas", self._dados(nivel), nivel)

    def test_operacao_nula_sem_snapshot(self):
        self.assertIsNone(self._dados("operador")["operacao"])

    def test_falha_nas_execucoes_nao_derruba_a_pagina(self):
        def explode():
            raise RuntimeError("banco travado")
        dados = pagina_inicial.montar_dados(
            "total", _url, contadores=lambda n: {}, niveis_por_contador=NIVEIS,
            snapshot=lambda d: None, execucoes=explode)
        self.assertEqual(dados["rotinas"], {"rodando": [], "falhas": []})
        self.assertEqual(len(dados["pendencias"]), 5)


if __name__ == "__main__":
    unittest.main()
