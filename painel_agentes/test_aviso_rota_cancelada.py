# -*- coding: utf-8 -*-
"""
Aviso de rota cancelada (botao "Cancelar e notificar" do Planejamento, 27/10/2026).

    py -3.11 -m unittest test_aviso_rota_cancelada   (de dentro de painel_agentes/)
"""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _RAIZ / "portal_cliente", _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import aviso_rota_cancelada as arc  # noqa: E402
import chamados as ch  # noqa: E402
import controle_notificacoes as cn  # noqa: E402
import grupos_equipe as ge  # noqa: E402

CNPJ_A = "11111111000111"
CNPJ_B = "22222222000122"

RASCUNHO = {
    "id": 7, "nome": "#3 ZONA NORTE", "motorista_nome": "João", "data_alvo": "2026-10-28",
    "paradas": [
        {"codigo": "PS-1", "sender_id": 101, "remetente_nome": "Alfa"},
        {"codigo": "PS-2", "sender_id": 202, "remetente_nome": "Beta"},
        {"codigo": "PS-3", "sender_id": 101, "remetente_nome": "Alfa"},
        {"codigo": "PS-3", "sender_id": 101, "remetente_nome": "Alfa"},
    ],
}
CONFIG_PILOTO = {"portal_cliente": {"chamados": {"forcar_destino": "hugo@freshlogbr.com"}}}


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "dados.db"
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE interno (cnpj_embarcador TEXT, sender_id INTEGER, nome_remetente TEXT, "
                     "apelido TEXT, email TEXT)")
        conn.executemany("INSERT INTO interno VALUES (?,?,?,?,?)", [
            (CNPJ_A, 101, "ALFA LTDA", "Alfa", "a@x.com"),
            (CNPJ_B, 202, "BETA LTDA", "Beta", "b@x.com"),
            (arc.CNPJ_TESTE, 22812827, "CLIENTE TESTE", "", ""),
        ])
        conn.commit()
        conn.close()
        self.patch = mock.patch.object(ch, "DB_PATH", self.db)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def _estado(self, estado):
        conn = cn.conectar(self.db)
        try:
            cn.definir_estado(conn, arc.TIPO_NOTIFICACAO, estado, "teste")
        finally:
            conn.close()

    def _avisar(self, config=None, rascunho=RASCUNHO):
        return arc.avisar({**rascunho, "paradas": [dict(p) for p in rascunho["paradas"]]},
                          "motorista não conseguiu realizar a coleta", "hugo", config or {}, db_path=self.db)

    def _chamados(self):
        conn = ch.conectar()
        try:
            rows = conn.execute("SELECT * FROM portal_chamados ORDER BY id").fetchall()
            return [(dict(r), [m["texto"] for m in ch.mensagens(conn, r["id"])]) for r in rows]
        finally:
            conn.close()

    def _mensagens_grupo(self):
        conn = ge.conectar(self.db)
        try:
            g = conn.execute("SELECT id FROM equipe_grupos WHERE nome = ?", (arc.NOME_GRUPO,)).fetchone()
            return [m["texto"] for m in ge.mensagens(conn, g["id"])] if g else []
        finally:
            conn.close()


class Agrupar(unittest.TestCase):
    def test_um_por_embarcador_sem_repetir_pedido(self):
        grupos = arc.agrupar_por_embarcador(RASCUNHO["paradas"])
        self.assertEqual([(g["nome"], g["codigos"]) for g in grupos], [("Alfa", ["PS-1", "PS-3"]), ("Beta", ["PS-2"])])


class Avisar(_Base):
    def test_ligado_abre_conversa_de_cada_embarcador_so_com_os_pedidos_dele(self):
        self._estado("LIGADO")
        r = self._avisar()
        chamados = self._chamados()
        self.assertEqual([c["cnpj_embarcador"] for c, _ in chamados], [CNPJ_A, CNPJ_B])
        alfa, msgs_alfa = chamados[0]
        self.assertEqual((alfa["status"], alfa["tipo"], alfa["assunto"]), (ch.STATUS_AGUARDANDO_FL, "CLIENTE", "Rota cancelada"))
        self.assertIn("PS-1, PS-3", msgs_alfa[0])
        self.assertIn("motorista não conseguiu realizar a coleta", msgs_alfa[0])
        self.assertIn("28/10", msgs_alfa[0])
        self.assertNotIn("PS-2", msgs_alfa[0])
        self.assertNotIn("ZONA NORTE", msgs_alfa[0])  # nome interno da rota nao vai pro cliente
        self.assertTrue(r["grupo"])
        self.assertTrue(all(e["aviso"].startswith("avisado no chat") for e in r["embarcadores"]))

    def test_grupo_rotas_canceladas_resume_rota_pedidos_e_embarcadores(self):
        self._estado("LIGADO")
        self._avisar()
        msgs = self._mensagens_grupo()
        self.assertEqual(len(msgs), 1)
        texto = msgs[0]
        for trecho in ("Rota cancelada · #3 ZONA NORTE · João · 28/10", "Motivo: motorista não conseguiu",
                       "Cancelada por hugo", "3 pedido(s) · 2 embarcador(es)", "Alfa (2): PS-1, PS-3 · avisado no chat",
                       "Beta (1): PS-2"):
            self.assertIn(trecho, texto)

    def test_sem_linha_na_tela_e_forcar_destino_vai_pro_cliente_teste(self):
        r = self._avisar(CONFIG_PILOTO)
        chamados = self._chamados()
        self.assertEqual({c["cnpj_embarcador"] for c, _ in chamados}, {arc.CNPJ_TESTE})
        self.assertIn("[Piloto · destino real: Alfa]", chamados[0][1][0])
        self.assertIn("piloto", r["embarcadores"][0]["aviso"])

    def test_desligado_so_avisa_o_grupo(self):
        self._estado("DESLIGADO")
        r = self._avisar()
        self.assertEqual(self._chamados(), [])
        self.assertIn("NÃO avisado (tipo desligado)", self._mensagens_grupo()[0])
        self.assertTrue(r["grupo"])

    def test_embarcador_sem_cadastro_aparece_no_grupo_como_nao_avisado(self):
        self._estado("LIGADO")
        rascunho = {**RASCUNHO, "paradas": [{"codigo": "PS-9", "sender_id": 999, "remetente_nome": "Gama"}]}
        r = self._avisar(rascunho=rascunho)
        self.assertEqual(self._chamados(), [])
        self.assertEqual(r["embarcadores"][0]["aviso"], "NÃO avisado (sem cadastro em interno)")

    def test_grupo_fora_nao_derruba(self):
        self._estado("LIGADO")
        with mock.patch.object(ge, "postar", side_effect=RuntimeError("fora")):
            r = self._avisar()
        self.assertFalse(r["grupo"])
        self.assertEqual(len(self._chamados()), 2)


def _carregar_painel():
    """Sobe o app do painel sem config.yaml (mesmo truque de test_notificacoes_tela)."""
    import builtins
    import importlib.util
    import io
    abrir = builtins.open

    def sem_config(arq, *a, **kw):
        if Path(str(arq)).name == "config.yaml" and not Path(str(arq)).exists():
            return io.StringIO("painel_agentes:\n  usuario: u\n  senha: s\n  secret_key: chave-de-teste\n")
        return abrir(arq, *a, **kw)

    nome = "painel_agentes_app"
    if nome not in sys.modules:
        spec = importlib.util.spec_from_file_location(nome, _AQUI / "painel_agentes.py")
        sys.modules[nome] = importlib.util.module_from_spec(spec)
        with mock.patch.object(builtins, "open", sem_config):
            spec.loader.exec_module(sys.modules[nome])
    return sys.modules[nome]


class Rota(unittest.TestCase):
    URL = "/api/planejamento/cancelar-e-notificar"
    ORIGEM = {"Origin": "http://localhost"}

    def setUp(self):
        from datetime import date
        import acesso_telas
        self.painel = _carregar_painel()
        rascunho = {**RASCUNHO, "data_alvo": date.today().isoformat()}
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        for alvo, attr, kw in (
            # banco de acessos proprio: na VPS a "rodada de sessoes" do banco real derruba a sessao do teste
            (acesso_telas, "DB_PATH", {"new": Path(tmp.name) / "dados.db"}),
            (self.painel, "_carregar_config", {"return_value": {"vuupt_api": {"token": "tk"},
                                                                "painel_agentes": {"usuario": "u", "senha": "s"}}}),
            (self.painel.rascunhos_rota, "buscar_rascunho", {"return_value": rascunho}),
            (self.painel.rascunhos_rota, "cancelar_rota_enviada", {"return_value": {"rascunho_id": 7, "ok": True}}),
            (arc, "avisar", {"return_value": {"grupo": True, "embarcadores": []}}),
        ):
            p = mock.patch.object(alvo, attr, **kw)
            setattr(self, attr, p.start())
            self.addCleanup(p.stop)
        self.painel.app.config["TESTING"] = True
        self.cliente = self.painel.app.test_client()

    def _logar(self, nivel="operador"):
        with self.cliente.session_transaction() as sess:
            sess["nivel_acesso"] = nivel
            sess["usuario"] = "ana"

    def test_cancela_e_avisa_com_motivo_e_quem_clicou(self):
        self._logar()
        r = self.cliente.post(self.URL, json={"rascunho_id": 7, "motivo": "  motorista   não coletou "}, headers=self.ORIGEM)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])
        self.cancelar_rota_enviada.assert_called_once_with(7, "tk")
        args = self.avisar.call_args.args
        self.assertEqual((args[0]["id"], args[1], args[2]), (7, "motorista não coletou", "ana"))

    def test_sem_motivo_nao_cancela(self):
        self._logar()
        r = self.cliente.post(self.URL, json={"rascunho_id": 7, "motivo": "  "}, headers=self.ORIGEM)
        self.assertEqual(r.status_code, 400)
        self.cancelar_rota_enviada.assert_not_called()

    def test_cancelamento_recusado_nao_avisa_ninguem(self):
        self.cancelar_rota_enviada.return_value = {"rascunho_id": 7, "ok": False, "erro": "já em deslocamento"}
        self._logar()
        r = self.cliente.post(self.URL, json={"rascunho_id": 7, "motivo": "x"}, headers=self.ORIGEM)
        self.assertEqual(r.get_json(), {"ok": False, "erro": "já em deslocamento"})
        self.avisar.assert_not_called()

    def test_leitura_nao_pode(self):
        self._logar("leitura")
        r = self.cliente.post(self.URL, json={"rascunho_id": 7, "motivo": "x"}, headers=self.ORIGEM)
        self.assertNotEqual(r.status_code, 200)
        self.cancelar_rota_enviada.assert_not_called()


if __name__ == "__main__":
    unittest.main()
