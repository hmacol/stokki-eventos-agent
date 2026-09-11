# -*- coding: utf-8 -*-
"""
test_incrementar_rascunhos.py

Testes de planejamento_rotas.incrementar_rascunhos_com_selecionados --
botão "Incrementar" da barra de seleção do pool (Hugo, 10/09): põe cada
pedido selecionado no rascunho (status RASCUNHO) mais próximo que o
comporte, nunca em rota já enviada à VUUPT, sem teto de pedidos por
rota.

Cobre: escolha pela distância do centroide (raio de 20 km), rota
ENVIADA ignorada, erro sem rascunho editável, teto de caixas, carga
seco x frio pelo conteúdo da rota, motorista do rascunho (viagem/zona),
pedido já em rascunho contado como indisponível, e reordenação (2-opt)
só dos rascunhos afetados.

Nenhum teste toca no SQLite nem na rede: rascunhos_rota, catálogo de
motoristas, tipos de carga e classificações geográficas são mockados.
Rodar (da raiz):
    python -m unittest painel_agentes.test_incrementar_rascunhos -v
"""
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(Path(__file__).parent))

import planejamento_rotas as pr
import rascunhos_rota

DATA = date(2026, 9, 11)
SECO, FRIO = 1000, 2000  # sender_ids
ZONA_NORTE, ZONA_SUL = (-23.48, -46.62), (-23.65, -46.70)  # ~20 km entre si


def _parada(sid, lat, lng, caixas=1, sender=SECO, endereco=None, codigo=None):
    return {"service_id": sid, "codigo": codigo or f"#PS-{sid}", "titulo": "", "endereco": endereco or f"Rua {sid}, São Paulo - SP",
            "latitude": lat, "longitude": lng, "sender_id": sender, "remetente_nome": "X", "destinatario_nome": "",
            "nivel_dificuldade": 1, "volume_caixas": caixas,
            "horario_atendimento_inicio": "00:00", "horario_atendimento_fim": "23:59",
            "janela_inicio": None, "janela_fim": None, "janela_fonte": None}


def _rascunho(rid, nome, paradas, status="RASCUNHO", agent_id=None, tipo_veiculo=None):
    return {"id": rid, "nome": nome, "status": status, "agent_id": agent_id, "tipo_veiculo": tipo_veiculo,
            "particao": "Seco", "paradas": paradas}


class _Motorista:
    def __init__(self, agent_id, aceita_viagens=True, zonas=("NORTE", "SUL")):
        self.agent_id = agent_id
        self.aceita_viagens = aceita_viagens
        self.zonas_preferidas = set(zonas)


class IncrementarRascunhosTestCase(unittest.TestCase):

    def setUp(self):
        self.rascunhos = []
        self.motoristas = []
        patches = [
            mock.patch.object(pr, "_carregar_config", return_value={"google_maps": {"api_key": ""}, "motoristas": {}}),
            mock.patch.object(rascunhos_rota, "listar_rascunhos_do_dia", side_effect=lambda d: self.rascunhos),
            mock.patch.object(rascunhos_rota, "adicionar_parada"),
            mock.patch.object(rascunhos_rota, "otimizar_sequencia"),
            mock.patch.object(pr, "carregar_tipos_carga_por_sender", return_value={SECO: "Seco", FRIO: "Refrigerado"}),
            mock.patch.object(pr, "macro_regiao_do_servico", return_value="GRANDE_SP"),
            mock.patch.object(pr, "classificar_rota_viagem", return_value=False),
            mock.patch.object(pr, "classificar_zona", side_effect=lambda s, k=None: (
                None if s["latitude"] is None else ("NORTE" if float(s["latitude"]) > -23.55 else "SUL"))),
            mock.patch.object(pr, "obter_coordenadas", return_value=None),
            mock.patch("geocodificacao.geocodificar", return_value=(-23.50, -46.65)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.adicionar = rascunhos_rota.adicionar_parada
        self.otimizar = rascunhos_rota.otimizar_sequencia
        catalogo = mock.Mock()
        catalogo.motoristas = self.motoristas
        mock.patch.object(pr.CatalogoMotoristas, "carregar", return_value=catalogo).start()
        self.addCleanup(mock.patch.stopall)

    # -- escolha da rota -------------------------------------------------

    def test_vai_pro_rascunho_mais_proximo(self):
        self.rascunhos = [
            _rascunho(1, "#1 Norte", [_parada(11, *ZONA_NORTE), _parada(12, -23.49, -46.63)]),
            _rascunho(2, "#2 Sul", [_parada(21, *ZONA_SUL)]),
        ]
        pedido = _parada(99, -23.47, -46.61)
        r = pr.incrementar_rascunhos_com_selecionados(DATA, [pedido])
        self.assertEqual([a["rascunho_id"] for a in r["alocados"]], [1])
        self.assertEqual(r["orfaos"], [])
        self.adicionar.assert_called_once_with(1, pedido, None)
        self.otimizar.assert_called_once_with(1)

    def test_fora_do_raio_vira_orfao(self):
        self.rascunhos = [_rascunho(1, "#1", [_parada(11, *ZONA_NORTE)])]
        pedido = _parada(99, -23.90, -47.10)  # ~65 km
        r = pr.incrementar_rascunhos_com_selecionados(DATA, [pedido])
        self.assertEqual(r["alocados"], [])
        self.assertEqual(r["orfaos"], ["#PS-99"])
        self.adicionar.assert_not_called()
        self.otimizar.assert_not_called()

    def test_sem_teto_de_pedidos(self):
        muitas = [_parada(100 + i, -23.48 + i * 0.001, -46.62) for i in range(30)]
        self.rascunhos = [_rascunho(1, "#1", muitas)]
        r = pr.incrementar_rascunhos_com_selecionados(DATA, [_parada(99, -23.47, -46.61)])
        self.assertEqual(len(r["alocados"]), 1)

    # -- rotas que nunca são candidatas ----------------------------------

    def test_rota_enviada_nao_e_candidata(self):
        self.rascunhos = [
            _rascunho(1, "#1 enviada", [_parada(11, -23.47, -46.61)], status="ENVIADO"),
            _rascunho(2, "#2 rascunho", [_parada(21, *ZONA_SUL)]),
        ]
        r = pr.incrementar_rascunhos_com_selecionados(DATA, [_parada(99, -23.47, -46.61)])
        # a enviada é a mais próxima, mas o pedido está a >20 km da #2: órfão
        self.assertEqual(r["alocados"], [])
        self.assertEqual(r["orfaos"], ["#PS-99"])

    def test_sem_rascunho_editavel_da_erro(self):
        self.rascunhos = [_rascunho(1, "#1", [_parada(11, *ZONA_NORTE)], status="ENVIADO")]
        with self.assertRaises(ValueError) as ctx:
            pr.incrementar_rascunhos_com_selecionados(DATA, [_parada(99, *ZONA_NORTE)])
        self.assertIn("Nenhuma rota em rascunho", str(ctx.exception))

    def test_pedido_ja_em_rascunho_conta_como_indisponivel(self):
        self.rascunhos = [_rascunho(1, "#1", [_parada(11, *ZONA_NORTE)])]
        with self.assertRaises(ValueError):
            pr.incrementar_rascunhos_com_selecionados(DATA, [_parada(11, *ZONA_NORTE)])
        r = pr.incrementar_rascunhos_com_selecionados(DATA, [_parada(11, *ZONA_NORTE), _parada(99, *ZONA_NORTE)])
        self.assertEqual(r["pedidos_indisponiveis"], 1)
        self.assertEqual(len(r["alocados"]), 1)

    # -- travas -----------------------------------------------------------

    def test_teto_de_caixas(self):
        self.rascunhos = [_rascunho(1, "#1", [_parada(11, *ZONA_NORTE, caixas=98)])]
        r = pr.incrementar_rascunhos_com_selecionados(
            DATA, [_parada(98, *ZONA_NORTE, caixas=2), _parada(99, *ZONA_NORTE, caixas=1)])
        self.assertEqual([a["codigo"] for a in r["alocados"]], ["#PS-98"])
        self.assertEqual(r["orfaos"], ["#PS-99"])

    def test_carga_fria_nao_entra_em_rota_seca(self):
        self.rascunhos = [
            _rascunho(1, "#1 seca", [_parada(11, *ZONA_NORTE, sender=SECO)]),
            _rascunho(2, "#2 fria", [_parada(21, -23.50, -46.66, sender=FRIO)]),
        ]
        # pedido frio bem mais perto da rota seca: vai pra fria mesmo assim
        r = pr.incrementar_rascunhos_com_selecionados(DATA, [_parada(99, -23.48, -46.62, sender=FRIO)])
        self.assertEqual([a["rascunho_id"] for a in r["alocados"]], [2])

    def test_rota_mista_aceita_qualquer_carga(self):
        self.rascunhos = [_rascunho(1, "#1 mista", [_parada(11, *ZONA_NORTE, sender=SECO),
                                                    _parada(12, *ZONA_NORTE, sender=FRIO)])]
        r = pr.incrementar_rascunhos_com_selecionados(DATA, [_parada(99, *ZONA_NORTE, sender=FRIO)])
        self.assertEqual(len(r["alocados"]), 1)

    def test_zona_do_motorista_do_rascunho(self):
        self.motoristas.append(_Motorista(500, zonas=("SUL",)))
        self.rascunhos = [_rascunho(1, "#1", [_parada(11, *ZONA_NORTE)], agent_id=500)]
        r = pr.incrementar_rascunhos_com_selecionados(DATA, [_parada(99, -23.47, -46.61)])  # NORTE
        self.assertEqual(r["orfaos"], ["#PS-99"])

    def test_rascunho_sem_motorista_nao_restringe(self):
        self.rascunhos = [_rascunho(1, "#1", [_parada(11, *ZONA_NORTE)], agent_id=None)]
        with mock.patch.object(pr, "classificar_rota_viagem", return_value=True):
            r = pr.incrementar_rascunhos_com_selecionados(DATA, [_parada(99, -23.47, -46.61)])
        self.assertEqual(len(r["alocados"]), 1)

    def test_viagem_exige_motorista_que_aceita(self):
        self.motoristas.append(_Motorista(500, aceita_viagens=False))
        self.rascunhos = [_rascunho(1, "#1", [_parada(11, *ZONA_NORTE)], agent_id=500)]
        with mock.patch.object(pr, "classificar_rota_viagem", return_value=True):
            r = pr.incrementar_rascunhos_com_selecionados(DATA, [_parada(99, -23.47, -46.61)])
        self.assertEqual(r["orfaos"], ["#PS-99"])

    def test_sem_coordenada_vai_pro_rascunho_com_menos_pedidos(self):
        self.rascunhos = [
            _rascunho(1, "#1", [_parada(11, *ZONA_NORTE), _parada(12, *ZONA_NORTE)]),
            _rascunho(2, "#2", [_parada(21, *ZONA_SUL)]),
        ]
        r = pr.incrementar_rascunhos_com_selecionados(DATA, [_parada(99, None, None)])
        self.assertEqual([a["rascunho_id"] for a in r["alocados"]], [2])


if __name__ == "__main__":
    unittest.main()
