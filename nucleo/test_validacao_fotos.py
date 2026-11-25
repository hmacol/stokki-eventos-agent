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

    def test_foto_de_outra_coisa_vira_fachada_e_nunca_libera(self):
        # Hugo, 20/11: fachada não vale; grava e pede outra foto, sem o
        # "não consigo melhorar" nem depois das tentativas.
        with _modelo(_leitura_canhoto(documento="NAO_E_DOCUMENTO", motivo="A foto mostra uma fachada.")):
            r = vf.validar_foto(_cfg(), "CANHOTO", _imagem(), nf_alvo="12345")
        self.assertEqual((r["resultado"], r["guardar_como"], r["exige_outra_foto"]), (vf.REPROVADO, vf.TIPO_FACHADA, True))
        self.assertIn("ficou guardada", r["motivo"])
        p = vf.aplicar_tentativas(_cfg(max_tentativas=2), r, 5)
        self.assertEqual((p["pode_seguir"], p["revisao_humana"]), (False, False))

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


def _leitura_canhoteira(linhas, **kw) -> dict:
    return _leitura_canhoto(**{"documento": "CANHOTEIRA_FRESHLOG", "numeros_nf": [],
                               "linhas_canhoteira": [{"codigo_pedido": c, "preenchida": p} for c, p in linhas], **kw})


class TestCanhoteira(unittest.TestCase):
    """Canhoteira da FreshLog (Hugo, 20/11): vale, mas a folha junta várias
    entregas -- confere a linha do PS da parada, não a NF."""

    def test_linha_do_pedido_preenchida_aprova_sem_cobrar_nf(self):
        with _modelo(_leitura_canhoteira([("PS-44598", True), ("PS-44581", True), ("PS-44608", False)])):
            r = vf.validar_foto(_cfg(), "CANHOTO", _imagem(), nf_alvo="12345", codigos_pedido=["PS-44581"])
        self.assertEqual(r["resultado"], vf.APROVADO)
        self.assertIn("PS-44581", r["motivo"])

    def test_linha_do_pedido_em_branco_reprova(self):
        # Mesma folha: a linha 3 (PS-44608) ficou sem recebedor.
        with _modelo(_leitura_canhoteira([("PS-44598", True), ("PS-44608", False)])):
            r = vf.validar_foto(_cfg(), "CANHOTO", _imagem(), codigos_pedido=["PS-44608"])
        self.assertEqual(r["resultado"], vf.REPROVADO)
        self.assertIn("sem recebedor", r["motivo"])

    def test_linha_do_pedido_fora_da_foto_reprova(self):
        with _modelo(_leitura_canhoteira([("PS-44598", True), ("PS-44581", True)])):
            r = vf.validar_foto(_cfg(), "CANHOTO", _imagem(), codigos_pedido=["PS-44605"])
        self.assertEqual(r["resultado"], vf.REPROVADO)
        self.assertIn("não mostra a linha do PS-44605", r["motivo"])

    def test_folha_cortada_chamada_de_outra_coisa_ainda_e_canhoteira(self):
        # Teste de 20/11 (#59): sem o cabeçalho, o modelo às vezes diz
        # OUTRO_COMPROVANTE, mas lê as linhas PS -- vale a regra da folha.
        with _modelo(_leitura_canhoteira([("PS-44534", True)], documento="OUTRO_COMPROVANTE")):
            r = vf.validar_foto(_cfg(), "CANHOTO", _imagem(), nf_alvo="181005", codigos_pedido=["PS-44534"])
        self.assertEqual(r["resultado"], vf.APROVADO)

    def test_reentrega_casa_com_o_codigo_base(self):
        with _modelo(_leitura_canhoteira([("PS-44598 · NF —", True)])):
            r = vf.validar_foto(_cfg(), "CANHOTO", _imagem(), codigos_pedido=vf.codigos_base("#PS-44598-R1"))
        self.assertEqual(r["resultado"], vf.APROVADO)

    def test_mesma_foto_decide_por_parada_sem_chamar_o_modelo(self):
        with _modelo(_leitura_canhoteira([("PS-1", True), ("PS-2", False)])):
            r = vf.validar_foto(_cfg(), "CANHOTO", _imagem(), codigos_pedido=["PS-1"])
        self.assertEqual(r["resultado"], vf.APROVADO)
        with mock.patch.object(vf, "_chamar_modelo", side_effect=AssertionError("não devia chamar")):
            outra = vf.redecidir_canhoto(_cfg(), r, codigos_pedido=["PS-2"])
        self.assertEqual(outra["resultado"], vf.REPROVADO)
        self.assertIsNone(vf.redecidir_canhoto(_cfg(), {"resultado": vf.REPROVADO}, codigos_pedido=["PS-1"]))


class TestOrientacao(unittest.TestCase):
    def _foto(self, w=400, h=200):
        buf = io.BytesIO()
        Image.new("RGB", (w, h), "white").save(buf, format="JPEG")
        return buf.getvalue()

    def test_gira_pra_posicao_de_leitura(self):
        foto = self._foto()
        for topo, tamanho in (("DE_CIMA_PARA_BAIXO", (200, 400)), ("DE_BAIXO_PARA_CIMA", (200, 400)), ("DE_CABECA_PARA_BAIXO", (400, 200))):
            girada = vf.corrigir_orientacao(foto, {"leitura": {"sentido_do_texto": topo}})
            self.assertNotEqual(girada, foto, topo)
            self.assertEqual(Image.open(io.BytesIO(girada)).size, tamanho, topo)

    def test_texto_em_pe_ou_sem_leitura_nao_mexe(self):
        foto = self._foto()
        for res in (None, {}, {"leitura": {"sentido_do_texto": "NORMAL"}}, {"leitura": {"sentido_do_texto": "SEM_TEXTO"}}):
            self.assertIs(vf.corrigir_orientacao(foto, res), foto)

    def test_conferencia_desvira_quando_o_lado_veio_trocado(self):
        leitura = _leitura_canhoto(sentido_do_texto="DE_CIMA_PARA_BAIXO")
        respostas = [(leitura, {"entrada": 100, "saida": 10}), ({"texto": "DE_CABECA_PARA_BAIXO"}, {"entrada": 50, "saida": 5})]
        with mock.patch.object(vf, "_chamar_modelo", side_effect=respostas) as m:
            r = vf.validar_foto(_cfg(), "CANHOTO", _imagem(), nf_alvo="12345")
        self.assertEqual((m.call_count, r["giro"], r["tokens"]), (2, "ROTATE_270", {"entrada": 150, "saida": 15}))
        # a decisão refeita pela leitura guardada mantém o giro conferido
        self.assertEqual(vf.giro_para_leitura(vf.redecidir_canhoto(_cfg(), r, nf_alvo="12345")), "ROTATE_270")

    def test_conferencia_em_pe_mantem_e_texto_normal_nem_confere(self):
        respostas = [(_leitura_canhoto(sentido_do_texto="DE_CABECA_PARA_BAIXO"), {}), ({"texto": "EM_PE"}, {})]
        with mock.patch.object(vf, "_chamar_modelo", side_effect=respostas):
            self.assertEqual(vf.validar_foto(_cfg(), "CANHOTO", _imagem(), nf_alvo="12345")["giro"], "ROTATE_180")
        with mock.patch.object(vf, "_chamar_modelo", side_effect=[(_leitura_canhoto(sentido_do_texto="NORMAL"), {})]) as m:
            r = vf.validar_foto(_cfg(), "CANHOTO", _imagem(), nf_alvo="12345")
        self.assertEqual((m.call_count, r["giro"]), (1, None))

    def test_falha_na_conferencia_mantem_o_giro_da_leitura(self):
        respostas = [(_leitura_canhoto(sentido_do_texto="DE_BAIXO_PARA_CIMA"), {}), RuntimeError("sem rede")]
        with mock.patch.object(vf, "_chamar_modelo", side_effect=respostas):
            r = vf.validar_foto(_cfg(), "CANHOTO", _imagem(), nf_alvo="12345")
        self.assertEqual((r["resultado"], r["giro"]), (vf.APROVADO, "ROTATE_270"))

    def test_direcao_do_giro(self):
        # Texto lido de cima para baixo = topo das letras virado pra
        # direita: o que está à direita tem que subir.
        im = Image.new("RGB", (400, 200), "white")
        ImageDraw.Draw(im).rectangle([380, 90, 399, 110], fill="black")
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        girada = Image.open(io.BytesIO(vf.corrigir_orientacao(buf.getvalue(), {"leitura": {"sentido_do_texto": "DE_CIMA_PARA_BAIXO"}})))
        self.assertLess(girada.convert("L").getpixel((100, 10)), 80)      # preto em cima
        self.assertGreater(girada.convert("L").getpixel((100, 390)), 200)  # branco embaixo


class TestChamadaModelo(unittest.TestCase):
    def _chamar(self, modelo, esforco):
        cfg = {**_cfg(modelo=modelo, esforco=esforco)}
        resp = mock.Mock(stop_reason="end_turn", usage=mock.Mock(input_tokens=1, output_tokens=1),
                         content=[mock.Mock(type="text", text="{}")])
        falso = mock.MagicMock()   # o SDK pode nem estar instalado na máquina de teste
        falso.Anthropic.return_value.messages.create.return_value = resp
        with mock.patch.dict(sys.modules, {"anthropic": falso}):
            vf._chamar_modelo(cfg, b"x", "image/jpeg", "p", {"type": "object"})
        return falso.Anthropic.return_value.messages.create.call_args.kwargs

    def test_padrao_e_haiku_5_5_com_esforco_low(self):
        cfg = _cfg()
        self.assertEqual((cfg["modelo"], cfg["esforco"]), ("claude-haiku-5-5", "low"))
        self.assertEqual(self._chamar("claude-haiku-5-5", "low")["output_config"]["effort"], "low")

    def test_haiku_4_5_nunca_recebe_effort(self):
        self.assertNotIn("effort", self._chamar("claude-haiku-4-5", "low")["output_config"])


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


def _leitura_carregamento(**kw) -> dict:
    return {"legivel": True, "eh_carga": True, "bau_inteiro": True, "ocupacao_pct": 73,
            "problemas": [], "motivo": "Bau inteiro visivel.", **kw}


class TestCarregamento(unittest.TestCase):
    def test_aprova_bau_inteiro(self):
        with _modelo(_leitura_carregamento()):
            r = vf.validar_foto(_cfg(), "CARREGAMENTO", _imagem())
        self.assertEqual(r["resultado"], vf.APROVADO)
        self.assertEqual(r["ocupacao_pct"], 70)

    def test_reprova_bau_cortado(self):
        with _modelo(_leitura_carregamento(bau_inteiro=False, problemas=["CORTADA"], motivo="Bau cortado.")):
            r = vf.validar_foto(_cfg(), "CARREGAMENTO", _imagem())
        self.assertEqual(r["resultado"], vf.REPROVADO)
        self.assertIn("baú inteiro", r["motivo"])
        self.assertEqual(r["ocupacao_pct"], 70)

    def test_reprova_nao_e_carga(self):
        with _modelo(_leitura_carregamento(eh_carga=False, ocupacao_pct=None)):
            r = vf.validar_foto(_cfg(), "CARREGAMENTO", _imagem())
        self.assertEqual(r["resultado"], vf.REPROVADO)

    def test_haiku_fora_nao_trava(self):
        with mock.patch.object(vf, "_chamar_modelo", side_effect=RuntimeError("fora")):
            r = vf.validar_foto(_cfg(), "CARREGAMENTO", _imagem())
        self.assertEqual(r["resultado"], vf.NAO_VERIFICADO)
        self.assertTrue(vf.aplicar_tentativas(_cfg(), r, 1)["pode_seguir"])

    def test_libera_na_ultima_tentativa_sem_excecao_da_fachada(self):
        cfg = _cfg()
        with _modelo(_leitura_carregamento(bau_inteiro=False)):
            r = vf.validar_foto(cfg, "CARREGAMENTO", _imagem())
        p = vf.aplicar_tentativas(cfg, r, cfg["max_tentativas"])
        self.assertTrue(p["pode_seguir"])
        self.assertTrue(p["revisao_humana"])


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
                             codigo_pedido TEXT, numero_nf TEXT, status TEXT)""")

    def tearDown(self):
        self.conn.close()
        self._patch.stop()
        self._tmp.cleanup()

    def _doc(self, h, codigo, nf, tipo="Nota Fiscal"):
        self.conn.execute("INSERT INTO documentos_processados VALUES (?, ?, ?, ?, 'ENVIADO')", (h, tipo, codigo, nf))

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

    def test_codigo_da_parada_vem_com_cerquilha_e_sufixo(self):
        # Achado na VPS em 12/09: a parada traz '#PS-36623-R2' e o
        # documento guarda 'PS-36623'. Sem normalizar, só 3 de 1.017
        # paradas de 15 dias achavam a nota.
        self._doc("h1", "PS-36623", "5482")
        for codigo in ("#PS-36623", "PS-36623-R2", "#PS-36623-R1", "#PS-36623-R1-R1", "#PS-36623-C1", "ps-36623"):
            self.assertEqual(vf.nfs_do_pedido(self.conn, codigo), ["5482"], codigo)

    def test_code_agrupando_pedidos_combinados(self):
        # O `code` da VUUPT pode juntar pedidos por vírgula.
        self._doc("h1", "PS-36100", "11")
        self._doc("h2", "PS-36200", "22")
        self.assertEqual(sorted(vf.nfs_do_pedido(self.conn, "#PS-36100,#PS-36200-R1")), ["11", "22"])

    def test_codigos_base(self):
        self.assertEqual(vf.codigos_base("#PS-36623-R2"), ["PS-36623"])
        self.assertEqual(vf.codigos_base("#PS-36100, #PS-36100-R1"), ["PS-36100"])   # dedupe
        self.assertEqual(vf.codigos_base(None), [])
        # Código que não é PS-NNNNN (Lalamove, teste) passa como veio.
        self.assertEqual(vf.codigos_base("LALA-TESTE-1"), ["LALA-TESTE-1"])


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
                        codigo_pedido TEXT, numero_nf TEXT, status TEXT)""")
        conn.execute("INSERT INTO documentos_processados VALUES ('h1', 'Nota Fiscal', 'PS-A', '12345', 'ENVIADO')")
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

    def _comprovantes(self):
        conn = banco.conectar()
        rows = [dict(r) for r in conn.execute("SELECT tipo, uuid, caminho_local, sha256 FROM nucleo_comprovantes ORDER BY id")]
        conn.close()
        return rows

    def test_fachada_no_ato_fica_gravada_e_pede_outra_foto(self):
        foto = _imagem()
        with _modelo(_leitura_canhoto(documento="NAO_E_DOCUMENTO")):
            r = self._validar(foto, tipo="CANHOTO", parada_id=self.parada_id, nf="12345", tentativa=2).get_json()
        self.assertEqual((r["resultado"], r["pode_seguir"], r["revisao_humana"]), (vf.REPROVADO, False, False))
        rows = self._comprovantes()
        self.assertEqual([(c["tipo"], c["uuid"]) for c in rows], [("FACHADA", f"fachada-{r['sha256'][:16]}")])
        self.assertTrue((Path(api_motorista._RAIZ) / rows[0]["caminho_local"]).exists()
                        or Path(rows[0]["caminho_local"]).exists())

    def test_falha_ao_guardar_fachada_ainda_devolve_o_veredito(self):
        # Erro HTTP aqui faria o app achar que está sem sinal e ACEITAR a
        # fachada como canhoto (revisão de segurança de 20/11).
        with _modelo(_leitura_canhoto(documento="NAO_E_DOCUMENTO")), \
                mock.patch.object(operacao, "registrar_comprovante", side_effect=operacao.OperacaoInvalida("x", 409)):
            r = self._validar(_imagem(), tipo="CANHOTO", parada_id=self.parada_id, nf="12345", tentativa=1)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual((r.get_json()["resultado"], r.get_json()["pode_seguir"]), (vf.REPROVADO, False))

    def test_fachada_que_chega_pela_fila_nao_vira_canhoto(self):
        with _modelo(_leitura_canhoto(documento="NAO_E_DOCUMENTO")):
            env = self.cli.post(f"/api/paradas/{self.parada_id}/comprovantes", headers=self.h,
                                data={"arquivo": (io.BytesIO(_imagem()), "f.jpg"), "tipo": "CANHOTO", "uuid": "c9"},
                                content_type="multipart/form-data")
        self.assertEqual(env.status_code, 201, env.get_json())
        self.assertEqual([c["tipo"] for c in self._comprovantes()], ["FACHADA"])

    def test_foto_gravada_sai_girada_e_guarda_o_sha_do_original(self):
        im = Image.new("RGB", (600, 300), "white")
        ImageDraw.Draw(im).text((20, 20), "NF 12345", fill="black")
        buf = io.BytesIO()
        im.save(buf, format="JPEG")
        foto = buf.getvalue()
        with _modelo(_leitura_canhoto(sentido_do_texto="DE_CIMA_PARA_BAIXO")):
            env = self.cli.post(f"/api/paradas/{self.parada_id}/comprovantes", headers=self.h,
                                data={"arquivo": (io.BytesIO(foto), "f.jpg"), "tipo": "CANHOTO", "uuid": "c8", "nf": "12345"},
                                content_type="multipart/form-data")
        self.assertEqual(env.status_code, 201, env.get_json())
        c = self._comprovantes()[0]
        caminho = Path(c["caminho_local"])
        caminho = caminho if caminho.is_absolute() else Path(api_motorista._RAIZ) / caminho
        self.assertEqual(Image.open(caminho).size, (300, 600))     # em pé
        import hashlib
        self.assertEqual(c["sha256"], hashlib.sha256(foto).hexdigest())

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
