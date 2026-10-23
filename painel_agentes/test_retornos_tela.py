# -*- coding: utf-8 -*-
"""
Tela /retornos (recebimento de retornos no galpao, Hugo 23/10/2026): rotas,
niveis, upload de fotos/assinatura e alerta.

    py -3.11 -m unittest test_retornos_tela   (de dentro de painel_agentes/)
"""
import io
import json
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

from nucleo import banco as nucleo_banco  # noqa: E402
import grupos_equipe  # noqa: E402
import retornos  # noqa: E402

import importlib.util  # noqa: E402
_spec = importlib.util.spec_from_file_location("painel_agentes_app", _AQUI / "painel_agentes.py")
painel_agentes = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = painel_agentes
_spec.loader.exec_module(painel_agentes)

ORIGEM = {"Origin": "http://localhost"}
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 200
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 200


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "dados.db"
        self.pasta = Path(self._tmp.name) / "retornos"
        for alvo, valor in ((retornos, ("DB_PATH", self.db)), (retornos, ("PASTA_ARQUIVOS", self.pasta)),
                            (grupos_equipe, ("DB_PATH", self.db)), (nucleo_banco, ("DB_PATH", self.db))):
            p = mock.patch.object(alvo, *valor)
            p.start()
            self.addCleanup(p.stop)
        self.alertas = []
        p = mock.patch.object(retornos, "postar_alerta", lambda conn, texto: self.alertas.append(texto))
        p.start()
        self.addCleanup(p.stop)

        conn = nucleo_banco.conectar(self.db)
        rid = conn.execute("INSERT INTO nucleo_rotas (data_rota, nome, motorista_nome, motorista_cpf) "
                           "VALUES (?, 'Rota A', 'João', '111')", (date.today().isoformat(),)).lastrowid
        hoje = f"{date.today().isoformat()} 15:00:00"
        self.p1 = conn.execute("INSERT INTO nucleo_paradas (rota_id, ordem, service_id, codigo, remetente_nome, "
                               "volume_caixas, situacao, motivo_texto, completed_at) "
                               "VALUES (?, 1, 501, 'PS-1', 'EMB A', 5, 'INSUCESSO', 'Cliente ausente', ?)",
                               (rid, hoje)).lastrowid
        self.p2 = conn.execute("INSERT INTO nucleo_paradas (rota_id, ordem, service_id, codigo, remetente_nome, "
                               "volume_caixas, situacao, motivo_texto, completed_at) "
                               "VALUES (?, 2, 502, 'PS-2', 'EMB A', 2, 'INSUCESSO', 'Local fechado', ?)",
                               (rid, hoje)).lastrowid
        conn.commit()
        conn.close()
        painel_agentes.app.config["TESTING"] = True
        self.cliente = painel_agentes.app.test_client()

    def logar(self, nivel):
        with self.cliente.session_transaction() as s:
            s["nivel_acesso"] = nivel
            s["usuario"] = "teste"

    def enviar(self, itens, *, foto_carga=True, assinatura=True, extras=None, recebedor="Carlos"):
        data = {"dados": json.dumps({"recebedor": recebedor, "itens": itens})}
        if foto_carga:
            data["foto_carga"] = [(io.BytesIO(JPEG), "carga.jpg")]
        if assinatura:
            data["assinatura"] = (io.BytesIO(PNG), "assinatura.png")
        data.update(extras or {})
        return self.cliente.post("/api/retornos/descarga", data=data, content_type="multipart/form-data",
                                 headers=ORIGEM)

    def item(self, parada_id, caixas, **kw):
        return {"parada_id": parada_id, "caixas_recebidas": caixas, "avaria": False, "motivo": "", "observacao": "",
                **kw}

    def contar(self, tabela):
        conn = retornos.conectar(self.db)
        n = conn.execute(f"SELECT COUNT(*) FROM {tabela}").fetchone()[0]
        conn.close()
        return n


class Tela(Base):
    def test_galpao_abre_retornos(self):
        self.logar("galpao")
        r = self.cliente.get("/retornos")
        self.assertEqual(r.status_code, 200)
        self.assertIn("PODE_CONFIRMAR = true", r.get_data(as_text=True))
        dados = self.cliente.get("/api/retornos").get_json()
        self.assertEqual([p["codigo"] for p in dados["motoristas"][0]["pedidos"]], ["PS-1", "PS-2"])
        self.assertIn("Local fechado", dados["motivos"])

    def test_atendimento_ve_sem_confirmar(self):
        self.logar("atendimento")
        r = self.cliente.get("/retornos")
        self.assertEqual(r.status_code, 200)
        self.assertIn("PODE_CONFIRMAR = false", r.get_data(as_text=True))

    def test_sem_login_vai_pro_login(self):
        r = self.cliente.get("/retornos")
        self.assertEqual(r.status_code, 302)
        self.assertIn("login", r.headers["Location"])

    def test_atendimento_nao_confirma(self):
        self.logar("atendimento")
        self.assertEqual(self.enviar([self.item(self.p1, 5)]).status_code, 403)

    def test_link_no_app_do_galpao(self):
        self.logar("galpao")
        self.assertIn("/retornos", self.cliente.get("/wms").get_data(as_text=True))


class Descarga(Base):
    def test_descarga_ok_grava_e_alerta(self):
        self.logar("galpao")
        r = self.enviar([self.item(self.p1, 3)])
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        descarga_id = r.get_json()["descarga_id"]
        arquivos = sorted(x.name for x in (self.pasta / str(descarga_id)).iterdir())
        self.assertEqual(len(arquivos), 2)
        self.assertEqual(len(self.alertas), 1)
        self.assertIn("PS-1", self.alertas[0])
        det = self.cliente.get(f"/retornos/descarga/{descarga_id}").get_json()
        foto = det["fotos_carga"][0]
        self.assertEqual(self.cliente.get(f"/retornos/arquivo/{descarga_id}/{foto}").data, JPEG)

    def test_descarga_sem_divergencia_nao_alerta(self):
        self.logar("operador")
        r = self.enviar([self.item(self.p1, 5), self.item(self.p2, 2)])
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.alertas, [])
        self.assertEqual(self.cliente.get("/api/retornos").get_json()["motoristas"], [])

    def test_avaria_exige_foto_e_guarda(self):
        self.logar("galpao")
        r = self.enviar([self.item(self.p1, 5, avaria=True)])
        self.assertEqual(r.status_code, 400)
        r = self.enviar([self.item(self.p1, 5, avaria=True)],
                        extras={f"foto_avaria_{self.p1}": [(io.BytesIO(JPEG), "av.jpg")]})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        det = self.cliente.get(f"/retornos/descarga/{r.get_json()['descarga_id']}").get_json()
        self.assertEqual(len(det["itens"][0]["fotos_avaria"]), 1)

    def test_descarga_sem_foto_carga_400(self):
        self.logar("galpao")
        r = self.enviar([self.item(self.p1, 5)], foto_carga=False)
        self.assertEqual(r.status_code, 400)
        self.assertIn("foto da carga", r.get_json()["erro"])
        self.assertEqual(self.contar("retornos_recebidos"), 0)
        self.assertEqual(self.contar("retornos_descargas"), 0)
        self.assertEqual(list(self.pasta.iterdir()) if self.pasta.exists() else [], [])

    def test_descarga_recusa_arquivo_grande(self):
        self.logar("galpao")
        grande = b"\xff\xd8" + b"0" * (10 * 1024 * 1024 + 1)
        r = self.enviar([self.item(self.p1, 5)], foto_carga=False,
                        extras={"foto_carga": [(io.BytesIO(grande), "carga.jpg")]})
        self.assertEqual(r.status_code, 400)
        self.assertIn("Foto muito grande", r.get_json()["erro"])

    def test_recusa_arquivo_que_nao_e_imagem(self):
        self.logar("galpao")
        r = self.enviar([self.item(self.p1, 5)], foto_carga=False,
                        extras={"foto_carga": [(io.BytesIO(b"<html>"), "carga.html")]})
        self.assertEqual(r.status_code, 400)

    def test_arquivo_nao_sai_da_pasta(self):
        self.logar("galpao")
        self.assertEqual(self.cliente.get("/retornos/arquivo/1/..%2Fdados.db").status_code, 404)
        self.assertEqual(self.cliente.get("/retornos/arquivo/1/nada.jpg").status_code, 404)

    def test_falha_gcs_nao_impede(self):
        self.logar("galpao")
        cfg = {"painel_agentes": {"secret_key": "x", "usuario": "teste", "senha": "teste"},
               "gcs": {"bucket_name": "b", "credenciais_json": "c.json"}}
        with mock.patch.object(painel_agentes, "_carregar_config", return_value=cfg), \
                mock.patch("documentos_pedido.storage_gcs.enviar_documento", side_effect=RuntimeError("fora")):
            r = self.enviar([self.item(self.p1, 5)])
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def test_historico(self):
        self.logar("galpao")
        self.enviar([self.item(self.p1, 5)])
        self.logar("leitura")
        lista = self.cliente.get("/api/retornos/descargas").get_json()["descargas"]
        self.assertEqual(len(lista), 1)
        self.assertEqual(lista[0]["recebedor"], "Carlos")


class NoGalpao(Base):
    def test_torre_badge_no_galpao(self):
        self.logar("galpao")
        self.enviar([self.item(self.p1, 3), self.item(self.p2, 2)])
        ins = [{"service_id": 501}, {"service_id": 502}, {"service_id": 999}]
        painel_agentes.torre_controle._badges_insucessos(ins)
        textos = [[b["texto"] for b in i["badges"]] for i in ins]
        self.assertTrue(any(t.startswith("No galpão") and "divergência" in t for t in textos[0]), textos[0])
        self.assertTrue(any(t.startswith("No galpão") and "divergência" not in t for t in textos[1]), textos[1])
        self.assertEqual(textos[2], [])

    def test_torre_sem_tabela_de_retornos_nao_quebra(self):
        ins = [{"service_id": 501}]
        with mock.patch.object(retornos, "conectar", side_effect=RuntimeError("sem banco")):
            painel_agentes.torre_controle._badges_insucessos(ins)
        self.assertFalse(any(b["texto"].startswith("No galpão") for b in ins[0]["badges"]))

    def test_consulta_mostra_no_galpao(self):
        self.logar("galpao")
        self.enviar([self.item(self.p1, 3)])
        self.logar("operador")
        detalhe = {"codigo": "PS-1", "pedido": None, "paradas": [], "eventos": [], "comprovantes": {}}
        with mock.patch("nucleo.consulta.detalhar_pedido", return_value=detalhe):
            html = self.cliente.get("/consulta/pedido/PS-1").get_data(as_text=True)
        self.assertIn("No galpão", html)
        self.assertIn("Carlos", html)
        self.assertIn("3 de 5", html)


if __name__ == "__main__":
    unittest.main()
