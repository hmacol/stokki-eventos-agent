# Fluxo de documentos: correções + vigia diário — Plano de implementação

> **Para agentes:** SUB-SKILL OBRIGATÓRIA: superpowers:subagent-driven-development (recomendado) ou superpowers:executing-plans. Passos com checkbox (`- [ ]`).

**Objetivo:** fazer o fluxo de documentos de pedido (NF, boleto → GCS → romaneio) cobrir todos os embarcadores sem buracos, e criar um vigia diário que avisa quando algo sai do trilho.

**Arquitetura:** correções cirúrgicas em `documentos_pedido/` (seleção, leitor de boleto, classificador, registros, arquivos), uma regra pura nova de "boleto esperado" (`documentos_pedido/regras_documentos.py`) usada pelo romaneio e pelo vigia, e um job novo `documentos_pedido/vigia_documentos.py` que só lê o banco e o log e manda o resumo pelo `notificar_execucao` (e-mail sempre, WhatsApp ALERTAS FRESH só com problema).

**Stack:** Python 3.11, SQLite (`dados/dados.db`), pdfplumber, pypdf, unittest, systemd timer na VPS.

**Spec:** a análise de 05/10/2026 nesta sessão (resumo na seção "Contexto medido"). Não há documento de spec separado.

## Contexto medido (produção, 05/10/2026)

- **Sem NF:** 25 de 26 pedidos "sem NF" tinham a DANFE na Stokki (teste `--modo-teste --pedidos`). Nunca foram pegos porque a seleção só visita "Aguardando Transportador"; quando a NF chega depois do "Sent", fica de fora.
- **Boleto:** 175 boletos em REVISAO_MANUAL.
  - 156 sem NF extraída:
    - Dourado `151359P01 N`: 86.
    - DANFE da NUU classificada como boleto: 35 + 3.
    - Vida Veg `000288185-001`: 27.
    - Costa Nilo `000063849A`: 1.
  - 19 com a DANFE ainda não indexada.
- **Sem arquivo:** 56 revisões, todas por causa do prefixo `{uid}_` ausente no `nome_arquivo`.
- **Revisões duplicadas:** PS-37626 regravado 166 vezes, NF 646 5 vezes. Toda revisão é gravada com `codigo_pedido=NULL`.
- **Quadro FATURA/DUPLICATA da DANFE** (amostra de 180):
  - com duplicata: 49 com boleto e 48 sem (NUU, Sotille, Vida Veg);
  - sem quadro: 72 sem boleto e 11 com (Dourado, Gambarini).
- **Decisões do Hugo (05/10):**
  - "qualquer status" = Padrão Puro, Dourado e Quatro Estrelas;
  - mandam boleto (nem sempre pra todo cliente): grupo Marchef, NUU, Dourado, Muai, Ciao, Sotille, Vida Veg;
  - boleto esperado decidido pela NF;
  - vigia 1x por dia em horário fora do padrão.

## Restrições globais

- Trabalhar num worktree a partir de `origin/master`. O `master` local está 87 commits atrás e cheio de alterações de outras sessões: `git worktree add .claude/worktrees/documentos-vigia -b documentos-vigia origin/master`.
- Python: `py -3.11`. Testes: `py -3.11 -m unittest <modulo>` a partir da raiz do worktree. Não há pytest.
- Sem acento em strings de log/comentário só se o arquivo já seguir esse padrão. Os arquivos de `documentos_pedido/` usam acento: manter.
- `py -3.11 -m py_compile` em todo arquivo editado.
- Commits: `Documentos: ...` / `Romaneio: ...` / `Vigia: ...`, em português, terminando com `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Só `git add` dos arquivos da tarefa.
- Commit/push/deploy só quando o Hugo pedir.
- Nunca rodar nada contra produção fora de `--modo-teste` sem o Hugo.
- `config.yaml`: nunca ler valores, nunca commitar.
- Atualizar `MAPA_DO_SISTEMA.txt` no mesmo commit que cria arquivo, rotina ou coluna (Tarefa 9).

## Foco de revisão

1. **Boleto de layout novo:** se o regex de linha casar um número que não é a NF (ex.: "Nosso Número" com data na frente), o boleto vai pro pedido errado. Os testes da Tarefa 1 cobrem os 7 layouts reais conhecidos, e a Tarefa 1 traz um caso negativo.
2. **Revisão manual com `codigo_pedido` agora preenchido:** nenhum leitor pode tratá-la como NF válida. A Tarefa 3 filtra `status='ENVIADO'` em `carregar_indice_nf`, `pedidos_nf_pendentes`, `validacao_fotos` e `enviar_stokki`, com teste do índice.
3. **Pedido que nunca terá NF:** fica sendo revisitado a cada rodada dentro da janela de "Sent" recentes (custo de tempo). A janela é limitada a 300 pedidos, com teste de que Padrão Puro e Pedramoura ficam fora (Tarefa 5).
4. **DANFE sem quadro de fatura de embarcador fora da lista:** o romaneio NÃO acusa "sem boleto". É intencional; teste na Tarefa 7.
5. **Vigia num dia sem rotas** (domingo): não pode virar alerta falso. Teste na Tarefa 9.

---

### Tarefa 1: Leitor de boleto entende Dourado, Vida Veg e Costa Nilo

**Arquivos:**
- Modificar: `documentos_pedido/boleto_parser.py` (constante `PADRAO_DOC_LINHA`, ~linhas 43-50, e docstring do módulo)
- Criar: `documentos_pedido/test_boleto_parser.py`

**Interfaces:** `extrair_metadados_boleto(caminho_pdf: Path, texto_pdf: str | None) -> dict` (inalterada).

- [ ] **Passo 1: escrever o teste (falha)**

```python
# -*- coding: utf-8 -*-
"""
Layouts reais de boleto (05/10/2026): o número do documento é a NF.
Rodar:  py -3.11 -m unittest documentos_pedido.test_boleto_parser
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from boleto_parser import extrair_metadados_boleto  # noqa: E402

BENEF = "Beneficiário EMBARCADOR TESTE LTDA 66.259.821/0001-55 Agência/Código Beneficiário\n"
PAGADOR = "Pagador CPF/CNPJ do Pagador\nCLIENTE TESTE LTDA CNPJ: 33.764.590/0001-88\n"


def _meta(linha_valores: str, extra: str = "") -> dict:
    texto = (BENEF + "Data do Documento Número do Documento Espécie Doc. Aceite Data do Processamento\n"
             + linha_valores + "\n" + extra + PAGADOR)
    return extrair_metadados_boleto(Path("x.pdf"), texto_pdf=texto)


class LayoutsReais(unittest.TestCase):
    def test_dourado_nf_com_parcela_p01_sem_especie(self):
        m = _meta("17/09/2026 151359P01 N 18/09/2026 26/201728-6")
        self.assertEqual(m["numero_nf"], "151359")
        self.assertEqual(m["parcela_atual"], 1)

    def test_vidaveg_parcela_de_tres_digitos(self):
        m = _meta("10/08/2026 000288185-001 DM N 04/00000117818-1")
        self.assertEqual(m["numero_nf"], "288185")
        self.assertEqual(m["parcela_atual"], 1)

    def test_costa_nilo_letra_no_fim(self):
        m = _meta("02/10/2026 000063849A DM N 05/10/2026 1.448,92")
        self.assertEqual(m["numero_nf"], "63849")
        self.assertIsNone(m["parcela_atual"])

    def test_itaueira_nf_parcela_total(self):
        m = _meta("27/07/2026 8482-1/1 DM NÃO 27/07/2026")
        self.assertEqual((m["numero_nf"], m["parcela_atual"], m["total_parcelas"]), ("8482", 1, 1))

    def test_dourado_antigo_santander(self):
        self.assertEqual(_meta("06/08/2026 149636 DM N 06/08/2026")["numero_nf"], "149636")

    def test_olist_historico_tem_prioridade(self):
        m = _meta("31/07/2026 1040162/01 DM N 31/07/2026", extra="Histórico: Ref. a NF nº 40162, parcela 1\n")
        self.assertEqual(m["numero_nf"], "40162")
        self.assertEqual(m["parcela_atual"], 1)

    def test_de_tommaso_documento_sem_data(self):
        texto = BENEF + "Nº do documento 035880 DM N Processamento 11/08/2026\n" + PAGADOR
        self.assertEqual(extrair_metadados_boleto(Path("x.pdf"), texto_pdf=texto)["numero_nf"], "35880")

    def test_linha_sem_especie_nem_n_nao_vira_nf(self):
        # "Data do Processamento" + valor: não é linha de documento
        m = _meta("05/10/2026 1448 RS 10,00")
        self.assertIsNone(m["numero_nf"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Passo 2: rodar e ver falhar**

`py -3.11 -m unittest documentos_pedido.test_boleto_parser -v`

Esperado: FAIL em `test_dourado_nf_com_parcela_p01_sem_especie`, `test_vidaveg_parcela_de_tres_digitos` e `test_costa_nilo_letra_no_fim`.

Se `test_de_tommaso_documento_sem_data` falhar também, o rótulo real é "documento 035880 DM", como já está em `PADRAO_DOC_SEM_DATA`. Nesse caso, ajuste o texto do teste para `"documento 035880 DM N"` e não mude o regex.

- [ ] **Passo 3: implementar**

Em `boleto_parser.py`, trocar `PADRAO_DOC_LINHA` por:

```python
# Linha de valores da tabela do boleto -- data do documento, número do
# documento e espécie. Layouts reais:
#   "27/07/2026 8482-1/1 DM NÃO"    Itaueira (NF-parcela/total)
#   "06/08/2026 149636 DM N"        Dourado antigo (Santander)
#   "31/07/2026 1040162/01 DM N"    Olist (doc/parcela)
#   "17/09/2026 151359P01 N"        Dourado desde 09/2026: NF + "P" + parcela, sem espécie (05/10)
#   "10/08/2026 000288185-001 DM N" Vida Veg/Bradesco: parcela com 3 dígitos (05/10)
#   "02/10/2026 000063849A DM N"    Costa Nilo/Itaú: letra colada na NF (05/10)
PADRAO_DOC_LINHA = re.compile(
    r"\b\d{2}/\d{2}/\d{4}\s+(\d{3,9})[A-Z]?(?:(?:[-/]|P)(\d{1,3})(?:/(\d{1,2}))?)?\s+(?:D[MS]|N)\b"
)
```

Na docstring do módulo, acrescentar à lista de formatos:

```
  5. Dourado (desde 09/2026): "151359P01" -- NF + "P" + parcela, sem DM.
  6. Vida Veg / Bradesco (237-2): "000288185-001" -- parcela com 3 dígitos.
  7. Laticínio Costa Nilo / Itaú: "000063849A" -- letra depois da NF.
```

- [ ] **Passo 4: rodar e ver passar**

`py -3.11 -m unittest documentos_pedido.test_boleto_parser -v` → OK (8 testes).

- [ ] **Passo 5: commit**

```bash
git add documentos_pedido/boleto_parser.py documentos_pedido/test_boleto_parser.py
git commit -m "Documentos: leitor de boleto entende Dourado (P01), Vida Veg (-001) e Costa Nilo (letra)"
```

---

### Tarefa 2: DANFE com quadro de duplicata não vira boleto

**Arquivos:**
- Modificar: `documentos_pedido/classificador.py` (entrada `"Boleto"` de `TIPOS_DOCUMENTO`)
- Criar: `documentos_pedido/test_classificador.py`

**Interfaces:** `classificar_documento(caminho_pdf: Path) -> dict` (inalterada).

**Por quê:** "vencimento" aparece no quadro FATURA/DUPLICATA de toda DANFE faturada. Como Boleto vem antes de Nota Fiscal na lista, a DANFE da NUU ("NFS SP.pdf") virava boleto.

- [ ] **Passo 1: teste (falha)**

```python
# -*- coding: utf-8 -*-
"""
Rodar:  py -3.11 -m unittest documentos_pedido.test_classificador
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))

import classificador  # noqa: E402

DANFE_FATURADA = """RECEBEMOS DE MARIA DOLORES OS PRODUTOS CONSTANTES DA NOTA FISCAL INDICADA AO LADO
DANFE Documento Auxiliar da Nota Fiscal Eletrônica Nº 040.119
FATURA / DUPLICATA
Num. 001 Venc. 12/11/2026 Valor R$ 3.801,60
CHAVE DE ACESSO 3526 0811 ..."""

BOLETO_DOURADO = """Local de Pagamento Vencimento
PAGAVEL PREFERENCIALMENTE EM CANAIS ELETRONICOS 24/09/2026
Beneficiário LATICINIOS DOURADO 66.259.821/0001-55
Data do Documento Número do Documento Espécie Doc. Aceite Nosso Número
17/09/2026 151359P01 N 18/09/2026 26/201728-6
Recibo do Pagador"""

DANFE_E_BOLETO = DANFE_FATURADA + "\n" + BOLETO_DOURADO


def _tipo(nome: str, texto: str) -> str:
    with patch.object(classificador, "_texto_do_pdf", return_value=texto):
        return classificador.classificar_documento(Path(nome))["tipo"]


class Classificacao(unittest.TestCase):
    def test_danfe_faturada_sem_nome_e_nota_fiscal(self):
        self.assertEqual(_tipo("76649_NFS SP.pdf", DANFE_FATURADA), "Nota Fiscal")

    def test_boleto_por_nome_e_texto(self):
        self.assertEqual(_tipo("119050_BOLETOS_1.pdf", BOLETO_DOURADO), "Boleto")

    def test_boleto_com_nome_neutro_pelo_texto(self):
        self.assertEqual(_tipo("34166536632000182388132106.pdf", BOLETO_DOURADO), "Boleto")

    def test_pdf_misto_com_nome_de_boleto_continua_boleto(self):
        self.assertEqual(_tipo("76803_DANFEs_Boletos_2026-08-11 (1)_NF3.pdf", DANFE_E_BOLETO), "Boleto")

    def test_danfe_pelo_nome(self):
        self.assertEqual(_tipo("PS-40896_DANFE.pdf", DANFE_FATURADA), "Nota Fiscal")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Passo 2:** `py -3.11 -m unittest documentos_pedido.test_classificador -v` → FAIL em `test_danfe_faturada_sem_nome_e_nota_fiscal`, que devolve "Boleto".

- [ ] **Passo 3: implementar** — trocar a linha do Boleto em `TIPOS_DOCUMENTO`:

```python
    # "vencimento" sozinho saiu em 05/10: toda DANFE faturada tem
    # "Venc." no quadro FATURA/DUPLICATA, e Boleto vem antes de Nota
    # Fiscal nesta lista -- a DANFE da NUU ("NFS SP.pdf") virava boleto.
    # Ficam só marcas que DANFE não tem.
    ("Boleto", [r"boleto", r"\bbolet[oa]\b"],
     [r"linha digit[aá]vel", r"c[oó]digo de barras", r"boleto banc[aá]rio",
      r"local de pagamento", r"recibo do pagador", r"nosso n[uú]mero"]),
```

- [ ] **Passo 4:** rodar o teste → OK (5).

- [ ] **Passo 5: conferência com o acervo real (VPS, só leitura).**

Copiar o `classificador.py` novo para `/tmp/clsnovo/` e rodar o script abaixo como `www-data`. Ele reclassifica todo Boleto e toda NF dos últimos 60 dias e lista quem muda de tipo:

```python
# /tmp/conferir_classificador.py
import sqlite3, sys, collections
sys.path.insert(0, '/tmp/clsnovo')
sys.path.insert(1, '/opt/stokki-eventos/documentos_pedido')
import classificador
from localizar_arquivos import resolver_arquivo_local
c = sqlite3.connect('file:/opt/stokki-eventos/dados/dados.db?mode=ro', uri=True)
mud = collections.Counter(); ex = []
for tipo, nome in c.execute("select tipo, nome_arquivo from documentos_processados "
                            "where tipo in ('Boleto','Nota Fiscal') and processado_em >= date('now','-60 days')"):
    cam = resolver_arquivo_local(nome)
    if not cam:
        continue
    novo = classificador.classificar_documento(cam)["tipo"]
    if novo != tipo:
        mud[(tipo, novo)] += 1; ex.append((tipo, novo, nome))
print(mud); print(*ex[:40], sep='\n')
```

Comandos:

```bash
ssh -i ~/.ssh/atendimento_vps root@187.127.52.197 "mkdir -p /tmp/clsnovo"
scp -i ~/.ssh/atendimento_vps documentos_pedido/classificador.py root@187.127.52.197:/tmp/clsnovo/
scp -i ~/.ssh/atendimento_vps conferir_classificador.py root@187.127.52.197:/tmp/
ssh -i ~/.ssh/atendimento_vps root@187.127.52.197 "chmod -R a+r /tmp/clsnovo /tmp/conferir_classificador.py && sudo -u www-data /opt/stokki-eventos/venv/bin/python /tmp/conferir_classificador.py"
```

Esperado:
- só `('Boleto', 'Nota Fiscal')` em arquivos "NFS SP", "CROSS SP__NFS SP" e "XML - FRESHLOG__NFe";
- nenhum boleto de verdade virando NF.

Se aparecer boleto real mudando, PARAR e mostrar a lista ao Hugo.

- [ ] **Passo 6: commit**

```bash
git add documentos_pedido/classificador.py documentos_pedido/test_classificador.py
git commit -m "Documentos: DANFE faturada (quadro de duplicata) não é mais classificada como boleto"
```

---

### Tarefa 3: Revisão manual com pedido, sem duplicar, e nada se perde em erro

**Arquivos:**
- Modificar: `documentos_pedido/fingerprint_documentos.py`
- Modificar: `documentos_pedido/processar_documentos.py` (`processar_um_documento`, imports)
- Modificar: `nucleo/validacao_fotos.py` (consulta de NFs, ~linha 211)
- Modificar: `portal_cliente/enviar_stokki.py` (`_codigo_em_documentos`, ~linha 688)
- Criar: `documentos_pedido/test_registros_documentos.py`

**Interfaces produzidas:**
- `fingerprint_documentos.marcar_processado(..., cobranca: int | None = None)`: novo parâmetro opcional, usado pela Tarefa 7.
- `fingerprint_documentos.ja_em_revisao(codigo_pedido: str, tipo: str | None, numero_nf: str | None, motivo: str) -> bool`.
- Coluna nova `documentos_processados.cobranca INTEGER`, usada pela Tarefa 7.
- `processar_documentos.processar_um_documento(...)`: mesma assinatura. Agora NUNCA levanta exceção; erro inesperado vira REVISAO_MANUAL e retorna `"ERRO"`.

- [ ] **Passo 1: teste (falha)**

```python
# -*- coding: utf-8 -*-
"""
Registros de documentos (05/10/2026): revisão manual guarda o pedido
quando ele é conhecido, não duplica a mesma revisão a cada rodada, e
falha de GCS/erro inesperado vira revisão (a retentativa pega) em vez
de sumir. Índice NF->pedido só com ENVIADO.
Rodar:  py -3.11 -m unittest documentos_pedido.test_registros_documentos
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

_RAIZ = Path(__file__).parent.parent
for _p in (_RAIZ, _RAIZ / "documentos_pedido"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import fingerprint_documentos as fp  # noqa: E402
import processar_documentos as pdoc  # noqa: E402


class _BancoTemp(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patch = patch.object(fp, "DB_PATH", Path(self._tmp.name) / "t.db")
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()


class Fingerprint(_BancoTemp):
    def test_indice_nf_ignora_revisao_manual(self):
        fp.marcar_processado("h1", "stokki", "PS-1_DANFE.pdf", "Nota Fiscal", "PS-1", "ENVIADO", numero_nf="10")
        fp.marcar_processado("h2", "stokki", "PS-2_DANFE.pdf", "Nota Fiscal", "PS-2", "REVISAO_MANUAL",
                             motivo="suspeita", numero_nf="20")
        self.assertEqual({r["numero_nf"] for r in fp.carregar_indice_nf()}, {"10"})

    def test_ja_em_revisao(self):
        fp.marcar_processado("h1", "stokki", "PS-1_DANFE.pdf", "Nota Fiscal", "PS-1", "REVISAO_MANUAL",
                             motivo="XML errado", numero_nf="24944")
        self.assertTrue(fp.ja_em_revisao("PS-1", "Nota Fiscal", "24944", "XML errado"))
        self.assertFalse(fp.ja_em_revisao("PS-1", "Nota Fiscal", "24945", "XML errado"))
        self.assertFalse(fp.ja_em_revisao("PS-9", "Nota Fiscal", "24944", "XML errado"))

    def test_coluna_cobranca(self):
        fp.marcar_processado("h1", "stokki", "PS-1_DANFE.pdf", "Nota Fiscal", "PS-1", "ENVIADO",
                             numero_nf="10", cobranca=1)
        con = fp._conectar()
        self.assertEqual(con.execute("select cobranca from documentos_processados").fetchone()[0], 1)
        con.close()


class ProcessarUmDocumento(unittest.TestCase):
    """Bordas mockadas: hash, classificação, texto, casamento, GCS, banco."""

    def _rodar(self, tipo="Nota Fiscal", casou="PS-1", texto="", gcs_erro=None, ja_rev=False,
               casar_erro=None, nome="PS-1_DANFE.pdf"):
        item = {"caminho_local": Path(nome), "nome_arquivo": nome, "assunto_email": None}
        with patch.object(pdoc, "calcular_hash", return_value="hX"), \
             patch.object(pdoc, "ja_processado", return_value=False), \
             patch.object(pdoc, "classificar_documento", return_value={"tipo": tipo, "confianca": "alta"}), \
             patch.object(pdoc, "_extrair_texto_pdf_completo", return_value=texto), \
             patch.object(pdoc, "extrair_nf_da_danfe", return_value=("24944", None)), \
             patch.object(pdoc, "_validar_danfe_do_stokki", return_value=None), \
             patch.object(pdoc, "casar_documento_com_pedido", side_effect=casar_erro,
                          return_value={"codigo_pedido": casou, "metodo": "nome_arquivo",
                                        "motivo_falha": None if casou else "não casou"}), \
             patch.object(pdoc.storage_gcs, "enviar_documento", side_effect=gcs_erro, return_value="gs://x"), \
             patch.object(pdoc, "ja_em_revisao", return_value=ja_rev), \
             patch.object(pdoc, "marcar_processado") as marcar:
            status = pdoc.processar_um_documento(item, MagicMock(), {}, modo_teste=False)
        return status, marcar

    def test_placeholder_grava_codigo_do_pedido(self):
        status, marcar = self._rodar(texto="CHAVE DE ACESSO 9908 2623 0000")
        self.assertEqual(status, "REVISAO_MANUAL")
        self.assertEqual(marcar.call_args.args[4], "PS-1")

    def test_mesma_revisao_nao_duplica(self):
        status, marcar = self._rodar(texto="CHAVE DE ACESSO 9908 2623 0000", ja_rev=True)
        self.assertEqual(status, "REVISAO_MANUAL")
        marcar.assert_not_called()

    def test_falha_no_gcs_vira_revisao_com_pedido(self):
        status, marcar = self._rodar(gcs_erro=RuntimeError("403"))
        self.assertEqual(status, "ERRO")
        self.assertEqual(marcar.call_args.args[5], "REVISAO_MANUAL")
        self.assertEqual(marcar.call_args.args[4], "PS-1")
        self.assertIn("GCS", marcar.call_args.kwargs["motivo"])

    def test_erro_inesperado_nao_derruba_e_vira_revisao(self):
        status, marcar = self._rodar(casar_erro=ConnectionError("vuupt fora"))
        self.assertEqual(status, "ERRO")
        self.assertEqual(marcar.call_args.args[5], "REVISAO_MANUAL")
        self.assertIn("vuupt fora", marcar.call_args.kwargs["motivo"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Passo 2:** `py -3.11 -m unittest documentos_pedido.test_registros_documentos -v` → FAIL. Faltam `ja_em_revisao` e `cobranca`, o placeholder grava `None` e a exceção escapa.

- [ ] **Passo 3: implementar `fingerprint_documentos.py`**

(a) Em `_COLUNAS_NOVAS`, acrescentar ao fim:

```python
    # 1 = a DANFE traz duplicata com vencimento (há cobrança, boleto
    # esperado); 0 = quadro de fatura sem duplicata; NULL = sem quadro
    # ou não é NF. Ver regras_documentos.cobranca_da_danfe (05/10).
    ("cobranca", "INTEGER"),
```

(b) Em `marcar_processado`:
- novo parâmetro `cobranca: int | None = None`;
- incluir a coluna `cobranca` no INSERT, com o `?` correspondente;
- no `ON CONFLICT`, acrescentar `cobranca = COALESCE(excluded.cobranca, cobranca)`;
- na tupla de valores, `cobranca` vai logo antes de `agora`.

(c) Em `carregar_indice_nf` e `pedidos_nf_pendentes`, acrescentar `AND status = 'ENVIADO'` ao WHERE, com este comentário em cada um:

```python
    # status='ENVIADO' (05/10): revisão manual agora também grava o
    # pedido quando ele é conhecido (DANFE suspeita, placeholder) -- sem
    # o filtro, uma DANFE de outra entrega envenenaria o índice.
```

(d) Função nova, depois de `ja_enviado_para_pedido`:

```python
def ja_em_revisao(codigo_pedido: str, tipo: str | None, numero_nf: str | None, motivo: str) -> bool:
    """True se a MESMA revisão (pedido + tipo + NF + motivo) já está
    registrada. A DANFE é regerada a cada rodada com hash novo -- sem
    isso, um XML errado na Stokki (PS-37626) virava uma linha nova por
    rodada (166 em 6 semanas, até 05/10)."""
    conn = _conectar()
    row = conn.execute(
        "SELECT 1 FROM documentos_processados WHERE status = 'REVISAO_MANUAL' AND codigo_pedido = ? "
        "AND COALESCE(tipo, '') = COALESCE(?, '') AND COALESCE(numero_nf, '') = COALESCE(?, '') "
        "AND motivo = ?",
        (codigo_pedido, tipo, numero_nf, motivo),
    ).fetchone()
    conn.close()
    return row is not None
```

- [ ] **Passo 4: implementar `processar_documentos.py`**

(a) Imports:
- acrescentar `ja_em_revisao` ao import de `fingerprint_documentos`;
- acrescentar `extrair_codigo_pedido` ao import de `matcher`.

(b) Renomear a função atual `processar_um_documento` para `_processar_um_documento`. A assinatura nova recebe `hash_conteudo` e não calcula mais o hash nem checa `ja_processado`:

```python
def _processar_um_documento(item: dict, vuupt, config: dict, modo_teste: bool,
                            tipos_permitidos: set[str] | None, indexador_nf: IndexadorNF | None,
                            hash_conteudo: str) -> str:
```

Apagar dela estas linhas, que vão para o wrapper:

```python
    hash_conteudo = calcular_hash(caminho)
    if not ignorar_ja_processado and ja_processado(hash_conteudo):
        return "JA_PROCESSADO"
```

(c) Helper novo, logo acima de `_processar_um_documento`:

```python
def _registrar_revisao(hash_conteudo: str, origem: str, nome_arquivo: str, tipo: str | None,
                       codigo_pedido: str | None, motivo: str, modo_teste: bool, **extras) -> None:
    """Grava REVISAO_MANUAL, sem duplicar a mesma revisão do mesmo
    pedido (ver fingerprint_documentos.ja_em_revisao)."""
    if modo_teste:
        return
    if codigo_pedido and ja_em_revisao(codigo_pedido, tipo, extras.get("numero_nf"), motivo):
        logger.info(f"  {nome_arquivo}: mesma revisão já registrada pra {codigo_pedido} -- não duplica.")
        return
    marcar_processado(hash_conteudo, origem, nome_arquivo, tipo, codigo_pedido,
                      "REVISAO_MANUAL", motivo=motivo, **extras)
```

(d) Dentro de `_processar_um_documento`:

O bloco do placeholder (hoje `if not modo_teste: marcar_processado(... None, "REVISAO_MANUAL", motivo=motivo_placeholder, numero_nf=numero_nf)`) vira:

```python
            _registrar_revisao(hash_conteudo, "email" if assunto_email is not None else "stokki",
                               nome_arquivo, classificacao["tipo"], extrair_codigo_pedido(nome_arquivo),
                               motivo_placeholder, modo_teste, numero_nf=numero_nf)
```

O bloco "não casou" vira o código abaixo (`codigo_pedido` continua `None`):

```python
        _registrar_revisao(hash_conteudo, origem, nome_arquivo, classificacao["tipo"], None,
                           correspondencia["motivo_falha"], modo_teste,
                           numero_nf=numero_nf, numero_parcela=numero_parcela,
                           total_parcelas=total_parcelas, cnpj_contraparte=cnpj_contraparte)
```

O bloco da DANFE suspeita vira:

```python
                _registrar_revisao(hash_conteudo, origem, nome_arquivo, classificacao["tipo"], codigo_pedido,
                                   motivo_suspeita, modo_teste,
                                   numero_nf=numero_nf, cnpj_contraparte=cnpj_contraparte)
```

O `except` do GCS vira:

```python
    except Exception as e:
        logger.error(f"  {nome_arquivo}: falha ao enviar pro GCS: {e}")
        # Antes (até 05/10) o documento sumia: e-mail já marcado como lido
        # e nada no banco. Como revisão, a retentativa da rodada completa
        # tenta subir de novo.
        _registrar_revisao(hash_conteudo, origem, nome_arquivo, classificacao["tipo"], codigo_pedido,
                           f"Falha ao enviar pro GCS: {e}", modo_teste,
                           numero_nf=numero_nf, numero_parcela=numero_parcela,
                           total_parcelas=total_parcelas, cnpj_contraparte=cnpj_contraparte)
        return "ERRO"
```

(e) Wrapper público, com a mesma assinatura de antes e a docstring antiga movida para cá, com o acréscimo:

```python
def processar_um_documento(item: dict, vuupt, config: dict, modo_teste: bool,
                           tipos_permitidos: set[str] | None = None,
                           indexador_nf: IndexadorNF | None = None,
                           ignorar_ja_processado: bool = False) -> str:
    """<docstring antiga>

    Nunca levanta exceção (05/10): erro inesperado (Vuupt fora, PDF
    estranho) vira REVISAO_MANUAL e devolve "ERRO" -- antes abortava a
    rodada inteira com os e-mails já marcados como lidos."""
    hash_conteudo = calcular_hash(item["caminho_local"])
    if not ignorar_ja_processado and ja_processado(hash_conteudo):
        return "JA_PROCESSADO"
    try:
        return _processar_um_documento(item, vuupt, config, modo_teste, tipos_permitidos,
                                       indexador_nf, hash_conteudo)
    except Exception as e:
        logger.exception(f"  {item['nome_arquivo']}: erro inesperado -- vai pra revisão manual: {e}")
        _registrar_revisao(hash_conteudo, "email" if item.get("assunto_email") is not None else "stokki",
                           item["nome_arquivo"], None, extrair_codigo_pedido(item["nome_arquivo"]),
                           f"Erro inesperado no processamento: {e}", modo_teste)
        return "ERRO"
```

- [ ] **Passo 5: filtrar ENVIADO nos dois leitores de fora**

`nucleo/validacao_fotos.py`, na consulta das NFs:
- acrescentar `AND d.status = 'ENVIADO'` depois de `AND d.tipo = 'Nota Fiscal'`;
- acrescentar `AND o.status = 'ENVIADO'` na subconsulta, depois de `o.tipo = 'Nota Fiscal'`.

`portal_cliente/enviar_stokki.py`, em `_codigo_em_documentos`: acrescentar `AND status = 'ENVIADO' ` depois de `WHERE tipo = 'Nota Fiscal'`.

- [ ] **Passo 6: rodar os testes**

```bash
py -3.11 -m unittest documentos_pedido.test_registros_documentos documentos_pedido.test_processar_documentos_modos -v
```

Esperado: OK. Os testes de modos continuam passando porque mockam `processar_um_documento` indiretamente via `buscar_documentos_do_pedido` vazio.

Rodar também a suíte do portal a partir da raiz: `py -3.11 -m unittest discover -s portal_cliente -p "test_*.py"`. Esperado: o mesmo resultado de antes da mudança. Rodar antes e depois para comparar.

- [ ] **Passo 7: commit**

```bash
git add documentos_pedido/fingerprint_documentos.py documentos_pedido/processar_documentos.py \
        documentos_pedido/test_registros_documentos.py nucleo/validacao_fotos.py portal_cliente/enviar_stokki.py
git commit -m "Documentos: revisão manual guarda o pedido e não duplica; falha de GCS/erro inesperado vira revisão"
```

---

### Tarefa 4: Arquivos sempre localizáveis (e-mail com prefixo, anexos com o mesmo rótulo)

**Arquivos:**
- Modificar: `documentos_pedido/email_documentos.py` (`_extrair_pdfs_anexados._salvar`)
- Modificar: `documentos_pedido/localizar_arquivos.py` (`resolver_arquivo_local`)
- Modificar: `documentos_pedido/processar_documentos.py` (`retentar_revisao_manual`; remover `_PASTAS_CACHE_DOCUMENTOS`)
- Modificar: `documentos_pedido/stokki_documentos.py` (`buscar_documentos_do_pedido`; função nova `_nome_unico`)
- Criar: `documentos_pedido/test_arquivos_documentos.py`

**Interfaces produzidas:**
- `localizar_arquivos.resolver_arquivo_local(nome_arquivo: str) -> Path | None`: mesma assinatura, agora com um fallback a mais.
- `stokki_documentos._nome_unico(nome: str, usados: set[str]) -> str`.

- [ ] **Passo 1: teste (falha)**

```python
# -*- coding: utf-8 -*-
"""
Rodar:  py -3.11 -m unittest documentos_pedido.test_arquivos_documentos
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import localizar_arquivos as la  # noqa: E402


class ResolverArquivo(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.anexos = base / "anexos_temp"
        self.anexos.mkdir()
        self._p = patch.object(la, "PASTAS_BUSCA", [base / "downloads_stokki_temp", self.anexos])
        self._p2 = patch.object(la, "_PASTA_ANEXOS", self.anexos)
        self._p.start(); self._p2.start()

    def tearDown(self):
        self._p.stop(); self._p2.stop(); self._tmp.cleanup()

    def test_nome_direto(self):
        (self.anexos / "76649_NFS SP.pdf").write_bytes(b"x")
        self.assertEqual(la.resolver_arquivo_local("76649_NFS SP.pdf").name, "76649_NFS SP.pdf")

    def test_legado_sem_prefixo_uid_com_candidato_unico(self):
        (self.anexos / "76649_danfe(17).PDF").write_bytes(b"x")
        self.assertEqual(la.resolver_arquivo_local("danfe(17).PDF").name, "76649_danfe(17).PDF")

    def test_legado_ambiguo_nao_chuta(self):
        (self.anexos / "1_BOLETOS.pdf").write_bytes(b"x")
        (self.anexos / "2_BOLETOS.pdf").write_bytes(b"y")
        self.assertIsNone(la.resolver_arquivo_local("BOLETOS.pdf"))

    def test_prefixo_que_nao_e_uid_nao_conta(self):
        (self.anexos / "PS-1_BOLETOS.pdf").write_bytes(b"x")
        self.assertIsNone(la.resolver_arquivo_local("BOLETOS.pdf"))


class NomeUnico(unittest.TestCase):
    def test_rotulo_repetido_ganha_sufixo(self):
        import stokki_documentos as sd
        usados: set[str] = set()
        self.assertEqual(sd._nome_unico("PS-1_BOLETO.pdf", usados), "PS-1_BOLETO.pdf")
        self.assertEqual(sd._nome_unico("PS-1_BOLETO.pdf", usados), "PS-1_BOLETO_2.pdf")
        self.assertEqual(sd._nome_unico("PS-1_BOLETO.pdf", usados), "PS-1_BOLETO_3.pdf")


if __name__ == "__main__":
    unittest.main()
```

Se `import stokki_documentos` puxar o playwright no import e falhar localmente, mover `_nome_unico` para `localizar_arquivos.py` (sem imports pesados) e importar de lá em `stokki_documentos.py`. Nesse caso, ajustar o teste para `la._nome_unico`.

- [ ] **Passo 2:** rodar → FAIL.

- [ ] **Passo 3: implementar**

`localizar_arquivos.py`: adicionar `import re` e substituir `resolver_arquivo_local`:

```python
_PASTA_ANEXOS = _PASTA_DADOS / "anexos_temp"


def resolver_arquivo_local(nome_arquivo: str) -> Path | None:
    """
    Devolve o Path do primeiro arquivo com esse nome nas pastas de
    busca, ou None se não estiver em nenhuma (aí o chamador decide:
    registrar pendência, tentar GCS etc).

    Legado (até 05/10/2026): a busca ampla de e-mail gravava no banco o
    nome SEM o prefixo "{uid}_" que o arquivo tem em anexos_temp/ --
    56 documentos ficavam "sem arquivo". Aceita o arquivo com prefixo
    numérico só quando há UM candidato ("BOLETOS.pdf" se repete entre
    e-mails; na dúvida, não chuta).
    """
    if not nome_arquivo:
        return None
    for pasta in PASTAS_BUSCA:
        candidato = pasta / nome_arquivo
        if candidato.is_file():
            return candidato
    if _PASTA_ANEXOS.is_dir():
        padrao = re.compile(r"\d+_" + re.escape(nome_arquivo))
        candidatos = [p for p in _PASTA_ANEXOS.iterdir() if padrao.fullmatch(p.name)]
        if len(candidatos) == 1:
            return candidatos[0]
    return None
```

`email_documentos.py`, em `_salvar`: trocar `"nome_arquivo": nome_arquivo,` por:

```python
            # Nome COM o prefixo do UID, igual ao arquivo em disco (05/10):
            # sem ele o romaneio e a retentativa não achavam o arquivo.
            "nome_arquivo": caminho_local.name,
```

`processar_documentos.py`, em `retentar_revisao_manual`, trocar a busca nas pastas:

```python
        caminho = next((p / row["nome_arquivo"] for p in _PASTAS_CACHE_DOCUMENTOS
                        if (p / row["nome_arquivo"]).exists()), None)
```

por:

```python
        caminho = resolver_arquivo_local(row["nome_arquivo"])
```

Também:
- adicionar `from localizar_arquivos import resolver_arquivo_local` aos imports;
- apagar a constante `_PASTAS_CACHE_DOCUMENTOS` e o comentário dela (ficam órfãos);
- ajustar a docstring para dizer "as pastas de `localizar_arquivos.PASTAS_BUSCA`".

`stokki_documentos.py`: função nova, acima de `buscar_documentos_do_pedido`:

```python
def _nome_unico(nome: str, usados: set[str]) -> str:
    """Dois anexos com o mesmo rótulo no pedido ("BOLETO" duas vezes,
    parcelas) viravam o mesmo arquivo em disco e o segundo apagava o
    primeiro antes de ser processado (achado 05/10). O primeiro mantém
    o nome de sempre; os seguintes ganham _2, _3..."""
    if nome not in usados:
        usados.add(nome)
        return nome
    base, ponto, ext = nome.rpartition(".")
    i = 2
    while f"{base}_{i}{ponto}{ext}" in usados:
        i += 1
    novo = f"{base}_{i}{ponto}{ext}"
    usados.add(novo)
    return novo
```

Em `buscar_documentos_do_pedido`:
- antes do `for doc in listar_documentos_da_aba(page):`, adicionar `nomes_usados: set[str] = set()`;
- depois de `nome_arquivo = f"{codigo_ps}_{nome_arquivo}"`, adicionar `nome_arquivo = _nome_unico(nome_arquivo, nomes_usados)`.

- [ ] **Passo 4:** `py -3.11 -m unittest documentos_pedido.test_arquivos_documentos documentos_pedido.test_processar_documentos_modos -v` → OK.

- [ ] **Passo 5: commit**

```bash
git add documentos_pedido/email_documentos.py documentos_pedido/localizar_arquivos.py \
        documentos_pedido/processar_documentos.py documentos_pedido/stokki_documentos.py \
        documentos_pedido/test_arquivos_documentos.py
git commit -m "Documentos: anexo de e-mail e anexos de mesmo rótulo sempre localizáveis no romaneio e na retentativa"
```

---

### Tarefa 5: Seleção pega NF que chega depois do "Sent", de todos os embarcadores; IMAP com falha aparece como erro

**Arquivos:**
- Modificar: `documentos_pedido/selecionar_pedidos.py`
- Modificar: `documentos_pedido/email_documentos.py` (`ULTIMO_ERRO_IMAP`)
- Modificar: `documentos_pedido/processar_documentos.py` (etapas de e-mail, log do pedido vazio, "Resumo geral")
- Criar: `documentos_pedido/test_selecionar_pedidos.py`

**Interfaces:**
- `selecionar_pedidos.descobrir_expedidos_recentes(sessao, limite: int = 300) -> list[tuple[str, bool]]`: mesmo formato `(codigo, danfe_bloqueada)`, agora para todos os embarcadores.
- `email_documentos.ULTIMO_ERRO_IMAP: dict[str, str | None]`, com chaves `"inbox"` e `"embarcadores"`.

- [ ] **Passo 1: teste (falha)**

```python
# -*- coding: utf-8 -*-
"""
Rodar:  py -3.11 -m unittest documentos_pedido.test_selecionar_pedidos
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_RAIZ = Path(__file__).parent.parent
for _p in (_RAIZ, _RAIZ / "documentos_pedido"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import selecionar_pedidos as sel  # noqa: E402


def _linha(id_stokki: int, stkkc: str, nome: str = "CLIENTE X") -> dict:
    return {"id": f'<a href="/provider/outbound/show/{id_stokki}">{id_stokki}</a>',
            "client": f'{nome} <span class="text-muted">#stkkc-{stkkc}</span>'}


class ExpedidosRecentes(unittest.TestCase):
    def _rodar(self, linhas, com_nf=frozenset()):
        paginas = [{"aaData": linhas}, {"aaData": []}]
        with patch.object(sel.stokki_pedidos, "listar_pedidos", side_effect=paginas) as listar, \
             patch("fingerprint_documentos.pedidos_com_documento_enviado", return_value=set(com_nf)):
            return sel.descobrir_expedidos_recentes(object()), listar

    def test_todos_os_embarcadores_sem_filtro_de_cliente(self):
        res, listar = self._rodar([_linha(40598, "70")])
        self.assertEqual(res, [("PS-40598", False)])
        self.assertEqual(listar.call_args_list[0].kwargs.get("cliente", ""), "")
        self.assertEqual(listar.call_args_list[0].kwargs["status"], "Sent")

    def test_quem_ja_tem_nf_fica_fora(self):
        res, _ = self._rodar([_linha(1, "70"), _linha(2, "70")], com_nf={"PS-1"})
        self.assertEqual([c for c, _ in res], ["PS-2"])

    def test_embarcador_sem_nf_fica_fora(self):
        res, _ = self._rodar([_linha(1, "23"), _linha(2, "96"), _linha(3, "70")])
        self.assertEqual([c for c, _ in res], ["PS-3"])

    def test_dourado_marca_danfe_bloqueada(self):
        res, _ = self._rodar([_linha(5, "18")])
        self.assertEqual(res, [("PS-5", True)])

    def test_erro_da_stokki_nao_derruba(self):
        with patch.object(sel.stokki_pedidos, "listar_pedidos", side_effect=RuntimeError("500")), \
             patch("fingerprint_documentos.pedidos_com_documento_enviado", return_value=set()):
            self.assertEqual(sel.descobrir_expedidos_recentes(object()), [])


class ListaQualquerStatus(unittest.TestCase):
    def test_decisao_do_hugo_05_10(self):
        self.assertEqual(set(sel.EMBARCADORES_QUALQUER_STATUS), {"23", "18", "98"})


if __name__ == "__main__":
    unittest.main()
```

Conferir em `stokki/pedidos.py::extrair_id_da_linha` o formato real do campo `id`, que precisa ter `/show/<id>`. Se o regex esperar outro caminho, ajustar o HTML de `_linha`.

- [ ] **Passo 2:** rodar → FAIL.

- [ ] **Passo 3: implementar `selecionar_pedidos.py`**

(a) Docstring do módulo, primeiro parágrafo:

```
Decide quais pedidos (PS-XXXXX) o agente de documentos vai buscar na
Stokki. Padrão Puro, Dourado e Quatro Estrelas entram em QUALQUER etapa
aberta da Stokki (decisão do Hugo, 05/10/2026); o resto entra em
"Aguardando Transportador"; e os "Sent" mais recentes de TODOS os
embarcadores que ainda não têm NF no banco entram também (a NF costuma
ser anexada depois da expedição -- 25 pedidos sem NF em 14 dias, até 05/10).
```

(b) `EMBARCADORES_QUALQUER_STATUS`:

```python
# {id_stokki: nome_legivel} -- decisão do Hugo, 05/10/2026 (era Padrão
# Puro, Dourado e Jersey; o pipeline.py tinha Quatro Estrelas no lugar
# de Padrão Puro). Mesmos IDs de pipeline.py::EMBARCADORES_IMPORTAR_ABERTOS.
EMBARCADORES_QUALQUER_STATUS: dict[str, str] = {
    "23": "PADRAO PURO LTDA",
    "18": "LATICINIOS DOURADO - INDUSTRIA E COMERCIO LTDA",
    "98": "COMERCIO DE CEREAIS QUATRO ESTRELAS LTDA",
}
```

(c) Apagar `EMBARCADORES_BOLETO_EMAIL`, que fica sem uso, e o comentário acima dela. Manter `STATUS_EXPEDIDO = "Sent"`.

(d) Substituir `descobrir_expedidos_recentes`:

```python
def descobrir_expedidos_recentes(sessao: StokkiSession, limite: int = 300) -> list[tuple[str, bool]]:
    """
    Os `limite` pedidos "Sent" mais recentes de TODOS os embarcadores
    que ainda não têm Nota Fiscal/Pedido de Venda ENVIADO no banco.
    Até 05/10 isso valia só pra 3 embarcadores de boleto por e-mail; a
    medição de 05/10 achou 25 pedidos de 9 embarcadores com a DANFE na
    Stokki e nunca buscada, porque a NF foi anexada depois do "Sent".

    Embarcador sem NF (Padrão Puro, Pedramoura) fica fora. Retorna
    [(codigo, danfe_bloqueada)] -- danfe_bloqueada: Dourado/Muai, só
    a aba Documentos é olhada (ver EMBARCADORES_DANFE_SOMENTE_EMAIL).
    Páginas de 100 (o DataTables da Stokki não é testado com mais).
    """
    from fingerprint_documentos import pedidos_com_documento_enviado

    com_nf = pedidos_com_documento_enviado()
    codigos: list[tuple[str, bool]] = []
    vistos: set[str] = set()
    try:
        for pagina in range(0, (limite + 99) // 100):
            resposta = stokki_pedidos.listar_pedidos(
                sessao, status=STATUS_EXPEDIDO, pagina=pagina, por_pagina=100,
                ordenar_coluna="1", ordenar_dir="desc",
            )
            linhas = resposta.get("aaData", [])
            if not linhas:
                break
            for linha in linhas:
                codigo = _codigo_da_linha(linha)
                if not codigo or codigo in vistos or codigo in com_nf or _pedido_sem_nf(linha):
                    continue
                vistos.add(codigo)
                codigos.append((codigo, _pedido_danfe_bloqueada(linha)))
    except Exception as e:
        logger.warning(f"Erro ao buscar expedidos recentes: {e}")
    return codigos
```

Conferir se `listar_pedidos` interpreta `pagina` como índice de página ou como offset: ler o corpo da função em `stokki/pedidos.py`. Se for offset (`start`), passar `pagina=pagina * 100`.

(e) Em `descobrir_pedidos`, trocar o texto do log final para:

```python
    logger.info(f"Expedidos recentes ainda sem NF (todos os embarcadores): "
               f"{len(codigos) - n_antes} pedido(s) adicionados.")
```

E atualizar a docstring: "todos os pedidos em aberto dos embarcadores prioritários + 'Aguardando Transportador' do resto + 'Sent' recentes ainda sem NF".

- [ ] **Passo 4: implementar o erro de IMAP**

`email_documentos.py`, perto das constantes do topo:

```python
# Última falha de IMAP de cada busca (None = rodou sem erro). Até 05/10
# a falha virava lista vazia e a etapa saía "ok, 0 encontrado(s)" na
# notificação; processar_documentos.py lê isto pra marcar como erro.
ULTIMO_ERRO_IMAP: dict[str, str | None] = {"inbox": None, "embarcadores": None}
```

Em `buscar_pdfs_por_email`:
- logo depois de `resultado = []`, adicionar `ULTIMO_ERRO_IMAP["inbox"] = None`;
- no `except Exception as e:`, depois do `logger.exception`, adicionar `ULTIMO_ERRO_IMAP["inbox"] = str(e)`.

Em `buscar_pdfs_por_email_embarcadores`, o mesmo com a chave `"embarcadores"`.

`processar_documentos.py`:
- adicionar `import email_documentos` aos imports, junto do `from email_documentos import buscar_pdfs_por_email`.
- Etapa 1: trocar o `resumo_etapas["Documentos (e-mail)"] = {...}` por:

```python
            erro_imap = email_documentos.ULTIMO_ERRO_IMAP.get("inbox")
            resumo_etapas["Documentos (e-mail)"] = (
                {"status": "erro", "detalhe": f"falha no IMAP: {erro_imap}"} if erro_imap
                else {"status": "ok", "detalhe": f"{len(itens_email)} encontrado(s)"})
```

- Em `_etapas_email_embarcadores`, o mesmo padrão com a chave `"embarcadores"` e o detalhe atual.
- Em "Resumo geral", trocar a condição de status por:

```python
        "status": "erro" if contadores["ERRO"] or any(
            (info or {}).get("status") != "ok" for info in resumo_etapas.values()) else "ok",
```

- [ ] **Passo 5: log do pedido sem documento.** No loop da etapa 2, depois de `itens_stokki = buscar_documentos_do_pedido(...)`:

```python
                    if not itens_stokki:
                        # Antes ficava em silêncio (PS-35977 no teste de 05/10)
                        logger.info(f"  {codigo_ps}: nenhum documento novo na Stokki "
                                    f"(sem DANFE a gerar e aba Documentos vazia).")
```

- [ ] **Passo 6:** `py -3.11 -m unittest documentos_pedido.test_selecionar_pedidos documentos_pedido.test_processar_documentos_modos -v` → OK.

Se algum teste de modos verificar `resumo_etapas["Resumo geral"]["status"] == "ok"` com uma etapa "erro" simulada, revisar se a expectativa ainda faz sentido e mostrar ao Hugo antes de mudar o teste.

- [ ] **Passo 7: commit**

```bash
git add documentos_pedido/selecionar_pedidos.py documentos_pedido/email_documentos.py \
        documentos_pedido/processar_documentos.py documentos_pedido/test_selecionar_pedidos.py
git commit -m "Documentos: busca NF dos Sent recentes de todos os embarcadores; lista qualquer status = Padrão Puro, Dourado, Quatro Estrelas; IMAP com falha vira erro"
```

---

### Tarefa 6: `pipeline.py` com a mesma lista "qualquer status" (CONFIRMADO pelo Hugo em 05/10)

**Arquivos:** Modificar `pipeline.py` (`EMBARCADORES_IMPORTAR_ABERTOS`, ~linha 119).

Isso muda a IMPORTAÇÃO para a Vuupt, não só os documentos, e o Hugo confirmou em 05/10:
- Padrão Puro passa a subir em Open, Separating, Ready to Pack e On hold;
- Jersey Vale passa a subir só em "Aguardando Transportador".

- [ ] **Passo 1:** trocar o dicionário:

```python
# Decisão do Hugo, 05/10/2026: Padrão Puro, Dourado e Quatro Estrelas
# (Jersey Vale saiu; Padrão Puro entrou). Mesma lista de
# documentos_pedido/selecionar_pedidos.py::EMBARCADORES_QUALQUER_STATUS.
EMBARCADORES_IMPORTAR_ABERTOS: dict[str, str] = {
    "98": "COMERCIO DE CEREAIS QUATRO ESTRELAS LTDA",
    "18": "LATICINIOS DOURADO - INDUSTRIA E COMERCIO LTDA",
    "23": "PADRAO PURO LTDA",
}
```

- [ ] **Passo 2:** `git grep -n "EMBARCADORES_IMPORTAR_ABERTOS\|\"79\"" -- '*.py'`. Conferir se algum outro lugar (vigia de pedidos, `test_*`) depende da Jersey nessa lista e listar esses lugares para o Hugo.

- [ ] **Passo 3:** `py -3.11 -m py_compile pipeline.py` e os testes que citem o pipeline, se existirem (`git grep -l "import pipeline" -- 'test_*.py' '*/test_*.py'`).

- [ ] **Passo 4: commit**

```bash
git add pipeline.py
git commit -m "Pipeline: importa em qualquer status Padrão Puro, Dourado e Quatro Estrelas (decisão do Hugo 05/10)"
```

---

### Tarefa 7: Regra do boleto esperado, a partir da DANFE

**Arquivos:**
- Criar: `documentos_pedido/regras_documentos.py` (puro, sem imports do projeto)
- Criar: `documentos_pedido/test_regras_documentos.py`
- Modificar: `documentos_pedido/processar_documentos.py` (grava `cobranca` da NF; backfill na rodada completa)
- Modificar: `documentos_pedido/fingerprint_documentos.py` (`listar_nfs_sem_cobranca`, `atualizar_cobranca`)

**Interfaces produzidas:**
- `regras_documentos.cobranca_da_danfe(texto: str) -> int | None`
- `regras_documentos.boleto_esperado(sender_id: int | None, cobrancas: list[int | None]) -> bool`
- `regras_documentos.SENDERS_BOLETO_SEM_FATURA: dict[int, str]`
- `fingerprint_documentos.listar_nfs_sem_cobranca(dias: int = 30) -> list[dict]` (chaves `hash_conteudo`, `nome_arquivo`)
- `fingerprint_documentos.atualizar_cobranca(hash_conteudo: str, cobranca: int | None) -> None`

- [ ] **Passo 1: teste (falha)**

```python
# -*- coding: utf-8 -*-
"""
Rodar:  py -3.11 -m unittest documentos_pedido.test_regras_documentos
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from regras_documentos import cobranca_da_danfe, boleto_esperado  # noqa: E402

DOURADO = 11426239
QUATRO_ESTRELAS = 21785428


class Cobranca(unittest.TestCase):
    def test_nfephp_com_duplicata(self):
        t = "FATURA / DUPLICATA\nNum. 001\nVenc. 21/10/2026\nValor R$ 1.125,60\nCÁLCULO DO IMPOSTO"
        self.assertEqual(cobranca_da_danfe(t), 1)

    def test_tres_parcelas(self):
        t = ("FATURA / DUPLICATA\nNum. 001 Num. 002 Num. 003\nVenc. 22/10/2026 Venc. 22/11/2026 "
             "Venc. 22/12/2026\nValor R$ 532,16\nCÁLCULO DO IMPOSTO")
        self.assertEqual(cobranca_da_danfe(t), 1)

    def test_layout_tabela(self):
        t = "FATURA / DUPLICATAS\nNº DUPLICATA VENC. VALOR\n001 31/08/2026 1.342,86\nBASE DE CÁLCULO DO ICMS"
        self.assertEqual(cobranca_da_danfe(t), 1)

    def test_quadro_vazio(self):
        t = "FATURA / DUPLICATA\nNÚMERO VENCIMENTO VALOR\nCÁLCULO DO IMPOSTO\nVenc. 01/01/2027"
        self.assertEqual(cobranca_da_danfe(t), 0)   # o "Venc." depois do quadro não conta

    def test_sem_quadro(self):
        self.assertIsNone(cobranca_da_danfe("DANFE Nº 178130 CHAVE DE ACESSO ..."))
        self.assertIsNone(cobranca_da_danfe(""))


class BoletoEsperado(unittest.TestCase):
    def test_duplicata_manda(self):
        self.assertTrue(boleto_esperado(QUATRO_ESTRELAS, [1]))

    def test_quadro_vazio_dispensa_mesmo_na_lista(self):
        self.assertFalse(boleto_esperado(DOURADO, [0]))

    def test_sem_quadro_cai_na_lista_do_hugo(self):
        self.assertTrue(boleto_esperado(DOURADO, [None]))
        self.assertTrue(boleto_esperado(DOURADO, []))          # sem NF nenhuma

    def test_sem_quadro_fora_da_lista_nao_acusa(self):
        self.assertFalse(boleto_esperado(QUATRO_ESTRELAS, [None]))
        self.assertFalse(boleto_esperado(None, []))

    def test_duas_nfs_uma_com_duplicata(self):
        self.assertTrue(boleto_esperado(QUATRO_ESTRELAS, [0, 1]))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Passo 2:** rodar → FAIL (módulo não existe).

- [ ] **Passo 3: criar `documentos_pedido/regras_documentos.py`**

```python
# -*- coding: utf-8 -*-
"""
regras_documentos.py

Regras de "que documento o pedido precisa ter", compartilhadas pelo
romaneio (roteirizacao/gerar_pdf_romaneios.py) e pelo vigia
(documentos_pedido/vigia_documentos.py). Sem imports do projeto: é
importado de fora de documentos_pedido/.

Boleto esperado (decisão do Hugo, 05/10/2026: "quase todos têm boleto,
mas não necessariamente pra todos os clientes"): quem diz é a própria
NF. DANFE com duplicata no quadro FATURA/DUPLICATA = venda a prazo =
tem boleto. Medido em 180 DANFEs reais (05/10): 49 com duplicata e
boleto, 48 com duplicata e sem boleto (NUU, Sotille, Vida Veg -- é a
pendência real), 83 sem o quadro (Quatro Estrelas, Jersey, e a DANFE
que a Dourado manda por e-mail). Sem quadro não dá pra decidir pela NF:
vale a lista de embarcadores que o Hugo disse que mandam boleto.
"""
import re

# sender_id da VUUPT (tabela interno) -> nome. Lista do Hugo, 05/10:
# grupo Marchef, NUU, Dourado, Muai, Ciao (= De Tommaso), Sotille,
# Vida Veg, Jersey Vale. Muai sem sender_id na interno em 05/10:
# acrescentar quando tiver.
SENDERS_BOLETO_SEM_FATURA: dict[int, str] = {
    11426239: "DOURADO",
    20562589: "DE TOMMASO (CIAO)",
    20011781: "JERSEY VALE",
    16362183: "NUU",
    20135423: "VIDAVEG",
    15714169: "AMAZONIKA (SOTILLE)",
    18164791: "MARCHEF - ITAUEIRA",
    21849438: "MARCHEF - JEE",
    18164790: "MARCHEF - ALMAZ",
    18638577: "MARCHEF - GOURMAR",
    21849668: "MARCHEF - MARCHEF",
    21849487: "MARCHEF - 3M",
    19276807: "MARCHEF - COPACAM",
    19445576: "MARCHEF - AOS AREIA",
    19013058: "MARCHEF - FONSECA",
}

_RE_QUADRO_FATURA = re.compile(r"FATURA\s*/?\s*DUPLICATA", re.IGNORECASE)
# O quadro termina onde começa o cálculo do imposto (todos os layouts vistos)
_RE_FIM_QUADRO = re.compile(r"C[ÁA]LCULO\s+DO\s+IMPOSTO|BASE\s+DE\s+C[ÁA]LCULO", re.IGNORECASE)
# Uma duplicata: "Venc. 21/10/2026" (NFePHP/Olist) ou "001 31/08/2026 1.342,86" (tabela)
_RE_DUPLICATA = re.compile(
    r"(?:Venc\.?|Vencimento)\s*\d{2}/\d{2}/\d{4}"
    r"|\d{2}/\d{2}/\d{4}\s*(?:R\$\s*)?\d{1,3}(?:\.\d{3})*,\d{2}",
    re.IGNORECASE,
)


def cobranca_da_danfe(texto: str) -> int | None:
    """1 = quadro de fatura com duplicata; 0 = quadro sem duplicata;
    None = DANFE sem o quadro (ou texto vazio)."""
    m = _RE_QUADRO_FATURA.search(texto or "")
    if not m:
        return None
    resto = texto[m.end():]
    fim = _RE_FIM_QUADRO.search(resto)
    quadro = resto[:fim.start()] if fim else resto[:400]
    return 1 if _RE_DUPLICATA.search(quadro) else 0


def boleto_esperado(sender_id: int | None, cobrancas: list[int | None]) -> bool:
    """cobrancas: o valor de cobranca_da_danfe de cada NF do pedido
    (lista vazia = pedido sem NF)."""
    if any(c == 1 for c in cobrancas):
        return True
    if cobrancas and all(c == 0 for c in cobrancas):
        return False
    return sender_id in SENDERS_BOLETO_SEM_FATURA
```

- [ ] **Passo 4:** rodar o teste → OK (10).

- [ ] **Passo 5: gravar a cobrança no processamento.**

`processar_documentos.py`:
- import: `from regras_documentos import cobranca_da_danfe`;
- em `_processar_um_documento`, no ramo `if classificacao["tipo"] == "Nota Fiscal":`, logo depois de `numero_nf, cnpj_contraparte = extrair_nf_da_danfe(texto_completo)`, adicionar:

```python
        cobranca = cobranca_da_danfe(texto_completo)
```

- inicializar `cobranca = None` junto de `numero_nf = cnpj_contraparte = None`;
- na chamada `marcar_processado(... "ENVIADO", ...)` do envio ao GCS, acrescentar `cobranca=cobranca`.

`fingerprint_documentos.py`, duas funções novas no fim:

```python
def listar_nfs_sem_cobranca(dias: int = 30) -> list[dict]:
    """NFs ENVIADAS recentes gravadas antes da coluna cobranca (05/10) --
    candidatas ao backfill a partir do PDF local."""
    conn = _conectar()
    rows = conn.execute(
        "SELECT hash_conteudo, nome_arquivo FROM documentos_processados "
        "WHERE tipo = 'Nota Fiscal' AND status = 'ENVIADO' AND cobranca IS NULL "
        "AND processado_em >= datetime('now', ?)", (f"-{int(dias)} days",)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def atualizar_cobranca(hash_conteudo: str, cobranca: int | None) -> None:
    conn = _conectar()
    conn.execute("UPDATE documentos_processados SET cobranca = ? WHERE hash_conteudo = ?",
                 (cobranca, hash_conteudo))
    conn.commit()
    conn.close()
```

**Armadilha:** NF sem quadro grava `cobranca=None` e continuaria eternamente em `listar_nfs_sem_cobranca`. Para não reler o PDF toda rodada, o backfill grava `-1` quando o quadro não existe, e a regra trata `-1` como None. Ajustar então:
- `boleto_esperado`: normalizar no início com `cobrancas = [None if c == -1 else c for c in cobrancas]`;
- teste novo em `BoletoEsperado`:

```python
    def test_menos_um_e_sem_quadro(self):
        self.assertFalse(boleto_esperado(QUATRO_ESTRELAS, [-1]))
        self.assertTrue(boleto_esperado(DOURADO, [-1]))
```

- no processamento normal, gravar `cobranca if cobranca is not None else -1` para Nota Fiscal (e `None` para os outros tipos).

`processar_documentos.py`, função nova, depois de `_backfill_nf_danfes_locais`:

```python
def _backfill_cobranca_nfs(dias: int = 30) -> None:
    """NFs enviadas antes de 05/10 não têm a coluna cobranca. Lê o PDF
    local uma vez e grava (1/0, ou -1 = sem quadro de fatura)."""
    from fingerprint_documentos import listar_nfs_sem_cobranca, atualizar_cobranca
    preenchidas = 0
    for row in listar_nfs_sem_cobranca(dias):
        caminho = resolver_arquivo_local(row["nome_arquivo"])
        if not caminho:
            continue
        valor = cobranca_da_danfe(_extrair_texto_pdf_completo(caminho))
        atualizar_cobranca(row["hash_conteudo"], -1 if valor is None else valor)
        preenchidas += 1
    if preenchidas:
        logger.info(f"Backfill de cobrança: {preenchidas} NF(s) marcada(s).")
```

Chamar só na rodada completa e fora do modo teste, logo antes da etapa 4:

```python
        if not escopado and not incremental and not modo_teste:
            _backfill_cobranca_nfs()
```

No teste de modos, acrescentar o mock `stack.enter_context(patch.object(pdoc, "_backfill_cobranca_nfs"))` em `_Base._rodar`.

- [ ] **Passo 6:**

```bash
py -3.11 -m unittest documentos_pedido.test_regras_documentos documentos_pedido.test_registros_documentos documentos_pedido.test_processar_documentos_modos -v
```

Esperado: OK.

- [ ] **Passo 7: commit**

```bash
git add documentos_pedido/regras_documentos.py documentos_pedido/test_regras_documentos.py \
        documentos_pedido/processar_documentos.py documentos_pedido/fingerprint_documentos.py \
        documentos_pedido/test_processar_documentos_modos.py
git commit -m "Documentos: boleto esperado decidido pela DANFE (quadro de duplicata) com lista de embarcadores de fallback"
```

---

### Tarefa 8: Romaneio só acusa "sem boleto" quando o boleto é esperado

**Arquivos:**
- Modificar: `roteirizacao/gerar_pdf_romaneios.py` (`montar_pdf_rota`, `gerar_capa`; função nova `avaliar_documentos_pedido`)
- Criar: `roteirizacao/test_romaneio_boleto_esperado.py`

**Interfaces:**
- Consome `regras_documentos.boleto_esperado` (Tarefa 7) e a coluna `cobranca` (Tarefa 3/7).
- Produz `gerar_pdf_romaneios.avaliar_documentos_pedido(codigo: str, sender_id: int | None, docs_por_pedido: dict) -> dict`, com as chaves `nfs`, `boletos`, `nf_dispensada`, `boleto_esperado`, `faltas` (lista de str). É usada pelo vigia (Tarefa 9).

- [ ] **Passo 1: teste (falha)**

```python
# -*- coding: utf-8 -*-
"""
Rodar (da raiz):  py -3.11 -m unittest roteirizacao.test_romaneio_boleto_esperado
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import gerar_pdf_romaneios as gpr  # noqa: E402

DOURADO, QUATRO_ESTRELAS, PADRAO_PURO = 11426239, 21785428, 12887364


def _nf(cobranca, codigo="PS-1"):
    return {"tipo": "Nota Fiscal", "codigo_pedido": codigo, "numero_nf": "10", "origem": "stokki",
            "processado_em": "2026-10-05", "hash_conteudo": "h", "nome_arquivo": "PS-1_DANFE.pdf",
            "cobranca": cobranca}


def _abrir_tudo(rows):
    return [(r, None) for r in rows], []


class AvaliarDocumentos(unittest.TestCase):
    def _av(self, sender, docs):
        with patch.object(gpr, "_abrir_documentos", side_effect=_abrir_tudo):
            return gpr.avaliar_documentos_pedido("PS-1", sender, {"PS-1": docs})

    def test_nf_com_duplicata_sem_boleto_acusa(self):
        self.assertIn("sem boleto", self._av(QUATRO_ESTRELAS, [_nf(1)])["faltas"])

    def test_nf_sem_quadro_fora_da_lista_nao_acusa(self):
        r = self._av(QUATRO_ESTRELAS, [_nf(-1)])
        self.assertNotIn("sem boleto", r["faltas"])
        self.assertFalse(r["boleto_esperado"])

    def test_dourado_sem_quadro_acusa(self):
        self.assertIn("sem boleto", self._av(DOURADO, [_nf(None)])["faltas"])

    def test_nf_dispensada_continua_dispensada(self):
        r = self._av(PADRAO_PURO, [])
        self.assertTrue(r["nf_dispensada"])
        self.assertNotIn("sem nota fiscal", r["faltas"])

    def test_sem_nf_acusa_nf(self):
        self.assertIn("sem nota fiscal", self._av(QUATRO_ESTRELAS, [])["faltas"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Passo 2:** rodar → FAIL (`avaliar_documentos_pedido` não existe).

- [ ] **Passo 3: implementar**

(a) Import, junto de `from localizar_arquivos import resolver_arquivo_local`:

```python
from regras_documentos import boleto_esperado as _boleto_esperado
```

(b) Função nova, logo antes de `montar_pdf_rota`. Ela é o trecho que hoje está dentro do loop, extraído sem mudar o comportamento de NF:

```python
def avaliar_documentos_pedido(codigo: str, sender_id: int | None, docs_por_pedido: dict) -> dict:
    """NF/boletos utilizáveis do pedido e o que falta -- a mesma régua da
    capa do romaneio, reaproveitada pelo vigia de documentos (05/10).
    "sem boleto" só quando o boleto é esperado (regras_documentos)."""
    docs = [d for sub in _codigos_base_lista(codigo) for d in docs_por_pedido.get(sub, [])]
    nf_dispensada = sender_id in SENDERS_SEM_NF
    if nf_dispensada:
        nfs, problemas_nf = [], []
    else:
        nfs, problemas_nf = _abrir_documentos(selecionar_nfs(docs))
        if not nfs and sender_id in SENDERS_PEDIDO_VENDA_SUBSTITUI_NF:
            nfs, problemas_nf = _abrir_documentos(selecionar_pedidos_de_venda(docs))
    boletos, problemas_bol = _abrir_documentos(selecionar_boletos(docs))
    esperado = _boleto_esperado(sender_id, [row.get("cobranca") for row, _r in nfs
                                            if row.get("tipo") == "Nota Fiscal"])

    faltas = []
    if not nfs and not nf_dispensada:
        faltas.append("sem nota fiscal")
    if not boletos and esperado:
        faltas.append("sem boleto")
    faltas.extend(problemas_nf + problemas_bol)
    return {"nfs": nfs, "boletos": boletos, "nf_dispensada": nf_dispensada,
            "boleto_esperado": esperado, "faltas": faltas}
```

(c) Em `montar_pdf_rota`, substituir o bloco que vai de `docs = [d for sub in _codigos_base_lista(codigo) ...` até `pendencias.extend(f"{codigo}: {f}" for f in faltas)`, inclusive o comentário longo do `nf_dispensada`, por:

```python
        # nf_dispensada: embarcadores da CANHOTEIRA -- NF nunca aberta,
        # emendada ou contada (achado 20/08). Régua completa em
        # avaliar_documentos_pedido().
        av = avaliar_documentos_pedido(codigo, sender_id, docs_por_pedido)
        nfs, boletos, nf_dispensada = av["nfs"], av["boletos"], av["nf_dispensada"]
        pendencias.extend(f"{codigo}: {f}" for f in av["faltas"])
```

No dicionário `itens.append({...})`, acrescentar:

```python
            # boleto não esperado (NF à vista / embarcador fora da lista)
            # -> capa mostra "—" em vez do X vermelho (05/10)
            "boleto_dispensado": not boletos and not av["boleto_esperado"],
```

(d) Em `gerar_capa`, trocar `_marca(draw, _COL_BOL_CX, meio, item["tem_boleto"])` por:

```python
        if item.get("boleto_dispensado"):
            draw.text((_COL_BOL_CX, meio), "—", font=fonte_txt, fill=CINZA_TXT, anchor="mm")
        else:
            _marca(draw, _COL_BOL_CX, meio, item["tem_boleto"])
```

- [ ] **Passo 4:**

```bash
py -3.11 -m unittest roteirizacao.test_romaneio_boleto_esperado roteirizacao.test_canhoteira_transportadora roteirizacao.test_documentacao_rota -v
```

Esperado: OK.

- [ ] **Passo 5: gerar um romaneio em modo teste na VPS** (só depois do deploy da Tarefa 10; anotar aqui):

```bash
sudo -u www-data venv/bin/python roteirizacao/gerar_pdf_romaneios.py --modo-teste --data <hoje>
```

Abrir um PDF de `roteirizacao/dados/romaneios/teste/` e conferir que Quatro Estrelas mostra "—" no boleto e NUU sem boleto mostra o X.

- [ ] **Passo 6: commit**

```bash
git add roteirizacao/gerar_pdf_romaneios.py roteirizacao/test_romaneio_boleto_esperado.py
git commit -m "Romaneio: 'sem boleto' só quando a NF tem duplicata ou o embarcador está na lista de boleto"
```

---

### Tarefa 9: Vigia diário do fluxo de documentos

**Arquivos:**
- Criar: `documentos_pedido/vigia_documentos.py`
- Criar: `documentos_pedido/test_vigia_documentos.py`
- Criar: `infra/stokki-vigia-documentos.service`, `infra/stokki-vigia-documentos.timer`
- Modificar: `MAPA_DO_SISTEMA.txt` (tabela de timers, seção `documentos_pedido/`, tabela `documentos_processados.cobranca`)

**Interfaces:**
- Consome `gerar_pdf_romaneios.avaliar_documentos_pedido` e `gerar_pdf_romaneios.carregar_documentos_por_pedido` (Tarefa 8), mais `notificar_execucao_agente.notificar_execucao`.
- Produz a CLI `python documentos_pedido/vigia_documentos.py [--data AAAA-MM-DD] [--modo-teste] [--sem-notificar]`.

**O que o vigia confere** (só lê o banco e o log; não toca na Stokki, na Vuupt nem no GCS):

1. **Rodadas (24h):**
   - pelo menos 1 rodada real (não teste) terminou;
   - nenhuma com `'ERRO': n>0`;
   - nenhuma linha "Erro no processamento de documentos" nem "Erro ao buscar PDFs".
2. **Nota fiscal:** pedidos nas rotas de hoje com "sem nota fiscal".
3. **Boleto:** pedidos nas rotas de hoje com "sem boleto" (boleto esperado).
4. **Arquivos:** "arquivo não localizado" ou "PDF ilegível" nas rotas de hoje.
5. **Revisão manual (24h):** quantas entraram, agrupadas pelo início do motivo. É informativo e fica "ok".

**Horário:** todo dia às **06:43**. Fica depois do romaneio das 04:00 e da impressão (04h05–07h), e antes da saída dos motoristas. Nenhuma outra rotina roda em :43.

**Avisos:**
- e-mail para o Hugo todo dia, via `notificar_execucao`;
- WhatsApp ALERTAS FRESH só quando alguma checagem fica "erro" (comportamento padrão de `avisar_execucao`).

- [ ] **Passo 1: teste (falha)**

```python
# -*- coding: utf-8 -*-
"""
Rodar (da raiz):  py -3.11 -m unittest documentos_pedido.test_vigia_documentos
"""
import sqlite3
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

_RAIZ = Path(__file__).parent.parent
for _p in (_RAIZ, _RAIZ / "documentos_pedido"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import vigia_documentos as vd  # noqa: E402

AGORA = datetime(2026, 10, 6, 6, 43)


class Rodadas(unittest.TestCase):
    def test_rodada_ok(self):
        linhas = ["2026-10-05 22:31:10,1 [INFO] processar_documentos - Processamento de documentos "
                  "finalizado em 1800.0s. Contadores: {'JA_PROCESSADO': 212, 'ENVIADO': 16, "
                  "'REVISAO_MANUAL': 1, 'ERRO': 0}"]
        r = vd.checar_rodadas(linhas, AGORA)
        self.assertEqual(r["status"], "ok")

    def test_rodada_com_erro(self):
        linhas = ["2026-10-05 22:31:10,1 [INFO] processar_documentos - Processamento de documentos "
                  "finalizado em 10.0s. Contadores: {'ENVIADO': 0, 'ERRO': 54}"]
        self.assertEqual(vd.checar_rodadas(linhas, AGORA)["status"], "erro")

    def test_so_rodada_de_teste_ou_antiga_e_erro(self):
        linhas = [
            "2026-10-05 20:27:20,2 [INFO] processar_documentos - [MODO TESTE] [ESCOPADO] Processamento "
            "de documentos finalizado em 231.2s. Contadores: {'ERRO': 0}",
            "2026-10-03 22:31:10,1 [INFO] processar_documentos - Processamento de documentos "
            "finalizado em 1.0s. Contadores: {'ERRO': 0}",
        ]
        self.assertEqual(vd.checar_rodadas(linhas, AGORA)["status"], "erro")

    def test_falha_imap_e_erro(self):
        linhas = [
            "2026-10-05 22:31:10,1 [INFO] processar_documentos - Processamento de documentos "
            "finalizado em 1.0s. Contadores: {'ERRO': 0}",
            "2026-10-05 22:01:00,1 [ERROR] email_documentos - Erro ao buscar PDFs por e-mail: timeout",
        ]
        self.assertEqual(vd.checar_rodadas(linhas, AGORA)["status"], "erro")


class Pendencias(unittest.TestCase):
    def test_agrupa_por_tipo_e_embarcador(self):
        servicos = [{"codigo": "PS-1", "sender_id": 1, "embarcador": "QUATRO ESTRELAS"},
                    {"codigo": "PS-2", "sender_id": 2, "embarcador": "NUU"},
                    {"codigo": "PS-3", "sender_id": 2, "embarcador": "NUU"}]
        faltas = {"PS-1": ["sem nota fiscal"], "PS-2": ["sem boleto"],
                  "PS-3": ["arquivo não localizado: x.pdf"]}
        with patch.object(vd, "_avaliar", side_effect=lambda s, docs: {"faltas": faltas[s["codigo"]]}):
            r = vd.checar_pendencias(servicos, {})
        self.assertEqual(r["Nota fiscal (rotas do dia)"]["status"], "erro")
        self.assertIn("QUATRO ESTRELAS: PS-1", r["Nota fiscal (rotas do dia)"]["detalhe"])
        self.assertIn("NUU: PS-2", r["Boleto (rotas do dia)"]["detalhe"])
        self.assertEqual(r["Arquivos (rotas do dia)"]["status"], "erro")

    def test_dia_sem_rotas_nao_e_alerta(self):
        r = vd.checar_pendencias([], {})
        self.assertTrue(all(v["status"] == "ok" for v in r.values()))


class ServicosDoDia(unittest.TestCase):
    def test_le_rotas_do_dia_sem_canceladas_e_sem_coleta(self):
        con = sqlite3.connect(":memory:")
        con.executescript("""
            CREATE TABLE nucleo_rotas (vuupt_route_id INT, data_rota TEXT, status TEXT, cancelada_em TEXT, nome TEXT);
            CREATE TABLE nucleo_pedidos (codigo TEXT, sender_id INT, remetente_nome TEXT, tipo TEXT,
                                         vuupt_route_id INT, excluido_em TEXT);
            INSERT INTO nucleo_rotas VALUES (1,'2026-10-06','PLANEJADA',NULL,'Rota 1'),
                                            (2,'2026-10-06','CANCELADA','2026-10-05','Rota 2'),
                                            (3,'2026-10-05','CONCLUIDA',NULL,'Rota 3');
            INSERT INTO nucleo_pedidos VALUES ('#PS-1',11,'NUU','delivery',1,NULL),
                                              ('#PS-2',11,'NUU','pickup',1,NULL),
                                              ('#PS-3',11,'NUU','delivery',2,NULL),
                                              ('#PS-4',11,'NUU','delivery',3,NULL),
                                              ('#PS-5',11,'NUU','delivery',1,'2026-10-05');
        """)
        s = vd.servicos_do_dia(con, "2026-10-06")
        self.assertEqual([x["codigo"] for x in s], ["PS-1"])


if __name__ == "__main__":
    unittest.main()
```

Antes de implementar `servicos_do_dia`, conferir na VPS os valores reais de `nucleo_rotas.status` (ex.: CANCELADA) e de `nucleo_pedidos.tipo`:

```bash
ssh ... "cd /opt/stokki-eventos && venv/bin/python -c \"import sqlite3;c=sqlite3.connect('file:dados/dados.db?mode=ro',uri=True);print(c.execute('select status,count(*) from nucleo_rotas group by 1').fetchall());print(c.execute('select tipo,count(*) from nucleo_pedidos group by 1').fetchall())\""
```

Se os valores forem outros, ajustar o SQL e o teste juntos.

- [ ] **Passo 2:** rodar → FAIL (módulo não existe).

- [ ] **Passo 3: criar `documentos_pedido/vigia_documentos.py`**

```python
# -*- coding: utf-8 -*-
"""
vigia_documentos.py

Vigia diário do fluxo de documentos (pedido do Hugo, 05/10/2026:
"garantir que esse processo está perfeito"). Roda 1x por dia às 06:43
(stokki-vigia-documentos.timer), depois do romaneio das 04h e antes da
saída dos motoristas.

Só LÊ: banco (rotas do dia, documentos_processados) e o log do
processar_documentos. Não toca Stokki, Vuupt nem GCS. A régua de
"o que falta" é a mesma da capa do romaneio
(gerar_pdf_romaneios.avaliar_documentos_pedido).

Resumo vai por notificar_execucao: e-mail todo dia; WhatsApp ALERTAS
FRESH só quando alguma checagem fica "erro".

COMO USAR:
    py -3.11 documentos_pedido/vigia_documentos.py --sem-notificar
    py -3.11 documentos_pedido/vigia_documentos.py --data 2026-10-06 --modo-teste
"""
import argparse
import re
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(_RAIZ / "roteirizacao"))
sys.path.append(str(Path(__file__).parent))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

DB_PATH = _RAIZ / "dados" / "dados.db"
LOG_DOCUMENTOS = Path(__file__).parent / "dados" / "processar_documentos.log"
JANELA_HORAS = 24
MAX_CODIGOS_POR_EMBARCADOR = 12

_RE_FIM_RODADA = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*Processamento de documentos finalizado.*Contadores: (\{.*\})")
_RE_ERRO_GRAVE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*(Erro no processamento de documentos|Erro ao buscar PDFs)")
_RE_CONTADOR_ERRO = re.compile(r"'ERRO':\s*(\d+)")


def _quando(texto: str) -> datetime:
    return datetime.strptime(texto, "%Y-%m-%d %H:%M:%S")


def checar_rodadas(linhas_log: list[str], agora: datetime) -> dict:
    """Rodadas reais das últimas 24h: pelo menos uma, nenhuma com ERRO,
    nenhuma falha geral/IMAP. Rodada [MODO TESTE] não conta."""
    limite = agora - timedelta(hours=JANELA_HORAS)
    reais, com_erro, graves = 0, 0, []
    for linha in linhas_log:
        m = _RE_FIM_RODADA.match(linha)
        if m and _quando(m.group(1)) >= limite and "[MODO TESTE]" not in linha:
            reais += 1
            m_erro = _RE_CONTADOR_ERRO.search(m.group(2))
            if m_erro and int(m_erro.group(1)) > 0:
                com_erro += 1
            continue
        g = _RE_ERRO_GRAVE.match(linha)
        if g and _quando(g.group(1)) >= limite:
            graves.append(f"{g.group(1)[11:16]} {g.group(2)}")
    problemas = []
    if not reais:
        problemas.append(f"nenhuma rodada terminou nas últimas {JANELA_HORAS}h")
    if com_erro:
        problemas.append(f"{com_erro} rodada(s) com documento em ERRO")
    problemas.extend(graves[:5])
    if problemas:
        return {"status": "erro", "detalhe": "; ".join(problemas)}
    return {"status": "ok", "detalhe": f"{reais} rodada(s) sem erro nas últimas {JANELA_HORAS}h"}


def servicos_do_dia(con: sqlite3.Connection, data_iso: str) -> list[dict]:
    """Entregas das rotas do dia (espelho do núcleo), sem rota
    cancelada, sem serviço excluído, sem coleta."""
    rows = con.execute("""
        SELECT p.codigo, p.sender_id, p.remetente_nome
        FROM nucleo_rotas r JOIN nucleo_pedidos p ON p.vuupt_route_id = r.vuupt_route_id
        WHERE r.data_rota = ? AND r.cancelada_em IS NULL AND COALESCE(r.status, '') != 'CANCELADA'
          AND p.excluido_em IS NULL AND p.tipo = 'delivery'
        ORDER BY p.codigo
    """, (data_iso,)).fetchall()
    return [{"codigo": (c or "").lstrip("#"), "sender_id": s, "embarcador": (n or "?").split(" - ")[-1].strip()}
            for c, s, n in rows]


def _avaliar(servico: dict, docs_por_pedido: dict) -> dict:
    import gerar_pdf_romaneios as gpr
    return gpr.avaliar_documentos_pedido(servico["codigo"], servico["sender_id"], docs_por_pedido)


def _detalhe_por_embarcador(itens: list[tuple[str, str]]) -> str:
    por_emb: dict[str, list[str]] = defaultdict(list)
    for emb, codigo in itens:
        por_emb[emb].append(codigo)
    partes = []
    for emb, codigos in sorted(por_emb.items(), key=lambda kv: -len(kv[1])):
        extra = len(codigos) - MAX_CODIGOS_POR_EMBARCADOR
        lista = ", ".join(codigos[:MAX_CODIGOS_POR_EMBARCADOR]) + (f" (+{extra})" if extra > 0 else "")
        partes.append(f"{emb}: {lista}")
    return " | ".join(partes)


def checar_pendencias(servicos: list[dict], docs_por_pedido: dict) -> dict:
    grupos = {"Nota fiscal (rotas do dia)": [], "Boleto (rotas do dia)": [], "Arquivos (rotas do dia)": []}
    for s in servicos:
        for falta in _avaliar(s, docs_por_pedido)["faltas"]:
            if falta == "sem nota fiscal":
                chave = "Nota fiscal (rotas do dia)"
            elif falta == "sem boleto":
                chave = "Boleto (rotas do dia)"
            else:
                chave = "Arquivos (rotas do dia)"
            grupos[chave].append((s["embarcador"], s["codigo"]))
    resultado = {}
    for nome, itens in grupos.items():
        if itens:
            resultado[nome] = {"status": "erro",
                               "detalhe": f"{len(itens)} pedido(s) -- {_detalhe_por_embarcador(itens)}"}
        else:
            resultado[nome] = {"status": "ok", "detalhe": f"nada faltando em {len(servicos)} entrega(s)"}
    return resultado


def checar_revisoes(con: sqlite3.Connection, agora: datetime) -> dict:
    desde = (agora - timedelta(hours=JANELA_HORAS)).strftime("%Y-%m-%d %H:%M:%S")
    rows = con.execute("SELECT motivo FROM documentos_processados "
                       "WHERE status = 'REVISAO_MANUAL' AND processado_em >= ?", (desde,)).fetchall()
    if not rows:
        return {"status": "ok", "detalhe": "nenhuma revisão manual nova"}
    motivos = Counter((m or "?")[:60] for (m,) in rows)
    topo = "; ".join(f"{n}x {m}" for m, n in motivos.most_common(4))
    return {"status": "ok", "detalhe": f"{len(rows)} nova(s) -- {topo}"}


def main(data_iso: str | None = None, modo_teste: bool = False, notificar: bool = True) -> dict:
    inicio = time.time()
    agora = datetime.now()
    data_iso = data_iso or date.today().isoformat()
    resumo: dict = {}

    try:
        linhas = LOG_DOCUMENTOS.read_text(encoding="utf-8", errors="ignore").splitlines()[-20000:]
    except OSError as e:
        linhas = []
        resumo["Log do processar_documentos"] = {"status": "erro", "detalhe": f"não deu pra ler: {e}"}
    resumo["Rodadas do fluxo"] = checar_rodadas(linhas, agora)

    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        servicos = servicos_do_dia(con, data_iso)
        resumo["Rotas do dia"] = {"status": "ok", "detalhe": f"{len(servicos)} entrega(s) em {data_iso}"}
        import gerar_pdf_romaneios as gpr
        codigos = {c for s in servicos for c in gpr._codigos_base_lista(s["codigo"])}
        docs_por_pedido, _ = gpr.carregar_documentos_por_pedido(codigos)
        resumo.update(checar_pendencias(servicos, docs_por_pedido))
        resumo["Revisão manual (24h)"] = checar_revisoes(con, agora)
    finally:
        con.close()

    duracao = time.time() - inicio
    for nome, info in resumo.items():
        print(f"[{info['status'].upper():4}] {nome}: {info['detalhe']}")
    if notificar:
        import yaml
        from notificar_execucao_agente import notificar_execucao
        with open(_RAIZ / "config.yaml", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
        notificar_execucao(resumo, duracao, modo_teste, config, titulo="Vigia de documentos")
    return resumo


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Vigia diário do fluxo de documentos (só leitura)")
    parser.add_argument("--data", default=None, help="Data das rotas (AAAA-MM-DD). Padrão: hoje.")
    parser.add_argument("--modo-teste", action="store_true", help="Assunto do e-mail com [MODO TESTE]")
    parser.add_argument("--sem-notificar", action="store_true", help="Só imprime, não manda e-mail/WhatsApp")
    a = parser.parse_args()
    main(a.data, modo_teste=a.modo_teste, notificar=not a.sem_notificar)
```

**Atenção:** `import gerar_pdf_romaneios` configura logging com FileHandler em `roteirizacao/dados/gerar_pdf_romaneios.log`, o que é aceitável (só log). Se o import puxar algo que exija rede ou config, mover o import para dentro de `main` e `_avaliar` (já está assim) e conferir com `--sem-notificar` localmente.

- [ ] **Passo 4:** `py -3.11 -m unittest documentos_pedido.test_vigia_documentos -v` → OK (8).

- [ ] **Passo 5: units**

`infra/stokki-vigia-documentos.service`:

```ini
[Unit]
Description=Stokki Eventos - Vigia diario do fluxo de documentos (so leitura: rotas do dia x NF/boleto/arquivo, rodadas do processar_documentos)
OnFailure=stokki-alerta-falha@%n.service

[Service]
Type=oneshot
User=www-data
WorkingDirectory=/opt/stokki-eventos/documentos_pedido
ExecStart=/opt/stokki-eventos/venv/bin/python /opt/stokki-eventos/documentos_pedido/vigia_documentos.py
```

`infra/stokki-vigia-documentos.timer`:

```ini
[Unit]
Description=Dispara stokki-vigia-documentos.service todo dia as 06:43 (depois do romaneio das 04h e da impressao, antes da saida dos motoristas)

[Timer]
OnCalendar=*-*-* 06:43:00
Persistent=false
AccuracySec=1min

[Install]
WantedBy=timers.target
```

- [ ] **Passo 6: `MAPA_DO_SISTEMA.txt`** (editar no worktree, que parte do `origin/master`):
- na tabela de timers, acrescentar a linha `todo dia 06:43   vigia-documentos   documentos_pedido/vigia_documentos.py`, no mesmo formato das vizinhas;
- na seção `documentos_pedido/`, acrescentar:
  - `regras_documentos.py  Boleto esperado (quadro de duplicata da DANFE + lista de fallback).`
  - `vigia_documentos.py   Vigia diário 06:43: rotas do dia x NF/boleto/arquivo, rodadas do log.`
- na tabela de banco, `documentos_processados` ganha a coluna `cobranca` (1 duplicata / 0 quadro vazio / -1 sem quadro).

- [ ] **Passo 7: commit**

```bash
git add documentos_pedido/vigia_documentos.py documentos_pedido/test_vigia_documentos.py \
        infra/stokki-vigia-documentos.service infra/stokki-vigia-documentos.timer MAPA_DO_SISTEMA.txt
git commit -m "Vigia: conferência diária 06:43 do fluxo de documentos (rotas do dia x NF/boleto/arquivo + rodadas)"
```

---

### Tarefa 10: Prova em produção (só com o OK do Hugo para deploy)

Seguir a skill `deploy-vps`. Resumo do que provar, nesta ordem:

- [ ] **Passo 1: suíte completa local** (da raiz do worktree):

```bash
py -3.11 -m unittest documentos_pedido.test_boleto_parser documentos_pedido.test_classificador \
  documentos_pedido.test_registros_documentos documentos_pedido.test_arquivos_documentos \
  documentos_pedido.test_selecionar_pedidos documentos_pedido.test_regras_documentos \
  documentos_pedido.test_processar_documentos_modos documentos_pedido.test_vigia_documentos
py -3.11 -m unittest roteirizacao.test_romaneio_boleto_esperado roteirizacao.test_canhoteira_transportadora roteirizacao.test_documentacao_rota
```

Esperado: tudo OK.

- [ ] **Passo 2: merge e push** só quando o Hugo pedir. Na VPS:
- `git pull --no-rebase`;
- `chown -R www-data:www-data /opt/stokki-eventos`;
- copiar `infra/stokki-vigia-documentos.*` para `/etc/systemd/system/`;
- `systemctl daemon-reload`;
- `systemctl enable --now stokki-vigia-documentos.timer`;
- `systemctl restart painel-agentes`, porque o botão "Imprimir rota" importa o romaneio e os documentos.

Antes, conferir `systemctl list-timers` e `ls /etc/systemd/system | grep vigia-doc`, porque o Hugo pode já ter criado algo.

- [ ] **Passo 3: teste escopado** com os mesmos pedidos de 05/10, fora de :10 e longe das 18h/22h:

```bash
sudo -u www-data venv/bin/python documentos_pedido/processar_documentos.py --modo-teste --pedidos PS-35977,PS-40896,PS-40598
```

Esperado: DANFEs encontradas, e o PS-35977 com a linha "nenhum documento novo na Stokki".

- [ ] **Passo 4: seleção** (só leitura da Stokki, sem processar):

```bash
sudo -u www-data venv/bin/python -c "import sys; sys.path[:0]=['/opt/stokki-eventos','/opt/stokki-eventos/documentos_pedido']; import yaml, selecionar_pedidos as s; from stokki.auth import StokkiSession; c=yaml.safe_load(open('/opt/stokki-eventos/config.yaml')); r=s.descobrir_expedidos_recentes(StokkiSession(c)); print(len(r), r[:10])"
```

Esperado: uma lista com dezenas de pedidos, e nenhum do Padrão Puro.

- [ ] **Passo 5: vigia sem notificar.**

```bash
sudo -u www-data venv/bin/python documentos_pedido/vigia_documentos.py --sem-notificar
```

Conferir se os números batem com a realidade do dia. Depois rodar 1x com `--modo-teste` para o Hugo ver o e-mail.

- [ ] **Passo 6: a primeira rodada completa real** (18h ou 22h) roda o backfill de cobrança e a retentativa com o novo leitor de boleto. No dia seguinte, medir de novo:
- boletos em REVISAO_MANUAL (esperado: muito menos que 175);
- "sem_arquivo" da retentativa (esperado: perto de 0);
- linhas novas de PS-37626 (esperado: 0).

- [ ] **Passo 7 (opcional, só com o Hugo):** limpar as 165 linhas duplicadas antigas do PS-37626 e as da NF 646. Hoje cada rodada completa retenta todas. SQL para mostrar antes de executar:

```sql
DELETE FROM documentos_processados
WHERE status='REVISAO_MANUAL' AND motivo LIKE 'DANFE gerada do XML anexado na Stokki é da NF 24944%'
  AND hash_conteudo NOT IN (SELECT hash_conteudo FROM documentos_processados
                            WHERE motivo LIKE 'DANFE gerada do XML anexado na Stokki é da NF 24944%'
                            ORDER BY processado_em DESC LIMIT 1);
```

Fazer backup do banco antes (`stokki-backup-horario` já roda de hora em hora; conferir o último).

---

## Fora deste plano (anotado)

- **Muai** na lista de boleto de fallback: falta o sender_id (não está na `interno` em 05/10).
- **Casamento frouxo** (R3b sem CNPJ, cliente com 1 serviço sem filtro de data): não mexido. Os casos medidos não mostraram erro de casamento, só falta de casamento.
- **Busca ampla da INBOX x busca dos embarcadores** compartilhando os Message-IDs: não medido. O vigia vai mostrar se boleto de embarcador some.
