# -*- coding: utf-8 -*-
"""
test_torre_marcar_entregue.py

"Marcar como entregue" pelo painel (Hugo, 08/10): fecha o servico na
Vuupt por API (accept -> start -> check-in -> [checklist] -> check-out)
sem passar pelo app do motorista, e deixa auditoria em
torre_entregas_manuais + tratativas_pedido. Desde 29/10 com foto do
canhoto/nota no checklist da Vuupt ou justificativa da lista fixa.

Nenhum teste toca na VUUPT nem no dados.db real: o banco vai pra uma
pasta temporaria e o cliente da VUUPT e um duble.
Rodar (da raiz):
    py -3.11 -m unittest painel_agentes.test_torre_marcar_entregue -v
"""
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(Path(__file__).parent))

import torre_controle  # noqa: E402
from vuupt_checklist import CAMPO_CANHOTO, CAMPO_NOTA_RETORNO, ChecklistNaoGravado  # noqa: E402


def _jpeg() -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), "white").save(buf, "JPEG")
    return buf.getvalue()


FOTO = _jpeg()
PDF = b"%PDF-1.4 canhoto"


class _VuuptFalso:
    """Devolve o servico com o status configurado em `status` e anota as
    chamadas de concluir_como_agente. `falha` faz o fechamento estourar."""
    status = "on_route"
    tipo = "delivery"
    servico = True
    falha = None
    chamadas = []
    ultima = None
    retorno = 555
    outros = {}      # service_id -> campos diferentes (outros pedidos de um lote); None = nao existe
    lidos = []

    def __init__(self, token):
        self.token = token

    def buscar_servico_por_id(self, service_id):
        _VuuptFalso.lidos.append(service_id)
        extra = _VuuptFalso.outros.get(service_id, {})
        if not _VuuptFalso.servico or extra is None:
            return None
        return {"id": service_id, "code": f"#PS-{service_id}", "status": _VuuptFalso.status,
                "title": "Cliente Exemplo", "type": _VuuptFalso.tipo,
                "sender_id": 10, "customer_id": 20, "address": "Rua Zilda, 288 - São Paulo",
                "latitude": -23.497015, "longitude": -46.660775, **extra}

    def concluir_como_agente(self, service_id, sucesso=True, failed_reason_id=None, status_atual="",
                             fotos_checklist=None, servico=None):
        if _VuuptFalso.falha:
            raise _VuuptFalso.falha
        _VuuptFalso.chamadas.append((service_id, sucesso, status_atual))
        _VuuptFalso.ultima = {"fotos": fotos_checklist, "servico": servico}
        return _VuuptFalso.retorno if fotos_checklist else None


class TestMarcarEntregueManual(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.salvar = mock.Mock(return_value={})
        self.existe = mock.Mock(return_value=False)
        self._patches = [
            mock.patch.object(torre_controle, "_RAIZ", Path(self._tmp.name)),
            mock.patch.object(torre_controle, "VuuptClient", _VuuptFalso),
            mock.patch.object(torre_controle, "_carregar_config", lambda: {"vuupt_api": {"token": "x"}}),
            mock.patch.object(torre_controle, "tratativas"),
            mock.patch("nucleo.canhotos_manuais.salvar", self.salvar),
            mock.patch("nucleo.canhotos_manuais.existe", self.existe),
            mock.patch("nucleo.banco.conectar", lambda: mock.Mock()),
        ]
        for p in self._patches:
            p.start()
        _VuuptFalso.status = "on_route"
        _VuuptFalso.tipo = "delivery"
        _VuuptFalso.servico = True
        _VuuptFalso.falha = None
        _VuuptFalso.chamadas = []
        _VuuptFalso.ultima = None
        _VuuptFalso.outros = {}
        _VuuptFalso.lidos = []

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def _entregas(self):
        return torre_controle._buscar_entregas_manuais([77])

    def _marcar(self, canhoto=None, nota=None, justificativa=None, texto=None, lote_referencia=None):
        return torre_controle.marcar_entregue_manual(77, "hugo", canhoto, nota, justificativa, texto,
                                                     lote_referencia=lote_referencia)

    # --- foto no checklist ---

    def test_com_canhoto_preenche_checklist_e_conclui(self):
        r = self._marcar(canhoto=FOTO)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["codigo"], "#PS-77")
        self.assertEqual(r["checklist_id"], 555)
        self.assertEqual(_VuuptFalso.chamadas, [(77, True, "on_route")])
        self.assertEqual(list(_VuuptFalso.ultima["fotos"]), [CAMPO_CANHOTO])
        self.assertEqual(_VuuptFalso.ultima["fotos"][CAMPO_CANHOTO][:2], b"\xff\xd8")
        # copia local do canhoto, em PDF, com o codigo do servico (nunca do navegador)
        self.salvar.assert_called_once()
        self.assertEqual(self.salvar.call_args.args[1:3], ("#PS-77", 77))
        self.assertEqual(self.salvar.call_args.args[3][:5], b"%PDF-")
        registro = self._entregas()[77]
        self.assertEqual(registro["observacao"], "com foto do canhoto")
        self.assertEqual(registro["fotos"], 1)

    def test_com_canhoto_e_nota_duas_fotos(self):
        r = self._marcar(canhoto=FOTO, nota=FOTO)
        self.assertTrue(r["ok"], r)
        self.assertEqual(set(_VuuptFalso.ultima["fotos"]), {CAMPO_CANHOTO, CAMPO_NOTA_RETORNO})
        self.assertEqual(self._entregas()[77]["fotos"], 2)

    def test_checklist_no_servico_do_request(self):
        # o servico lido uma vez e repassado: o client nao precisa buscar de novo
        self._marcar(canhoto=FOTO)
        self.assertEqual(_VuuptFalso.ultima["servico"]["id"], 77)
        self.assertEqual(_VuuptFalso.ultima["servico"]["type"], "delivery")

    def test_pdf_so_copia_local_e_avisa(self):
        r = self._marcar(canhoto=PDF)
        self.assertTrue(r["ok"], r)
        self.assertIn("PDF", r["aviso"])
        self.assertEqual(self._entregas()[77]["observacao"], "com canhoto em PDF (só cópia local)")
        self.assertNotIn("checklist_id", r)
        self.assertFalse(_VuuptFalso.ultima["fotos"])
        self.salvar.assert_called_once()
        self.assertEqual(self.salvar.call_args.args[3], PDF)

    def test_pdf_com_nota_descarta_a_nota(self):
        # regra do Hugo (30/10): a nota so vai junto da FOTO do canhoto; PDF nao e foto
        r = self._marcar(canhoto=PDF, nota=FOTO)
        self.assertTrue(r["ok"], r)
        self.assertFalse(_VuuptFalso.ultima["fotos"])
        self.assertNotIn("checklist_id", r)
        self.assertIn("A foto da nota de retorno só vai junto da foto do canhoto.", r["aviso"])
        self.assertIn("PDF", r["aviso"])
        self.assertEqual(self._entregas()[77]["fotos"], 0)

    def test_nota_sem_canhoto_descartada_e_conclui_pela_justificativa(self):
        r = self._marcar(nota=FOTO, justificativa="sem_bateria")
        self.assertTrue(r["ok"], r)
        self.assertFalse(_VuuptFalso.ultima["fotos"])
        self.assertNotIn("checklist_id", r)
        self.assertEqual(r["aviso"], "A foto da nota de retorno só vai junto da foto do canhoto.")

    def test_nota_sem_canhoto_nem_e_decodificada(self):
        # descartada antes de abrir: arquivo ruim na nota sozinha nao recusa o pedido
        r = self._marcar(nota=b"nao sou imagem", justificativa="sem_bateria")
        self.assertTrue(r["ok"], r)
        self.assertIn("só vai junto da foto do canhoto", r["aviso"])

    def test_nota_sem_canhoto_em_pedido_ja_concluido_tambem_avisa(self):
        _VuuptFalso.status = "done"
        r = self._marcar(nota=FOTO, justificativa="sem_bateria")
        self.assertTrue(r["ja_estava"])
        self.assertEqual(r["aviso"], "A foto da nota de retorno só vai junto da foto do canhoto.")

    def test_arquivo_que_nao_e_foto_recusa_sem_tocar_em_nada(self):
        r = self._marcar(canhoto=b"nao sou imagem")
        self.assertFalse(r["ok"])
        self.assertEqual(_VuuptFalso.chamadas, [])
        self.salvar.assert_not_called()

    def test_checklist_falha_nao_conclui_e_mostra_erro(self):
        _VuuptFalso.falha = ChecklistNaoGravado("finish 500")
        r = self._marcar(canhoto=FOTO)
        self.assertFalse(r["ok"])
        self.assertIn("Checklist não gravado na VUUPT", r["erro"])
        self.assertIn("finish 500", r["erro"])
        self.assertEqual(self._entregas(), {})
        torre_controle.tratativas.registrar_evento.assert_not_called()
        self.salvar.assert_called_once()  # a copia local fica (spec 4.3)

    def test_tipo_nao_delivery_com_foto_recusa(self):
        _VuuptFalso.tipo = "pickup"
        r = self._marcar(canhoto=FOTO)
        self.assertFalse(r["ok"])
        self.assertIn("entrega", r["erro"])
        self.assertEqual(_VuuptFalso.chamadas, [])
        self.salvar.assert_not_called()

    def test_ja_done_nao_toca_vuupt_e_avisa(self):
        _VuuptFalso.status = "done"
        r = self._marcar(canhoto=FOTO)
        self.assertTrue(r["ok"])
        self.assertTrue(r["ja_estava"])
        self.assertIn("Já estava concluído", r["aviso"])
        self.assertEqual(_VuuptFalso.chamadas, [])
        self.salvar.assert_called_once()
        self.assertEqual(self._entregas(), {})
        # copia salva em pedido ja concluido deixa tratativa
        evento = torre_controle.tratativas.registrar_evento.call_args
        self.assertIn("pedido já concluído", evento.kwargs["texto"])
        self.assertIn("hugo", evento.kwargs["texto"])

    def test_ja_done_com_canhoto_existente_nao_sobrescreve(self):
        _VuuptFalso.status = "done"
        self.existe.return_value = True
        r = self._marcar(canhoto=FOTO)
        self.assertTrue(r["ok"])
        self.assertIn("Já havia canhoto", r["aviso"])
        self.salvar.assert_not_called()
        torre_controle.tratativas.registrar_evento.assert_not_called()

    def test_nao_done_sobrescreve_copia_do_proprio_pedido(self):
        # nova tentativa (checklist falhou antes) substitui a copia: nao consulta existe()
        self._marcar(canhoto=FOTO)
        self.salvar.assert_called_once()
        self.existe.assert_not_called()

    def test_erro_nomeia_o_arquivo_ruim(self):
        r = self._marcar(canhoto=b"nao sou imagem")
        self.assertTrue(r["erro"].startswith("Canhoto: "), r)
        r = self._marcar(canhoto=FOTO, nota=b"nao sou imagem")
        self.assertTrue(r["erro"].startswith("Foto da nota de retorno: "), r)
        self.assertIn("não é foto", r["erro"])
        self.salvar.assert_not_called()

    # --- justificativa ---

    def test_sem_foto_sem_justificativa_recusa(self):
        r = self._marcar()
        self.assertFalse(r["ok"])
        self.assertEqual(_VuuptFalso.chamadas, [])

    def test_outro_sem_texto_recusa(self):
        r = self._marcar(justificativa="outro", texto="ab")
        self.assertFalse(r["ok"])
        self.assertEqual(_VuuptFalso.chamadas, [])

    def test_sem_foto_com_justificativa_conclui_sem_checklist(self):
        r = self._marcar(justificativa="sem_bateria")
        self.assertTrue(r["ok"], r)
        self.assertFalse(_VuuptFalso.ultima["fotos"])
        self.assertNotIn("checklist_id", r)
        self.salvar.assert_not_called()

    def test_registro_grava_justificativa_e_checklist_id(self):
        self._marcar(nota=FOTO, justificativa="outro", texto="Cliente retirou na porta")
        registro = self._entregas()[77]
        self.assertEqual(registro["justificativa"], "outro")
        self.assertEqual(registro["texto_outro"], "Cliente retirou na porta")
        self.assertIsNone(registro["checklist_id"])     # nota sem canhoto nao vai pro checklist
        self.assertEqual(registro["fotos"], 0)
        self.assertIn("Cliente retirou na porta", registro["observacao"])
        texto = torre_controle.tratativas.registrar_evento.call_args.kwargs["texto"]
        self.assertIn("hugo", texto)
        self.assertNotIn("checklist", texto)
        # com a foto do canhoto o id do checklist e gravado
        self._marcar(canhoto=FOTO, nota=FOTO)
        registro = self._entregas()[77]
        self.assertEqual(registro["checklist_id"], 555)
        self.assertEqual(registro["fotos"], 2)

    def test_tratativa_cita_a_justificativa(self):
        self._marcar(justificativa="confirmado_contato")
        chamadas = torre_controle.tratativas.registrar_evento.call_args_list
        self.assertEqual(len(chamadas), 1)
        self.assertEqual(chamadas[0].args[:3], ("#PS-77", "TORRE", "ENTREGUE_MANUAL"))
        self.assertEqual(chamadas[0].kwargs["service_id"], 77)
        self.assertIn("telefone ou WhatsApp", chamadas[0].kwargs["texto"])
        registro = self._entregas()[77]
        self.assertEqual(registro["status_anterior"], "on_route")
        self.assertEqual(registro["usuario"], "hugo")
        self.assertEqual(registro["fotos"], 0)

    # --- travas que ja existiam ---

    def test_assigned_tambem_fecha(self):
        _VuuptFalso.status = "assigned"
        r = self._marcar(justificativa="sem_bateria")
        self.assertTrue(r["ok"])
        self.assertEqual(_VuuptFalso.chamadas, [(77, True, "assigned")])

    def test_ja_entregue_nao_chama_vuupt_nem_registra(self):
        _VuuptFalso.status = "done"
        r = self._marcar(justificativa="sem_bateria")
        self.assertTrue(r["ok"])
        self.assertTrue(r["ja_estava"])
        self.assertNotIn("aviso", r)
        self.assertEqual(_VuuptFalso.chamadas, [])
        self.assertEqual(self._entregas(), {})
        torre_controle.tratativas.registrar_evento.assert_not_called()

    def test_sem_motorista_recusa(self):
        _VuuptFalso.status = "not_assigned"
        r = self._marcar(justificativa="sem_bateria")
        self.assertFalse(r["ok"])
        self.assertIn("motorista", r["erro"].lower())
        self.assertEqual(_VuuptFalso.chamadas, [])

    def test_cancelado_recusa(self):
        _VuuptFalso.status = "canceled"
        r = self._marcar(justificativa="sem_bateria")
        self.assertFalse(r["ok"])
        self.assertIn("cancelad", r["erro"].lower())
        self.assertEqual(_VuuptFalso.chamadas, [])

    def test_insucesso_recusa(self):
        _VuuptFalso.status = "failed"
        r = self._marcar(justificativa="sem_bateria")
        self.assertFalse(r["ok"])
        self.assertEqual(_VuuptFalso.chamadas, [])

    def test_nao_encontrado_recusa(self):
        _VuuptFalso.servico = False
        r = self._marcar(justificativa="sem_bateria")
        self.assertFalse(r["ok"])
        self.assertIn("encontrado", r["erro"].lower())

    def test_erro_longo_da_vuupt_truncado(self):
        _VuuptFalso.falha = RuntimeError("check-out do servico 77: status 502: <html>" + "x" * 5000)
        r = self._marcar(justificativa="sem_bateria")
        self.assertFalse(r["ok"])
        self.assertLessEqual(len(r["erro"]), 260)
        self.assertIn("502", r["erro"])

    # --- foto em massa (regra do Hugo, 30/10) ---

    def test_lote_com_referencia_compativel_conclui(self):
        _VuuptFalso.outros = {12: {"customer_id": 20}}
        r = self._marcar(canhoto=FOTO, lote_referencia=12)
        self.assertTrue(r["ok"], r)
        self.assertIn(12, _VuuptFalso.lidos)

    def test_lote_com_embarcador_diferente_recusa_sem_tocar_em_nada(self):
        _VuuptFalso.outros = {12: {"sender_id": 99}}
        r = self._marcar(canhoto=FOTO, lote_referencia=12)
        self.assertFalse(r["ok"])
        self.assertEqual(r["erro"], torre_controle.MSG_FOTO_EM_MASSA)
        self.assertEqual(_VuuptFalso.chamadas, [])
        self.salvar.assert_not_called()

    def test_lote_com_destino_diferente_recusa(self):
        _VuuptFalso.outros = {12: {"customer_id": 21, "address": "Av. Paulista, 1000",
                                   "latitude": -23.5614, "longitude": -46.6559}}
        r = self._marcar(canhoto=FOTO, lote_referencia=12)
        self.assertFalse(r["ok"])
        self.assertEqual(r["erro"], torre_controle.MSG_FOTO_EM_MASSA)

    def test_lote_sem_foto_nao_confere_referencia(self):
        _VuuptFalso.outros = {12: {"sender_id": 99}}
        r = self._marcar(justificativa="sem_bateria", lote_referencia=12)
        self.assertTrue(r["ok"], r)
        self.assertNotIn(12, _VuuptFalso.lidos)

    def test_lote_referencia_sumida_recusa(self):
        _VuuptFalso.outros = {12: None}
        r = self._marcar(canhoto=FOTO, lote_referencia=12)
        self.assertFalse(r["ok"])
        self.assertEqual(_VuuptFalso.chamadas, [])

    def test_validar_lote_foto(self):
        _VuuptFalso.outros = {1: {}, 2: {"customer_id": 21}, 3: {"sender_id": 99}}
        self.assertEqual(torre_controle.validar_lote_foto([1, 2]), {"ok": True})
        r = torre_controle.validar_lote_foto([1, 2, 3])
        self.assertFalse(r["ok"])
        self.assertEqual(r["erro"], torre_controle.MSG_FOTO_EM_MASSA)
        self.assertEqual(r["fora"], ["#PS-3"])

    def test_validar_lote_foto_pedido_sumido_fica_fora(self):
        _VuuptFalso.outros = {1: {}, 2: None}
        r = torre_controle.validar_lote_foto([1, 2])
        self.assertFalse(r["ok"])
        self.assertEqual(r["fora"], ["#2"])

    def test_falha_na_vuupt_nao_registra(self):
        _VuuptFalso.falha = RuntimeError("check-out do servico 77: status 500")
        r = self._marcar(justificativa="sem_bateria")
        self.assertFalse(r["ok"])
        self.assertIn("500", r["erro"])
        self.assertEqual(self._entregas(), {})
        torre_controle.tratativas.registrar_evento.assert_not_called()

    def test_tabela_ganha_colunas_novas_em_banco_antigo(self):
        import sqlite3
        db = Path(self._tmp.name) / "dados" / "dados.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(db)
        c.execute("CREATE TABLE torre_entregas_manuais (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                  "service_id INTEGER NOT NULL, codigo TEXT, status_anterior TEXT, usuario TEXT, "
                  "observacao TEXT, marcado_em TEXT NOT NULL)")
        c.commit()
        c.close()
        torre_controle._conectar_tratadas().close()
        torre_controle._conectar_tratadas().close()  # idempotente
        c = sqlite3.connect(db)
        colunas = {r[1] for r in c.execute("PRAGMA table_info(torre_entregas_manuais)")}
        c.close()
        self.assertTrue({"justificativa", "texto_outro", "checklist_id", "fotos"} <= colunas)


class TestMesmoDestinoParaFoto(unittest.TestCase):
    """Regra pura da foto em massa: mesmo embarcador (sender_id) E mesmo
    destinatario (customer_id) OU mesmo local (endereco normalizado ou ate 50 m)."""

    A = {"sender_id": 10, "customer_id": 20, "address": "Rua Zilda, 288 - São Paulo",
         "latitude": -23.497015, "longitude": -46.660775}

    def _b(self, **kw):
        return {**self.A, **kw}

    def test_mesmo_destinatario(self):
        self.assertTrue(torre_controle.mesmo_destino_para_foto(
            self.A, self._b(address="outro", latitude=-23.6, longitude=-46.7)))

    def test_destinatario_diferente_mesmo_endereco(self):
        # lote entregue numa transportadora: clientes diferentes, mesmo endereco
        self.assertTrue(torre_controle.mesmo_destino_para_foto(
            self.A, self._b(customer_id=21, address="  rua ZILDA 288, sao paulo ", latitude=None, longitude=None)))

    def test_destinatario_diferente_a_menos_de_50_m(self):
        # ~30 m ao norte, endereco escrito diferente (coordenada em texto, como a Vuupt manda as vezes)
        self.assertTrue(torre_controle.mesmo_destino_para_foto(
            self.A, self._b(customer_id=21, address="R. Zilda 300", latitude="-23.496745", longitude="-46.660775")))

    def test_embarcador_diferente(self):
        self.assertFalse(torre_controle.mesmo_destino_para_foto(self.A, self._b(sender_id=11)))

    def test_mais_de_50_m(self):
        # ~80 m e endereco diferente
        self.assertFalse(torre_controle.mesmo_destino_para_foto(
            self.A, self._b(customer_id=21, address="R. Zilda 380", latitude=-23.496295, longitude=-46.660775)))

    def test_sem_embarcador_nao_prova_nada(self):
        self.assertFalse(torre_controle.mesmo_destino_para_foto(
            self._b(sender_id=None), self._b(sender_id=None)))

    def test_sem_destinatario_nem_endereco_nem_coordenada(self):
        vazio = {"sender_id": 10}
        self.assertFalse(torre_controle.mesmo_destino_para_foto(vazio, dict(vazio)))


if __name__ == "__main__":
    unittest.main()
