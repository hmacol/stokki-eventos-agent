# -*- coding: utf-8 -*-
import unittest
from datetime import date
from unittest import mock

import notificar_whatsapp as nw
from nucleo import enviar_pendencias_motoristas as env

PEND = {"documentos": ["cnh", "antt"], "campos": ["PIX", "nascimento"]}


def _pessoa(pid, nome, cpf="123", tel="11988887777", ativo=True):
    return {"principal_id": pid, "nome": nome, "cpf": cpf, "ativo": ativo, "ids": [pid],
            "linhas": [{"TELEFONE_MOTORISTA": tel}]}


class TextoTest(unittest.TestCase):
    def test_lista_pendencias_e_link(self):
        t = nw.texto_pendencias_motorista("Ana", PEND["documentos"], PEND["campos"], "https://x/atualizar/tok", date(2026, 12, 6))
        self.assertIn("Olá, Ana!", t)
        self.assertIn("CNH, ANTT", t)
        self.assertIn("chave PIX, data de nascimento", t)
        self.assertIn("https://x/atualizar/tok", t)
        self.assertIn("até 06/12", t)

    def test_sem_documentos_nao_cria_linha_vazia(self):
        t = nw.texto_pendencias_motorista("Ana", [], ["placa"], "L", date(2026, 12, 6))
        self.assertNotIn("Documentos", t)
        self.assertIn("Dados: placa", t)


class SelecionarTest(unittest.TestCase):
    def _rodar(self, pessoas, aberto=None):
        with mock.patch.object(env.atu, "pessoas", return_value=pessoas), \
             mock.patch.object(env.atu, "pendencias", return_value=PEND), \
             mock.patch.object(env.atu, "link_em_aberto", side_effect=lambda c, pid: (aberto or {}).get(pid)), \
             mock.patch.object(env.atu, "ficha_vigente", return_value=None):
            return env.selecionar(None)

    def test_separa_quem_recebe_de_quem_fica_de_fora(self):
        recebem, fora = self._rodar([
            _pessoa(1, "Ana Silva"), _pessoa(2, "Motorista TESTE"), _pessoa(3, "Sem Cpf", cpf=None),
            _pessoa(4, "Sem Fone", tel=""), _pessoa(5, "Com Link"), _pessoa(6, "Inativo", ativo=False),
            _pessoa(7, "LALAMOVE (virtual)")], aberto={5: {"id": 9}})
        self.assertEqual([d["principal_id"] for d in recebem], [1])
        self.assertEqual(recebem[0]["telefone"], "11988887777")
        self.assertEqual({d["principal_id"]: d["motivo"] for d in fora},
                         {2: "cadastro de teste/virtual", 3: "sem CPF", 4: "sem telefone",
                          5: "ja tem link em aberto", 7: "cadastro de teste/virtual"})


class EnviarTest(unittest.TestCase):
    def test_desligado_nao_gera_link(self):
        with mock.patch.object(env.atu, "gerar_link") as gerar:
            r = env.enviar(None, {}, [{"principal_id": 1, "nome": "Ana", "telefone": "11", "pendencias": PEND}], "https://x")
        gerar.assert_not_called()
        self.assertEqual(r, [{"principal_id": 1, "situacao": "desligado"}])

    def test_teto_interrompe_o_lote_e_cancela_o_link(self):
        conn = mock.MagicMock()
        dest = [{"principal_id": i, "nome": f"N{i}", "telefone": "11988887777", "pendencias": PEND} for i in (1, 2)]
        with mock.patch.object(env.atu, "gerar_link", return_value={"id": 7, "token": "t", "expira_em": "2026-12-06 10:00:00"}), \
             mock.patch.object(env.banco, "agora", return_value="agora"):
            r = env.enviar(conn, {"atualizacao_motoristas": {"forcar_destino_whatsapp": ""}}, dest, "https://x",
                           avisar=lambda *a, **k: ("teto", False))
        self.assertEqual([x["situacao"] for x in r], ["teto", "nao_tentado"])


class PrivadoTest(unittest.TestCase):
    def test_sem_chave_no_config_nao_envia(self):
        self.assertEqual(nw.avisar_motorista_pendencias_privado(1, "11988887777", "Ana", "oi", {}), ("desligado", False))

    def test_piloto_vai_pro_numero_do_piloto_marcado(self):
        cfg = {"atualizacao_motoristas": {"forcar_destino_whatsapp": "5511999990000"},
               "whatsapp_notificacoes": {"ativo": True}}
        with mock.patch.object(nw, "gateway_clientes_ok", return_value=True), \
             mock.patch.object(nw, "despachar", return_value="enviado") as d:
            r = nw.avisar_motorista_pendencias_privado(3, "11988887777", "Ana", "oi", cfg)
        self.assertEqual(r, ("enviado", True))
        self.assertEqual(d.call_args.kwargs["grupo_id"], "5511999990000@c.us")
        self.assertTrue(d.call_args.args[3].startswith("[teste → Ana +5511988887777]"))

    def test_real_confere_whatsapp_e_vai_pro_motorista(self):
        cfg = {"atualizacao_motoristas": {"forcar_destino_whatsapp": ""}}
        with mock.patch.object(nw, "gateway_clientes_ok", return_value=True), \
             mock.patch.object(nw.integracao_openwa, "numero_existe", return_value=True), \
             mock.patch.object(nw, "despachar", return_value="enviado") as d:
            r = nw.avisar_motorista_pendencias_privado(3, "11988887777", "Ana", "oi", cfg)
        self.assertEqual(r, ("enviado", False))
        self.assertEqual(d.call_args.kwargs["grupo_id"], "5511988887777@c.us")

    def test_real_numero_sem_whatsapp_nao_envia(self):
        cfg = {"atualizacao_motoristas": {"forcar_destino_whatsapp": ""}}
        with mock.patch.object(nw, "gateway_clientes_ok", return_value=True), \
             mock.patch.object(nw.integracao_openwa, "numero_existe", return_value=False), \
             mock.patch.object(nw, "despachar") as d:
            r = nw.avisar_motorista_pendencias_privado(3, "11988887777", "Ana", "oi", cfg)
        d.assert_not_called()
        self.assertEqual(r, ("numero_sem_whatsapp", False))


if __name__ == "__main__":
    unittest.main()
