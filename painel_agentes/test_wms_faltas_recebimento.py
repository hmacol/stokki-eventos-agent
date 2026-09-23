# -*- coding: utf-8 -*-
"""
test_wms_faltas_recebimento.py

Encerrar o recebimento com divergencia (wms_pedidos.encerrar_com_divergencia)
e o relatorio de faltas que vai pro cliente (wms_faltas_recebimento.py).

Nenhum teste aqui manda e-mail de verdade: o envio e sempre um duble que
so guarda o que teria sido enviado.

Rodar (da raiz):
    py -3.11 -m unittest painel_agentes.test_wms_faltas_recebimento -v
"""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(Path(__file__).parent))

import wms  # noqa: E402
import wms_pedidos  # noqa: E402
import wms_faltas_recebimento as faltas_mod  # noqa: E402

CNPJ = "11111111000111"
STKKC_ID = "48"


class EnvioFalso:
    """Duble de email_utils.enviar_email -- guarda, nao envia."""

    def __init__(self, ok=True):
        self.ok = ok
        self.chamadas = []

    def __call__(self, destinatarios, assunto, corpo, config_email, cc=None, **kwargs):
        self.chamadas.append({"para": list(destinatarios), "assunto": assunto, "corpo": corpo,
                              "cc": list(cc or [])})
        return self.ok


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "dados.db"
        self.conn = wms_pedidos.conectar(self.db)
        # Cadastro do embarcador, como na `interno` de producao.
        self.conn.execute(
            "CREATE TABLE interno (cnpj_embarcador TEXT PRIMARY KEY, nome_remetente TEXT, apelido TEXT, "
            "email TEXT, notificar_email INTEGER NOT NULL DEFAULT 1, sender_id INTEGER, stkkc_id INTEGER)")
        self.conn.execute("INSERT INTO interno VALUES (?,?,?,?,?,?,?)",
                          (CNPJ, "MARIA DOLORES ALIMENTOS LTDA", "Maria Dolores",
                           "compras@mariadolores.com.br; fiscal@mariadolores.com.br", 1, 101, int(STKKC_ID)))
        self.conn.execute(
            "INSERT INTO wms_produtos (id, stokki_id, sku, descricao, embarcador, unidade, qtd_por_caixa, "
            "atualizado_em) VALUES (1, 900, 'SKU1', 'FILE DE TILAPIA 1KG', 'MARIA DOLORES', 'UN', 1, "
            "'2026-09-23 08:00:00')")
        self.conn.execute(
            "INSERT INTO wms_produtos (id, stokki_id, sku, descricao, embarcador, unidade, qtd_por_caixa, "
            "atualizado_em) VALUES (2, 901, 'SKU2', 'CAMARAO CINZA 500G', 'MARIA DOLORES', 'UN', 1, "
            "'2026-09-23 08:00:00')")
        self.conn.commit()
        self.rid = wms_pedidos.registrar_recebimento(
            self.conn,
            {"id_stokki": 2490, "codigo": "#PE-2490", "embarcador": "MARIA DOLORES",
             "stkkc_id": STKKC_ID, "situacao": "Recebido", "chegada": "2026-09-23"},
            [{"linha": 1, "sku": "SKU1", "ean_linha": "", "descricao": "FILE DE TILAPIA 1KG",
              "qtd_embalagem": 10},
             {"linha": 2, "sku": "SKU2", "ean_linha": "", "descricao": "CAMARAO CINZA 500G",
              "qtd_embalagem": 6}])
        self.conn.commit()
        self.config = {"email": {"remetente": "entregas@freshlogbr.com"},
                       "notificacao_faltas_recebimento": {"forcar_destino": "hugo@freshlogbr.com"},
                       "wms": {"embarcador_piloto_id": STKKC_ID}}

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def _itens(self):
        return {i["linha"]: i for i in wms_pedidos.itens_do_recebimento(self.conn, self.rid)}

    def _enderecar(self, linha, qtd):
        wms_pedidos.contabilizar_enderecamento(self.conn, self.rid, self._itens()[linha]["id"], qtd)

    def _notificar(self, envio=None, config=None, modo_teste=False):
        envio = envio or EnvioFalso()
        r = faltas_mod.notificar_faltas(self.conn, self.rid, config or self.config,
                                        enviar=envio, modo_teste=modo_teste)
        return r, envio


class TestEncerrarComDivergencia(Base):
    def test_sem_falta_nenhuma_nao_deixa_encerrar_com_divergencia(self):
        """Recebimento que chegou inteiro fecha sozinho -- o botao nao serve
        pra ele, e nao pode gerar relatorio de falta que nao existe."""
        self._enderecar(1, 10)
        self._enderecar(2, 6)
        self.assertEqual(
            self.conn.execute("SELECT estado FROM wms_recebimentos WHERE id = ?", (self.rid,)).fetchone()[0],
            "ENDERECADO")
        with self.assertRaises(wms.ErroWMS):
            wms_pedidos.encerrar_com_divergencia(self.conn, self.rid, "nada faltou")

    def test_encerramento_normal_nao_dispara_email(self):
        """Fechou sozinho (ENDERECADO) -> nao ha o que reportar."""
        self._enderecar(1, 10)
        self._enderecar(2, 6)
        r, envio = self._notificar()
        self.assertFalse(r["enviado"])
        self.assertEqual(r["motivo"], "nao_encerrado")
        self.assertEqual(envio.chamadas, [])

    def test_congela_a_falta_de_cada_linha_e_muda_o_estado(self):
        self._enderecar(1, 6)          # faltaram 4
        self._enderecar(2, 6)          # chegou inteiro
        r = wms_pedidos.encerrar_com_divergencia(
            self.conn, self.rid, "  2 caixas   chegaram avariadas ", operador={"nome": "Jonas"})
        self.assertEqual([(f["linha"], f["falta_un"]) for f in r["faltas"]], [(1, 4.0)])
        rec = r["recebimento"]
        self.assertEqual(rec["estado"], "DIVERGENCIA")
        self.assertEqual(rec["observacao_divergencia"], "2 caixas chegaram avariadas")
        self.assertEqual(rec["encerrado_por"], "Jonas")
        self.assertTrue(rec["encerrado_em"])
        itens = self._itens()
        self.assertEqual(itens[1]["falta_un"], 4.0)
        self.assertEqual(itens[2]["falta_un"], 0)

    def test_nao_mexe_em_estoque(self):
        """O que chegou ja entrou pelas ENTRADAs; o que faltou nunca existiu."""
        self._enderecar(1, 6)
        antes_mov = self.conn.execute("SELECT COUNT(*) FROM wms_movimentos").fetchone()[0]
        antes_saldo = self.conn.execute("SELECT COUNT(*) FROM wms_saldos").fetchone()[0]
        wms_pedidos.encerrar_com_divergencia(self.conn, self.rid, "nao veio")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM wms_movimentos").fetchone()[0], antes_mov)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM wms_saldos").fetchone()[0], antes_saldo)

    def test_linha_sem_produto_no_catalogo_nao_vira_falta_do_cliente(self):
        """Nao conseguimos enderecar por problema NOSSO de cadastro --
        chamar isso de falta seria acusar o transportador do cliente."""
        wms_pedidos.registrar_recebimento(
            self.conn, {"id_stokki": 2490, "codigo": "#PE-2490", "stkkc_id": STKKC_ID},
            [{"linha": 3, "sku": "NAO-EXISTE", "ean_linha": "", "descricao": "PRODUTO SEM CADASTRO",
              "qtd_embalagem": 5}])
        self.conn.commit()
        self._enderecar(1, 10)
        self._enderecar(2, 3)
        faltas = wms_pedidos.faltas_do_recebimento(self.conn, self.rid)
        self.assertEqual([f["linha"] for f in faltas], [2])

    def test_encerrar_duas_vezes_e_recusado(self):
        self._enderecar(1, 6)
        wms_pedidos.encerrar_com_divergencia(self.conn, self.rid, "")
        with self.assertRaises(wms.ErroWMS):
            wms_pedidos.encerrar_com_divergencia(self.conn, self.rid, "")


class TestRelatorio(Base):
    def _encerrar(self, obs="2 caixas chegaram avariadas"):
        self._enderecar(1, 6)
        self._enderecar(2, 0.5)
        return wms_pedidos.encerrar_com_divergencia(self.conn, self.rid, obs, operador={"nome": "Jonas"})

    def test_relatorio_tem_as_linhas_certas(self):
        self._encerrar()
        r, envio = self._notificar()
        self.assertTrue(r["enviado"], r["motivo"])
        self.assertEqual(r["faltas"], 2)
        corpo = envio.chamadas[0]["corpo"]
        self.assertIn("#PE-2490", envio.chamadas[0]["assunto"])
        self.assertIn("FILE DE TILAPIA 1KG", corpo)
        self.assertIn("CAMARAO CINZA 500G", corpo)
        # numero do jeito que o cliente le (virgula, nao ponto)
        for texto in ("10 UN", "6 UN", "4 UN", "5,5 UN", "0,5 UN"):
            self.assertIn(texto, corpo)
        self.assertIn("2 caixas chegaram avariadas", corpo)
        self.assertIn("Maria Dolores", corpo)

    def test_email_nasce_redirecionado_quando_forcar_destino_esta_preenchido(self):
        self._encerrar()
        r, envio = self._notificar()
        self.assertTrue(r["redirecionado"])
        self.assertEqual(envio.chamadas[0]["para"], ["hugo@freshlogbr.com"])
        self.assertEqual(envio.chamadas[0]["cc"], [])
        self.assertIn("Redirecionado (piloto)", envio.chamadas[0]["corpo"])
        self.assertIn("compras@mariadolores.com.br", envio.chamadas[0]["corpo"])

    def test_sem_a_secao_no_config_o_padrao_e_redirecionar(self):
        """Config sem a chave = comportamento seguro, sem depender de
        ninguem lembrar de configurar."""
        self._encerrar()
        r, envio = self._notificar(config={"email": {}, "wms": {"embarcador_piloto_id": STKKC_ID}})
        self.assertTrue(r["redirecionado"])
        self.assertEqual(envio.chamadas[0]["para"], ["hugo@freshlogbr.com"])

    def test_envio_real_vai_pro_cliente_com_copia_pra_entregas(self):
        self._encerrar()
        config = dict(self.config, notificacao_faltas_recebimento={"forcar_destino": ""})
        r, envio = self._notificar(config=config)
        self.assertFalse(r["redirecionado"])
        self.assertEqual(envio.chamadas[0]["para"],
                         ["compras@mariadolores.com.br", "fiscal@mariadolores.com.br"])
        self.assertEqual(envio.chamadas[0]["cc"], ["entregas@freshlogbr.com"])
        self.assertNotIn("Redirecionado (piloto)", envio.chamadas[0]["corpo"])

    def test_rodar_duas_vezes_nao_manda_dois_emails(self):
        self._encerrar()
        envio = EnvioFalso()
        self._notificar(envio=envio)
        r2, _ = self._notificar(envio=envio)
        self.assertEqual(len(envio.chamadas), 1)
        self.assertFalse(r2["enviado"])
        self.assertEqual(r2["motivo"], "ja_enviado")

    def test_falha_no_envio_nao_marca_como_enviado(self):
        self._encerrar()
        r, _ = self._notificar(envio=EnvioFalso(ok=False))
        self.assertEqual(r["motivo"], "falha_no_envio")
        r2, envio2 = self._notificar()
        self.assertTrue(r2["enviado"])
        self.assertEqual(len(envio2.chamadas), 1)

    def test_embarcador_que_desligou_no_portal_nao_recebe(self):
        import preferencias_notificacao as pn
        pn.salvar(self.conn, CNPJ, [], {"faltas_recebimento": False}, "cliente")
        self._encerrar()
        r, envio = self._notificar()
        self.assertFalse(r["enviado"])
        self.assertEqual(r["motivo"], "desligado_pelo_cliente")
        self.assertEqual(envio.chamadas, [])

    def test_email_escolhido_no_portal_vence_o_do_cadastro(self):
        import preferencias_notificacao as pn
        pn.salvar(self.conn, CNPJ, ["recebimento@mariadolores.com.br"], {}, "cliente")
        self._encerrar()
        config = dict(self.config, notificacao_faltas_recebimento={"forcar_destino": ""})
        r, envio = self._notificar(config=config)
        self.assertEqual(envio.chamadas[0]["para"], ["recebimento@mariadolores.com.br"])
        self.assertTrue(r["enviado"])

    def test_embarcador_desconhecido_nao_recebe_falta_de_outro(self):
        self.conn.execute("UPDATE wms_recebimentos SET stkkc_id = '99' WHERE id = ?", (self.rid,))
        self.conn.commit()
        self._encerrar()
        r, envio = self._notificar()
        self.assertEqual(r["motivo"], "sem_embarcador")
        self.assertEqual(envio.chamadas, [])

    def test_chave_mestra_desligada_segura_o_envio_real(self):
        self._encerrar()
        config = dict(self.config, notificacao_faltas_recebimento={"forcar_destino": ""},
                      notificacoes_automaticas={"ativo": False})
        r, envio = self._notificar(config=config)
        self.assertEqual(r["motivo"], "desativado")
        self.assertEqual(envio.chamadas, [])


class TestNumeroComoATelaMostra(unittest.TestCase):
    """O `:g` de antes tinha 6 digitos significativos: 123456,7 virava
    "123457" (arredondava calado) e 1000000 virava "1e+06". O operador
    confere na tela (num() do wms.html) e o cliente le no e-mail -- os dois
    tem que dizer o mesmo numero, porque isso vira cobranca."""

    def test_nao_arredonda_e_nao_vira_notacao_cientifica(self):
        self.assertEqual(faltas_mod._num(123456.7), "123.456,7")
        self.assertEqual(faltas_mod._num(1000000), "1000000")

    def test_inteiro_sem_separador_e_quebrado_em_pt_br(self):
        # mesmo criterio do num() da tela: Number.isInteger -> String(n)
        self.assertEqual(faltas_mod._num(4), "4")
        self.assertEqual(faltas_mod._num(27.5), "27,5")
        self.assertEqual(faltas_mod._num(0.5), "0,5")
        self.assertEqual(faltas_mod._num(0), "0")
        self.assertEqual(faltas_mod._num(1234.125), "1.234,125")


class TestReenvioManual(Base):
    """Falha de SMTP nao tem reenvio automatico -- a rota nao aceita
    encerrar de novo. O que salva o cliente de nunca ser avisado e a fila
    de reenvio da equipe."""

    def _encerrar(self):
        self._enderecar(1, 6)
        wms_pedidos.encerrar_com_divergencia(self.conn, self.rid, "nao veio", operador={"nome": "Jonas"})

    def test_envio_que_falhou_fica_na_fila_e_o_que_deu_certo_sai(self):
        self._encerrar()
        self._notificar(envio=EnvioFalso(ok=False))
        fila = faltas_mod.pendentes_de_envio(self.conn)
        self.assertEqual([r["id_stokki"] for r in fila], [2490])
        r, _ = self._notificar()
        self.assertTrue(r["enviado"])
        self.assertEqual(faltas_mod.pendentes_de_envio(self.conn), [])

    def test_reenvio_repete_a_falta_congelada_mesmo_se_a_stokki_mudar(self):
        self._encerrar()
        self._notificar(envio=EnvioFalso(ok=False))
        # a Stokki reescreve a quantidade anunciada depois do encerramento
        wms_pedidos.registrar_recebimento(
            self.conn, {"id_stokki": 2490, "codigo": "#PE-2490", "stkkc_id": STKKC_ID},
            [{"linha": 1, "sku": "SKU1", "ean_linha": "", "descricao": "FILE DE TILAPIA 1KG",
              "qtd_embalagem": 6}])
        self.conn.commit()
        r, envio = self._notificar()
        self.assertTrue(r["enviado"])
        self.assertIn("4 UN", envio.chamadas[0]["corpo"])

    def test_forcar_remanda_o_que_ja_tinha_sido_enviado(self):
        self._encerrar()
        self._notificar()
        envio = EnvioFalso()
        r = faltas_mod.notificar_faltas(self.conn, self.rid, self.config, enviar=envio,
                                        forcar_reenvio=True)
        self.assertTrue(r["enviado"])
        self.assertEqual(len(envio.chamadas), 1)

    def test_linha_de_comando_reenvia_de_verdade(self):
        """--reenviar e o unico caminho de recuperacao que existe: tem que
        funcionar de ponta a ponta, nao so as funcoes por baixo."""
        from unittest import mock
        self._encerrar()
        self._notificar(envio=EnvioFalso(ok=False))
        envio = EnvioFalso()
        conectar = wms_pedidos.conectar   # o modulo e o mesmo objeto: guarda antes de trocar
        with mock.patch.object(faltas_mod.wms_pedidos, "conectar",
                               side_effect=lambda *a, **k: conectar(self.db)), \
             mock.patch.object(faltas_mod, "_carregar_config_yaml", return_value=self.config), \
             mock.patch.object(faltas_mod.email_utils, "enviar_email", envio):
            self.assertEqual(faltas_mod.main(["--reenviar", "2490"]), 0)
        self.assertEqual(len(envio.chamadas), 1)
        self.assertEqual(envio.chamadas[0]["para"], ["hugo@freshlogbr.com"])
        self.assertEqual(faltas_mod.pendentes_de_envio(self.conn), [])

    def test_forcar_destino_preenchido_nao_desliga_a_idempotencia(self):
        """`forcar` (destino do config) e `forcar_reenvio` sao coisas
        diferentes -- confundir os dois mandava o e-mail duas vezes."""
        self._encerrar()
        envio = EnvioFalso()
        self._notificar(envio=envio)
        self._notificar(envio=envio)
        self.assertEqual(len(envio.chamadas), 1)


class TestEmailIdentificaACarga(Base):
    def test_ficha_da_carga_e_link_do_portal(self):
        self._enderecar(1, 6)
        wms_pedidos.encerrar_com_divergencia(self.conn, self.rid, "nao veio", operador={"nome": "Jonas"})
        _, envio = self._notificar()
        corpo = envio.chamadas[0]["corpo"]
        for texto in ("#PE-2490", "Chegada no galpão", "23/09/2026", "Situação na Stokki", "Recebido",
                      "Destinatário", "MARIA DOLORES", "Conferência", "Jonas",
                      "https://app.freshhub.com.br/cliente"):
            self.assertIn(texto, corpo)


class TestUmCriterioSo(Base):
    """Fechar sozinho e "nao ha falta nenhuma" -- a mesma funcao decide as
    duas coisas. Enquanto eram duas comparacoes iguais em lugares
    diferentes, divergir criava o recebimento que nao fecha e tambem nao
    tem falta pra reportar."""

    def test_fecha_sozinho_exatamente_quando_nao_ha_falta(self):
        for linha, qtd in ((1, 4), (1, 6), (2, 6)):
            self._enderecar(linha, qtd)
            estado = self.conn.execute("SELECT estado FROM wms_recebimentos WHERE id = ?",
                                       (self.rid,)).fetchone()[0]
            sem_falta = not wms_pedidos.faltas_do_recebimento(self.conn, self.rid)
            self.assertEqual(estado == "ENDERECADO", sem_falta,
                             f"depois de {qtd} na linha {linha}: estado={estado}, sem_falta={sem_falta}")

    def test_enderecamento_atrasado_nao_apaga_o_encerramento_com_divergencia(self):
        self._enderecar(1, 6)
        self._enderecar(2, 6)
        wms_pedidos.encerrar_com_divergencia(self.conn, self.rid, "nao veio")
        self._enderecar(1, 4)   # a mercadoria apareceu depois
        self.assertEqual(
            self.conn.execute("SELECT estado FROM wms_recebimentos WHERE id = ?", (self.rid,)).fetchone()[0],
            "DIVERGENCIA")


class TestPreferenciaNova(unittest.TestCase):
    """O tipo novo entrou em preferencias_notificacao.TIPOS, entao o botao
    Notificacoes do portal ja o mostra -- e o banco que ja existe (a VPS)
    precisa ganhar a coluna, senao o primeiro SELECT depois do deploy
    estoura 'no such column'."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "dados.db"

    def tearDown(self):
        self._tmp.cleanup()

    def test_tipo_esta_no_catalogo_com_o_formato_dos_outros(self):
        import preferencias_notificacao as pn
        info = pn.TIPOS["faltas_recebimento"]
        self.assertEqual(set(info), {"grupo", "rotulo", "descricao", "default"})
        self.assertIn(info["grupo"], ("acompanhamento", "acao"))

    def test_banco_antigo_ganha_a_coluna_sem_estourar(self):
        import preferencias_notificacao as pn
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE interno (cnpj_embarcador TEXT PRIMARY KEY, nome_remetente TEXT, apelido TEXT, "
            "email TEXT, notificar_email INTEGER NOT NULL DEFAULT 1, sender_id INTEGER, stkkc_id INTEGER)")
        conn.execute("INSERT INTO interno VALUES (?,?,?,?,?,?,?)",
                     (CNPJ, "Maria Dolores", None, "a@b.com", 1, 101, 48))
        # tabela como era ANTES do tipo novo existir
        conn.execute("CREATE TABLE preferencias_notificacao (cnpj_embarcador TEXT PRIMARY KEY, "
                     "emails TEXT NOT NULL DEFAULT '', nfs_em_rota INTEGER NOT NULL DEFAULT 1, "
                     "entrega_concluida INTEGER NOT NULL DEFAULT 1, resumo_diario INTEGER NOT NULL DEFAULT 0, "
                     "insucesso INTEGER NOT NULL DEFAULT 1, pedidos_em_espera INTEGER NOT NULL DEFAULT 1, "
                     "agendamento INTEGER NOT NULL DEFAULT 1, atualizado_em TEXT, atualizado_por TEXT)")
        conn.execute("INSERT INTO preferencias_notificacao (cnpj_embarcador, emails) VALUES (?, 'x@y.com')",
                     (CNPJ,))
        conn.commit()
        # leitura da rotina de lote NAO cria coluna: tem que valer o default
        self.assertFalse(pn.carregar_embarcadores("faltas_recebimento", chave="stkkc_id",
                                                  db_path=self.db)[48]["desligado"])
        # a tela do portal migra a tabela e continua lendo certo
        self.assertTrue(pn.ler(conn, CNPJ)["tipos"]["faltas_recebimento"])
        pn.salvar(conn, CNPJ, ["x@y.com"], {"faltas_recebimento": False}, "cliente")
        self.assertFalse(pn.ler(conn, CNPJ)["tipos"]["faltas_recebimento"])
        conn.close()


class TestTelaDoGalpao(unittest.TestCase):
    """A tela e JS puro; aqui so se confere a LIGACAO (mesmo criterio dos
    outros testes de tela deste projeto)."""

    _WMS = (Path(__file__).parent / "templates" / "wms.html").read_text(encoding="utf-8")

    def test_posta_na_rota_de_encerrar(self):
        self.assertIn("'/recebimentos/' + r.id + '/encerrar-divergencia'", self._WMS)

    def test_pede_confirmacao_dizendo_que_o_cliente_sera_avisado(self):
        self.assertIn("O cliente vai receber por e-mail", self._WMS)
        self.assertIn("confirm(", self._WMS)

    def test_manda_a_observacao_do_operador(self):
        self.assertIn("div-obs", self._WMS)
        self.assertIn("body: { observacao: obs }", self._WMS)

    def test_linha_sem_produto_no_catalogo_nao_entra_nas_faltas_da_tela(self):
        self.assertIn(".filter((it) => it.produto_id && it.qtd_un !== null && it.falta > 0)", self._WMS)

    def test_tentativa_repetida_nao_aparece_como_erro_pro_operador(self):
        """Resposta perdida no tablet + novo toque: o trabalho dele ja foi
        gravado, entao a tela mostra sucesso."""
        self.assertIn("resp.ja_encerrado", self._WMS)
        self.assertIn("envio.motivo === 'ja_enviado'", self._WMS)


if __name__ == "__main__":
    unittest.main()
