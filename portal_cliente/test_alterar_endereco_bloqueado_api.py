# -*- coding: utf-8 -*-
"""
Rota do portal para o cliente trocar o endereco de entrega de um pedido
barrado pela area (POST /api/envios/<id>/acao, tipo alterar_endereco).

    py -3.11 -m unittest portal_cliente.test_alterar_endereco_bloqueado_api
"""
import builtins
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

import auth_cliente as auth  # noqa: E402

DONO, OUTRO = "54993021000184", "22310186000118"
HTTPS = "https://localhost"
CEP_OK = {"cep": "01310-100", "logradouro": "Avenida Paulista", "bairro": "Bela Vista", "cidade": "São Paulo", "uf": "SP", "ibge": "3550308"}
NFE = {"numero_nf": "10", "destinatario_doc": "09014480000467", "destinatario_nome": "Cliente X",
       "destinatario_endereco": "Rua A, 1", "destinatario_bairro": "Centro", "destinatario_municipio": "Curitiba",
       "destinatario_uf": "PR", "destinatario_cep": "80010000", "transportadora_nome": "", "transportadora_cnpj": ""}
CORPO = {"tipo": "alterar_endereco", "endereco": {"cep": "01310-100", "numero": "1000"}}


class AlterarEnderecoBloqueadoApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        real_open = builtins.open

        def abrir(arq, *a, **k):  # worktree sem config.yaml (segredo): so o import do app ve um de teste
            if str(arq).endswith("config.yaml") and not Path(arq).exists():
                return io.StringIO("portal_cliente: {secret_key: teste-teste-teste-teste-teste-teste-1234}")
            return real_open(arq, *a, **k)

        try:
            with mock.patch.object(builtins, "open", abrir):
                import app as portal
        except Exception as e:  # noqa: BLE001
            raise unittest.SkipTest(f"app do portal nao importa neste ambiente ({e})")
        cls.portal = portal

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        db = Path(self._tmp.name) / "dados.db"
        import bloqueio_area
        self.aviso = mock.Mock(return_value=True)
        self.area = mock.Mock(return_value=[])
        self._patches = [
            mock.patch.object(auth, "DB_PATH", db),
            mock.patch.object(self.portal.envios, "DB_PATH", db),
            mock.patch.dict(self.portal._CONFIG, {"portal_cliente": {"secret_key": "x", "envios": {"endereco_entrega_ativo": True}}}),
            mock.patch.object(self.portal.envios, "caminho_xml", return_value=mock.Mock(read_bytes=lambda: b"")),
            mock.patch.object(self.portal.envios, "ler_nfe", return_value=dict(NFE)),
            mock.patch.object(self.portal.envios, "_identificar_area", self.area),
            mock.patch("cotacao.consultar_cep", return_value=dict(CEP_OK)),
            mock.patch.object(bloqueio_area, "avisar_endereco_alterado", self.aviso),
        ]
        for p in self._patches:
            p.start()
        self.conn = auth.conectar()
        self.conn.execute("CREATE TABLE interno (cnpj_embarcador TEXT, sender_id INTEGER, nome_remetente TEXT, "
                          "apelido TEXT, email TEXT, stkkc_id INTEGER)")
        self.conn.executemany("INSERT INTO interno VALUES (?, ?, ?, NULL, ?, 1)",
                              [(DONO, 100, "MARCHEF", "a@b.c"), (OUTRO, 200, "VIDAVEG", "a@b.c")])
        self.conn.commit()
        env = self.portal.envios.conectar()
        env.execute("INSERT INTO portal_envios (id, cnpj_embarcador, chave_nfe, numero_nf, destinatario_nome, destinatario_municipio, "
                    "destinatario_uf, xml_path, status, bloqueio_motivo, bloqueio_chamado_id, origem, criado_em, atualizado_em) "
                    "VALUES (1, ?, 'c1', '10', 'Cliente X', 'Curitiba', 'PR', 'a.xml', 'AGUARDANDO_LIBERACAO', 'fora_sp', 77, 'xml', "
                    "datetime('now', 'localtime'), datetime('now', 'localtime'))", (DONO,))
        env.commit()
        env.close()

    def tearDown(self):
        self.conn.close()
        for p in reversed(self._patches):
            p.stop()
        self._tmp.cleanup()

    def _logar(self, cnpj):
        conta = auth.definir_pin(self.conn, cnpj, "123456")
        cli = self.portal.app.test_client()
        with cli.session_transaction(base_url=HTTPS) as s:
            s["cnpj"], s["v"] = cnpj, auth.versao_conta(conta)
        return cli

    def _linha(self):
        env = self.portal.envios.conectar()
        try:
            return dict(env.execute("SELECT * FROM portal_envios WHERE id = 1").fetchone())
        finally:
            env.close()

    def test_lista_oferece_o_botao(self):
        r = self._logar(DONO).get("/api/envios", base_url=HTTPS)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        j = r.get_json()
        self.assertTrue(j["endereco_entrega_ativo"])
        self.assertTrue(j["envios"][0]["pode_alterar_endereco"])

    def test_endereco_atendido_solta_pra_fila_e_avisa_o_chamado(self):
        r = self._logar(DONO).post("/api/envios/1/acao", json=CORPO, base_url=HTTPS)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        j = r.get_json()
        self.assertTrue(j["liberado"])
        linha = self._linha()
        self.assertEqual((linha["status"], linha["destinatario_municipio"], linha["endereco_entrega_origem"]),
                         ("NA_FILA", "São Paulo", "cliente"))
        # a area foi conferida com o endereco novo
        self.assertIn("São Paulo - SP", self.area.call_args[0][0][0]["address"])
        envio, resultado, _por = self.aviso.call_args[0]
        self.assertEqual(envio["bloqueio_chamado_id"], 77)
        self.assertTrue(resultado["liberado"])

    def test_endereco_ainda_fora_continua_aguardando(self):
        self.area.return_value = [({"id": 0}, "sp_nao_atendido")]
        r = self._logar(DONO).post("/api/envios/1/acao", json=CORPO, base_url=HTTPS)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertFalse(r.get_json()["liberado"])
        self.assertEqual(self._linha()["status"], "AGUARDANDO_LIBERACAO")

    def test_outro_embarcador_nao_alcanca(self):
        r = self._logar(OUTRO).post("/api/envios/1/acao", json=CORPO, base_url=HTTPS)
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self._linha()["status"], "AGUARDANDO_LIBERACAO")
        self.aviso.assert_not_called()

    def test_endereco_que_nao_e_objeto_vira_erro_do_cliente(self):
        r = self._logar(DONO).post("/api/envios/1/acao", json={"tipo": "alterar_endereco", "endereco": "x"}, base_url=HTTPS)
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertEqual(self._linha()["status"], "AGUARDANDO_LIBERACAO")


if __name__ == "__main__":
    unittest.main()
