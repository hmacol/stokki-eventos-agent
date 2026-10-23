# -*- coding: utf-8 -*-
"""
Recebimento de retornos (Hugo, 23/10/2026): lista dos insucessos/parciais
que voltam ao galpao, registro da descarga, divergencia e alerta no grupo
"Alertas" do /atendimento.

    py -3.11 -m unittest test_retornos   (de dentro de painel_agentes/)
"""
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from nucleo import banco as nucleo_banco  # noqa: E402
import grupos_equipe as ge  # noqa: E402
import retornos  # noqa: E402

HOJE = date(2026, 10, 23)  # sexta


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "dados.db"
        nucleo_banco.conectar(self.db).close()
        self.conn = retornos.conectar(self.db)
        self.conn.execute("CREATE TABLE motivos_ocorrencia (id INTEGER PRIMARY KEY, vuupt_failed_reason_id INTEGER, "
                          "motivo_texto TEXT, categoria TEXT)")
        self.conn.execute("INSERT INTO motivos_ocorrencia VALUES (7, 5744, 'Não coletado', 'X')")
        self.conn.commit()
        retornos.definir_inicio(self.conn, date(2026, 1, 1))
        self._rotas = {}
        self._service = 1000

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def rota(self, motorista, dia=HOJE, cpf=None):
        chave = (motorista, dia)
        if chave not in self._rotas:
            cur = self.conn.execute(
                "INSERT INTO nucleo_rotas (data_rota, nome, motorista_nome, motorista_cpf) VALUES (?, ?, ?, ?)",
                (dia.isoformat(), f"Rota {motorista}", motorista, cpf or f"cpf-{motorista}"))
            self._rotas[chave] = cur.lastrowid
        return self._rotas[chave]

    def parada(self, motorista, codigo, *, dia=HOJE, situacao="INSUCESSO", caixas=5, motivo="Cliente ausente",
               failed_reason_id=8490, motivo_id=None, embarcador="Maria Dolores"):
        self._service += 1
        cur = self.conn.execute(
            "INSERT INTO nucleo_paradas (rota_id, ordem, service_id, codigo, destinatario_nome, remetente_nome, "
            "volume_caixas, situacao, motivo_id, motivo_texto, failed_reason_id, completed_at) "
            "VALUES (?, 1, ?, ?, 'Mercado X', ?, ?, ?, ?, ?, ?, ?)",
            (self.rota(motorista, dia), self._service, codigo, embarcador, caixas, situacao, motivo_id, motivo,
             failed_reason_id, f"{dia.isoformat()} 15:30:00"))
        self.conn.commit()
        return cur.lastrowid

    def receber(self, itens, fotos_carga=("carga.jpg",), assinatura="assinatura.png", recebedor="Carlos"):
        descarga_id = retornos.reservar_descarga(self.conn)
        return retornos.registrar_descarga(self.conn, descarga_id=descarga_id, itens=list(itens), recebedor=recebedor,
                                           usuario="galpao", fotos_carga=list(fotos_carga), assinatura=assinatura)

    @staticmethod
    def item(parada_id, caixas=5, avaria=False, fotos_avaria=(), motivo="Cliente ausente", observacao=""):
        return {"parada_id": parada_id, "caixas_recebidas": caixas, "avaria": avaria,
                "fotos_avaria": list(fotos_avaria), "motivo": motivo, "observacao": observacao}


class Lista(Base):
    def test_lista_insucesso_e_parcial_agrupa_por_motorista(self):
        self.parada("João", "PS-1", caixas=5)
        self.parada("João", "PS-2", caixas=3)
        self.parada("João", "PS-3", situacao="PARCIAL", caixas=8, motivo="Produto Avariado", failed_reason_id=None)
        self.parada("Maria", "PS-4")
        grupos = retornos.listar_pendentes(self.conn, HOJE)
        self.assertEqual(len(grupos), 2)
        joao = next(g for g in grupos if g["motorista_nome"] == "João")
        self.assertEqual(len(joao["pedidos"]), 3)
        self.assertEqual(joao["total_caixas"], 8)
        parcial = next(p for p in joao["pedidos"] if p["codigo"] == "PS-3")
        self.assertTrue(parcial["parcial"])
        self.assertIsNone(parcial["caixas_esperadas"])

    def test_lista_exclui_nao_coletado(self):
        self.parada("João", "PS-1", failed_reason_id=5744, motivo="Não coletado")
        self.parada("João", "PS-2", failed_reason_id=None, motivo_id=7, motivo="Não foi coletado")
        self.parada("João", "PS-3", failed_reason_id=None, motivo="  NÃO COLETADO ")
        self.assertEqual(retornos.listar_pendentes(self.conn, HOJE), [])

    def test_lista_ignora_entregue(self):
        self.parada("João", "PS-1", situacao="ENTREGUE")
        self.assertEqual(retornos.listar_pendentes(self.conn, HOJE), [])

    def test_lista_janela_7_dias(self):
        self.parada("João", "PS-1", dia=HOJE - timedelta(days=8))
        self.parada("João", "PS-2", dia=HOJE - timedelta(days=7))
        codigos = [p["codigo"] for g in retornos.listar_pendentes(self.conn, HOJE) for p in g["pedidos"]]
        self.assertEqual(codigos, ["PS-2"])

    def test_lista_tira_recebidos(self):
        p1 = self.parada("João", "PS-1")
        self.parada("João", "PS-2")
        self.receber([self.item(p1)])
        codigos = [p["codigo"] for g in retornos.listar_pendentes(self.conn, HOJE) for p in g["pedidos"]]
        self.assertEqual(codigos, ["PS-2"])

    def test_desde_ontem(self):
        self.parada("João", "PS-1", dia=HOJE - timedelta(days=1))
        self.parada("Maria", "PS-2")
        grupos = {g["motorista_nome"]: g for g in retornos.listar_pendentes(self.conn, HOJE)}
        self.assertTrue(grupos["João"]["desde_ontem"])
        self.assertFalse(grupos["Maria"]["desde_ontem"])


class Registro(Base):
    def test_registrar_sem_divergencia(self):
        p1 = self.parada("João", "PS-1")
        r = self.receber([self.item(p1)])
        self.assertEqual(r["divergentes"], [])
        self.assertEqual(r["parciais"], [])
        self.assertIsNone(retornos.texto_alerta_descarga(r, "Carlos"))

    def test_registrar_divergencias(self):
        p1 = self.parada("João", "PS-1", caixas=5)
        p2 = self.parada("João", "PS-2")
        p3 = self.parada("João", "PS-3")
        p4 = self.parada("João", "PS-4")
        r = self.receber([
            self.item(p1, caixas=3),
            self.item(p2, avaria=True, fotos_avaria=["av.jpg"]),
            self.item(p3, motivo="Local fechado"),
            self.item(p4, observacao="caixa molhada por fora"),
        ])
        self.assertEqual(sorted(d["codigo"] for d in r["divergentes"]), ["PS-1", "PS-2", "PS-3"])
        texto = retornos.texto_alerta_descarga(r, "Carlos")
        self.assertIn("PS-1", texto)
        self.assertIn("3 de 5", texto)
        self.assertIn("avaria", texto.lower())
        self.assertIn("Cliente ausente → Local fechado", texto)
        self.assertIn("João", texto)
        self.assertNotIn("PS-4", texto)

    def test_parcial_sempre_no_alerta_e_exige_caixas(self):
        p1 = self.parada("João", "PS-1", situacao="PARCIAL", motivo="Produto Avariado", failed_reason_id=None)
        with self.assertRaises(retornos.ErroRetorno):
            self.receber([self.item(p1, caixas=None, motivo="Produto Avariado")])
        r = self.receber([self.item(p1, caixas=2, motivo="Produto Avariado")])
        self.assertEqual(r["divergentes"], [])
        texto = retornos.texto_alerta_descarga(r, "Carlos")
        self.assertIn("PARCIAL: avisar o cliente e solicitar a entrada no estoque", texto)
        self.assertIn("PS-1", texto)

    def test_obrigatorios(self):
        p1 = self.parada("João", "PS-1")
        casos = [
            dict(itens=[self.item(p1)], fotos_carga=()),
            dict(itens=[self.item(p1)], assinatura=""),
            dict(itens=[self.item(p1)], recebedor="  "),
            dict(itens=[]),
            dict(itens=[self.item(p1, avaria=True)]),
        ]
        for caso in casos:
            with self.assertRaises(retornos.ErroRetorno, msg=repr(caso)):
                self.receber(**caso)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM retornos_recebidos").fetchone()[0], 0)

    def test_motivo_fora_da_lista_recusa(self):
        p1 = self.parada("João", "PS-1")
        with self.assertRaises(retornos.ErroRetorno):
            self.receber([self.item(p1, motivo="Inventado")])

    def test_receber_duas_vezes_mesma_parada_recusa(self):
        p1 = self.parada("João", "PS-1")
        self.receber([self.item(p1)])
        with self.assertRaises(retornos.ErroRetorno) as ctx:
            self.receber([self.item(p1)])
        self.assertIn("PS-1", str(ctx.exception))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM retornos_recebidos").fetchone()[0], 1)

    def test_registrar_usa_dados_do_nucleo(self):
        p1 = self.parada("João", "PS-1", caixas=5)
        item = self.item(p1, caixas=5)
        item["caixas_esperadas"] = 99
        item["motorista_nome"] = "Outro"
        r = self.receber([item])
        self.assertEqual(r["motorista_nome"], "João")
        linha = self.conn.execute("SELECT caixas_esperadas FROM retornos_recebidos").fetchone()
        self.assertEqual(linha[0], 5)

    def test_status_por_service_ids(self):
        p1 = self.parada("João", "PS-1", caixas=5)
        self.parada("João", "PS-2")
        sid1, sid2 = 1001, 1002
        self.receber([self.item(p1, caixas=4)])
        st = retornos.status_por_service_ids(self.conn, [sid1, sid2])
        self.assertEqual(set(st), {sid1})
        self.assertEqual(st[sid1]["recebedor"], "Carlos")
        self.assertTrue(st[sid1]["divergente"])
        self.assertTrue(st[sid1]["recebido_em"])

    def test_status_por_codigo_e_historico(self):
        p1 = self.parada("João", "PS-1")
        r = self.receber([self.item(p1)])
        self.assertEqual([x["codigo"] for x in retornos.status_por_codigo(self.conn, "#ps-1")], ["PS-1"])
        self.assertEqual([d["id"] for d in retornos.listar_descargas(self.conn)], [r["descarga_id"]])
        det = retornos.detalhar_descarga(self.conn, r["descarga_id"])
        self.assertEqual(det["recebedor"], "Carlos")
        self.assertEqual(det["fotos_carga"], ["carga.jpg"])
        self.assertEqual([i["codigo"] for i in det["itens"]], ["PS-1"])


class Alerta(Base):
    def test_postar_alerta_cria_grupo_uma_vez(self):
        cg = ge.conectar(self.db)
        retornos.postar_alerta(cg, "primeiro")
        retornos.postar_alerta(cg, "segundo")
        grupos = cg.execute("SELECT id, nome FROM equipe_grupos").fetchall()
        self.assertEqual([g["nome"] for g in grupos], ["Alertas"])
        autores = [m["autor"] for m in cg.execute("SELECT autor FROM equipe_grupos_mensagens")]
        self.assertEqual(autores, ["Recebimento de retornos"] * 2)
        cg.close()


class Revisao(Base):
    def test_inicio_corta_legado(self):
        # Primeiro uso grava o dia de inicio: o que caiu antes ja foi recebido no Lovable.
        self.parada("João", "PS-1", dia=HOJE - timedelta(days=2))
        self.parada("João", "PS-2", dia=HOJE)
        retornos.definir_inicio(self.conn, HOJE - timedelta(days=1))
        codigos = [p["codigo"] for g in retornos.listar_pendentes(self.conn, HOJE) for p in g["pedidos"]]
        self.assertEqual(codigos, ["PS-2"])
        self.assertEqual(retornos.pendentes_atrasados(self.conn, HOJE + timedelta(days=1))[0]["pedidos"][0]["codigo"],
                         "PS-2")

    def test_inicio_nasce_no_primeiro_uso(self):
        tmp = Path(self._tmp.name) / "novo.db"
        nucleo_banco.conectar(tmp).close()
        c = retornos.conectar(tmp)
        self.assertEqual(retornos.inicio(c), date.today().isoformat())
        c.close()

    def test_lista_exclui_lalamove(self):
        self.parada("LALAMOVE", "PS-1")
        self.conn.execute("UPDATE nucleo_rotas SET agent_id = 77")
        self.parada("João", "PS-2")
        self.conn.commit()
        nomes = [g["motorista_nome"] for g in retornos.listar_pendentes(self.conn, HOJE)]
        self.assertEqual(nomes, ["João"])
        self.conn.execute("UPDATE nucleo_rotas SET motorista_nome = 'Agente X' WHERE agent_id = 77")
        self.conn.commit()
        nomes = [g["motorista_nome"] for g in retornos.listar_pendentes(self.conn, HOJE, agent_ids_excluir={77})]
        self.assertEqual(nomes, ["João"])

    def _rota_vuupt_sem_nome(self, agent_id, codigo):
        """Rota da Vuupt cujo agente saiu da planilha BD_MOTORISTAS: o
        sincronizador grava motorista_nome e motorista_cpf nulos."""
        self.parada(f"tmp-{agent_id}", codigo)
        self.conn.execute("UPDATE nucleo_rotas SET agent_id = ?, motorista_nome = NULL, motorista_cpf = NULL "
                          "WHERE nome = ?", (agent_id, f"Rota tmp-{agent_id}"))
        self.conn.commit()

    def test_rota_vuupt_sem_nome_usa_cadastro_e_nao_junta_motoristas(self):
        self.conn.execute("CREATE TABLE nucleo_cadastros_vuupt (tipo TEXT, id INTEGER, nome TEXT, dados_json TEXT, "
                          "atualizado_em TEXT, PRIMARY KEY (tipo, id))")
        self.conn.execute("INSERT INTO nucleo_cadastros_vuupt VALUES ('usuario', 48907, 'Renato Forte', '{}', '')")
        self.conn.commit()
        self._rota_vuupt_sem_nome(48907, "PS-1")
        self._rota_vuupt_sem_nome(45594, "PS-2")
        grupos = {g["motorista_nome"]: [p["codigo"] for p in g["pedidos"]]
                  for g in retornos.listar_pendentes(self.conn, HOJE)}
        self.assertEqual(grupos, {"Renato Forte": ["PS-1"], "Agente 45594": ["PS-2"]})

    def test_descarga_nao_mistura_agentes_sem_nome(self):
        self._rota_vuupt_sem_nome(48907, "PS-1")
        self._rota_vuupt_sem_nome(45594, "PS-2")
        ids = [p["parada_id"] for g in retornos.listar_pendentes(self.conn, HOJE) for p in g["pedidos"]]
        with self.assertRaises(retornos.ErroRetorno):
            self.receber([self.item(i) for i in ids])

    def test_alerta_atraso_grande_vira_varias_mensagens_sem_perder_nada(self):
        ontem = HOJE - timedelta(days=1)
        for n in range(120):
            self.parada(f"Motorista {n:03d}", f"PS-{n}", dia=ontem, embarcador="Embarcador de nome bem comprido Ltda")
        msgs = retornos.mensagens_alerta_atraso(retornos.pendentes_atrasados(self.conn, HOJE))
        self.assertGreater(len(msgs), 1)
        self.assertTrue(all(len(m) <= ge.TEXTO_MAX for m in msgs))
        juntas = "\n".join(msgs)
        for n in range(120):
            self.assertIn(f"PS-{n} ·", juntas)


class Atraso(Base):
    def test_atraso_pega_so_dias_anteriores(self):
        self.parada("João", "PS-1", dia=HOJE)
        self.parada("João", "PS-2", dia=HOJE - timedelta(days=1))
        codigos = [p["codigo"] for g in retornos.pendentes_atrasados(self.conn, HOJE) for p in g["pedidos"]]
        self.assertEqual(codigos, ["PS-2"])

    def test_atraso_uma_vez_so(self):
        p = self.parada("João", "PS-1", dia=HOJE - timedelta(days=1))
        retornos.marcar_alertados(self.conn, [p])
        self.assertEqual(retornos.pendentes_atrasados(self.conn, HOJE), [])

    def test_atraso_ignora_recebido(self):
        p = self.parada("João", "PS-1", dia=HOJE - timedelta(days=1))
        self.receber([self.item(p)])
        self.assertEqual(retornos.pendentes_atrasados(self.conn, HOJE), [])

    def test_texto_atraso_agrupa_motorista(self):
        ontem = HOJE - timedelta(days=1)
        self.parada("Maria", "PS-1", dia=ontem, caixas=4)
        self.parada("João", "PS-2", dia=ontem, situacao="PARCIAL", failed_reason_id=None)
        texto = retornos.texto_alerta_atraso(retornos.pendentes_atrasados(self.conn, HOJE))
        self.assertTrue(texto.startswith("Retornos não recebidos até 07h"))
        self.assertLess(texto.index("João"), texto.index("Maria"))
        self.assertIn("PS-1 · Maria Dolores · 4 cx · Cliente ausente", texto)
        self.assertIn("PS-2 · Maria Dolores · parcial", texto)

    def test_atraso_fim_de_semana_e_feriado(self):
        import alertar_retornos_atrasados as job
        postados = []
        self.parada("João", "PS-1", dia=date(2026, 10, 23))  # sexta
        self.parada("João", "PS-2", dia=date(2026, 10, 30))  # sexta antes de Finados (seg 02/11)
        with mock.patch.object(retornos, "DB_PATH", self.db), mock.patch.object(ge, "DB_PATH", self.db), \
                mock.patch.object(retornos, "postar_alerta", lambda conn, texto: postados.append(texto)):
            self.assertEqual(job.main(["--data", "2026-10-24"]), 0)  # sabado
            self.assertEqual(job.main(["--data", "2026-10-25"]), 0)  # domingo
            self.assertEqual(postados, [])
            self.assertEqual(job.main(["--data", "2026-10-26", "--modo-teste"]), 0)  # segunda, teste
            self.assertEqual(postados, [])
            self.assertEqual(job.main(["--data", "2026-10-26"]), 0)  # segunda
            self.assertEqual(len(postados), 1)
            self.assertIn("PS-1", postados[0])
            self.assertNotIn("PS-2", postados[0])
            self.assertEqual(job.main(["--data", "2026-10-27"]), 0)  # nada novo
            self.assertEqual(len(postados), 1)
            self.assertEqual(job.main(["--data", "2026-11-02"]), 0)  # Finados
            self.assertEqual(len(postados), 1)
            self.assertEqual(job.main(["--data", "2026-11-03"]), 0)
            self.assertEqual(len(postados), 2)
            self.assertIn("PS-2", postados[1])


if __name__ == "__main__":
    unittest.main()
