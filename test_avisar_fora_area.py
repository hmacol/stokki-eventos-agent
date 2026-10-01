# -*- coding: utf-8 -*-
"""
Aviso em massa aos embarcadores sobre pedidos fora da area de atendimento
(botao "Avisar clientes" do planejamento, Hugo 30/09/2026).

    py -3.11 -m unittest test_avisar_fora_area
"""
import sqlite3
import unittest
from datetime import datetime
from unittest.mock import patch

import avisar_fora_area as afa

AGORA = datetime(2026, 9, 30, 10, 12, 0)
S1 = {"id": 1, "code": "#PS-1", "sender_id": 10, "address": "Rua A, 1 - Centro, Curitiba - PR, 80000-000"}
S2 = {"id": 2, "code": "PS-2", "sender_id": 10, "address": "Rua B, 2 - Centro, Blumenau - SC, 89000-000"}
S3 = {"id": 3, "code": "PS-3", "sender_id": 20, "address": "Rua C, 3 - Centro, Bauru - SP, 17000-000"}
S4 = {"id": 4, "code": "PS-4", "sender_id": 10, "address": "Rua D, 4 - Centro, Bauru - SP, 17000-000"}
S5 = {"id": 5, "code": "PS-5", "sender_id": 30, "address": "Rua E, 5 - Centro, Manaus - AM, 69000-000"}
SERVICOS = [S1, S2, S3, S4, S5]
TIPOS = {1: afa.TIPO_FORA_SP, 2: afa.TIPO_FORA_SP, 3: afa.TIPO_SP, 4: afa.TIPO_SP, 5: afa.TIPO_FORA_SP}


def _config(**wa):
    cfg = {"ativo": True, "base_url": "http://x/api", "api_key": "k", "sessao": "s", "grupo_id": "9@g.us"}
    cfg.update(wa)
    return {"whatsapp_notificacoes": cfg, "email": {"remetente": "a@b.com"}, "avisos_fora_area": {}}


class _ComBanco(unittest.TestCase):
    def setUp(self):
        self.conn = afa.conectar(":memory:")
        self.conn.executescript("""
            CREATE TABLE interno (cnpj_embarcador TEXT PRIMARY KEY, nome_remetente TEXT, apelido TEXT,
                                  email TEXT, sender_id INTEGER);
            INSERT INTO interno VALUES ('1', 'ACME LTDA', 'ACME', 'a@acme.com; b@acme.com', 10),
                                       ('2', 'BETA', NULL, '', 20);
        """)
        afa.garantir_coluna_grupo(self.conn)
        self.conn.execute("UPDATE interno SET whatsapp_grupo_id = '111@g.us' WHERE sender_id = 10")
        self.addCleanup(self.conn.close)
        self.email = patch.object(afa, "enviar_email", return_value=True)
        self.enviar_email = self.email.start()
        self.addCleanup(self.email.stop)
        self.wa = patch.object(afa.notificar_whatsapp, "despachar", return_value="enviado")
        self.despachar = self.wa.start()
        self.addCleanup(self.wa.stop)

    def linhas(self):
        return [tuple(r) for r in self.conn.execute(
            "SELECT service_id, sender_id, tipo, canal, situacao, destino, por FROM avisos_fora_area ORDER BY id"
        ).fetchall()]


class Coluna(unittest.TestCase):
    def test_garantir_coluna_duas_vezes(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE interno (cnpj_embarcador TEXT, email TEXT, sender_id INTEGER)")
        afa.garantir_coluna_grupo(conn)
        afa.garantir_coluna_grupo(conn)
        colunas = [r[1] for r in conn.execute("PRAGMA table_info(interno)")]
        self.assertEqual(colunas.count("whatsapp_grupo_id"), 1)


class Texto(unittest.TestCase):
    PEDIDOS = [{"codigo": "PS-1", "cidade": "Curitiba", "uf": "PR"}, {"codigo": "PS-2", "cidade": "Blumenau", "uf": "SC"}]

    def test_fora_sp_com_email(self):
        self.assertEqual(afa.texto_whatsapp(afa.TIPO_FORA_SP, self.PEDIDOS, com_email=True), "\n".join([
            "⚠️ *Fresh Log · pedidos fora da área de atendimento*",
            "PS-1 · Curitiba/PR",
            "PS-2 · Blumenau/SC",
            "Esses destinos ficam fora do estado de SP. Haverá redespacho por transportadora? "
            "Se sim, nos envie o endereço completo com CEP e o nome da transportadora. Detalhes no e-mail.",
        ]))

    def test_sp_sem_email(self):
        texto = afa.texto_whatsapp(afa.TIPO_SP, self.PEDIDOS[:1], com_email=False)
        self.assertTrue(texto.endswith("Se quiser, fazemos uma cotação de entrega dedicada."))
        self.assertNotIn("Detalhes no e-mail", texto)

    def test_ate_dez_pedidos_e_mais_n(self):
        pedidos = [{"codigo": f"PS-{i}", "cidade": "X", "uf": "PR"} for i in range(13)]
        linhas = afa.texto_whatsapp(afa.TIPO_FORA_SP, pedidos, com_email=True).split("\n")
        self.assertEqual(len([l for l in linhas if l.startswith("PS-")]), 10)
        self.assertEqual(linhas[11], "e mais 3")

    def test_sem_cidade(self):
        texto = afa.texto_whatsapp(afa.TIPO_FORA_SP, [{"codigo": "PS-1", "cidade": None, "uf": None}], True)
        self.assertIn("\nPS-1\n", texto)

    def test_perde_formatacao_do_whatsapp(self):
        texto = afa.texto_whatsapp(afa.TIPO_FORA_SP, [{"codigo": "*PS-1*", "cidade": "_X_", "uf": "PR"}], True)
        self.assertIn("\nPS-1 · X/PR\n", texto)


class Previa(_ComBanco):
    def test_agrupa_por_embarcador_e_tipo(self):
        previa = afa.montar_previa(SERVICOS, TIPOS, _config(), self.conn)
        chaves = [(b["sender_id"], b["tipo"]) for b in previa["blocos"]]
        self.assertEqual(chaves, [(10, afa.TIPO_FORA_SP), (10, afa.TIPO_SP), (20, afa.TIPO_SP), (30, afa.TIPO_FORA_SP)])
        acme = previa["blocos"][0]
        self.assertEqual(acme["nome"], "ACME")
        self.assertEqual(acme["emails"], ["a@acme.com", "b@acme.com"])
        self.assertEqual(acme["whatsapp_grupo_id"], "111@g.us")
        self.assertEqual([p["codigo"] for p in acme["pedidos"]], ["PS-1", "PS-2"])
        self.assertEqual(acme["pedidos"][0]["cidade"], "Curitiba")
        self.assertEqual(acme["pedidos"][0]["uf"], "PR")
        self.assertTrue(acme["enviavel"])
        self.assertIsNone(acme["ultimo_aviso"])
        self.assertTrue(previa["whatsapp_disponivel"])

    def test_nome_do_grupo_quando_informado(self):
        previa = afa.montar_previa(SERVICOS, TIPOS, _config(), self.conn, grupos_nomes={"111@g.us": "ACME x Fresh"})
        self.assertEqual(previa["blocos"][0]["whatsapp_grupo_nome"], "ACME x Fresh")

    def test_sem_email_e_sem_grupo_nao_enviavel(self):
        previa = afa.montar_previa(SERVICOS, TIPOS, _config(), self.conn)
        beta = previa["blocos"][2]
        self.assertEqual(beta["nome"], "BETA")
        self.assertEqual(beta["emails"], [])
        self.assertIsNone(beta["whatsapp_grupo_id"])
        self.assertFalse(beta["enviavel"])

    def test_embarcador_sem_cadastro(self):
        previa = afa.montar_previa(SERVICOS, TIPOS, _config(), self.conn)
        sem = previa["blocos"][3]
        self.assertEqual(sem["nome"], "Remetente 30")
        self.assertFalse(sem["enviavel"])

    def test_pedido_fora_dos_tipos_e_ignorado(self):
        previa = afa.montar_previa(SERVICOS + [{"id": 9, "code": "PS-9", "sender_id": 10}], TIPOS, _config(), self.conn)
        self.assertEqual(previa["ignorados"], [9])

    def test_whatsapp_desligado(self):
        previa = afa.montar_previa(SERVICOS, TIPOS, _config(ativo=False), self.conn)
        self.assertFalse(previa["whatsapp_disponivel"])
        self.assertFalse(afa.whatsapp_disponivel(_config(api_key="")))
        self.assertFalse(afa.whatsapp_disponivel({}))

    def test_ultimo_aviso_vem_do_registro(self):
        afa.enviar([{"sender_id": 10, "tipo": afa.TIPO_FORA_SP, "service_ids": [1, 2], "canais": ["email"]}],
                   SERVICOS, TIPOS, _config(), "hugo", self.conn, agora=AGORA)
        previa = afa.montar_previa(SERVICOS, TIPOS, _config(), self.conn)
        self.assertEqual(previa["blocos"][0]["ultimo_aviso"], {"em": "30/09 10:12", "por": "hugo"})
        self.assertEqual(previa["blocos"][0]["pedidos"][0]["avisado_em"], "30/09 10:12")
        self.assertIsNone(previa["blocos"][1]["ultimo_aviso"])


class Enviar(_ComBanco):
    def item(self, sender_id=10, tipo=afa.TIPO_FORA_SP, ids=(1, 2), canais=("email", "whatsapp")):
        return {"sender_id": sender_id, "tipo": tipo, "service_ids": list(ids), "canais": list(canais)}

    def test_envia_email_e_whatsapp_e_registra(self):
        r = afa.enviar([self.item()], SERVICOS, TIPOS, _config(), "hugo", self.conn, agora=AGORA)
        self.assertEqual(r["resultados"], [{"sender_id": 10, "tipo": afa.TIPO_FORA_SP, "email": "enviado",
                                            "whatsapp": "enviado", "detalhe": ""}])
        destinos, assunto, corpo, cfg_email = self.enviar_email.call_args[0]
        self.assertEqual(destinos, ["a@acme.com", "b@acme.com"])
        self.assertIn("fora de SP", assunto)
        self.assertIn("#PS-1", corpo)
        self.assertEqual(cfg_email, {"remetente": "a@b.com"})
        kw = self.despachar.call_args.kwargs
        self.assertEqual(kw["grupo_id"], "111@g.us")
        self.assertFalse(kw["contar_no_teto"])
        self.assertEqual(self.despachar.call_args.args[1:3], ("avisar_fora_area", "fora_area_cliente"))
        self.assertIn("Detalhes no e-mail.", self.despachar.call_args.args[3])
        self.assertEqual(self.linhas(), [
            (1, 10, afa.TIPO_FORA_SP, "email", "enviado", "a@acme.com; b@acme.com", "hugo"),
            (2, 10, afa.TIPO_FORA_SP, "email", "enviado", "a@acme.com; b@acme.com", "hugo"),
            (1, 10, afa.TIPO_FORA_SP, "whatsapp", "enviado", "111@g.us", "hugo"),
            (2, 10, afa.TIPO_FORA_SP, "whatsapp", "enviado", "111@g.us", "hugo"),
        ])

    def test_email_com_varios_enderecos(self):
        afa.enviar([self.item(canais=("email",))], SERVICOS, TIPOS, _config(), "hugo", self.conn, agora=AGORA)
        self.assertEqual(self.enviar_email.call_args[0][0], ["a@acme.com", "b@acme.com"])
        self.despachar.assert_not_called()

    def test_forcar_destino_redireciona_so_o_email(self):
        config = _config()
        config["avisos_fora_area"] = {"forcar_destino": "hugo@x.com"}
        afa.enviar([self.item()], SERVICOS, TIPOS, config, "hugo", self.conn, agora=AGORA)
        self.assertEqual(self.enviar_email.call_args[0][0], ["hugo@x.com"])
        self.assertEqual(self.despachar.call_args.kwargs["grupo_id"], "111@g.us")
        self.assertEqual(self.linhas()[0][5], "hugo@x.com")

    def test_so_whatsapp_tira_a_frase_do_email(self):
        afa.enviar([self.item(canais=("whatsapp",))], SERVICOS, TIPOS, _config(), "hugo", self.conn, agora=AGORA)
        self.enviar_email.assert_not_called()
        self.assertNotIn("Detalhes no e-mail", self.despachar.call_args.args[3])

    def test_falha_em_um_bloco_nao_para_o_proximo(self):
        self.enviar_email.side_effect = [False, True]
        self.conn.execute("UPDATE interno SET email = 'b@beta.com' WHERE sender_id = 20")
        r = afa.enviar([self.item(canais=("email",)), self.item(20, afa.TIPO_SP, (3,), ("email",))],
                       SERVICOS, TIPOS, _config(), "hugo", self.conn, agora=AGORA)
        self.assertEqual([x["email"] for x in r["resultados"]], ["falhou", "enviado"])
        self.assertEqual([l[4] for l in self.linhas()], ["falhou", "falhou", "enviado"])

    def test_whatsapp_falhou_ou_nao_enviado_e_registrado(self):
        self.despachar.return_value = "nao_enviado"
        r = afa.enviar([self.item(canais=("whatsapp",))], SERVICOS, TIPOS, _config(), "hugo", self.conn, agora=AGORA)
        self.assertEqual(r["resultados"][0]["whatsapp"], "nao_enviado")
        self.assertEqual([l[4] for l in self.linhas()], ["falhou", "falhou"])

    def test_bloco_sem_canal_utilizavel_e_pulado(self):
        r = afa.enviar([self.item(20, afa.TIPO_SP, (3,))], SERVICOS, TIPOS, _config(), "hugo", self.conn, agora=AGORA)
        self.assertEqual(r["resultados"][0]["email"], "pulado")
        self.assertEqual(r["resultados"][0]["whatsapp"], "pulado")
        self.assertIn("sem e-mail", r["resultados"][0]["detalhe"])
        self.assertEqual(self.linhas(), [])

    def test_whatsapp_desligado_devolve_desligado(self):
        self.despachar.return_value = "desligado"
        r = afa.enviar([self.item(canais=("whatsapp",))], SERVICOS, TIPOS, _config(ativo=False), "hugo", self.conn, agora=AGORA)
        self.assertEqual(r["resultados"][0]["whatsapp"], "desligado")
        self.assertEqual(self.linhas(), [])

    def test_ids_repetidos_ou_desconhecidos(self):
        r = afa.enviar([self.item(ids=(1, 1, 99), canais=("email",))], SERVICOS, TIPOS, _config(), "hugo", self.conn, agora=AGORA)
        self.assertEqual(r["ignorados"], [99])
        self.assertEqual(len(self.linhas()), 1)

    def test_pedido_que_saiu_da_area_e_ignorado(self):
        tipos = dict(TIPOS)
        del tipos[2]
        r = afa.enviar([self.item(canais=("email",))], SERVICOS, tipos, _config(), "hugo", self.conn, agora=AGORA)
        self.assertEqual(r["ignorados"], [2])
        self.assertIn("#PS-1", self.enviar_email.call_args[0][2])
        self.assertNotIn("#PS-2", self.enviar_email.call_args[0][2])

    def test_tipo_do_item_diferente_da_classificacao_e_ignorado(self):
        r = afa.enviar([self.item(tipo=afa.TIPO_SP, canais=("email",))], SERVICOS, TIPOS, _config(), "hugo", self.conn, agora=AGORA)
        self.assertEqual(r["ignorados"], [1, 2])
        self.enviar_email.assert_not_called()

    def test_modo_teste_nao_envia_nem_registra(self):
        self.despachar.return_value = "modo_teste"   # o despachar real devolve isso em modo_teste
        r = afa.enviar([self.item()], SERVICOS, TIPOS, _config(), "hugo", self.conn, modo_teste=True, agora=AGORA)
        self.enviar_email.assert_not_called()
        self.assertEqual(r["resultados"][0]["email"], "modo_teste")
        self.assertEqual(r["resultados"][0]["whatsapp"], "modo_teste")
        self.assertTrue(self.despachar.call_args.kwargs["modo_teste"])
        self.assertEqual(self.linhas(), [])


if __name__ == "__main__":
    unittest.main()
