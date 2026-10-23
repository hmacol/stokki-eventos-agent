# -*- coding: utf-8 -*-
"""
test_rota_tarde_tela.py

rascunhos_rota: a coluna `turno` ('TARDE' ou NULL) marca a rota da tarde
(janela tardia que nao cabe na manha, Hugo 23/10). O nome da rota nao muda.

Usa um SQLite temporario (DB_PATH trocado).
Rodar (de dentro de painel_agentes/):
    py -3.11 -m unittest test_rota_tarde_tela -v
"""
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import planejamento_rotas
import rascunhos_rota


def _rascunho(nome, turno=None):
    r = {"nome": nome, "start_location_base_id": 6950, "end_location_base_id": 6950,
         "start_at": "2026-10-23T16:15:00Z", "sublote": [{"id": 1, "code": "#PS-1"}]}
    if turno:
        r["turno"] = turno
    return r


class LoteGravaTurnoTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        for p in (mock.patch.object(rascunhos_rota, "DB_PATH", Path(self._tmp.name) / "dados.db"),
                  mock.patch("mapa_util.carregar_remetentes_por_sender_id", return_value={})):
            p.start()
            self.addCleanup(p.stop)

    def test_lote_grava_turno(self):
        rascunhos_rota.criar_lote_rascunhos(date(2026, 10, 23), [
            _rascunho("Planejamento - 23/10/2026 - #1"),
            _rascunho("Planejamento - 23/10/2026 - #2", turno="TARDE"),
        ])
        conn = rascunhos_rota._conectar()
        try:
            turnos = [r["turno"] for r in conn.execute("SELECT turno FROM rascunhos_rota ORDER BY id")]
        finally:
            conn.close()
        self.assertEqual(turnos, [None, "TARDE"])

    def test_roteirizar_continua_numeracao_com_tarde(self):
        rascunhos_rota.criar_lote_rascunhos(date(2026, 10, 23), [
            _rascunho("Planejamento - 23/10/2026 - #18", turno="TARDE")])
        conn = rascunhos_rota._conectar()
        try:
            self.assertEqual(rascunhos_rota._proximo_numero_ordem(conn, date(2026, 10, 23)), 19)
        finally:
            conn.close()


def _rascunho_tela(turno="TARDE", **extra):
    paradas = [{"codigo": "#PS-1", "nivel_dificuldade": 1, "volume_caixas": 5,
                "latitude": -23.55, "longitude": -46.63,
                "janela_inicio": "14:00", "janela_fim": "17:00"}]
    return {"nome": "Planejamento - 23/10/2026 - #18", "turno": turno,
            "start_at": "2026-10-23T16:15:00Z", "paradas": paradas, **extra}


class TelaTardeTest(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(planejamento_rotas, "_garantir_coords_base", return_value=(-23.5, -46.6))
        p.start()
        self.addCleanup(p.stop)

    def test_selo_tarde(self):
        # Selo proprio (neutro), nao badge de trava: nao conta em "Com aviso
        # ativo" do Confirmar e enviar nem no qtd_travas da Torre.
        self.assertEqual(planejamento_rotas._selo_tarde(_rascunho_tela()), "Tarde · sai 13:15")
        badges = planejamento_rotas._badges_trava(_rascunho_tela())
        self.assertFalse([b for b in badges if "Tarde" in b], badges)

    def test_sem_selo_tarde_na_rota_normal(self):
        self.assertIsNone(planejamento_rotas._selo_tarde(_rascunho_tela(turno=None)))
        badges = planejamento_rotas._badges_trava(_rascunho_tela(turno=None))
        self.assertFalse([b for b in badges if "Tarde" in b])

    def test_tarde_sem_badge_de_rota_fraca(self):
        r = _rascunho_tela(rota_fraca_motivo="nao coube")
        self.assertFalse([b for b in planejamento_rotas._badges_trava(r) if "rota fraca" in b])

    def test_simulacao_usa_saida_da_rota(self):
        paradas = _rascunho_tela()["paradas"]
        sim = planejamento_rotas._simular_rascunho(paradas, hora_saida=13.25)
        self.assertGreater(sim["chegadas"][0], 13.25)


def _parada_pool(sid, lat, lng, janela=None):
    ini, fim = janela or (None, None)
    return {"service_id": sid, "codigo": f"#PS-{sid}", "titulo": "", "endereco": f"Rua {sid}, Sao Paulo - SP",
            "latitude": lat, "longitude": lng, "sender_id": 1000, "remetente_nome": "X",
            "destinatario_nome": "", "nivel_dificuldade": 1, "volume_caixas": 1,
            "horario_atendimento_inicio": "00:00", "horario_atendimento_fim": "23:59",
            "janela_inicio": ini, "janela_fim": fim, "janela_fonte": "teste" if ini else None}


class IncrementarRascunhoTardeTest(unittest.TestCase):
    """Botao Incrementar: a janela do pedido no rascunho da tarde e julgada
    a partir da saida dele (start_at), nao das 06h."""

    def setUp(self):
        self.rascunhos = []
        pr = planejamento_rotas
        catalogo = mock.Mock()
        catalogo.motoristas = []
        for p in (
            mock.patch.object(pr, "_carregar_config", return_value={"google_maps": {"api_key": ""}, "motoristas": {}}),
            mock.patch.object(rascunhos_rota, "listar_rascunhos_do_dia", side_effect=lambda d: self.rascunhos),
            mock.patch.object(rascunhos_rota, "adicionar_parada"),
            mock.patch.object(rascunhos_rota, "otimizar_sequencia"),
            mock.patch.object(pr, "carregar_tipos_carga_por_sender", return_value={1000: "Seco"}),
            mock.patch.object(pr, "macro_regiao_do_servico", return_value="GRANDE_SP"),
            mock.patch.object(pr, "classificar_rota_viagem", return_value=False),
            mock.patch.object(pr, "classificar_zona", return_value="NORTE"),
            mock.patch.object(pr, "obter_coordenadas", return_value=None),
            mock.patch.object(pr.CatalogoMotoristas, "carregar", return_value=catalogo),
            mock.patch("geocodificacao.geocodificar", return_value=(-23.4930, -46.6640)),
        ):
            p.start()
            self.addCleanup(p.stop)

    def _rascunho(self, turno, start_at):
        return {"id": 1, "nome": "#18", "status": "RASCUNHO", "agent_id": None, "tipo_veiculo": None,
                "particao": "Seco", "turno": turno, "start_at": start_at,
                "paradas": [_parada_pool(11, -23.5870, -46.6440, ("15:00", "23:59"))]}

    def test_pedido_tardio_cabe_no_rascunho_da_tarde(self):
        self.rascunhos = [self._rascunho("TARDE", "2026-10-23T17:15:00Z")]
        pedido = _parada_pool(99, -23.5780, -46.6440, ("14:30", "18:00"))
        with mock.patch.object(planejamento_rotas, "janela_viavel",
                               wraps=planejamento_rotas.janela_viavel) as jv:
            r = planejamento_rotas.incrementar_rascunhos_com_selecionados(date(2026, 10, 23), [pedido])
        self.assertEqual(jv.call_args.kwargs.get("hora_saida"), 14.25)
        self.assertEqual([a["rascunho_id"] for a in r["alocados"]], [1])

    def test_rascunho_da_manha_segue_06h(self):
        self.rascunhos = [self._rascunho(None, "2026-10-23T09:00:00Z")]
        pedido = _parada_pool(99, -23.5780, -46.6440, ("14:30", "18:00"))
        with mock.patch.object(planejamento_rotas, "janela_viavel", return_value=True) as jv:
            planejamento_rotas.incrementar_rascunhos_com_selecionados(date(2026, 10, 23), [pedido])
        self.assertIsNone(jv.call_args.kwargs.get("hora_saida"))


class RascunhoTardeBancoTest(unittest.TestCase):
    """Otimizar sequencia e Duplicar de um rascunho da tarde (SQLite temporario)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        for p in (mock.patch.object(rascunhos_rota, "DB_PATH", Path(self._tmp.name) / "dados.db"),
                  mock.patch("mapa_util.carregar_remetentes_por_sender_id", return_value={}),
                  mock.patch.object(rascunhos_rota, "_gmaps_key", return_value=""),
                  mock.patch("geocodificacao.geocodificar", return_value=(-23.4930, -46.6640))):
            p.start()
            self.addCleanup(p.stop)

    def _criar(self, turno, start_at):
        sublote = [{"id": i, "code": f"#PS-{i}", "latitude": -23.55 - 0.01 * i, "longitude": -46.63,
                    "_janela_inicio": "14:00", "_janela_fim": "18:00"} for i in range(1, 4)]
        r = {"nome": "Planejamento - 23/10/2026 - #18", "start_location_base_id": 6950,
             "end_location_base_id": 6950, "start_at": start_at, "sublote": sublote}
        if turno:
            r["turno"] = turno
        rascunhos_rota.criar_lote_rascunhos(date(2026, 10, 23), [r])
        conn = rascunhos_rota._conectar()
        try:
            return conn.execute("SELECT id FROM rascunhos_rota").fetchone()["id"]
        finally:
            conn.close()

    def _otimizar_espiando(self, rid):
        import roteirizacao_dados
        import otimizacao_rotas
        with mock.patch.object(roteirizacao_dados, "ordenar_com_janelas",
                               side_effect=lambda s, *a, **k: list(s)) as ocj, \
                mock.patch.object(otimizacao_rotas, "ordenar_2opt", side_effect=lambda s, *a, **k: list(s)) as o2:
            rascunhos_rota.otimizar_sequencia(rid)
        return ocj, o2

    def test_otimizar_sequencia_tarde_parte_da_saida(self):
        ocj, o2 = self._otimizar_espiando(self._criar("TARDE", "2026-10-23T17:15:00Z"))
        self.assertEqual(ocj.call_args.kwargs.get("hora_saida"), 14.25)
        o2.assert_not_called()

    def test_otimizar_sequencia_manha_segue_2opt(self):
        ocj, o2 = self._otimizar_espiando(self._criar(None, "2026-10-23T09:00:00Z"))
        o2.assert_called_once()
        ocj.assert_not_called()

    def test_duplicar_copia_turno(self):
        rid = self._criar("TARDE", "2026-10-23T17:15:00Z")
        novo = rascunhos_rota.duplicar_rascunho(rid)
        self.assertEqual(rascunhos_rota.buscar_rascunho(novo)["turno"], "TARDE")


class TorreSemMotoristaTardeTest(unittest.TestCase):
    def test_rascunho_da_tarde_nao_conta_sem_motorista(self):
        import torre_controle
        rascunhos = [{"agent_id": None, "turno": "TARDE", "paradas": [{}]},
                     {"agent_id": None, "turno": None, "paradas": [{}]},
                     {"agent_id": 7, "turno": None, "paradas": [{}]}]
        with mock.patch.object(rascunhos_rota, "listar_rascunhos_do_dia", return_value=rascunhos), \
                mock.patch.object(torre_controle, "listar_rotas", return_value=[]), \
                mock.patch("planejamento_rotas._badges_trava", return_value=[]):
            resumo = torre_controle._resumo_amanha(date(2026, 10, 23), "token")
        self.assertEqual(resumo["sem_motorista"], 1)
        self.assertEqual(resumo["qtd_rotas"], 3)


if __name__ == "__main__":
    unittest.main()
