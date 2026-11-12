# -*- coding: utf-8 -*-
"""
test_tabela_pedidos.py

Testes da tabela de todos os pedidos (nucleo/tabela_pedidos.py, Hugo
09/11). SQLite temporário, nada de Vuupt nem do dados.db real.
    py -3.11 -m unittest nucleo.test_tabela_pedidos -v
"""
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))

from nucleo import banco, tabela_pedidos as tp  # noqa: E402

HOJE = date.today()


def _dia(delta: int) -> str:
    return (HOJE + timedelta(days=delta)).isoformat()


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._patch = mock.patch.object(banco, "DB_PATH", Path(self._tmp.name) / "teste.db")
        self._patch.start()
        self.conn = banco.conectar()

    def tearDown(self):
        self.conn.close()
        self._patch.stop()
        self._tmp.cleanup()

    def _pedido(self, codigo, titulo=None, status="ENTREGUE", sender_id=10, remetente="FRUTA FINA",
                destinatario="MERCADO CENTRAL", cnpj="69016970000181", caixas=5, agendamento=None,
                criado_provedor=None, fluxo="ENTREGA", zona="CENTRO", reentrega_de=None):
        self.conn.execute("""
            INSERT INTO nucleo_pedidos (codigo, titulo, status, sender_id, remetente_nome, destinatario_nome,
                                        destinatario_codigo, endereco, caixas, agendamento_inicio,
                                        criado_em_provedor, fluxo, zona, reentrega_de_codigo)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'RUA DAS FLORES, 100', ?, ?, ?, ?, ?, ?)
        """, (codigo, titulo or f"#{codigo} - NF1 / FF / {destinatario}", status, sender_id, remetente,
              destinatario, cnpj, caixas, agendamento, criado_provedor or f"{_dia(0)} 08:00:00", fluxo, zona, reentrega_de))
        self.conn.commit()

    def _rota(self, data_rota, nome="Rota 1", agent_id=501, motorista="JOAO DA SILVA"):
        cur = self.conn.execute("""
            INSERT INTO nucleo_rotas (data_rota, nome, agent_id, motorista_nome, status)
            VALUES (?, ?, ?, ?, 'CONCLUIDA')""", (data_rota, nome, agent_id, motorista))
        self.conn.commit()
        return cur.lastrowid

    def _parada(self, rota_id, codigo, ordem=1, completed_at=None, motivo=None, situacao="ENTREGUE"):
        self.conn.execute("""
            INSERT INTO nucleo_paradas (rota_id, ordem, codigo, situacao, completed_at, motivo_texto)
            VALUES (?, ?, ?, ?, ?, ?)""", (rota_id, ordem, codigo, situacao, completed_at, motivo))
        self.conn.commit()

    def _codigos(self, filtros=None, **kw):
        return [l["codigo"] for l in tp.listar(filtros or {}, conn=self.conn, **kw)["linhas"]]


class TestListar(_Base):
    def test_padrao_e_ultimos_7_dias_pela_data_da_rota(self):
        self._pedido("PS-1")
        self._parada(self._rota(_dia(-3)), "PS-1")
        self._pedido("PS-2")
        self._parada(self._rota(_dia(-10)), "PS-2")
        self.assertEqual(self._codigos(), ["PS-1"])
        self.assertEqual(tp.listar({}, conn=self.conn)["periodo"], {"de": _dia(-6), "ate": _dia(0)})

    def test_sem_rota_usa_agendamento_e_depois_criacao(self):
        self._pedido("PS-1", agendamento=f"{_dia(-20)} 07:00:00")
        self._pedido("PS-2", criado_provedor=f"{_dia(-1)} 10:00:00")
        self.assertEqual(self._codigos(), ["PS-2"])
        self.assertEqual(self._codigos({"de": _dia(-21), "ate": _dia(-19)}), ["PS-1"])

    def test_rota_e_motorista_vem_da_ultima_parada(self):
        self._pedido("PS-1")
        self._parada(self._rota(_dia(-2), nome="Rota velha", motorista="ANA"), "PS-1")
        rota_nova = self._rota(_dia(-1), nome="Rota nova", motorista="BRUNO")
        self._parada(rota_nova, "PS-1", ordem=4, completed_at=f"{_dia(-1)} 14:30:00")
        linhas = tp.listar({}, conn=self.conn)["linhas"]
        self.assertEqual(len(linhas), 1)
        self.assertEqual((linhas[0]["rota"], linhas[0]["motorista"], linhas[0]["ordem"], linhas[0]["rota_id"]),
                         ("Rota nova", "BRUNO", 4, rota_nova))

    def test_referencia_sai_do_titulo(self):
        self._pedido("PS-1", titulo="#PS-1 - 153403 / DOURADO / CASA DO QUEIJO")
        self._pedido("PS-2", titulo="[RETIRADA] #PS-2 - REF9 / FF / X / via TRANSP")
        self._pedido("PS-3", titulo="#PS-3 / LATICINIOS DOURADO - INDUSTRIA / X")
        ref = {l["codigo"]: l["referencia"] for l in tp.listar({}, conn=self.conn)["linhas"]}
        self.assertEqual(ref, {"PS-1": "153403", "PS-2": "REF9", "PS-3": None})

    def test_filtros_status_fluxo_embarcador_motorista_zona(self):
        self._pedido("PS-1", status="ENTREGUE", sender_id=10, zona="CENTRO")
        self._pedido("PS-2", status="INSUCESSO", sender_id=20, zona="NORTE")
        self._pedido("PS-3", fluxo=None, titulo="[RETIRADA] #PS-3 - R / FF / X")
        self._parada(self._rota(_dia(-1), agent_id=77), "PS-2")
        self.assertEqual(self._codigos({"status": "insucesso"}), ["PS-2"])
        self.assertEqual(self._codigos({"sender_id": "20"}), ["PS-2"])
        self.assertEqual(self._codigos({"agent_id": 77}), ["PS-2"])
        self.assertEqual(self._codigos({"zona": "NORTE"}), ["PS-2"])
        self.assertEqual(self._codigos({"fluxo": "RETIRADA"}), ["PS-3"])

    def test_filtro_canhoto_igual_canhotos_pendentes(self):
        """Hugo, 11/11: com canhoto = foto no checklist da Vuupt, canhoto/assinatura
        do app ou canhoto subido pelo painel; sem canhoto = ENTREGUE sem nenhum
        deles (checklist sem informacao fica fora, como em /canhotos-pendentes)."""
        from nucleo import canhotos_manuais
        canhotos_manuais.garantir_tabela(self.conn)
        rota = self._rota(_dia(-1))
        for codigo, status, qtd in (("PS-1", "ENTREGUE", 2), ("PS-2", "ENTREGUE", 0), ("PS-3", "ENTREGUE", 0),
                                    ("PS-4", "ENTREGUE", 0), ("PS-5", "ENTREGUE", None), ("PS-6", "ABERTO", 0),
                                    ("PS-7", "ENTREGUE", 0)):
            self._pedido(codigo, status=status)
            self.conn.execute("UPDATE nucleo_pedidos SET qtd_checklists = ? WHERE codigo = ?", (qtd, codigo))
            self._parada(rota, codigo)
        paradas = {c: i for c, i in self.conn.execute("SELECT codigo, id FROM nucleo_paradas")}
        self.conn.execute("INSERT INTO nucleo_comprovantes (parada_id, tipo) VALUES (?, 'ASSINATURA')", (paradas["PS-2"],))
        self.conn.execute("INSERT INTO nucleo_comprovantes (parada_id, tipo) VALUES (?, 'NF_DEVOLUCAO')", (paradas["PS-7"],))
        self.conn.execute("INSERT INTO canhotos_manuais (codigo, caminho, enviado_em) VALUES ('PS-3', 'x.pdf', '2026-11-11')")
        self.conn.commit()
        self.assertEqual(sorted(self._codigos({"canhoto": "com"})), ["PS-1", "PS-2", "PS-3"])
        self.assertEqual(sorted(self._codigos({"canhoto": "sem"})), ["PS-4", "PS-7"])
        self.assertEqual(len(self._codigos({"canhoto": "qualquer"})), 7)
        # a coluna Canhoto usa o mesmo critério
        self.assertEqual(sorted(self._codigos({"colunas": {"canhoto": "sim"}})), ["PS-1", "PS-2", "PS-3"])
        # as abas contam com o filtro de canhoto aplicado
        self.assertEqual(tp.contar_por_status({"canhoto": "sem"}, conn=self.conn)["total"], 2)
        # as filas do computador (Hugo, 11/11) ignoram status e canhoto escolhidos:
        # cada fila mostra quanto tem no periodo, nao dentro da fila aberta
        filas = tp.contar_filas({"canhoto": "sem", "status": "ABERTO"}, conn=self.conn)
        self.assertEqual((filas["total"], filas["ENTREGUE"], filas["ABERTO"], filas["sem_canhoto"]), (7, 6, 1, 2))
        self.assertEqual(tp.contar_filas({"sender_id": "999"}, conn=self.conn)["sem_canhoto"], 0)

    def test_filtro_canhoto_cria_a_tabela_de_canhotos_manuais_se_faltar(self):
        self._pedido("PS-1")
        self.assertEqual(self._codigos({"canhoto": "sem"}), [])

    def test_busca_livre_aceita_cerquilha_e_cnpj_formatado(self):
        self._pedido("PS-43565")
        self._pedido("PS-2", cnpj="11222333000144", destinatario="PADARIA")
        self.assertEqual(self._codigos({"q": "#PS-43565"}), ["PS-43565"])
        self.assertEqual(self._codigos({"q": "11.222.333/0001-44"}), ["PS-2"])
        self.assertEqual(self._codigos({"q": "padaria"}), ["PS-2"])

    def test_filtro_por_coluna_inclusive_data_em_formato_br(self):
        self._pedido("PS-1", destinatario="MERCADO CENTRAL")
        self._pedido("PS-2", destinatario="PADARIA")
        self._parada(self._rota(_dia(-1)), "PS-2", completed_at=f"{_dia(-1)} 09:15:00")
        self.assertEqual(self._codigos({"colunas": {"destinatario": "merc"}}), ["PS-1"])
        ontem = HOJE - timedelta(days=1)
        self.assertEqual(self._codigos({"colunas": {"conclusao": ontem.strftime("%d/%m")}}), ["PS-2"])
        self.assertEqual(self._codigos({"colunas": {"canhoto": "sim"}}), [])
        self.assertEqual(self._codigos({"colunas": {"canhoto": "não"}}), ["PS-1", "PS-2"])
        self.assertEqual(self._codigos({"colunas": {"coluna_que_nao_existe": "x"}}), ["PS-1", "PS-2"])

    def test_ordenacao_com_vazio_no_fim_e_chave_desconhecida_cai_na_data(self):
        self._pedido("PS-1", caixas=3)
        self._pedido("PS-2", caixas=None)
        self._pedido("PS-3", caixas=9)
        self.assertEqual(self._codigos(ordem="caixas", direcao="asc"), ["PS-1", "PS-3", "PS-2"])
        self.assertEqual(self._codigos(ordem="caixas", direcao="desc"), ["PS-3", "PS-1", "PS-2"])
        self.assertEqual(self._codigos(ordem="; DROP TABLE x"), ["PS-3", "PS-2", "PS-1"])

    def test_paginacao_e_totais(self):
        for i in range(1, 6):
            self._pedido(f"PS-{i}", caixas=2)
        r = tp.listar({}, pagina=3, por_pagina=2, ordem="codigo", direcao="asc", conn=self.conn)
        self.assertEqual((r["total"], r["total_caixas"], r["paginas"], r["pagina"]), (5, 10, 3, 3))
        self.assertEqual([l["codigo"] for l in r["linhas"]], ["PS-5"])
        self.assertEqual(tp.listar({}, pagina=99, por_pagina=2, conn=self.conn)["pagina"], 3)

    def test_periodo_por_outra_data(self):
        self._pedido("PS-1", criado_provedor=f"{_dia(-30)} 10:00:00", agendamento=f"{_dia(-1)} 07:00:00")
        self.assertEqual(self._codigos(), ["PS-1"])
        self.assertEqual(self._codigos({"data_tipo": "criacao"}), [])
        self.assertEqual(self._codigos({"data_tipo": "criacao", "de": _dia(-31), "ate": _dia(-29)}), ["PS-1"])

    def test_reentrega_vira_uma_linha_com_a_tentativa_mais_recente(self):
        self._pedido("PS-1", status="INSUCESSO")
        self._parada(self._rota(_dia(-2)), "PS-1", situacao="INSUCESSO")
        self._pedido("PS-1-R1", status="EM_ROTA", reentrega_de="PS-1")
        self._parada(self._rota(_dia(-1), nome="Rota R1"), "PS-1-R1")
        linhas = tp.listar({}, conn=self.conn)["linhas"]
        self.assertEqual([(l["codigo"], l["codigo_servico"], l["status"]) for l in linhas],
                         [("PS-1", "PS-1-R1", "EM_ROTA")])

    def test_entregue_vence_mesmo_mais_antigo(self):
        # Regra de 10/11 (revisão final): entregue vence tentativa FECHADA mais nova e tentativa
        # viva mais antiga que ele; só reentrega viva DEPOIS da entrega passa na frente (teste abaixo).
        self._pedido("PS-1", status="ENTREGUE"); self._parada(self._rota(_dia(-3)), "PS-1")
        self._pedido("PS-1-R1", status="INSUCESSO", reentrega_de="PS-1")
        self._parada(self._rota(_dia(-1)), "PS-1-R1", situacao="INSUCESSO")
        self.assertEqual([l["codigo_servico"] for l in tp.listar({}, conn=self.conn)["linhas"]], ["PS-1"])
        self.conn.execute("DELETE FROM nucleo_pedidos")
        self._pedido("PS-1", status="ABERTO", criado_provedor=f"{_dia(-1)} 09:00:00")
        self._pedido("PS-1-R1", status="ENTREGUE"); self._parada(self._rota(_dia(-3)), "PS-1-R1")
        self.assertEqual([l["codigo_servico"] for l in tp.listar({}, conn=self.conn)["linhas"]], ["PS-1-R1"])

    def test_reentrega_viva_depois_da_entrega_vira_a_linha(self):
        self._pedido("PS-1", status="ENTREGUE"); self._parada(self._rota(_dia(-3)), "PS-1")
        self._pedido("PS-1-R1", status="ABERTO", reentrega_de="PS-1", criado_provedor=f"{_dia(-1)} 09:00:00")
        self.assertEqual([l["codigo_servico"] for l in tp.listar({}, conn=self.conn)["linhas"]], ["PS-1-R1"])

    def test_reentrega_cancelada_depois_da_entrega_nao_esconde_a_entrega(self):
        self._pedido("PS-1", status="ENTREGUE"); self._parada(self._rota(_dia(-3)), "PS-1")
        self._pedido("PS-1-R1", status="CANCELADO", reentrega_de="PS-1", criado_provedor=f"{_dia(-1)} 09:00:00")
        self.assertEqual([l["codigo_servico"] for l in tp.listar({}, conn=self.conn)["linhas"]], ["PS-1"])

    def test_mesmo_dia_reentrega_aberta_vence_insucesso(self):
        self._pedido("PS-1", status="INSUCESSO", criado_provedor=f"{_dia(0)} 07:00:00")
        self._pedido("PS-1-R1", status="ABERTO", criado_provedor=f"{_dia(0)} 15:00:00")
        self.assertEqual([l["codigo_servico"] for l in tp.listar({}, conn=self.conn)["linhas"]], ["PS-1-R1"])

    def test_empresas_diferentes_com_mesmo_codigo_nao_se_misturam(self):
        self._pedido("PS-1", sender_id=10); self._pedido("PS-1-R1", sender_id=20)
        self.assertEqual(len(tp.listar({}, conn=self.conn)["linhas"]), 2)

    def test_busca_por_codigo_da_reentrega_acha_o_grupo(self):
        self._pedido("PS-1", status="INSUCESSO"); self._pedido("PS-1-R1", status="ABERTO")
        self.assertEqual(self._codigos({"q": "PS-1-R1"}), ["PS-1"])

    def test_agrupar_so_o_periodo_nao_muda_o_resultado(self):
        """12/11 (velocidade): só os pedidos com tentativa no período entram
        no agrupamento. O pedido cuja linha principal é de fora não aparece
        por causa de tentativa antiga dentro do período, e o que aparece
        continua trazendo as tentativas de fora dele."""
        # PS-1: insucesso antigo (fora), reentrega entregue ontem (dentro) -> aparece, 2 tentativas
        self._pedido("PS-1", status="INSUCESSO")
        self._parada(self._rota(_dia(-30)), "PS-1", situacao="INSUCESSO", motivo="AUSENTE")
        self._pedido("PS-1-R1", reentrega_de="PS-1")
        self._parada(self._rota(_dia(-1)), "PS-1-R1")
        # PS-2: entregue há 30 dias (linha principal, fora); reentrega cancelada ontem (dentro) -> não aparece
        self._pedido("PS-2")
        self._parada(self._rota(_dia(-30)), "PS-2")
        self._pedido("PS-2-R1", status="CANCELADO", reentrega_de="PS-2", agendamento=f"{_dia(-1)} 08:00:00")
        r = tp.listar({}, conn=self.conn)
        self.assertEqual([(l["codigo"], l["codigo_servico"], len(l["historico"])) for l in r["linhas"]],
                         [("PS-1", "PS-1-R1", 2)])
        self.assertEqual((r["total"], r["total_caixas"]), (1, 5))
        self.assertEqual(tp.contar_filas({}, conn=self.conn)["total"], 1)
        self.assertEqual(self._codigos({"q": "PS-1"}), ["PS-1"])   # busca pelo código da tentativa de fora
        antigo = {"de": _dia(-31), "ate": _dia(-29)}
        self.assertEqual([(l["codigo"], l["status"]) for l in tp.listar(antigo, conn=self.conn)["linhas"]],
                         [("PS-2", "ENTREGUE")])
        self.assertEqual(tp.exportar_csv(antigo, ["codigo"], conn=self.conn).count("PS-2;"), 1)
        self.assertEqual(tp.listar({"q": "nada-disso"}, pagina=4, conn=self.conn)["total"], 0)

    def test_total_e_caixas_contam_pedidos(self):
        self._pedido("PS-1", status="INSUCESSO", caixas=4); self._pedido("PS-1-R1", status="ABERTO", caixas=4)
        r = tp.listar({}, conn=self.conn)
        self.assertEqual((r["total"], r["total_caixas"]), (1, 4))

    def test_contagem_por_status_conta_pedidos_e_ignora_o_filtro_de_status(self):
        # Abas do celular (11/11): uma por status, todas com os mesmos filtros.
        self._pedido("PS-1", status="INSUCESSO"); self._pedido("PS-1-R1", status="ABERTO")   # 1 pedido, ABERTO
        self._pedido("PS-2", status="ENTREGUE")
        self._pedido("PS-3", status="ENTREGUE", sender_id=20, remetente="OUTRO")
        self._pedido("PS-4", status="CANCELADO", criado_provedor=f"{_dia(-30)} 08:00:00")   # fora do período
        c = tp.contar_por_status({"status": "ENTREGUE"}, conn=self.conn)
        self.assertEqual(c, {"ABERTO": 1, "EM_ROTA": 0, "ENTREGUE": 2, "INSUCESSO": 0, "CANCELADO": 0, "total": 3})
        self.assertEqual(tp.contar_por_status({"sender_id": "20"}, conn=self.conn)["total"], 1)

    def test_tentativas_do_grupo(self):
        self._pedido("PS-1", status="INSUCESSO")
        self._parada(self._rota(_dia(-2), nome="A"), "PS-1", situacao="INSUCESSO", motivo="Ausente")
        self._parada(self._rota(_dia(-2), nome="B"), "PS-1", situacao="CANCELADA")   # remanejo: não conta
        self._pedido("PS-1-R1", status="ABERTO")
        linha = tp.listar({}, conn=self.conn)["linhas"][0]
        t = linha["historico"]
        self.assertIsInstance(linha["tentativas"], (int, type(None)))   # coluna numerica intacta
        self.assertEqual([(x["codigo"], x["situacao"], x["motivo"]) for x in t],
                         [("PS-1", "INSUCESSO", "Ausente"), ("PS-1-R1", "ABERTO", None)])


class TestOpcoesFormatoCsv(_Base):
    def test_opcoes(self):
        self._pedido("PS-1", sender_id=10, remetente="FRUTA FINA", zona="CENTRO")
        self._pedido("PS-2", sender_id=20, remetente="DOURADO", zona="NORTE")
        self._rota(_dia(0), agent_id=5, motorista="ZECA")
        o = tp.opcoes(conn=self.conn)
        self.assertEqual([e["nome"] for e in o["embarcadores"]], ["DOURADO", "FRUTA FINA"])
        self.assertEqual(o["motoristas"], [{"agent_id": 5, "nome": "ZECA"}])
        self.assertEqual(o["zonas"], ["CENTRO", "NORTE"])

    def test_formatar(self):
        self.assertEqual(tp.formatar("data", "2026-11-09"), "09/11/2026")
        self.assertEqual(tp.formatar("conclusao", "2026-11-09 14:30:01"), "09/11/2026 14:30")
        self.assertEqual(tp.formatar("status", "EM_ROTA"), "Em rota")
        self.assertEqual(tp.formatar("caixas", None), "")

    def test_csv_traz_tudo_do_filtro_nas_colunas_pedidas(self):
        for i in range(1, 4):
            self._pedido(f"PS-{i}")
        texto = tp.exportar_csv({}, ["codigo", "status", "inexistente"], ordem="codigo", direcao="asc",
                                conn=self.conn)
        self.assertTrue(texto.startswith("﻿"))
        linhas = texto.lstrip("﻿").splitlines()
        self.assertEqual(linhas, ["Código;Status;Histórico de tentativas", "PS-1;Entregue;", "PS-2;Entregue;",
                                  "PS-3;Entregue;"])
        cabecalho = tp.exportar_csv({}, conn=self.conn).lstrip("﻿").splitlines()[0]
        self.assertEqual(cabecalho.count(";"), len(tp.COLUNAS))

    def test_csv_tem_historico_de_tentativas(self):
        self._pedido("PS-1", status="INSUCESSO")
        self._parada(self._rota(_dia(-1)), "PS-1", situacao="INSUCESSO", motivo="Ausente")
        self._pedido("PS-1-R1", status="ENTREGUE")
        self._parada(self._rota(_dia(0)), "PS-1-R1", situacao="ENTREGUE")
        linhas = tp.exportar_csv({}, ["codigo"], conn=self.conn).lstrip("﻿").splitlines()
        ontem, hoje = (HOJE - timedelta(days=1)).strftime("%d/%m"), HOJE.strftime("%d/%m")
        self.assertEqual(linhas, ["Código;Histórico de tentativas",
                                  f'PS-1;"{ontem} Insucesso (Ausente); {hoje} Entregue"'])

    def test_csv_neutraliza_formula_em_texto(self):
        # revisão de segurança 09/11: texto livre começando com = vira fórmula no Excel
        self._pedido("PS-1", destinatario='=HYPERLINK("http://x","clique")', caixas=3)
        texto = tp.exportar_csv({}, ["destinatario", "caixas"], conn=self.conn)
        linha = texto.lstrip("﻿").splitlines()[1]
        self.assertTrue(linha.startswith("\"'=HYPERLINK("), linha)
        self.assertTrue(linha.endswith(";3;"), linha)


if __name__ == "__main__":
    unittest.main()
