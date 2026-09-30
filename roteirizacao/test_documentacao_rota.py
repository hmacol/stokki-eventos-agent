# -*- coding: utf-8 -*-
"""
test_documentacao_rota.py

Testes da preparação automática da documentação de rota (09/09):
fila em segundo plano com dedupe, lote com uma listagem por data,
etapa de documentos só pros pedidos sem NF (e só se a trava da Stokki
liberar), PDF no mesmo nome do job das 04h com limpeza do arquivo
antigo da rota, descarte no cancelamento e agendar() nunca levantando.

Nada aqui toca VUUPT, Stokki ou o banco real: tudo é substituído por
mock. Rodar (da raiz):
    py -3.11 -m unittest roteirizacao.test_documentacao_rota -v
"""
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(Path(__file__).parent))

import documentacao_rota as dr
import gerar_pdf_romaneios as gpr


DATA = date(2026, 9, 10)


def _rota(rota_id, nome, agent_id=None, servicos=None, start_at="2026-09-10T09:00:00Z"):
    return {"id": rota_id, "name": nome, "agent_id": agent_id, "start_at": start_at,
            "services": servicos or []}


def _servico(sid, code, sender_id=1):
    return {"id": sid, "code": code, "sender_id": sender_id, "title": f"Cliente {sid}"}


class _Motorista:
    def __init__(self, agent_id, nome):
        self.agent_id, self.nome = agent_id, nome


class _Catalogo:
    motoristas = [_Motorista(7, "João da Silva")]


class BaseMock(unittest.TestCase):
    """Substitui tudo que é externo: VUUPT, banco de documentos, catálogo
    de motoristas, montagem do PDF (só cria um arquivo vazio) e a trava
    da Stokki."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pasta = Path(self.tmp.name)
        self.rotas = [
            _rota(101, "Planejamento - 10/09/2026 - #1", agent_id=7,
                  servicos=[_servico(1, "#PS-1001"), _servico(2, "#PS-1002, PS-1003")]),
            _rota(102, "Planejamento - 10/09/2026 - #2",
                  servicos=[_servico(3, "#PS-2001", sender_id=12887364)]),  # Padrão Puro: sem NF
            _rota(103, "Planejamento - 10/09/2026 - #3", servicos=[]),
        ]
        self.docs = {"PS-1001": [{"tipo": "Nota Fiscal"}], "PS-1002": [{"tipo": "Boleto"}]}
        self.montados = []

        def montar(rota, servicos, docs, emb, fat, nome_motorista, data_alvo, caminho):
            caminho.parent.mkdir(parents=True, exist_ok=True)
            caminho.write_bytes(b"%PDF")
            self.montados.append((rota["id"], nome_motorista, caminho))
            return {"pedidos": len(servicos), "nfs": 0, "boletos": 0, "canhoteira": 0, "pendencias": []}

        self.busca_stokki = mock.Mock(return_value=True)
        patches = [
            mock.patch.object(gpr, "PASTA_ROMANEIOS", self.pasta),
            mock.patch.object(gpr, "montar_pdf_rota", side_effect=montar),
            mock.patch.object(gpr, "carregar_documentos_por_pedido",
                              side_effect=lambda codigos: ({k: v for k, v in self.docs.items() if k in codigos}, 0)),
            mock.patch.object(gpr, "carregar_embarcadores", return_value=({}, {})),
            mock.patch.object(dr, "_carregar_config", return_value={"vuupt_api": {"token": "t"}}),
            mock.patch.object(dr, "_buscar_documentos_stokki", self.busca_stokki),
            mock.patch("avisar_motoristas_rotas.buscar_rotas_do_dia",
                       side_effect=lambda token, d: list(self.rotas) if d == DATA else []),
            mock.patch("regras.preferencias_motoristas.CatalogoMotoristas.carregar", return_value=_Catalogo()),
        ]
        mocks = []
        for p in patches:
            mocks.append(p.start())
            self.addCleanup(p.stop)
        self.listagem = mocks[6]
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self._limpar_fila)

    def _limpar_fila(self):
        # roda ANTES de tmp.cleanup (addCleanup é LIFO): o worker precisa
        # terminar de escrever na pasta temporária antes de ela sumir.
        dr.aguardar(timeout=30)
        with dr._cond:
            dr._fila.clear()
            dr._na_fila.clear()


class TestPreparacaoSincrona(BaseMock):

    def test_gera_pdf_no_mesmo_nome_do_job_das_04h(self):
        caminho = dr.preparar(101, DATA)
        esperado = self.pasta / "2026-09-10" / "romaneio_2026-09-10_rota1_id101_JOAO_DA_SILVA.pdf"
        self.assertEqual(caminho, esperado)
        self.assertTrue(esperado.is_file())

    def test_aceita_data_como_texto(self):
        self.assertIsNotNone(dr.preparar(101, "2026-09-10"))

    def test_resolve_data_pelo_start_at_quando_nao_informada(self):
        with mock.patch("rotas_client.buscar_rota", return_value={"route": self.rotas[0]}) as br:
            caminho = dr.preparar(101, None)
        br.assert_called_once_with("t", 101)
        self.assertIn("2026-09-10", str(caminho))

    def test_rota_inexistente_ou_sem_paradas_nao_gera(self):
        self.assertIsNone(dr.preparar(999, DATA))
        self.assertIsNone(dr.preparar(103, DATA))
        self.assertEqual(self.montados, [])

    def test_apaga_pdf_antigo_da_mesma_rota_com_motorista_diferente(self):
        pasta = self.pasta / "2026-09-10"
        pasta.mkdir(parents=True)
        antigo = pasta / "romaneio_2026-09-10_rota1_id101_SEM_MOTORISTA.pdf"
        antigo.write_bytes(b"x")
        outro = pasta / "romaneio_2026-09-10_rota2_id1010_SEM_MOTORISTA.pdf"  # id diferente, fica
        outro.write_bytes(b"x")
        dr.preparar(101, DATA)
        self.assertFalse(antigo.exists())
        self.assertTrue(outro.exists())
        self.assertTrue((pasta / "romaneio_2026-09-10_rota1_id101_JOAO_DA_SILVA.pdf").exists())

    def test_busca_documentos_so_dos_pedidos_sem_nf(self):
        # PS-1001 já tem NF; PS-1002 só tem boleto; PS-1003 nada -> 2 faltantes
        dr.preparar(101, DATA)
        self.busca_stokki.assert_called_once_with({"PS-1002", "PS-1003"})

    def test_embarcador_sem_nf_nao_dispara_busca(self):
        dr.preparar(102, DATA)
        self.busca_stokki.assert_not_called()
        self.assertEqual(len(self.montados), 1)

    def test_sem_busca_quando_desligado(self):
        dr.preparar(101, DATA, buscar_documentos=False)
        self.busca_stokki.assert_not_called()
        self.assertEqual(len(self.montados), 1)

    def test_falha_no_pdf_de_uma_rota_nao_derruba_as_outras(self):
        def montar_falhando(rota, *a, **k):
            if rota["id"] == 101:
                raise RuntimeError("boom")
            caminho = a[-1]
            caminho.parent.mkdir(parents=True, exist_ok=True)
            caminho.write_bytes(b"%PDF")
            return {"pedidos": 1, "nfs": 0, "boletos": 0, "canhoteira": 0, "pendencias": []}
        with mock.patch.object(gpr, "montar_pdf_rota", side_effect=montar_falhando):
            resultados = dr._preparar_lote([{"rota_id": 101, "data_alvo": DATA},
                                            {"rota_id": 102, "data_alvo": DATA}],
                                           buscar_documentos=False)
        self.assertEqual(set(resultados), {102})


class TestLoteEFila(BaseMock):

    def test_lote_faz_uma_listagem_por_data_e_uma_busca_stokki(self):
        resultados = dr._preparar_lote([{"rota_id": 101, "data_alvo": DATA},
                                        {"rota_id": 102, "data_alvo": DATA}])
        self.assertEqual(set(resultados), {101, 102})
        self.assertEqual(self.listagem.call_count, 1)
        self.busca_stokki.assert_called_once()

    def test_agendar_dedupe_e_worker_processa(self):
        self.assertTrue(dr.agendar(101, DATA, motivo="teste"))
        self.assertFalse(dr.agendar(101, DATA, motivo="de novo"))  # já na fila
        self.assertTrue(dr.agendar(102, "2026-09-10"))
        self.assertTrue(dr.aguardar(timeout=30))
        self.assertEqual(dr.pendentes(), 0)
        self.assertEqual(sorted(r for r, _, _ in self.montados), [101, 102])

    def test_agendar_varias(self):
        self.assertEqual(dr.agendar_varias([101, 102, 101], DATA), 2)
        self.assertTrue(dr.aguardar(timeout=30))

    def test_agendar_nunca_levanta(self):
        with mock.patch.object(dr, "_ativo", side_effect=RuntimeError("config quebrado")):
            self.assertFalse(dr.agendar(101, DATA))
        self.assertFalse(dr.agendar(0, DATA))
        self.assertFalse(dr.agendar(None, DATA))

    def test_desligado_no_config(self):
        with mock.patch.object(dr, "_cfg", return_value={"ativo": False}):
            self.assertFalse(dr.agendar(101, DATA))
        self.assertEqual(dr.pendentes(), 0)

    def test_erro_no_lote_nao_mata_o_worker(self):
        with mock.patch.object(dr, "_preparar_lote", side_effect=RuntimeError("boom")):
            dr.agendar(101, DATA)
            self.assertTrue(dr.aguardar(timeout=30))
        dr.agendar(102, DATA)
        self.assertTrue(dr.aguardar(timeout=30))
        self.assertEqual([r for r, _, _ in self.montados], [102])


class TestDescartar(BaseMock):

    def test_descartar_remove_pdfs_da_rota_e_tira_da_fila(self):
        pasta = self.pasta / "2026-09-10"
        pasta.mkdir(parents=True)
        (pasta / "romaneio_2026-09-10_rota1_id101_JOAO.pdf").write_bytes(b"x")
        (pasta / "romaneio_2026-09-10_rota1_id101_SEM_MOTORISTA.pdf").write_bytes(b"x")
        (pasta / "romaneio_2026-09-10_rota2_id102_X.pdf").write_bytes(b"x")
        with dr._cond:
            item = {"rota_id": 101, "data_alvo": DATA, "motivo": ""}
            dr._fila.append(item)
            dr._na_fila[101] = item
        self.assertEqual(dr.descartar(101, "2026-09-10"), 2)
        self.assertEqual(sorted(p.name for p in pasta.glob("*.pdf")),
                         ["romaneio_2026-09-10_rota2_id102_X.pdf"])
        self.assertEqual(dr.pendentes(), 0)

    def test_descartar_sem_data_ou_pasta_nao_faz_nada(self):
        self.assertEqual(dr.descartar(101, None), 0)
        self.assertEqual(dr.descartar(101, DATA), 0)


class TestTravaStokki(unittest.TestCase):

    def test_pula_etapa_quando_stokki_ocupada(self):
        with mock.patch("stokki.sessao_uso.adquirir", return_value=False), \
             mock.patch("stokki.sessao_uso.em_uso", return_value="painel: Pipeline"), \
             mock.patch("stokki.sessao_uso.liberar") as liberar:
            self.assertFalse(dr._buscar_documentos_stokki({"PS-1"}))
        liberar.assert_not_called()

    def test_roda_e_libera_a_trava(self):
        fake_pdoc = mock.Mock()
        fake_pdoc.main.return_value = {"ENVIADO": 1}
        with mock.patch("stokki.sessao_uso.adquirir", return_value=True), \
             mock.patch("stokki.sessao_uso.liberar") as liberar, \
             mock.patch.dict(sys.modules, {"processar_documentos": fake_pdoc}):
            self.assertTrue(dr._buscar_documentos_stokki({"PS-2", "PS-1"}))
        fake_pdoc.main.assert_called_once_with(modo_teste=False, pedidos_stokki=["PS-1", "PS-2"], notificar=False)
        liberar.assert_called_once_with(dr.DONO_TRAVA_STOKKI)

    def test_libera_a_trava_mesmo_com_erro(self):
        fake_pdoc = mock.Mock()
        fake_pdoc.main.side_effect = RuntimeError("playwright caiu")
        with mock.patch("stokki.sessao_uso.adquirir", return_value=True), \
             mock.patch("stokki.sessao_uso.liberar") as liberar, \
             mock.patch.dict(sys.modules, {"processar_documentos": fake_pdoc}):
            self.assertFalse(dr._buscar_documentos_stokki({"PS-1"}))
        liberar.assert_called_once_with(dr.DONO_TRAVA_STOKKI)


class TestDataDaRota(unittest.TestCase):

    def test_formatos_de_start_at(self):
        self.assertEqual(dr._data_da_rota({"start_at": "2026-09-10T09:00:00Z"}), date(2026, 9, 10))
        self.assertEqual(dr._data_da_rota({"start_at": "2026-09-10 06:00:00"}), date(2026, 9, 10))
        self.assertIsNone(dr._data_da_rota({"start_at": None}))
        self.assertIsNone(dr._data_da_rota({"start_at": "amanhã"}))


if __name__ == "__main__":
    unittest.main()
