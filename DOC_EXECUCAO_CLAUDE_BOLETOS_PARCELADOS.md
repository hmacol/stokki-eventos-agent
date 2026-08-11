# Documento de Especificação e Execução: Agente de Baixa e Casamento de Boletos Parcelados (Multi-Parcelas)

Este documento contém todas as instruções, regras de negócio, estrutura de código e comandos de validação para que o **Claude Code** (ou qualquer agente/desenvolvedor) implemente de forma autônoma e completa o suporte a **boletos parcelados (2 ou mais parcelas)** no fluxo de expedição de pedidos (`processar_documentos.py`) no projeto `agente_stokki_eventos`.

---

## 1. Contexto e Problema de Negócio

No fluxo de expedição de pedidos da Freshlog (Stokki/VUUPT), a primeira etapa (DANFEs e documentos da Stokki) foi concluída com sucesso. O novo desafio consiste em tratar boletos bancários com **pagamentos parcelados** (ex: 2 ou mais parcelas em 30/60/90 dias).

Estamos lidando com 3 cenários operacionais de boletos:
1. **Boletos Multi-Parcelas em Arquivo Único:** 1 PDF exclusivo de um pedido contendo 2 ou mais páginas/boletos (ex: Parcela 1/2 e Parcela 2/2).
2. **Boletos Multi-Parcelas em Arquivos Separados:** Boletos do mesmo pedido vindo em múltiplos arquivos PDFs (anexos diferentes no e-mail ou na Stokki), onde todos precisam ser casados com o pedido `PS-XXXXX` e vinculados à sua pasta.
3. **Boletos Multi-Pedidos e Multi-Parcelas em Lote:** 1 PDF único contendo boletos de vários pedidos diferentes intercalados, alguns dos quais são parcelados.

### A Causa Raiz da Ambiguidade Anterior:
A busca por CNPJ do pagador no VUUPT (`matcher.py`) falha quando o cliente possui mais de 1 pedido em aberto (`len(servicos) > 1`). Nesses casos, o CNPJ sozinho não diferencia a qual pedido o boleto pertence.

---

## 2. Solução Arquitetural: Cruzamento pela NF-e (DANFE / XML)

A solução definitiva consiste em utilizar o **Número da Nota Fiscal (NF-e)** como ponte entre a DANFE (já extraída da Stokki) e o Boleto Bancário.

```
[ STOKKI / XML ]  ---> Extract DANFE ---> Map { (CNPJ + NF_NUMERO) -> PS-XXXXX }
                                                  |
[ EMAIL / STOKKI ] ---> Parse Boleto  ---> Extract (CNPJ, NF_NUMERO, PARCELA, VALOR)
                                                  |
                                             MATCH SUCCESSFUL!
                                                  |
                                         Assign to PS-XXXXX
```

---

## 3. Especificação dos Módulos a Implementar / Alterar

### 3.1. `documentos_pedido/boleto_parser.py` (Novo Módulo)

Função principal: `extrair_metadados_boleto(caminho_pdf: Path, texto_pdf: str | None = None) -> dict`

Retorna um dicionário estruturado:
```python
{
    "cnpj_pagador": "12345678000190",
    "numero_nf": "12345",
    "parcela_atual": 1,
    "total_parcelas": 2,
    "valor": 450.00,
    "vencimento": "2026-09-10",
    "codigo_pedido_no_texto": "PS-35471", # Se constar no boleto/observações
    "linha_digitavel": "34191.00000 00000.000000 ..."
}
```

#### Expressões Regulares de Captura de NF e Parcela:
- **Número da NF:** 
  - `r"NF\s*[:\.]?\s*(\d{1,9})"`
  - `r"Seu\s+N[úu]mero\s*:\s*(\d{1,9})"`
  - `r"Doc(?:umento)?\s*:\s*(\d{1,9})"`
  - `r"\b(\d{1,8})[/-](?:0?1|0?2|0?3|A|B|C)\b"` (Exemplo: NF `12345/01` ou `12345-A`)
- **Parcela:**
  - `r"(\d{1,2})\s*[/|-]\s*(\d{1,2})"` (Exemplo: `01/02` -> Parcela 1 de 2)
  - `r"Parc(?:ela)?\s*(\d{1,2})"`

---

### 3.2. `documentos_pedido/matcher.py` (Refatoração)

#### 1. Mapeamento de Danfes (`IndexadorDANFE`):
Antes de casar boletos, o `matcher.py` lê os XMLs/DANFEs dos pedidos elegíveis em aberto na Stokki e constrói o índice em memória:
```python
MAPA_NF_PEDIDO = {
    # (cnpj_destinatario, numero_nf): codigo_ps
    ("12345678000190", "8482"): "PS-27776",
    # fallback por numero_nf se for único entre todos os pedidos abertos
    "8482": "PS-27776"
}
```

#### 2. Hierarquia de Casamento (`casar_documento_com_pedido`):
1. **Regra 1 (Código Direto):** Encontrou `PS-XXXXX` no nome do arquivo, assunto do e-mail ou texto do PDF.
2. **Regra 2 (NF + CNPJ):** `numero_nf` e `cnpj_pagador` do boleto batem com o mapa da DANFE.
3. **Regra 3 (NF Única):** `numero_nf` bate com 1 único pedido em aberto.
4. **Regra 4 (CNPJ + Valor Duplicata):** CNPJ possui apenas 1 pedido com aquele valor exato de parcela.
5. **Fallback:** Caso não seja possível casar com 100% de certeza, salvar em `documentos_processados` como `REVISAO_MANUAL` especificando o motivo da ambiguidade.

---

### 3.3. `documentos_pedido/boleto_splitter.py` (Refatoração)

Atualizar para suportar o desmembramento de PDFs multi-boletos mantendo o contexto de parcelas:
- Se o PDF contiver N boletos divididos por `"RECIBO DO PAGADOR"`:
  1. Separa o PDF em N arquivos temporários.
  2. Para cada arquivo separado, executa `boleto_parser.extrair_metadados_boleto()`.
  3. Casa cada arquivo individualmente com o seu devido pedido `PS-XXXXX`.

---

### 3.4. `documentos_pedido/fingerprint_documentos.py` (Atualização do Banco)

Atualizar o esquema SQLite em `dados/dados.db`:
```sql
ALTER TABLE documentos_processados ADD COLUMN numero_parcela INTEGER;
ALTER TABLE documentos_processados ADD COLUMN total_parcelas INTEGER;
ALTER TABLE documentos_processados ADD COLUMN numero_nf TEXT;
```

Ajustar a função `ja_enviado_para_pedido(codigo_pedido, tipo)` para não bloquear o envio de parcelas adicionais do mesmo tipo `"Boleto"`, registrando cada parcela por seu hash único e identificador de parcela.

---

## 4. Fluxo de Execução Recomendado

Para rodar o agente atualizado:

```bash
# 1. Execução em MODO TESTE (auditoria sem envio ao GCS)
py -3.11 documentos_pedido/processar_documentos.py --modo-teste

# 2. Execução direcionada para pedidos específicos
py -3.11 documentos_pedido/processar_documentos.py --modo-teste --pedidos PS-35471,PS-35472

# 3. Execução em PRODUÇÃO (envio ao GCS e registro no banco)
py -3.11 documentos_pedido/processar_documentos.py
```

---

## 5. Critérios de Aceite

- [x] Extração correta do número da NF-e e parcela em boletos bancários (Itaú, Bradesco, BB, Santander, etc.).
- [x] Cruzamento infalível entre Boletos e DANFEs de pedidos em aberto mesmo em clientes com múltiplos pedidos.
- [x] Suporte completo a múltiplos arquivos de boleto por pedido sem sobrescrever nem ignorar parcelas.
- [x] Suporte à separação automática de PDFs lote contendo boletos de múltiplos pedidos e parcelas.
- [x] Registro transparente e detalhado em `dados/processar_documentos.log` e e-mail de notificação.

---

## 6. Extensão 11/08/2026 — De Tommaso (NF + Boleto por e-mail)

Pedido do Hugo: buscar e-mails de **@detommaso.com.br** (domínio inteiro
via substring do FROM no IMAP — pedro@ é o mais usado, mas pode variar).

Como os e-mails chegam (analisado em e-mail real de 11/08, assunto
"NF e Boleto 11.08"):

- **NFs consolidadas**: 1-2 PDFs "NFs FRESH DD.MM[.ciao].pdf" com TODAS
  as DANFEs do dia (1 por página; nota de 2 páginas agrupada). Emissor:
  CIAO IND E COM PROD ALIMENTICIOS (CNPJ 05294174000128), layout
  próprio com "N. 000035880" (sem º).
- **Boletos individuais**: Itaú 341, 1 PDF por duplicata, nome do
  arquivo = 341 + CNPJ do pagador + sufixo. Número da NF no campo
  "Núm. do documento" ("documento 035880 DM"), sem histórico "Ref. a NF".
- **Pedidos de Venda**: PDFs "0400XX.pdf" com o pedido interno deles —
  NÃO são documentos do pipeline (novo tipo "Pedido de Venda" no
  classificador, fora de escopo).

Mudanças:

1. `email_documentos.REMETENTES_EMBARCADORES` agora carrega
   `{"nome", "tipos"}` por remetente; De Tommaso aceita
   `{"Boleto", "Nota Fiscal"}` (Dourado/NUU seguem só Boleto).
2. Novo `nf_splitter.py` (espelho do boleto_splitter): separa PDF
   multi-DANFE por página com "RECEBEMOS DE", salva em
   `dados/nfs_separadas/` (pasta incluída em localizar_arquivos.py).
3. `boleto_parser`: 4º formato real — `PADRAO_DOC_SEM_DATA`
   ("documento 035880 DM", linha sem data na frente).
4. `matcher`: `PADRAO_NF_DANFE` aceita "N. 000035880"; **nova Regra 3b**
   `nf_referencia_titulo` — o número da NF é a REFERÊNCIA no título do
   serviço VUUPT ("#PS-36008 - 024746 / ..."), confirmado em Jersey
   Vale, Grupo Trigo e De Tommaso. Busca com zero-padding de 6 dígitos;
   dígitos crus só como fallback COM verificação obrigatória de CNPJ
   (falso positivo real: NF 35881 casando no código "#PS-35881").
   Quando o documento tem CNPJ, o cliente do serviço precisa bater.
   Regra 4 pra NF passa a usar o CNPJ do DESTINATÁRIO da DANFE (o
   primeiro CNPJ do texto é o do emitente).

Ordem que faz tudo casar: o e-mail chega ~13h (mercadoria deixada no
CD), a importação Stokki→VUUPT roda às 18h (ExecutarTudo, passo 1 da
sequência) e o ProcessarDocumentos roda DEPOIS (passo 5) — na hora do
casamento os pedidos novos já existem com a NF na referência do título.
E-mails de até 7 dias atrás entram na primeira rodada real (backfill).

Atenção: os e-mails do De Tommaso caem numa label do Gmail (fora da
INBOX), então a busca ampla não os consome antes da busca direcionada.
Se um dia chegarem na INBOX, a busca ampla processa o PDF consolidado
SEM split — limitação conhecida.
