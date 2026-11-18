# -*- coding: utf-8 -*-
"""
Pagamentos pontuais (Adiantamento e Descarga, Hugo 18/11/2026): regras,
mensagens, trava do WhatsApp e rotas da tela /financeiro/pagamentos.

    py -3.11 -m unittest test_pagamentos_pontuais   (de dentro de painel_agentes/)
"""
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from nucleo import banco as nucleo_banco  # noqa: E402
import acesso_telas  # noqa: E402
import pagamentos_pontuais as pp  # noqa: E402

import importlib.util  # noqa: E402


def _carregar_painel():
    """Importa painel_agentes.py. Sem config.yaml (worktree limpo) usa um config
    de mentira so pro import (mesmo truque de test_fila_etiquetas_tela); os
    testes trocam _carregar_config depois."""
    import builtins
    spec = importlib.util.spec_from_file_location("painel_agentes_app", _AQUI / "painel_agentes.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    abrir = builtins.open

    def _open(arq, *a, **kw):
        if str(arq).endswith("config.yaml") and not Path(arq).exists():
            return io.StringIO("painel_agentes: {secret_key: teste}\n")
        return abrir(arq, *a, **kw)
    with mock.patch.object(builtins, "open", _open):
        spec.loader.exec_module(mod)
    return mod


painel_agentes = sys.modules.get("painel_agentes_app") or _carregar_painel()

ORIGEM = {"Origin": "http://localhost"}
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 200
PDF = b"%PDF-1.4\n" + b"0" * 200
CFG_DESLIGADO = {"painel_agentes": {"usuario": "u_teste", "senha": "s_teste"}}
CFG_LIGADO = {**CFG_DESLIGADO, "pagamentos_pontuais": {"ativo": True}}


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "dados.db"
        for alvo, attr, valor in ((pp, "DB_PATH", self.db), (pp, "PASTA_ARQUIVOS", Path(self._tmp.name) / "arq"),
                                  (nucleo_banco, "DB_PATH", self.db), (acesso_telas, "DB_PATH", self.db)):
            p = mock.patch.object(alvo, attr, valor)
            p.start()
            self.addCleanup(p.stop)
        conn = nucleo_banco.conectar(self.db)
        linhas = [
            # (nome, cpf, ativo, telefone, placa, veiculo)
            ("João da Silva", "123.456.789-00", "SIM", "5511987654321", "ABC1D23", "Fiorino"),
            ("João da Silva", "12345678900", "SIM", "", "XYZ9K88", "VUC"),        # mesma pessoa, 2º carro
            ("Ana Souza", "98765432100", "SIM", "11912345678", "DEF4G56", "Fiorino"),
            ("Pedro Inativo", "11122233344", "NAO", "", "", ""),
            ("Zé Sem CPF", "", "SIM", "", "", ""),
        ]
        for nome, cpf, ativo, tel, placa, veic in linhas:
            conn.execute("INSERT INTO bd_motoristas (NOME_MOTORISTA, CPF_MOTORISTA, ATIVO, TELEFONE_MOTORISTA, PLACA, "
                         "TIPO_VEICULO, criado_em, atualizado_em) VALUES (?,?,?,?,?,?,'2026-11-01','2026-11-01')",
                         (nome, cpf, ativo, tel, placa, veic))
        conn.execute("INSERT INTO motoristas (cpf, nome, pin_hash, pin_salt, chave_pix) VALUES ('12345678900', 'João', "
                     "'x', 'x', 'joao@pix.com')")
        conn.commit()
        conn.close()
        self.conn = pp.conectar()
        self.addCleanup(self.conn.close)
        self.motoristas = pp.listar_motoristas(self.conn)
        self.joao = next(m for m in self.motoristas if m["nome"] == "João da Silva")
        self.ana = next(m for m in self.motoristas if m["nome"] == "Ana Souza")

    def lancar(self, **kw):
        dados = {"tipo": "ADIANTAMENTO", "motorista_id": self.joao["id"], "valor": "150,00", **kw}
        return pp.lancar(self.conn, dados, "Maria")


class TestMotoristas(Base):
    def test_so_ativos_um_por_cpf_em_ordem(self):
        self.assertEqual([m["nome"] for m in self.motoristas], ["Ana Souza", "João da Silva", "Zé Sem CPF"])

    def test_junta_carros_e_pega_pix_do_app(self):
        self.assertEqual(self.joao["placa"], "ABC1D23 / XYZ9K88")
        self.assertEqual(self.joao["veiculo"], "Fiorino / VUC")
        self.assertEqual(self.joao["pix"], "joao@pix.com")
        self.assertEqual(self.ana["pix"], "")


class TestValidacao(Base):
    def test_valor(self):
        for bruto, esperado in (("150", 150.0), ("150.5", 150.5), ("150,50", 150.5), ("1.234,56", 1234.56),
                                ("R$ 80", 80.0)):
            self.assertEqual(pp.ler_valor(bruto), esperado, bruto)
        for ruim in ("", None, "0", "0,00", "-10", "abc", "10,123", "1e3"):
            with self.assertRaises(pp.ErroPagamento, msg=ruim):
                pp.ler_valor(ruim)

    def test_campos_obrigatorios(self):
        casos = [
            ({"tipo": ""}, "tipo"),
            ({"tipo": "OUTRO"}, "tipo"),
            ({"motorista_id": ""}, "motorista"),
            ({"motorista_id": 99999}, "não encontrado"),
            ({"valor": ""}, "valor"),
            ({"valor": "0"}, "maior que zero"),
            ({"valor": "-5"}, "maior que zero"),
        ]
        for extra, trecho in casos:
            with self.assertRaises(pp.ErroPagamento, msg=str(extra)) as ctx:
                self.lancar(**extra)
            self.assertIn(trecho, str(ctx.exception).lower())
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM pagamentos_pontuais").fetchone()[0], 0)

    def test_motorista_inativo_recusado(self):
        inativo = self.conn.execute("SELECT id FROM bd_motoristas WHERE NOME_MOTORISTA = 'Pedro Inativo'").fetchone()[0]
        with self.assertRaises(pp.ErroPagamento):
            self.lancar(motorista_id=inativo)

    def test_descarga_ao_local_exige_nome_e_pix(self):
        with self.assertRaisesRegex(pp.ErroPagamento, "nome do local"):
            self.lancar(tipo="DESCARGA", favorecido="LOCAL", local_pagamento="pix@local")
        with self.assertRaisesRegex(pp.ErroPagamento, "PIX"):
            self.lancar(tipo="DESCARGA", favorecido="LOCAL", local_nome="Atacadão")

    def test_adiantamento_ignora_local(self):
        pid = self.lancar(favorecido="LOCAL", local_nome="X", local_pagamento="Y")
        p = pp.obter(self.conn, pid)
        self.assertEqual(p["favorecido"], "MOTORISTA")
        self.assertIsNone(p["local_nome"])


class TestFluxo(Base):
    def test_adiantamento_confirmado_vira_pago(self):
        pid = self.lancar()
        self.assertEqual(pp.obter(self.conn, pid)["situacao"], pp.AGUARDANDO_PAGAMENTO)
        with self.assertRaisesRegex(pp.ErroPagamento, "comprovante"):
            pp.confirmar(self.conn, pid, b"", "Carlos")
        self.assertEqual(pp.confirmar(self.conn, pid, PDF, "Carlos"), pp.PAGO)
        p = pp.obter(self.conn, pid)
        self.assertEqual((p["situacao"], p["confirmado_por"], p["comprovante"]), (pp.PAGO, "Carlos", "comprovante_1.pdf"))
        self.assertIsNotNone(pp.caminho_arquivo(pid, "comprovante_1.pdf"))

    def test_descarga_pede_documento_depois_do_pagamento(self):
        pid = self.lancar(tipo="DESCARGA")
        with self.assertRaisesRegex(pp.ErroPagamento, "pagamento já confirmado"):
            pp.incluir_documento(self.conn, pid, JPEG, "Carlos")
        self.assertEqual(pp.confirmar(self.conn, pid, JPEG, "Carlos"), pp.AGUARDANDO_DOCUMENTO)
        with self.assertRaises(pp.ErroPagamento):
            pp.confirmar(self.conn, pid, JPEG, "Carlos")          # nao confirma duas vezes
        pp.incluir_documento(self.conn, pid, PDF, "Carlos")
        p = pp.obter(self.conn, pid)
        self.assertEqual((p["situacao"], p["documento"]), (pp.PAGO, "documento_1.pdf"))

    def test_arquivo_que_nao_e_foto_nem_pdf(self):
        pid = self.lancar()
        with self.assertRaisesRegex(pp.ErroPagamento, "foto"):
            pp.confirmar(self.conn, pid, b"MZ executavel", "Carlos")
        self.assertEqual(pp.obter(self.conn, pid)["situacao"], pp.AGUARDANDO_PAGAMENTO)

    def test_cancelar_so_aguardando_pagamento(self):
        pid = self.lancar()
        with self.assertRaisesRegex(pp.ErroPagamento, "motivo"):
            pp.cancelar(self.conn, pid, "  ", "Maria")
        pp.cancelar(self.conn, pid, "valor errado", "Maria")
        self.assertEqual(pp.obter(self.conn, pid)["situacao"], pp.CANCELADO)
        pago = self.lancar()
        pp.confirmar(self.conn, pago, JPEG, "Carlos")
        with self.assertRaises(pp.ErroPagamento):
            pp.cancelar(self.conn, pago, "tarde demais", "Maria")

    def test_caminho_arquivo_nao_sai_da_pasta(self):
        pid = self.lancar()
        pp.confirmar(self.conn, pid, JPEG, "Carlos")
        self.assertIsNone(pp.caminho_arquivo(pid, "../../dados.db"))


class TestMensagens(Base):
    def test_adiantamento_leva_dados_do_motorista(self):
        pid = self.lancar(observacao="rota 1336")
        texto = pp.texto_lancamento(pp.obter(self.conn, pid))
        self.assertIn(f"Solicitação de pagamento #{pid} · Adiantamento", texto)
        for trecho in ("R$ 150,00", "Pagar ao motorista", "Motorista: João da Silva", "CPF 123.456.789-00",
                       "Tel (11) 98765-4321", "Fiorino / VUC · Placa ABC1D23 / XYZ9K88", "PIX: joao@pix.com",
                       "Obs: rota 1336", "Lançado por Maria"):
            self.assertIn(trecho, texto)

    def test_descarga_ao_local_leva_dados_do_local(self):
        pid = self.lancar(tipo="DESCARGA", valor="1.280,5", favorecido="LOCAL", local_nome="Atacadão Guarulhos",
                          local_pagamento="pix 12.345.678/0001-90", local_endereco="Rod. Dutra km 225")
        texto = pp.texto_lancamento(pp.obter(self.conn, pid))
        for trecho in ("· Descarga", "R$ 1.280,50", "Pagar ao LOCAL da descarga: *Atacadão Guarulhos*",
                       "Endereço: Rod. Dutra km 225", "PIX / dados bancários: pix 12.345.678/0001-90",
                       "Motorista: João da Silva"):
            self.assertIn(trecho, texto)
        self.assertNotIn("CNPJ/CPF:", texto)       # opcional vazio nao aparece
        self.assertNotIn("PIX: joao", texto)       # quem recebe e o local

    def test_motorista_sem_pix(self):
        pid = self.lancar(motorista_id=self.ana["id"])
        self.assertIn("PIX: não cadastrado no app", pp.texto_lancamento(pp.obter(self.conn, pid)))


class TestAviso(Base):
    def test_desligado_so_simula(self):
        pid = self.lancar()
        iniciar = mock.Mock()
        aviso = pp.disparar_aviso(self.conn, pid, "lancamento", CFG_DESLIGADO, iniciar=iniciar)
        iniciar.assert_not_called()
        self.assertEqual(aviso["situacao"], pp.AVISO_SIMULADO)
        p = pp.obter(self.conn, pid)
        self.assertEqual(p["aviso_situacao"], "simulado")
        self.assertIn("Solicitação de pagamento", p["aviso_texto"])
        self.assertFalse(pp.pode_reenviar(p))

    def test_ligado_manda_pro_grupo_achado_pelo_nome(self):
        pid = self.lancar()
        iniciar = mock.Mock()
        pp.disparar_aviso(self.conn, pid, "lancamento", CFG_LIGADO, iniciar=iniciar)
        iniciar.assert_called_once()
        self.assertEqual(pp.obter(self.conn, pid)["aviso_situacao"], "enviando")
        despachar = mock.Mock(return_value="enviado")
        grupos = lambda cfg: [{"id": "1@g.us", "name": "Financeiro"}, {"id": "2@g.us", "name": "FINANCEIRO + LOGISTICA"}]
        self.assertEqual(pp.enviar_aviso(CFG_LIGADO, pid, despachar=despachar, listar=grupos), "enviado")
        args, kw = despachar.call_args
        self.assertEqual((args[1], args[2], kw["grupo_id"]), ("pagamento_pontual", "lancamento", "2@g.us"))
        self.assertEqual(pp.obter(self.conn, pid)["aviso_situacao"], "enviado")

    def test_grupo_do_config_vence_o_nome(self):
        cfg = {**CFG_LIGADO, "pagamentos_pontuais": {"ativo": True, "grupo_id": "9@g.us"}}
        self.assertEqual(pp.grupo_destino(cfg, pp.config_pagamentos(cfg), listar=lambda c: []), "9@g.us")

    def test_sem_grupo_fica_pra_reenviar(self):
        pid = self.lancar()
        pp.disparar_aviso(self.conn, pid, "lancamento", CFG_LIGADO, iniciar=mock.Mock())
        despachar = mock.Mock()
        self.assertEqual(pp.enviar_aviso(CFG_LIGADO, pid, despachar=despachar, listar=lambda c: None), "nao_enviado")
        despachar.assert_not_called()
        p = pp.obter(self.conn, pid)
        self.assertIn("não encontrado", p["aviso_motivo"])
        self.assertTrue(pp.pode_reenviar(p))


class TestTela(Base):
    def setUp(self):
        super().setUp()
        self.cfg = CFG_DESLIGADO
        p = mock.patch.object(painel_agentes, "_carregar_config", side_effect=lambda: self.cfg)
        p.start()
        self.addCleanup(p.stop)
        painel_agentes.app.config["TESTING"] = True
        self.cliente = painel_agentes.app.test_client()

    def logar(self, nivel):
        with self.cliente.session_transaction() as s:
            s["nivel_acesso"] = nivel
            s["usuario"] = "maria"

    def test_niveis(self):
        for nivel, esperado in (("total", 200), ("atendimento", 200), ("logistica", 200), ("operador", 403),
                                ("leitura", 403), ("galpao", 403)):
            self.logar(nivel)
            self.assertEqual(self.cliente.get("/financeiro/pagamentos").status_code, esperado, nivel)

    def test_tela_lista_motoristas_ativos(self):
        self.logar("logistica")
        html = self.cliente.get("/financeiro/pagamentos").get_data(as_text=True)
        self.assertIn("João da Silva", html)
        self.assertIn("Ana Souza", html)
        self.assertNotIn("Pedro Inativo", html)
        self.assertIn("Modo simulação", html)

    def test_lancar_pela_api_valida(self):
        self.logar("logistica")
        for corpo in ({"motorista_id": self.joao["id"], "valor": "10"},
                      {"tipo": "ADIANTAMENTO", "valor": "10"},
                      {"tipo": "ADIANTAMENTO", "motorista_id": self.joao["id"]},
                      {"tipo": "ADIANTAMENTO", "motorista_id": self.joao["id"], "valor": "0"},
                      {"tipo": "ADIANTAMENTO", "motorista_id": self.joao["id"], "valor": "-3"}):
            r = self.cliente.post("/api/financeiro/pagamentos", json=corpo, headers=ORIGEM)
            self.assertEqual(r.status_code, 400, corpo)
            self.assertTrue(r.get_json()["erro"])

    def test_sem_origem_bloqueia(self):
        self.logar("logistica")
        r = self.cliente.post("/api/financeiro/pagamentos",
                              json={"tipo": "ADIANTAMENTO", "motorista_id": self.joao["id"], "valor": "10"})
        self.assertEqual(r.status_code, 403)

    def test_fluxo_descarga_pela_api(self):
        self.logar("logistica")
        r = self.cliente.post("/api/financeiro/pagamentos", headers=ORIGEM, json={
            "tipo": "DESCARGA", "motorista_id": self.joao["id"], "valor": "80", "favorecido": "LOCAL",
            "local_nome": "CD Cliente", "local_pagamento": "pix@cd"})
        self.assertEqual(r.status_code, 200, r.get_json())
        j = r.get_json()
        pid = j["id"]
        self.assertEqual(j["aviso"]["situacao"], "simulado")
        self.assertIn("Pagar ao LOCAL da descarga: *CD Cliente*", j["aviso"]["texto"])
        self.assertEqual(j["pagamento"]["lancado_por"], "maria")

        r = self.cliente.post(f"/api/financeiro/pagamentos/{pid}/documento", headers=ORIGEM,
                              data={"documento": (io.BytesIO(PDF), "doc.pdf")})
        self.assertEqual(r.status_code, 400)                       # antes de confirmar, nao
        r = self.cliente.post(f"/api/financeiro/pagamentos/{pid}/confirmar", headers=ORIGEM, data={})
        self.assertEqual(r.status_code, 400)                       # sem comprovante, nao
        r = self.cliente.post(f"/api/financeiro/pagamentos/{pid}/confirmar", headers=ORIGEM,
                              data={"comprovante": (io.BytesIO(JPEG), "comp.jpg")})
        self.assertEqual(r.get_json()["pagamento"]["situacao"], pp.AGUARDANDO_DOCUMENTO)
        r = self.cliente.post(f"/api/financeiro/pagamentos/{pid}/documento", headers=ORIGEM,
                              data={"documento": (io.BytesIO(PDF), "doc.pdf")})
        j = r.get_json()
        self.assertEqual(j["pagamento"]["situacao"], pp.PAGO)
        self.assertIn(f"Documento da descarga #{pid} incluído", j["aviso"]["texto"])
        r = self.cliente.get(f"/financeiro/pagamentos/{pid}/arquivo/documento_1.pdf")
        self.assertEqual((r.status_code, r.data[:4]), (200, b"%PDF"))
        r.close()

    def test_cancelar_avisa(self):
        self.logar("atendimento")
        pid = self.lancar()
        r = self.cliente.post(f"/api/financeiro/pagamentos/{pid}/cancelar", headers=ORIGEM, json={"motivo": "duplicado"})
        j = r.get_json()
        self.assertEqual(j["pagamento"]["situacao"], pp.CANCELADO)
        self.assertIn("cancelada", j["aviso"]["texto"])
        self.assertIn("Motivo: duplicado", j["aviso"]["texto"])

    def test_ligado_dispara_em_segundo_plano(self):
        self.cfg = CFG_LIGADO
        self.logar("logistica")
        with mock.patch.object(pp.threading, "Thread") as thread:
            r = self.cliente.post("/api/financeiro/pagamentos", headers=ORIGEM,
                                  json={"tipo": "ADIANTAMENTO", "motorista_id": self.ana["id"], "valor": "50"})
        self.assertEqual(r.get_json()["aviso"]["situacao"], "enviando")
        thread.assert_called_once()
        self.assertIs(thread.call_args.kwargs["target"], pp.enviar_aviso)

    def test_reenviar_so_quando_pendente(self):
        self.logar("logistica")
        pid = self.lancar()
        pp.disparar_aviso(self.conn, pid, "lancamento", CFG_DESLIGADO)
        r = self.cliente.post(f"/api/financeiro/pagamentos/{pid}/reenviar-aviso", headers=ORIGEM)
        self.assertEqual(r.status_code, 400)
        self.conn.execute("UPDATE pagamentos_pontuais SET aviso_situacao = 'falhou' WHERE id = ?", (pid,))
        self.conn.commit()
        self.cfg = CFG_LIGADO
        with mock.patch.object(pp.threading, "Thread"):
            r = self.cliente.post(f"/api/financeiro/pagamentos/{pid}/reenviar-aviso", headers=ORIGEM)
        self.assertEqual(r.get_json()["aviso"]["situacao"], "enviando")


if __name__ == "__main__":
    unittest.main()
