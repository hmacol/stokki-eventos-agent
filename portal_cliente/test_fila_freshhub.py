# -*- coding: utf-8 -*-
"""
test_fila_freshhub.py -- worker da fila `stokki` do sistema novo (plano 1.3 do freshhub, Task 7).
O portal novo guarda o XML e enfileira; este worker cria na Stokki pelo MESMO wizard do portal antigo e
devolve o PS. Sem rede nem Stokki: cliente, wizard e conferencia de area entram como funcoes falsas.

    py -3.11 -m unittest portal_cliente.test_fila_freshhub -v
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

_RAIZ = Path(__file__).resolve().parent.parent
for _p in (_RAIZ, _RAIZ / "portal_cliente"):
    sys.path.insert(0, str(_p))

import fila_freshhub as ff  # noqa: E402
from nucleo.freshhub_client import ErroFreshhub  # noqa: E402

EMB = "12345678000195"
CH = {n: "3526" + str(n) * 40 for n in (1, 2, 3)}


def _item(id_="r1", modo="xml", chave="3526" + "1" * 40, **dados):
    return {"id": id_, "codigo": f"FH-0000{id_[-1]}", "destino": "stokki", "tentativas": 0,
            "dados": {"modo": modo, "objeto": f"freshhub/notas/{chave}.xml", "regra_xml": None, "liberado": False,
                      "codigo_interno": f"FH-0000{id_[-1]}", "fluxo": "ENTREGA", **dados}}


def _nfe(numero="1234", emitente=EMB, chave="3526" + "1" * 40):
    return {"chave_nfe": chave, "numero_nf": numero, "emitente_cnpj": emitente, "destinatario_nome": "D"}


class Falso:
    """Cliente da API nova: pendentes fixos, arquivo por id, resultados gravados."""

    def __init__(self, itens, arquivos=None, falha_resultado=False):
        self.itens = itens
        self.arquivos = arquivos or {}
        self.devolvidos = []
        self.falha_resultado = falha_resultado

    def pendentes(self, destino, limite=50):
        assert destino == "stokki"
        return list(self.itens)

    def arquivo(self, id_):
        if id_ not in self.arquivos:
            raise ErroFreshhub("GET arquivo: HTTP 404", status=404)
        return self.arquivos[id_]

    def resultado(self, id_, ok, resultado=None, erro=None):
        if self.falha_resultado:
            raise ErroFreshhub("POST resultado: HTTP 503", status=503)
        self.devolvidos.append((id_, ok, resultado, erro))


def _atender(cliente, nfes, wizard=None, area=None, simular=False, agendar=None):
    return ff.atender(
        cliente, simular=simular, ler_nfe=lambda conteudo: dict(nfes[conteudo]),
        classificar_area=area or (lambda nfe: None),
        criar_na_stokki=wizard or (lambda cnpj, arquivos: ({}, {})),
        registrar_agendamento=agendar,
    )


class TestFilaStokki(unittest.TestCase):
    def test_xml_criado_devolve_o_ps(self):
        c = Falso([_item("r1")], {"r1": b"A"})
        chamadas = []

        def wizard(cnpj, arquivos):
            chamadas.append((cnpj, [(nome, regra) for nome, _conteudo, regra in arquivos]))
            return {CH[1]: {"criado": True, "erro": ""}}, {"1234": "PS-77001"}

        resumo = _atender(c, {b"A": _nfe()}, wizard)
        self.assertEqual(chamadas, [(EMB, [("3526" + "1" * 40 + ".xml", None)])])
        self.assertEqual(c.devolvidos, [("r1", True, {"ps": "PS-77001", "numero_nf": "1234"}, None)])
        self.assertEqual(resumo["criados"], 1)

    def test_agrupa_por_emitente_e_um_wizard_por_embarcador(self):
        c = Falso([_item("r1"), _item("r2", chave="3526" + "2" * 40), _item("r3", chave="3526" + "3" * 40)],
                  {"r1": b"A", "r2": b"B", "r3": b"C"})
        nfes = {b"A": _nfe("1", chave="3526" + "1" * 40), b"B": _nfe("2", "22222222000122", "3526" + "2" * 40),
                b"C": _nfe("3", chave="3526" + "3" * 40)}
        por_cnpj = {}

        def wizard(cnpj, arquivos):
            por_cnpj[cnpj] = len(arquivos)
            return ({Path(n).stem: {"criado": True, "erro": ""} for n, _c, _r in arquivos},
                    {"1": "PS-1", "2": "PS-2", "3": "PS-3"})

        _atender(c, nfes, wizard)
        self.assertEqual(por_cnpj, {EMB: 2, "22222222000122": 1})
        self.assertEqual(sorted((i, r["ps"]) for i, _ok, r, _e in c.devolvidos),
                         [("r1", "PS-1"), ("r2", "PS-2"), ("r3", "PS-3")])

    def test_bloqueio_de_area_devolve_o_motivo_sem_chamar_o_wizard(self):
        c = Falso([_item("r1")], {"r1": b"A"})
        wizard = mock.Mock()
        _atender(c, {b"A": _nfe()}, wizard, area=lambda nfe: "fora_sp")
        wizard.assert_not_called()
        self.assertEqual(c.devolvidos, [("r1", False, {"bloqueio": "fora_sp"}, "fora da área atendida (fora_sp)")])

    def test_liberado_pula_a_conferencia_de_area(self):
        c = Falso([_item("r1", liberado=True)], {"r1": b"A"})
        area = mock.Mock(return_value="fora_sp")
        _atender(c, {b"A": _nfe()}, lambda cnpj, arq: ({CH[1]: {"criado": True, "erro": ""}}, {"1234": "PS-9"}),
                 area=area)
        area.assert_not_called()
        self.assertTrue(c.devolvidos[0][1])

    def test_dedicado_vai_no_resultado_pra_o_novo_marcar(self):
        c = Falso([_item("r1", liberado=True, dedicado={"valor": 120.5})], {"r1": b"A"})
        _atender(c, {b"A": _nfe()}, lambda cnpj, arq: ({CH[1]: {"criado": True, "erro": ""}}, {"1234": "PS-9"}))
        self.assertEqual(c.devolvidos[0][2], {"ps": "PS-9", "numero_nf": "1234", "dedicado": {"valor": 120.5}})

    def test_recusa_da_stokki_duplicado_e_criado_sem_ps_viram_erro_pra_equipe(self):
        c = Falso([_item("r1"), _item("r2", chave="3526" + "2" * 40), _item("r3", chave="3526" + "3" * 40)],
                  {"r1": b"A", "r2": b"B", "r3": b"C"})
        nfes = {b"A": _nfe("1", chave="3526" + "1" * 40), b"B": _nfe("2", chave="3526" + "2" * 40),
                b"C": _nfe("3", chave="3526" + "3" * 40)}

        def wizard(cnpj, arquivos):
            return ({CH[1]: {"criado": False, "erro": "Destinatário sem CEP"},
                     CH[2]: {"criado": False, "erro": "Chave da NFe já utilizada"},
                     CH[3]: {"criado": True, "erro": ""}}, {})

        _atender(c, nfes, wizard)
        por_id = {i: (ok, erro) for i, ok, _r, erro in c.devolvidos}
        self.assertEqual(por_id["r1"], (False, "Stokki recusou: Destinatário sem CEP"))
        self.assertFalse(por_id["r2"][0])
        self.assertIn("já existe na Stokki", por_id["r2"][1])
        self.assertFalse(por_id["r3"][0])
        self.assertIn("informe o PS", por_id["r3"][1])

    def test_planilha_e_retirada_ainda_nao_sao_automaticas(self):
        c = Falso([_item("r1", modo="planilha"), _item("r2", fluxo="RETIRADA")], {"r2": b"A"})
        wizard = mock.Mock()
        _atender(c, {b"A": _nfe()}, wizard)
        wizard.assert_not_called()
        self.assertEqual([(i, ok) for i, ok, _r, _e in c.devolvidos], [("r1", False), ("r2", False)])
        self.assertTrue(all("lance na Stokki" in e for _i, _ok, _r, e in c.devolvidos))

    def test_arquivo_sumido_ou_xml_ilegivel_vira_erro_e_o_lote_segue(self):
        c = Falso([_item("r1"), _item("r2", chave="3526" + "2" * 40), _item("r3", chave="3526" + "3" * 40)],
                  {"r2": b"RUIM", "r3": b"C"})

        def ler(conteudo):
            if conteudo == b"RUIM":
                raise ValueError("XML inválido")
            return _nfe("3", chave="3526" + "3" * 40)

        resumo = ff.atender(c, simular=False, ler_nfe=ler, classificar_area=lambda nfe: None,
                            criar_na_stokki=lambda cnpj, arq: ({CH[3]: {"criado": True, "erro": ""}}, {"3": "PS-3"}))
        por_id = {i: ok for i, ok, _r, _e in c.devolvidos}
        self.assertEqual(por_id, {"r1": False, "r2": False, "r3": True})
        self.assertEqual(resumo["erros"], 2)

    def test_falha_do_wizard_devolve_erro_pra_todo_o_grupo(self):
        c = Falso([_item("r1")], {"r1": b"A"})

        def wizard(cnpj, arquivos):
            raise RuntimeError("sessao caiu")

        _atender(c, {b"A": _nfe()}, wizard)
        self.assertEqual(c.devolvidos[0][:2], ("r1", False))
        self.assertIn("sessao caiu", c.devolvidos[0][3])

    def test_simular_devolve_ps_ficticio_sem_tocar_a_stokki(self):
        c = Falso([_item("r1"), _item("r2", chave="3526" + "2" * 40)], {"r1": b"A", "r2": b"B"})
        wizard = mock.Mock()
        _atender(c, {b"A": _nfe("1"), b"B": _nfe("2", chave="3526" + "2" * 40)}, wizard, simular=True)
        wizard.assert_not_called()
        self.assertEqual([r for _i, _ok, r, _e in c.devolvidos],
                         [{"ps": "PS-HML-1", "numero_nf": "1", "simulado": True},
                          {"ps": "PS-HML-2", "numero_nf": "2", "simulado": True}])

    def test_falha_ao_devolver_nao_derruba_o_lote(self):
        c = Falso([_item("r1")], {"r1": b"A"}, falha_resultado=True)
        resumo = _atender(c, {b"A": _nfe()}, lambda cnpj, arq: ({CH[1]: {"criado": True, "erro": ""}},
                                                                 {"1234": "PS-1"}))
        self.assertEqual(resumo["nao_devolvidos"], 1)

    def test_api_fora_ao_listar_e_lote_vazio(self):
        c = Falso([])
        self.assertEqual(_atender(c, {})["itens"], 0)
        c.pendentes = mock.Mock(side_effect=ErroFreshhub("HTTP 503", status=503))
        self.assertEqual(_atender(c, {})["itens"], 0)


class TestRevisao26(unittest.TestCase):
    """Revisao final de 26/10: agendamento chega ao legado; emitente tem que ser a empresa da replica."""

    def test_agendamento_e_registrado_com_o_ps(self):
        c = Falso([_item("r1", agendamento="2026-10-30", horario=["08:00", "12:00"], cnpj_empresa=EMB)],
                  {"r1": b"A"})
        agendados = []
        _atender(c, {b"A": {**_nfe(), "destinatario_doc": "98765432000110"}},
                 lambda cnpj, arq: ({CH[1]: {"criado": True, "erro": ""}}, {"1234": "PS-77"}),
                 agendar=agendados.append)
        self.assertEqual(agendados, [{"codigo_pedido": "PS-77", "agendamento_data": "2026-10-30",
                                      "agendamento_hora_inicio": "08:00", "agendamento_hora_fim": "12:00",
                                      "destinatario_doc": "98765432000110", "destinatario_nome": "D",
                                      "cnpj_embarcador": EMB, "numero_nf": "1234", "id": None}])
        self.assertEqual(c.devolvidos[0][2]["agendamento_registrado"], True)

    def test_emitente_diferente_da_empresa_da_replica_e_recusado_sem_wizard(self):
        c = Falso([_item("r1", cnpj_empresa="22222222000122")], {"r1": b"A"})
        wizard = mock.Mock()
        _atender(c, {b"A": _nfe()}, wizard)
        wizard.assert_not_called()
        self.assertEqual(c.devolvidos[0][:2], ("r1", False))
        self.assertIn("emitente", c.devolvidos[0][3])

    def test_agrupa_pela_empresa_da_replica(self):
        c = Falso([_item("r1", cnpj_empresa=EMB)], {"r1": b"A"})
        grupos = []
        _atender(c, {b"A": _nfe()}, lambda cnpj, arq: (grupos.append(cnpj) or {CH[1]: {"criado": True, "erro": ""}},
                                                        {"1234": "PS-1"}))
        self.assertEqual(grupos, [EMB])


class TestCross(unittest.TestCase):
    """Merge de 27/10: o portal antigo ganhou o fluxo Cross (item XD + troca de NF); o worker novo ainda nao
    sabe fazer isso, entao embarcador Cross vai pra equipe em vez de nascer pelo caminho de armazenagem."""

    def test_embarcador_cross_volta_pra_equipe_sem_wizard(self):
        c = Falso([_item("r1", cnpj_empresa=EMB)], {"r1": b"A"})
        wizard = mock.Mock()
        ff.atender(c, simular=False, ler_nfe=lambda conteudo: _nfe(), classificar_area=lambda nfe: None,
                   criar_na_stokki=wizard, recusar_embarcador=lambda cnpj: "embarcador usa o fluxo Cross")
        wizard.assert_not_called()
        self.assertEqual(c.devolvidos[0][:2], ("r1", False))
        self.assertIn("fluxo Cross", c.devolvidos[0][3])
        self.assertIn("informe o PS", c.devolvidos[0][3])


if __name__ == "__main__":
    unittest.main()
