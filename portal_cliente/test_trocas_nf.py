# -*- coding: utf-8 -*-
"""Fila de trocas de NF do fluxo Cross (trocas_nf_pendentes).

    py -3.11 -m unittest portal_cliente.test_trocas_nf -v
"""
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import envio_pedidos as ep  # noqa: E402
import trocas_nf as tq  # noqa: E402

CHAVE = "35" + "2" * 42
AGORA = datetime(2026, 10, 27, 9, 0, 0)
CONFIG = {"email": {"email_atendimento": "atendimento@freshlogbr.com"},
          "portal_cliente": {"envios": {"forcar_destino": ""}}}


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        raiz = Path(self.tmp.name)
        self.raiz = raiz
        self._patches = [mock.patch.object(ep, "DB_PATH", raiz / "d.db"), mock.patch.object(ep, "_RAIZ", raiz),
                         mock.patch.object(tq.sessao_uso, "adquirir", return_value="principal"),
                         mock.patch.object(tq.sessao_uso, "liberar")]
        for p in self._patches:
            p.start()
        self.conn = ep.conectar()
        tq.garantir_tabela(self.conn)
        self.xml = raiz / "dados" / "portal_envios" / f"{CHAVE}.xml"
        self.xml.parent.mkdir(parents=True)
        self.xml.write_bytes(b"<nfeProc/>")
        self.trocas = []

    def tearDown(self):
        self.conn.close()
        for p in self._patches:
            p.stop()
        self.tmp.cleanup()

    def _enfileirar(self, **kw):
        base = dict(cnpj_embarcador="66259821000155", client_id="18", numero_nf="152754", chave_nfe=CHAVE,
                    caminho_xml_original=str(self.xml.relative_to(self.raiz)), origem="portal")
        base.update(kw)
        return tq.enfileirar(self.conn, **base)

    def _trocar(self, resultado):
        def trocar(sessao, id_stokki, caminho, numero_nf, client_id):
            self.trocas.append((id_stokki, Path(caminho), numero_nf, client_id))
            return resultado
        return trocar

    def _linha(self):
        return dict(self.conn.execute("SELECT * FROM trocas_nf_pendentes WHERE chave_nfe = ?", (CHAVE,)).fetchone())

    def _processar(self, resultado, agora=AGORA, **kw):
        return tq.processar_pendentes(self.conn, CONFIG, fabrica_sessao=lambda: object(), trocar=self._trocar(resultado),
                                      agora=agora, **kw)


OK = {"ok": True, "ja_estava": False, "erro": "", "conferencia": "itens"}
FALHA = {"ok": False, "ja_estava": False, "erro": "Stokki 422: Chave inválida", "conferencia": ""}


class Enfileirar(_Base):
    def test_enfileirar_ignora_chave_repetida(self):
        a = self._enfileirar(id_stokki=42542)
        b = self._enfileirar()
        self.assertEqual(a, b)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM trocas_nf_pendentes").fetchone()[0], 1)
        self.assertEqual(self._linha()["id_stokki"], 42542)   # nao apaga o id que ja tinha

    def test_feita_nao_reabre_e_erro_so_reabre_quando_pedido(self):
        self._enfileirar()
        self.conn.execute("UPDATE trocas_nf_pendentes SET status = 'FEITA'")
        self._enfileirar()
        self.assertEqual(self._linha()["status"], "FEITA")
        self.conn.execute("UPDATE trocas_nf_pendentes SET status = 'ERRO', tentativas = 3")
        self._enfileirar()
        self.assertEqual(self._linha()["status"], "ERRO")
        self._enfileirar(reabrir=True)
        self.assertEqual((self._linha()["status"], self._linha()["tentativas"]), ("PENDENTE", 0))


class Processar(_Base):
    def test_sem_pendentes_nao_abre_sessao(self):
        fabrica = mock.Mock()
        r = tq.processar_pendentes(self.conn, CONFIG, fabrica_sessao=fabrica, trocar=self._trocar(OK), agora=AGORA)
        fabrica.assert_not_called()
        self.assertEqual(r, {"feitas": 0, "pendentes": 0, "erros": 0, "adiados": 0})

    def test_feita_grava_concluido_e_espelha_no_envio(self):
        self.conn.execute("INSERT INTO portal_envios (cnpj_embarcador, chave_nfe, xml_path, status, criado_em, atualizado_em) "
                          "VALUES ('66259821000155', ?, 'x', 'CRIADO', 'a', 'a')", (CHAVE,))
        envio_id = self.conn.execute("SELECT id FROM portal_envios").fetchone()[0]
        self._enfileirar(id_stokki=42542, envio_id=envio_id)
        r = self._processar(OK)
        self.assertEqual(r["feitas"], 1)
        linha = self._linha()
        self.assertEqual((linha["status"], linha["concluido_em"]), ("FEITA", "2026-10-27 09:00:00"))
        self.assertEqual(self.trocas[0][0], 42542)
        self.assertEqual(self.trocas[0][1], self.xml)   # caminho relativo resolvido na raiz
        env = dict(self.conn.execute("SELECT * FROM portal_envios").fetchone())
        self.assertEqual((env["troca_nf_status"], env["id_stokki"]), ("FEITA", 42542))

    def test_falha_conta_tentativa_e_so_tenta_depois_de_10_min(self):
        self._enfileirar(id_stokki=42542)
        r = self._processar(FALHA)
        self.assertEqual(r["pendentes"], 1)
        linha = self._linha()
        self.assertEqual((linha["status"], linha["tentativas"]), ("PENDENTE", 1))
        self.assertIn("Chave inválida", linha["erro"])
        self._processar(FALHA, agora=AGORA + timedelta(minutes=5))
        self.assertEqual(len(self.trocas), 1)
        self._processar(FALHA, agora=AGORA + timedelta(minutes=11))
        self.assertEqual(len(self.trocas), 2)

    def test_terceira_falha_vira_erro_e_avisa_equipe_nao_cliente(self):
        self._enfileirar(id_stokki=42542)
        with mock.patch.object(tq, "enviar_email") as envio:
            for i in range(3):
                self._processar(FALHA, agora=AGORA + timedelta(minutes=11 * i))
        self.assertEqual(self._linha()["status"], "ERRO")
        envio.assert_called_once()
        self.assertEqual(envio.call_args[0][0], ["atendimento@freshlogbr.com"])
        self.assertIn("Troca de NF falhou", envio.call_args[0][1])

    def test_id_stokki_nulo_resolve_pela_listagem(self):
        self._enfileirar()
        localizar = mock.Mock(return_value={"id_stokki": 42600, "codigo": "PS-42600", "caminho_nf": "users/x.xml"})
        self._processar(OK, localizar=localizar)
        localizar.assert_called_once()
        self.assertEqual(localizar.call_args[0][1:], ("152754", "18"))
        linha = self._linha()
        self.assertEqual((linha["status"], linha["id_stokki"], linha["codigo_pedido"]), ("FEITA", 42600, "PS-42600"))

    def test_pedido_nao_achado_conta_tentativa(self):
        self._enfileirar()
        self._processar(OK, localizar=mock.Mock(return_value=None))
        linha = self._linha()
        self.assertEqual((linha["status"], linha["tentativas"]), ("PENDENTE", 1))
        self.assertIn("não encontrado", linha["erro"])
        self.assertEqual(self.trocas, [])

    def test_trava_ocupada_adia_sem_tocar(self):
        self._enfileirar(id_stokki=42542)
        with mock.patch.object(tq.sessao_uso, "adquirir", return_value=False):
            r = self._processar(OK)
        self.assertEqual(r["adiados"], 1)
        self.assertEqual(self.trocas, [])
        self.assertEqual(self._linha()["tentativas"], 0)

    def test_login_falhou_adia_sem_gastar_tentativa(self):
        self._enfileirar(id_stokki=42542)
        def fabrica():
            raise RuntimeError("login falhou")
        r = tq.processar_pendentes(self.conn, CONFIG, fabrica_sessao=fabrica, trocar=self._trocar(OK), agora=AGORA)
        self.assertEqual(r["adiados"], 1)
        self.assertEqual(self._linha()["tentativas"], 0)

    def test_simular_nao_toca(self):
        self._enfileirar(id_stokki=42542)
        r = self._processar(OK, simular=True)
        self.assertEqual(self.trocas, [])
        self.assertEqual(r, {"feitas": 0, "pendentes": 0, "erros": 0, "adiados": 0})


class Ciclo(_Base):
    def test_ciclo_processa_a_fila_depois_dos_lotes(self):
        import enviar_stokki as es
        with mock.patch.object(es.ep, "limpar_temporarios"),                 mock.patch.object(es.tq, "processar_pendentes", return_value={"feitas": 1, "pendentes": 0, "erros": 0, "adiados": 0}) as proc:
            total = es.ciclo(CONFIG, simular=True)
        proc.assert_called_once()
        self.assertTrue(proc.call_args.kwargs["simular"])
        self.assertEqual(total["trocas_nf"]["feitas"], 1)


if __name__ == "__main__":
    unittest.main()
