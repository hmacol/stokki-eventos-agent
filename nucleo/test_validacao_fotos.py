# -*- coding: utf-8 -*-
"""
test_validacao_fotos.py

Validação automática das fotos do app (nucleo/validacao_fotos.py, Hugo
12/09): nitidez, número da NF no canhoto, recibo de pedágio, regra das
tentativas, o endpoint POST /api/fotos/validar e a revisão humana no
painel. O modelo NUNCA é chamado de verdade (patch em _chamar_modelo) --
os testes rodam sem rede e sem custo.
    python -m unittest nucleo.test_validacao_fotos -v
"""
import io
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from PIL import Image, ImageDraw, ImageFilter

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))

from nucleo import api_motorista, auth_motorista as auth, banco, operacao, rotas, validacao_fotos as vf

CPF = "12345678901"
PIN = "123456"
AGENT = 4242


def _imagem(nitida=True) -> bytes:
    """Uma "foto de canhoto": texto preto em fundo branco. `nitida=False`
    aplica o desfoque que a calibração usou como piso (raio 3)."""
    im = Image.new("RGB", (1200, 900), "white")
    d = ImageDraw.Draw(im)
    for y in range(60, 840, 24):
        d.text((40, y), "NOTA FISCAL 000012345 RECEBIDO POR MARIA DA SILVA RG 12.345.678-9", fill="black")
        d.line([(30, y + 18), (1170, y + 18)], fill="black", width=1)
    if not nitida:
        im = im.filter(ImageFilter.GaussianBlur(3))
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=70)
    return buf.getvalue()


def _cfg(**kw) -> dict:
    base = {"api_motorista": {"validacao_fotos": {"ativo": True, **kw}}, "anthropic": {"api_key": "sk-teste"}}
    return vf.config_validacao(base)


def _leitura_canhoto(**kw) -> dict:
    return {"legivel": True, "documento": "CANHOTO_NF", "numeros_nf": ["000012345"],
            "assinatura_ou_identificacao": True, "problemas": [], "motivo": "Canhoto legível.", **kw}


def _leitura_pedagio(**kw) -> dict:
    return {"legivel": True, "eh_recibo_pedagio": True, "valor_total": 12.5,
            "praca_ou_concessionaria": "Praça X", "data": "2026-09-12", "problemas": [], "motivo": "Recibo legível.", **kw}


def _modelo(leitura):
    """Substitui a chamada de visão. Devolve (leitura, tokens)."""
    return mock.patch.object(vf, "_chamar_modelo", return_value=(leitura, {"entrada": 2200, "saida": 120}))


class TestNitidez(unittest.TestCase):
    def test_desligada_nao_chama_nada(self):
        cfg = vf.config_validacao({})            # sem config = desligada
        self.assertFalse(cfg["ativo"])
        with mock.patch.object(vf, "_chamar_modelo", side_effect=AssertionError("não devia chamar o modelo")):
            r = vf.validar_foto(cfg, "CANHOTO", _imagem())
        self.assertEqual(r["resultado"], vf.NAO_VERIFICADO)

    def test_foto_tremida_reprova_sem_gastar_modelo(self):
        borrada, nitida = _imagem(nitida=False), _imagem()
        self.assertLess(vf.medir_nitidez(borrada), vf.NITIDEZ_MINIMA_PADRAO)
        self.assertGreater(vf.medir_nitidez(nitida), vf.NITIDEZ_MINIMA_PADRAO)
        with mock.patch.object(vf, "_chamar_modelo", side_effect=AssertionError("não devia chamar o modelo")):
            r = vf.validar_foto(_cfg(), "CANHOTO", borrada)
        self.assertEqual(r["resultado"], vf.REPROVADO)
        self.assertIn("DESFOCADA", r["problemas"])
        self.assertIn("tremida", r["motivo"])

    def test_arquivo_que_nao_e_imagem(self):
        r = vf.validar_foto(_cfg(), "CANHOTO", b"isto nao e uma imagem")
        self.assertEqual(r["resultado"], vf.NAO_VERIFICADO)

    def test_reduz_antes_de_mandar_pro_modelo(self):
        dados, media = vf.preparar_para_modelo(_imagem())
        self.assertEqual(media, "image/jpeg")
        self.assertLessEqual(max(Image.open(io.BytesIO(dados)).size), vf.LADO_MAX_MODELO)

    def test_falha_do_modelo_nao_trava_o_motorista(self):
        with mock.patch.object(vf, "_chamar_modelo", side_effect=RuntimeError("sem rede")):
            r = vf.validar_foto(_cfg(), "CANHOTO", _imagem())
        self.assertEqual(r["resultado"], vf.NAO_VERIFICADO)

    def test_sem_chave_do_modelo(self):
        cfg = vf.config_validacao({"api_motorista": {"validacao_fotos": {"ativo": True}}})
        self.assertEqual(vf.validar_foto(cfg, "CANHOTO", _imagem())["resultado"], vf.NAO_VERIFICADO)


class TestCanhotoNF(unittest.TestCase):
    def test_nf_confere(self):
        with _modelo(_leitura_canhoto()):
            r = vf.validar_foto(_cfg(), "CANHOTO", _imagem(), nf_alvo="12345")
        self.assertEqual(r["resultado"], vf.APROVADO)
        self.assertTrue(r["nf_confere"])
        self.assertEqual(r["nf_lidas"], ["12345"])     # zeros à esquerda somem

    def test_nf_da_foto_e_de_outro_pedido(self):
        with _modelo(_leitura_canhoto(numeros_nf=["99999"])):
            r = vf.validar_foto(_cfg(), "CANHOTO", _imagem(), nf_alvo="12345")
        self.assertEqual(r["resultado"], vf.REPROVADO)
        self.assertIn("99999", r["motivo"])
        self.assertIn("12345", r["motivo"])

    def test_nf_ilegivel_reprova(self):
        with _modelo(_leitura_canhoto(numeros_nf=[])):
            r = vf.validar_foto(_cfg(), "CANHOTO", _imagem(), nf_alvo="12345")
        self.assertEqual(r["resultado"], vf.REPROVADO)
        self.assertIn("número da NF", r["motivo"])

    def test_serie_junto_do_numero(self):
        # Impresso "1-12345" (série + número): termina com o esperado -> vale.
        with _modelo(_leitura_canhoto(numeros_nf=["112345"])):
            self.assertEqual(vf.validar_foto(_cfg(), "CANHOTO", _imagem(), nf_alvo="12345")["resultado"], vf.APROVADO)

    def test_pedido_sem_nf_conhecida_nao_cobra_numero(self):
        with _modelo(_leitura_canhoto(numeros_nf=[])):
            r = vf.validar_foto(_cfg(), "CANHOTO", _imagem(), nfs_esperadas=[], nf_alvo=None)
        self.assertEqual(r["resultado"], vf.APROVADO)
        self.assertIsNone(r["nf_confere"])

    def test_qualquer_nf_do_pedido_quando_o_app_nao_diz_qual(self):
        with _modelo(_leitura_canhoto(numeros_nf=["777"])):
            r = vf.validar_foto(_cfg(), "CANHOTO", _imagem(), nfs_esperadas=["12345", "777"])
        self.assertEqual((r["resultado"], r["nf_batidas"]), (vf.APROVADO, ["777"]))

    def test_foto_de_outra_coisa(self):
        with _modelo(_leitura_canhoto(documento="NAO_E_DOCUMENTO", motivo="A foto mostra uma caixa.")):
            r = vf.validar_foto(_cfg(), "CANHOTO", _imagem(), nf_alvo="12345")
        self.assertEqual((r["resultado"], r["motivo"]), (vf.REPROVADO, "A foto mostra uma caixa."))

    def test_ilegivel_pelo_modelo(self):
        with _modelo(_leitura_canhoto(legivel=False, problemas=["REFLEXO"], motivo="Reflexo cobre o texto.")):
            self.assertEqual(vf.validar_foto(_cfg(), "CANHOTO", _imagem(), nf_alvo="12345")["resultado"], vf.REPROVADO)

    def test_assinatura_e_aviso_por_padrao_e_trava_se_exigida(self):
        with _modelo(_leitura_canhoto(assinatura_ou_identificacao=False)):
            r = vf.validar_foto(_cfg(), "CANHOTO", _imagem(), nf_alvo="12345")
        self.assertEqual(r["resultado"], vf.APROVADO)
        self.assertIn("SEM_ASSINATURA", r["avisos"])
        with _modelo(_leitura_canhoto(assinatura_ou_identificacao=False)):
            r = vf.validar_foto(_cfg(exigir_assinatura=True), "CANHOTO", _imagem(), nf_alvo="12345")
        self.assertEqual(r["resultado"], vf.REPROVADO)

    def test_exigir_nf_desligado(self):
        with _modelo(_leitura_canhoto(numeros_nf=[])):
            r = vf.validar_foto(_cfg(exigir_nf=False), "CANHOTO", _imagem(), nf_alvo="12345")
        self.assertEqual(r["resultado"], vf.APROVADO)

    def test_recusa_do_modelo_nao_reprova(self):
        with mock.patch.object(vf, "_chamar_modelo", return_value=(None, {})):
            r = vf.validar_foto(_cfg(), "CANHOTO", _imagem(), nf_alvo="12345")
        self.assertEqual(r["resultado"], vf.NAO_VERIFICADO)


class TestPedagio(unittest.TestCase):
    def test_recibo_ok(self):
        with _modelo(_leitura_pedagio()):
            r = vf.validar_foto(_cfg(), "PEDAGIO", _imagem(), valor_informado=12.5)
        self.assertEqual((r["resultado"], r["valor_lido"], r["avisos"]), (vf.APROVADO, 12.5, []))

    def test_valor_diverge_e_so_aviso(self):
        with _modelo(_leitura_pedagio(valor_total=12.5)):
            r = vf.validar_foto(_cfg(), "PEDAGIO", _imagem(), valor_informado=125.0)
        self.assertEqual(r["resultado"], vf.APROVADO)
        self.assertIn("VALOR_DIVERGE", r["avisos"])

    def test_nao_e_recibo(self):
        with _modelo(_leitura_pedagio(eh_recibo_pedagio=False, motivo="Isso é uma nota de posto.")):
            r = vf.validar_foto(_cfg(), "PEDAGIO", _imagem(), valor_informado=12.5)
        self.assertEqual((r["resultado"], r["motivo"]), (vf.REPROVADO, "Isso é uma nota de posto."))


class TestTentativas(unittest.TestCase):
    def test_reprovado_trava_ate_o_limite(self):
        cfg = _cfg(max_tentativas=2)
        base = {"resultado": vf.REPROVADO, "motivo": "tremida"}
        p1 = vf.aplicar_tentativas(cfg, base, 1)
        self.assertEqual((p1["pode_seguir"], p1["tentativas_restantes"], p1["revisao_humana"]), (False, 1, False))
        p2 = vf.aplicar_tentativas(cfg, base, 2)
        self.assertEqual((p2["pode_seguir"], p2["tentativas_restantes"], p2["revisao_humana"]), (True, 0, True))

    def test_aprovado_e_nao_verificado_nunca_travam(self):
        cfg = _cfg(max_tentativas=2)
        for res in (vf.APROVADO, vf.NAO_VERIFICADO):
            r = vf.aplicar_tentativas(cfg, {"resultado": res}, 1)
            self.assertTrue(r["pode_seguir"])
            self.assertFalse(r["revisao_humana"])


class TestNfsDoPedido(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._patch = mock.patch.object(banco, "DB_PATH", Path(self._tmp.name) / "t.db")
        self._patch.start()
        self.conn = banco.conectar()
        self.conn.execute("""CREATE TABLE documentos_processados (hash_conteudo TEXT PRIMARY KEY, tipo TEXT,
                             codigo_pedido TEXT, numero_nf TEXT)""")

    def tearDown(self):
        self.conn.close()
        self._patch.stop()
        self._tmp.cleanup()

    def _doc(self, h, codigo, nf, tipo="Nota Fiscal"):
        self.conn.execute("INSERT INTO documentos_processados VALUES (?, ?, ?, ?)", (h, tipo, codigo, nf))

    def test_nf_do_pedido_e_normalizacao(self):
        self._doc("h1", "PS-1", "000012345")
        self._doc("h2", "PS-1", "12345")          # mesma nota, outro documento
        self._doc("h3", "PS-1", None, tipo="Boleto")
        self.assertEqual(vf.nfs_do_pedido(self.conn, "PS-1"), ["12345"])

    def test_duas_notas_no_mesmo_pedido(self):
        self._doc("h1", "PS-2", "111")
        self._doc("h2", "PS-2", "222")
        self.assertEqual(sorted(vf.nfs_do_pedido(self.conn, "PS-2")), ["111", "222"])

    def test_nf_compartilhada_entre_pedidos_e_ignorada(self):
        # Achado 12/09 nos dados reais: '245699' aparece em 11 pedidos --
        # é leitura errada do extrator, não a nota da entrega.
        self._doc("h1", "PS-3", "5482")
        self._doc("h2", "PS-3", "245699")
        self._doc("h3", "PS-4", "245699")
        self.assertEqual(vf.nfs_do_pedido(self.conn, "PS-3"), ["5482"])

    def test_sem_tabela_ou_sem_codigo(self):
        self.assertEqual(vf.nfs_do_pedido(self.conn, None), [])
        self.assertEqual(vf.nfs_do_pedido(self.conn, "PS-INEXISTENTE"), [])


class TestApiValidacao(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        raiz = Path(self._tmp.name)
        self._patch_db = mock.patch.object(banco, "DB_PATH", raiz / "t.db")
        self._patch_db.start()
        self.app = api_motorista.criar_app({
            "api_motorista": {"secret_key": "segredo-de-teste", "pasta_comprovantes": str(raiz / "fotos"),
                              "gcs_ativo": False, "validacao_fotos": {"ativo": True, "max_tentativas": 2}},
            "anthropic": {"api_key": "sk-teste"},
        })
        self.cli = self.app.test_client()
        conn = banco.conectar()
        conn.execute("""CREATE TABLE documentos_processados (hash_conteudo TEXT PRIMARY KEY, tipo TEXT,
                        codigo_pedido TEXT, numero_nf TEXT)""")
        conn.execute("INSERT INTO documentos_processados VALUES ('h1', 'Nota Fiscal', 'PS-A', '12345')")
        conn.commit()
        auth.criar_ou_atualizar_motorista(conn, CPF, "Motorista Teste", PIN, agent_id=AGENT, tipo_veiculo="VAN_HR")
        conn.close()
        r = self.cli.post("/api/login", json={"cpf": CPF, "pin": PIN})
        self.h = {"Authorization": f"Bearer {r.get_json()['acesso']}"}
        self.rota_id = self._rota()
        self.cli.post(f"/api/rotas/{self.rota_id}/aceitar", json={}, headers=self.h)
        rota = self.cli.post(f"/api/rotas/{self.rota_id}/iniciar", json={}, headers=self.h).get_json()
        self.parada_id = rota["paradas"][0]["id"]

    def tearDown(self):
        self._patch_db.stop()
        self._tmp.cleanup()

    def _rota(self):
        conn = banco.conectar()
        rota_id = rotas.materializar_rascunho({
            "id": 1, "data_alvo": date.today().isoformat(), "lote_id": "L", "nome": "R", "agent_id": AGENT,
            "vehicle_id": None, "motorista_nome": "Teste", "tipo_veiculo": None, "start_location_base_id": 1,
            "end_location_base_id": 1, "start_at": f"{date.today().isoformat()} 07:00:00", "km_estimado": 10.0,
            "status": "RASCUNHO", "vuupt_route_id": None,
            "paradas": [{"ordem": 1, "service_id": None, "codigo": "PS-A", "titulo": "A", "endereco": "Rua A",
                         "latitude": -23.5, "longitude": -46.6, "nivel_dificuldade": 1, "volume_caixas": 1}],
        }, banco.PROVEDOR_APP, conn=conn)
        conn.close()
        return rota_id

    def _validar(self, conteudo, **campos):
        dados = {"arquivo": (io.BytesIO(conteudo), "foto.jpg"), **{k: str(v) for k, v in campos.items()}}
        return self.cli.post("/api/fotos/validar", headers=self.h, data=dados, content_type="multipart/form-data")

    def test_checklist_anuncia_a_validacao(self):
        c = self.cli.get("/api/checklist", headers=self.h).get_json()
        self.assertEqual(c["validacao_fotos"], {"ativo": True, "max_tentativas": 2, "exigir_nf": True})

    def test_nf_da_parada_vai_pro_app(self):
        rota = self.cli.get(f"/api/rotas/{self.rota_id}", headers=self.h).get_json()
        self.assertEqual(rota["paradas"][0]["nfs"], ["12345"])

    def test_validar_aprova_e_o_envio_reaproveita_sem_chamar_de_novo(self):
        foto = _imagem()
        with _modelo(_leitura_canhoto()) as m:
            r = self._validar(foto, tipo="CANHOTO", parada_id=self.parada_id, nf="12345", tentativa=1)
            self.assertEqual(r.status_code, 200, r.get_json())
            self.assertEqual(r.get_json()["resultado"], vf.APROVADO)
            self.assertTrue(r.get_json()["pode_seguir"])
            self.assertEqual(m.call_count, 1)

            # A MESMA foto chega pela fila: usa o resultado guardado por sha256
            env = self.cli.post(f"/api/paradas/{self.parada_id}/comprovantes", headers=self.h,
                                data={"arquivo": (io.BytesIO(foto), "foto.jpg"), "tipo": "CANHOTO", "uuid": "c1", "nf": "12345"},
                                content_type="multipart/form-data")
            self.assertEqual(env.status_code, 201, env.get_json())
            self.assertEqual(env.get_json()["validacao"], vf.APROVADO)
            self.assertEqual(m.call_count, 1)   # não pagou o modelo de novo

        conn = banco.conectar()
        row = conn.execute("SELECT validado_por, resultado_validacao, dados_json FROM nucleo_comprovantes WHERE uuid = 'c1'").fetchone()
        conn.close()
        self.assertEqual((row["validado_por"], row["resultado_validacao"]), ("IA", vf.APROVADO))
        self.assertIn("12345", row["dados_json"])

    def test_reprovado_trava_e_depois_do_limite_libera(self):
        with _modelo(_leitura_canhoto(numeros_nf=["99999"])):
            r1 = self._validar(_imagem(), tipo="CANHOTO", parada_id=self.parada_id, nf="12345", tentativa=1).get_json()
            self.assertEqual((r1["resultado"], r1["pode_seguir"]), (vf.REPROVADO, False))
            # 2ª foto (outro conteúdo, senão cai no cache do sha)
            r2 = self._validar(_imagem(nitida=True) + b"\x00", tipo="CANHOTO", parada_id=self.parada_id,
                               nf="12345", tentativa=2).get_json()
        self.assertEqual((r2["resultado"], r2["pode_seguir"], r2["revisao_humana"]), (vf.REPROVADO, True, True))

    def test_foto_que_chega_sem_conferencia_previa_e_conferida_no_envio(self):
        with _modelo(_leitura_canhoto()) as m:
            env = self.cli.post(f"/api/paradas/{self.parada_id}/comprovantes", headers=self.h,
                                data={"arquivo": (io.BytesIO(_imagem()), "f.jpg"), "tipo": "CANHOTO", "uuid": "c2"},
                                content_type="multipart/form-data")
        self.assertEqual((env.status_code, m.call_count), (201, 1))
        conn = banco.conectar()
        v = conn.execute("SELECT no_ato FROM nucleo_validacoes_foto").fetchone()
        conn.close()
        self.assertEqual(v["no_ato"], 0)      # não foi conferida com o motorista no cliente

    def test_so_canhoto_e_conferido_no_envio(self):
        with mock.patch.object(vf, "_chamar_modelo", side_effect=AssertionError("PRODUTO não se confere")):
            r = self.cli.post(f"/api/paradas/{self.parada_id}/comprovantes", headers=self.h,
                              data={"arquivo": (io.BytesIO(_imagem()), "f.jpg"), "tipo": "PRODUTO", "uuid": "c3"},
                              content_type="multipart/form-data")
        self.assertEqual(r.status_code, 201, r.get_json())
        self.assertIsNone(r.get_json()["validacao"])

    def test_pedagio_guarda_o_veredito(self):
        with _modelo(_leitura_pedagio(valor_total=99.0)):
            r = self.cli.post(f"/api/rotas/{self.rota_id}/pedagios", headers=self.h,
                              data={"arquivo": (io.BytesIO(_imagem()), "r.jpg"), "valor": "12,50", "uuid": "p1"},
                              content_type="multipart/form-data")
        self.assertEqual(r.status_code, 201, r.get_json())
        self.assertEqual(r.get_json()["pedagios"][0]["validacao"], vf.APROVADO)
        conn = banco.conectar()
        dados = conn.execute("SELECT dados_json FROM nucleo_pedagios WHERE uuid = 'p1'").fetchone()[0]
        conn.close()
        self.assertIn("VALOR_DIVERGE", dados)   # recibo de R$ 99 x R$ 12,50 digitados

    def test_parada_de_outro_motorista(self):
        r = self._validar(_imagem(), tipo="CANHOTO", parada_id=999999, tentativa=1)
        self.assertEqual(r.status_code, 404)

    def test_extensao_recusada(self):
        r = self.cli.post("/api/fotos/validar", headers=self.h,
                          data={"arquivo": (io.BytesIO(b"x"), "foto.exe"), "tipo": "CANHOTO", "tentativa": "1"},
                          content_type="multipart/form-data")
        self.assertEqual(r.status_code, 400)


class TestApiValidacaoDesligada(unittest.TestCase):
    """Com o config padrão (desligada) o app tem que se comportar como antes."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        raiz = Path(self._tmp.name)
        self._patch_db = mock.patch.object(banco, "DB_PATH", raiz / "t.db")
        self._patch_db.start()
        self.app = api_motorista.criar_app({
            "api_motorista": {"secret_key": "s", "pasta_comprovantes": str(raiz / "fotos"), "gcs_ativo": False},
        })
        self.cli = self.app.test_client()
        conn = banco.conectar()
        auth.criar_ou_atualizar_motorista(conn, CPF, "Motorista Teste", PIN, agent_id=AGENT)
        conn.close()
        self.h = {"Authorization": f"Bearer {self.cli.post('/api/login', json={'cpf': CPF, 'pin': PIN}).get_json()['acesso']}"}

    def tearDown(self):
        self._patch_db.stop()
        self._tmp.cleanup()

    def test_checklist_diz_que_esta_desligada(self):
        self.assertFalse(self.cli.get("/api/checklist", headers=self.h).get_json()["validacao_fotos"]["ativo"])

    def test_endpoint_responde_sem_travar_e_sem_modelo(self):
        with mock.patch.object(vf, "_chamar_modelo", side_effect=AssertionError("desligada")):
            r = self.cli.post("/api/fotos/validar", headers=self.h,
                              data={"arquivo": (io.BytesIO(_imagem()), "f.jpg"), "tipo": "CANHOTO", "tentativa": "1"},
                              content_type="multipart/form-data")
        d = r.get_json()
        self.assertEqual((r.status_code, d["resultado"], d["pode_seguir"]), (200, vf.NAO_VERIFICADO, True))


class TestRevisaoHumana(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._patch = mock.patch.object(banco, "DB_PATH", Path(self._tmp.name) / "t.db")
        self._patch.start()
        self.conn = banco.conectar()
        self.rota_id = rotas.materializar_rascunho({
            "id": 1, "data_alvo": date.today().isoformat(), "lote_id": "L", "nome": "R", "agent_id": AGENT,
            "vehicle_id": None, "motorista_nome": "Teste", "tipo_veiculo": None, "start_location_base_id": 1,
            "end_location_base_id": 1, "start_at": f"{date.today().isoformat()} 07:00:00", "km_estimado": 10.0,
            "status": "RASCUNHO", "vuupt_route_id": None,
            "paradas": [{"ordem": 1, "service_id": None, "codigo": "PS-A", "titulo": "A", "endereco": "Rua A",
                         "latitude": -23.5, "longitude": -46.6, "nivel_dificuldade": 1, "volume_caixas": 1}],
        }, banco.PROVEDOR_APP, conn=self.conn)
        self.parada_id = self.conn.execute("SELECT id FROM nucleo_paradas WHERE rota_id = ?", (self.rota_id,)).fetchone()[0]

    def tearDown(self):
        self.conn.close()
        self._patch.stop()
        self._tmp.cleanup()

    def _comprovante(self, uuid, validacao):
        return operacao.registrar_comprovante(self.conn, self.parada_id, AGENT, "CANHOTO", uuid,
                                              "dados/x.jpg", "sha" + uuid, 10, None, validacao=validacao)["id"]

    def test_fila_do_painel_traz_reprovados_por_padrao(self):
        self._comprovante("ok", {"resultado": "APROVADO", "motivo": "ok"})
        rep = self._comprovante("ruim", {"resultado": "REPROVADO", "motivo": "NF não confere", "nf_alvo": "12345"})
        self._comprovante("sem", None)
        self.assertEqual([c["id"] for c in operacao.listar_comprovantes_painel(self.conn)], [rep])
        self.assertEqual(len(operacao.listar_comprovantes_painel(self.conn, None)), 3)
        self.assertEqual([c["uuid"] for c in operacao.listar_comprovantes_painel(self.conn, "PENDENTE")], ["sem"])
        item = operacao.listar_comprovantes_painel(self.conn)[0]
        self.assertEqual((item["codigo"], item["validacao"]["nf_alvo"]), ("PS-A", "12345"))

    def test_humano_sobrescreve_a_ia_e_guarda_o_veredito_dela(self):
        cid = self._comprovante("ruim", {"resultado": "REPROVADO", "motivo": "NF não confere"})
        r = operacao.revisar_comprovante(self.conn, cid, "APROVADO", "hugo", "dá pra ler sim")
        self.assertEqual((r["resultado_validacao"], r["validado_por"]), ("APROVADO", "HUMANO"))
        self.assertIn("ia_resultado", r["dados_json"])
        self.assertIn("REPROVADO", r["dados_json"])          # o que a IA tinha dito
        self.assertEqual(operacao.listar_comprovantes_painel(self.conn), [])   # saiu da fila
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM nucleo_eventos WHERE tipo='FOTO_REVISADA'").fetchone()[0], 1)

    def test_resultado_invalido(self):
        cid = self._comprovante("x", None)
        with self.assertRaises(operacao.OperacaoInvalida):
            operacao.revisar_comprovante(self.conn, cid, "TALVEZ", "hugo")


if __name__ == "__main__":
    unittest.main()
