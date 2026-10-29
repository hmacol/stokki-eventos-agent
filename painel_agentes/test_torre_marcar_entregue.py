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

    def __init__(self, token):
        self.token = token

    def buscar_servico_por_id(self, service_id):
        if not _VuuptFalso.servico:
            return None
        return {"id": service_id, "code": "#PS-77", "status": _VuuptFalso.status,
                "title": "Cliente Exemplo", "type": _VuuptFalso.tipo}

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
        self._patches = [
            mock.patch.object(torre_controle, "_RAIZ", Path(self._tmp.name)),
            mock.patch.object(torre_controle, "VuuptClient", _VuuptFalso),
            mock.patch.object(torre_controle, "_carregar_config", lambda: {"vuupt_api": {"token": "x"}}),
            mock.patch.object(torre_controle, "tratativas"),
            mock.patch("nucleo.canhotos_manuais.salvar", self.salvar),
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

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def _entregas(self):
        return torre_controle._buscar_entregas_manuais([77])

    def _marcar(self, canhoto=None, nota=None, justificativa=None, texto=None):
        return torre_controle.marcar_entregue_manual(77, "hugo", canhoto, nota, justificativa, texto)

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
        self.assertNotIn("checklist_id", r)
        self.assertFalse(_VuuptFalso.ultima["fotos"])
        self.salvar.assert_called_once()
        self.assertEqual(self.salvar.call_args.args[3], PDF)

    def test_pdf_com_nota_manda_so_a_nota(self):
        r = self._marcar(canhoto=PDF, nota=FOTO)
        self.assertTrue(r["ok"], r)
        self.assertEqual(list(_VuuptFalso.ultima["fotos"]), [CAMPO_NOTA_RETORNO])
        self.assertEqual(r["checklist_id"], 555)

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
        torre_controle.tratativas.registrar_evento.assert_not_called()

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
        self.assertEqual(registro["checklist_id"], 555)
        self.assertEqual(registro["fotos"], 1)
        self.assertIn("Cliente retirou na porta", registro["observacao"])
        texto = torre_controle.tratativas.registrar_evento.call_args.kwargs["texto"]
        self.assertIn("hugo", texto)
        self.assertIn("checklist", texto)

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


if __name__ == "__main__":
    unittest.main()
