# -*- coding: utf-8 -*-
"""
Tela /pedidos: todos os pedidos em tabela (Hugo, 09/11).

    py -3.11 -m unittest test_pedidos_tabela   (de dentro de painel_agentes/)
"""
import re
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from nucleo import banco  # noqa: E402

import importlib.util  # noqa: E402
_spec = importlib.util.spec_from_file_location("painel_agentes_app", _AQUI / "painel_agentes.py")
painel_agentes = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = painel_agentes  # o Flask acha a pasta templates/ pelo modulo registrado
_spec.loader.exec_module(painel_agentes)


class TestTelaPedidos(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        db = Path(self._tmp.name) / "dados.db"
        # acessos/sessões também no banco temporário: o real (VPS) tem contas e rodada de sessões
        for p in (mock.patch.object(banco, "DB_PATH", db),
                  mock.patch.object(painel_agentes.acesso_telas, "DB_PATH", db),
                  mock.patch.object(painel_agentes.contadores_menu, "contadores", return_value={})):
            p.start()
            self.addCleanup(p.stop)
        conn = banco.conectar()
        hoje = date.today().isoformat()
        for codigo, status, dest in (("PS-1", "ENTREGUE", "MERCADO CENTRAL"), ("PS-2", "INSUCESSO", "PADARIA")):
            conn.execute("""INSERT INTO nucleo_pedidos (codigo, titulo, status, destinatario_nome, remetente_nome,
                                                        sender_id, caixas, criado_em_provedor)
                            VALUES (?, ?, ?, ?, 'FRUTA FINA', 10, 4, ?)""",
                         (codigo, f"#{codigo} - NF9 / FF / {dest}", status, dest, f"{hoje} 08:00:00"))
        conn.commit()
        conn.close()
        painel_agentes.app.config["TESTING"] = True
        self.cliente = painel_agentes.app.test_client()

    def _logar(self, nivel):
        with self.cliente.session_transaction() as sess:
            sess["nivel_acesso"] = nivel
            sess["usuario"] = "teste"

    def test_lista_com_filtro_de_coluna_e_ordenacao_preservada(self):
        self._logar("leitura")
        r = self.cliente.get("/pedidos?f_destinatario=pada&ordem=caixas&direcao=asc")
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn("PS-2", html)
        self.assertNotIn(">PS-1<", html)
        self.assertIn("Destinatário: “pada”", html)
        # Link de página/ordem mantém o filtro de coluna
        self.assertIn("f_destinatario=pada", html)

    def test_csv_com_colunas_escolhidas(self):
        self._logar("atendimento")
        r = self.cliente.get("/pedidos/exportar.csv?colunas=codigo,referencia,status&ordem=codigo&direcao=asc")
        self.assertEqual(r.status_code, 200)
        self.assertIn("attachment", r.headers["Content-Disposition"])
        linhas = r.get_data(as_text=True).lstrip("﻿").splitlines()
        self.assertEqual(linhas, ["Código;Referência;Status;Histórico de tentativas",
                                  "PS-1;NF9;Entregue;", "PS-2;NF9;Insucesso;"])

    def test_selo_de_tentativas_abre_historico_do_pedido(self):
        conn = banco.conectar()
        hoje = date.today().isoformat()
        for codigo, status in (("PS-3", "INSUCESSO"), ("PS-3-R1", "ABERTO")):
            conn.execute("""INSERT INTO nucleo_pedidos (codigo, titulo, status, destinatario_nome, remetente_nome,
                                                        sender_id, caixas, criado_em_provedor)
                            VALUES (?, ?, ?, 'LOJA', 'FRUTA FINA', 10, 4, ?)""",
                         (codigo, f"#{codigo} - NF3 / FF / LOJA", status, f"{hoje} 08:00:00"))
        # a tentativa que falhou é a parada da rota (insucesso); a reentrega ainda está aberta
        rota = conn.execute("""INSERT INTO nucleo_rotas (data_rota, nome, agent_id, motorista_nome, status)
                               VALUES (?, 'Rota 7', 501, 'JOAO', 'CONCLUIDA')""", (hoje,)).lastrowid
        conn.execute("""INSERT INTO nucleo_paradas (rota_id, ordem, codigo, situacao, motivo_texto)
                        VALUES (?, 1, 'PS-3', 'INSUCESSO', 'Ausente')""", (rota,))
        conn.commit()
        conn.close()
        self._logar("leitura")
        html = self.cliente.get("/pedidos?q=PS-3").get_data(as_text=True)
        self.assertEqual(html.count('data-tentativas="10|PS-3"'), 1)
        self.assertIn("2 tentativas", html)
        self.assertIn('class="tentativas"', html)

    def test_rota_reenvio_so_para_quem_age(self):
        corpo = {"service_id": 1, "motivo": "cliente pediu"}
        self._logar("leitura")
        r = self.cliente.post("/api/pedidos/acao/reenvio", json=corpo, headers={"Origin": "http://localhost"})
        self.assertNotEqual(r.status_code, 200)
        self._logar("atendimento")
        with mock.patch("acoes_pedidos.reenviar",
                        return_value={"ok": True, "novo_code": "#PS-1-R1", "erro": ""}) as m:
            r = self.cliente.post("/api/pedidos/acao/reenvio", json=corpo, headers={"Origin": "http://localhost"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["novo_code"], "#PS-1-R1")
        self.assertEqual(m.call_args.args[:3], (1, "cliente pediu", "teste"))

    def test_leitura_nao_ve_menu_de_acoes_nem_caixinhas(self):
        self._logar("leitura")
        html = self.cliente.get("/pedidos").get_data(as_text=True)
        self.assertIn(">PS-1<", html)
        self.assertNotIn('class="menu-acoes"', html)
        self.assertNotIn('name="sel"', html)
        self.assertNotIn('id="barra-lote"', html)

    def test_quem_age_ve_menu_caixinhas_e_dados_da_linha(self):
        conn = banco.conectar()
        hoje = date.today().isoformat()
        conn.execute("UPDATE nucleo_pedidos SET vuupt_service_id = 111 WHERE codigo = 'PS-1'")
        # reentrega: a linha do pedido age sobre a tentativa principal (a mais recente)
        for codigo, status, sid in (("PS-3", "INSUCESSO", 301), ("PS-3-R1", "EM_ROTA", 302)):
            conn.execute("""INSERT INTO nucleo_pedidos (codigo, titulo, status, destinatario_nome, remetente_nome,
                                                        sender_id, caixas, criado_em_provedor, vuupt_service_id)
                            VALUES (?, ?, ?, 'LOJA', 'FRUTA FINA', 10, 4, ?, ?)""",
                         (codigo, f"#{codigo} - NF3 / FF / LOJA", status, f"{hoje} 08:00:00", sid))
        conn.commit()
        conn.close()
        self._logar("operador")
        html = self.cliente.get("/pedidos").get_data(as_text=True)
        self.assertEqual(html.count('class="menu-acoes"'), 3)
        self.assertEqual(html.count('name="sel"'), 3)
        self.assertIn('id="barra-lote"', html)
        self.assertRegex(html, r'<tr[^>]*data-service-id="111"[^>]*data-codigo="PS-1"[^>]*data-status="ENTREGUE"')
        self.assertRegex(html, r'<tr[^>]*data-service-id=""[^>]*data-codigo="PS-2"[^>]*data-status="INSUCESSO"')
        self.assertRegex(html, r'<tr[^>]*data-service-id="302"[^>]*data-codigo="PS-3-R1"[^>]*data-status="EM_ROTA"')
        # modal de baixa usa a lista fixa de justificativas
        self.assertIn('value="canhoto_depois"', html)

    def test_linha_leva_embarcador_e_destinatario_para_o_lote(self):
        # Endereço em lote só com o mesmo embarcador e destinatário: a tela compara pelos data-* da linha
        self._logar("operador")
        html = self.cliente.get("/pedidos").get_data(as_text=True)
        linhas = re.findall(r"<tr data-service-id=[^>]*>", html)
        self.assertEqual(len(linhas), 2)
        for tr in linhas:
            self.assertIn('data-sender-id="10"', tr)
        self.assertRegex(html, r'<tr[^>]*data-codigo="PS-1"[^>]*data-sender-id="10"[^>]*data-destinatario="MERCADO CENTRAL"')
        self.assertRegex(html, r'<tr[^>]*data-codigo="PS-2"[^>]*data-sender-id="10"[^>]*data-destinatario="PADARIA"')

    def test_acesso_personalizado_com_a_tela_ve_as_acoes(self):
        # Conta com lista de telas age como "total" dentro delas (requer_auth poe em g.nivel_acesso);
        # o nivel gravado na sessao segue "leitura".
        acesso = painel_agentes.acesso_telas
        conn = acesso.conectar()
        acesso.criar(conn, "ana", "Ana", "leitura", "senha-123", "teste", (), equipe="Logística")
        conn.execute("UPDATE painel_usuarios SET trocar_senha = 0 WHERE usuario = 'ana'")
        conn.commit()
        acesso.salvar(conn, "ana", ["pedidos_tabela"], "teste")
        conn.close()
        with self.cliente.session_transaction() as sess:
            sess["nivel_acesso"] = "leitura"
            sess["usuario"] = "ana"
        html = self.cliente.get("/pedidos").get_data(as_text=True)
        self.assertIn('class="menu-acoes"', html)

    def test_cancelar_stokki_bloqueado_em_pedido_entregue(self):
        self._logar("operador")
        html = self.cliente.get("/pedidos").get_data(as_text=True)
        self.assertRegex(html, r'acao === "cancelar_stokki"\) \{\s*if \(l\.status === "ENTREGUE"\) return "pedido já entregue";')

    def test_niveis_sem_acesso_nao_entram(self):
        self._logar("galpao")
        self.assertNotEqual(self.cliente.get("/pedidos").status_code, 200)
        self.assertNotEqual(self.cliente.get("/pedidos/exportar.csv").status_code, 200)
        self.assertNotEqual(self.cliente.get("/api/pedidos/contagem").status_code, 200)

    # ── celular (Hugo, 11/11): lista compacta + abas + tela do pedido ──
    def test_contagem_das_abas_ignora_o_status_escolhido(self):
        self._logar("leitura")
        r = self.cliente.get("/api/pedidos/contagem?status=ENTREGUE&embarcador=10")
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertEqual((d["ENTREGUE"], d["INSUCESSO"], d["ABERTO"], d["total"]), (1, 1, 0, 2))

    def test_celular_tem_lista_compacta_com_os_dados_do_pedido(self):
        self._logar("operador")
        html = self.cliente.get("/pedidos").get_data(as_text=True)
        itens = re.findall(r'<li class="pm"[^>]*>', html)
        self.assertEqual(len(itens), 2)
        self.assertRegex(html, r'<li class="pm"[^>]*data-codigo="PS-2"[^>]*data-status="INSUCESSO"[^>]*data-sender-id="10"')
        self.assertEqual(html.count('name="sel-m"'), 2)
        self.assertIn('id="m-selecionar"', html)
        dados = re.search(r'<script type="application/json" id="dados-m">(.*?)</script>', html, re.S)
        self.assertIsNotNone(dados)
        self.assertIn('"PS-1"', dados.group(1))
        # a tabela do computador continua com as mesmas contas
        self.assertEqual(html.count('class="menu-acoes"'), 2)
        self.assertEqual(html.count('name="sel"'), 2)

    def test_celular_filtros_tem_status(self):
        self._logar("leitura")
        html = self.cliente.get("/pedidos?status=INSUCESSO").get_data(as_text=True)
        form = re.search(r'<form[^>]*id="pm-form-filtros".*?</form>', html, re.S).group(0)
        self.assertRegex(form, r'<select[^>]*id="pm-status"[^>]*name="status"')
        self.assertRegex(form, r'<option value="INSUCESSO" selected>')
        self.assertRegex(form, r'<option value="">Todos</option>')
        # o status vem so do select (sem hidden duplicado no mesmo form)
        self.assertNotIn('type="hidden" name="status"', form)

    def test_celular_leitura_nao_seleciona(self):
        self._logar("leitura")
        html = self.cliente.get("/pedidos").get_data(as_text=True)
        self.assertEqual(len(re.findall(r'<li class="pm"', html)), 2)
        self.assertNotIn('name="sel-m"', html)
        self.assertNotIn('id="m-selecionar"', html)


if __name__ == "__main__":
    unittest.main()
