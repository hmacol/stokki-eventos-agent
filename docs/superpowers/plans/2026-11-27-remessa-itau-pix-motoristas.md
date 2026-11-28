# Remessa Itaú (Pix) dos motoristas — plano de implementação

> **Para quem executa:** use superpowers:executing-plans (ou subagent-driven-development) tarefa a tarefa. Passos com caixas `- [ ]`.

**Objetivo:** gerar o arquivo de remessa Pix (Sispag CNAB 240) dos pagamentos de motoristas, controlar os estados a pagar → enviado → pago/rejeitado e ler o arquivo de retorno do Itaú.

**Arquitetura:** módulo puro `remessa_itau.py` (chave Pix, registros de 240 colunas, leitura do retorno; sem banco, sem Flask) + `remessa_itau_db.py` (tabelas e transações sobre `pagamentos_esporadicos`) + rotas e tela em `/financeiro/esporadicos`. Tudo atrás de `itau_remessa.ativo` (padrão `false`).

**Tecnologia:** Python 3.11, Flask, SQLite, `unittest` (como o resto de `painel_agentes/`).

**Spec:** `docs/superpowers/specs/2026-11-27-remessa-itau-pix-motoristas-design.md`. Layout oficial extraído em `docs/referencias/itau-sispag-cnab240-2020.txt` (posições, notas 8, 9, 10, 11, 15, 34, 36, 37, 40 e tabela de ocorrências).

## Restrições globais (copiadas do spec)

- Registros de **240 caracteres**, ASCII maiúsculo sem acento; arquivo com um registro por linha, fim de linha `\r\n`.
- Código do banco `341`; layout de arquivo `080`, layout de lote `040`; tipo de operação `C`; **forma de pagamento `45` (PIX Transferência)**; **tipo de pagamento `20` (Fornecedores)**, lido de `itau_remessa.tipo_pagamento`.
- Segmento A: câmara `009` (pos. 18-20), identificação da transferência `04` = chave Pix (pos. 113-114), moeda `REA`, valor em centavos (15 posições). Segmento B: tipo de chave `01` telefone / `02` e-mail / `03` CPF-CNPJ / `04` aleatória (pos. 15-16), chave em 128-227 (telefone `+55DDDNÚMERO`, CPF/CNPJ só dígitos, UUID com hífens).
- `Seu Número` (A, pos. 74-93) = id do pagamento. Nome do favorecido: 30 posições.
- Dados da conta pagadora só em `config.yaml` (seção `itau_remessa`: `ativo`, `cnpj`, `agencia`, `conta`, `dac`, `nome_empresa`, `tipo_pagamento`); nunca no repo, no chat nem em log. Chave Pix e CPF não vão para log.
- Somente nível `total`; POSTs com `exige_mesma_origem`. Um pagamento por motorista por remessa. Rota nunca em duas remessas ativas (`pagamentos_esporadicos_rotas.rota_id UNIQUE`).
- Cancelar remessa só se o arquivo **nunca foi baixado**. `REJEITADO` não usa `desfeito_em`.
- Deploy: worktree limpo sobre `origin/master`, push por SHA literal, restart do `painel-agentes` **conferido** pelo horário de início; `MAPA_DO_SISTEMA.txt` atualizado. Nada liga em produção sem o Hugo.

## Foco de revisão (entradas que o spec implica e nenhuma tarefa exercita sozinha)

1. Chave de 11 dígitos válida como CPF **e** como celular → não elegível (ambígua), nunca chuta.
2. Nome com acento, `Ç`, minúsculas ou mais de 30 caracteres → maiúsculo ASCII, truncado em 30.
3. Valor com centavos (`340.10`, `0.1+0.2`) → inteiro de centavos exato, sem erro de ponto flutuante.
4. Gerar duas vezes com a mesma rota (clique duplo / duas abas) → a segunda falha com mensagem clara e nenhuma remessa parcial fica gravada.
5. Retorno reenviado, de outra remessa, ou com linha desconhecida → idempotente, não altera estado indevido e informa o que ignorou.

## Estrutura de arquivos

- Criar `painel_agentes/remessa_itau.py`: puro. `ChavePix`, `classificar_chave`, `montar_arquivo`, `ler_retorno`, `OCORRENCIAS`.
- Criar `painel_agentes/remessa_itau_db.py`: tabelas, elegibilidade, criar/baixar/cancelar remessa, confirmar/rejeitar manual, `aplicar_retorno`.
- Modificar `painel_agentes/relatorio_esporadicos.py`: situações novas em `gerar`, filtros `enviados`/`rejeitados`, migração das colunas.
- Modificar `painel_agentes/painel_agentes.py`, `templates/relatorio_esporadicos.html`, `acesso_telas.py` (regex da API de remessa), `MAPA_DO_SISTEMA.txt`.
- Testes: `painel_agentes/test_remessa_itau.py`, `test_remessa_itau_db.py`, e novos casos em `test_relatorio_esporadicos.py`.

---

### Tarefa 1: classificar a chave Pix

**Arquivos:** criar `painel_agentes/remessa_itau.py`; testar em `painel_agentes/test_remessa_itau.py`.

**Interfaces — produz:**
`@dataclass(frozen=True) class ChavePix: tipo: str  # "01"|"02"|"03"|"04"; valor: str; documento: str  # CPF/CNPJ só dígitos para o campo de inscrição; tipo_inscricao: int  # 1 CPF, 2 CNPJ`
`classificar_chave(chave: str, cpf_motorista: str) -> ChavePix | str` (devolve a **mensagem de motivo** quando não elegível).

- [ ] **Passo 1: testes que falham** em `test_remessa_itau.py::TestChave`, com estes casos (CPF válido de teste `52998224725`, CNPJ válido `11222333000181`):
  - e-mail `Ana@Pix.com ` → tipo `02`, valor `ana@pix.com`; `documento` = CPF do motorista.
  - chave `52998224725` com `cpf_motorista="52998224725"` → tipo `03`, tipo_inscricao 1.
  - chave CPF válido **diferente** do motorista e que não parece celular → string contendo `"outro titular"`.
  - `11222333000181` → tipo `03`, tipo_inscricao 2, `documento` = o próprio CNPJ.
  - `11987654321` (celular e CPF inválido) → tipo `01`, valor `+5511987654321`.
  - `5511987654321` (13 dígitos) e `+5511987654321` → tipo `01`, valor `+5511987654321`.
  - chave de 11 dígitos com DDD válido, 3º dígito `9` **e** dígito verificador de CPF válido (o teste procura uma por tentativa, ex.: varrendo `119` + 8 dígitos) e diferente do CPF do motorista → string contendo `"ambígua"` (Foco 1).
  - UUID `123e4567-e89b-12d3-a456-426614174000` → tipo `04`.
  - `1234567` (7 dígitos), vazio, `"abc"` → string com o motivo (`"sem chave"` para vazio, `"formato inválido"` demais).
- [ ] **Passo 2:** rodar `py -3.11 -m unittest test_remessa_itau` (de dentro de `painel_agentes/`); esperado: falha (módulo inexistente).
- [ ] **Passo 3:** implementar `classificar_chave`; CPF/CNPJ com dígito verificador (módulo 11); celular = DDD 11-99 + `9` + 8 dígitos; 11 dígitos que sejam CPF do próprio motorista valem CPF; ambos válidos e diferentes do motorista = ambígua.
- [ ] **Passo 4:** rodar o teste; esperado: passa.
- [ ] **Passo 5:** commit `Remessa Itau: classificacao da chave Pix`.

### Tarefa 2: montar o arquivo de remessa CNAB 240

**Arquivos:** modificar `painel_agentes/remessa_itau.py`; testar em `test_remessa_itau.py::TestArquivo`.

**Interfaces — consome:** `ChavePix` (Tarefa 1). **Produz:**
`@dataclass class ItemRemessa: pagamento_id: int; nome: str; valor: Decimal; chave: ChavePix`
`@dataclass class ContaPagadora: cnpj: str; agencia: str; conta: str; dac: str; nome_empresa: str; tipo_pagamento: str = "20"`
`montar_arquivo(conta: ContaPagadora, itens: list[ItemRemessa], data_pagamento: date, agora: datetime, sequencial_arquivo: int) -> str`
`para_centavos(valor: Decimal | float) -> int`, `limpar_nome(nome: str, n: int) -> str`.

- [ ] **Passo 1: testes que falham:**
  - `test_cada_registro_tem_240_colunas`: com 2 itens, arquivo tem 1 header + 1 header de lote + 2×(A+B) + trailer de lote + trailer = 8 linhas, todas de 240 caracteres.
  - `test_header_arquivo`: pos. 1-3 `341`, 8 `0`, 15-17 `080`, 18 `2`, 19-32 CNPJ, 53-57 agência (5 dígitos), 59-70 conta, 72 DAC, 143 `1`, 144-151 `DDMMAAAA`.
  - `test_header_lote`: 8 `1`, 9 `C`, 10-11 `20`, 12-13 `45`, 14-16 `040`.
  - `test_segmento_a_e_b`: A pos. 14 `A`, 18-20 `009`, 74-93 `Seu Número` = id, 94-101 data, 102-104 `REA`, 113-114 `04`, 120-134 centavos, 204-217 documento; B pos. 14 `B`, 15-16 tipo de chave, 128-227 chave; números sequenciais do registro (pos. 9-13) iguais entre A e B do mesmo pagamento.
  - `test_trailers`: trailer de lote pos. 18-23 = nº de registros do lote (header+detalhes+trailer), 24-41 = soma em centavos com 2 casas; trailer de arquivo pos. 18-23 = 1 lote, 24-29 = total de registros do arquivo.
  - Foco 2: `limpar_nome("José da Conceição Ñandú Filho Júnior da Silva", 30)` = 30 caracteres, só ASCII maiúsculo.
  - Foco 3: `para_centavos(Decimal("340.10")) == 34010`, `para_centavos(0.1 + 0.2) == 30`.
  - `test_data_anterior_a_hoje_rejeita`: `montar_arquivo` levanta `ValueError` se `data_pagamento < agora.date()`; levanta `ValueError` com lista vazia.
- [ ] **Passo 2:** rodar; esperado: falha.
- [ ] **Passo 3:** implementar seguindo as posições de `docs/referencias/itau-sispag-cnab240-2020.txt` (registros 0, 1, A, B, 5, 9). Campos não citados são brancos (alfa) ou zeros (numérico). Comentar a posição do manual em cada campo. Uma função por registro; ao final, validar `len(linha) == 240` e levantar `AssertionError` se não.
- [ ] **Passo 4:** rodar; esperado: passa.
- [ ] **Passo 5:** commit `Remessa Itau: gerador do arquivo CNAB 240 Pix`.

### Tarefa 3: estados, tabelas e elegibilidade

**Arquivos:** criar `painel_agentes/remessa_itau_db.py`; modificar `painel_agentes/relatorio_esporadicos.py` (`_SCHEMA`, `gerar`, `FILTROS`); testar em `test_remessa_itau_db.py` e `test_relatorio_esporadicos.py`.

**Interfaces — produz:**
`garantir_tabelas(conn) -> None` (idempotente; adiciona a `pagamentos_esporadicos` as colunas `situacao TEXT NOT NULL DEFAULT 'PAGO'`, `remessa_id INTEGER`, `ocorrencia TEXT`, `motivo TEXT`; cria `remessas_itau` com `id, gerada_em, gerada_por, data_pagamento, qtd, valor_total, arquivo_nome, arquivo_sha256, baixada_em, retorno_em, retorno_por, retorno_sha256`).
`elegibilidade(motorista: dict) -> tuple[ChavePix | None, str | None]`: ChavePix se pode entrar, ou o motivo (sem Pix, chave inválida/ambígua/outro titular, sem CPF, total pendente ≤ 0).
`gerar(...)` em `relatorio_esporadicos` passa a devolver `l["pago"]` apenas para `situacao='PAGO'`; para `ENVIADO` devolve `l["enviado"]` (`{pagamento_id, remessa_id}`); `FILTROS = ("pendentes", "enviados", "pagos", "rejeitados", "todos")`; linha `ENVIADO` não é pendente nem paga.

- [ ] **Passo 1: testes que falham:** (a) `garantir_tabelas` duas vezes não duplica nem apaga linhas e deixa pagamentos antigos com `situacao='PAGO'`; (b) com um pagamento `ENVIADO`, `gerar(..., "pendentes")` não traz a rota, `"enviados"` traz, `"pagos"` não traz; (c) `elegibilidade` devolve motivo para sem Pix, chave de outro titular, total zero; (d) um pagamento `REJEITADO` (sem linhas em `pagamentos_esporadicos_rotas`) aparece só em `"rejeitados"` e a rota volta a `"pendentes"`.
- [ ] **Passo 2:** rodar; esperado: falha.
- [ ] **Passo 3:** implementar. A situação de cada linha vem do join com `pagamentos_esporadicos` (sem `desfeito_em`); `gerar` chama `garantir_tabelas`. Manter o comportamento atual dos testes existentes.
- [ ] **Passo 4:** rodar `test_relatorio_esporadicos` e `test_remessa_itau_db`; esperado: passam (os 14 antigos continuam passando).
- [ ] **Passo 5:** commit `Remessa Itau: estados ENVIADO/REJEITADO e elegibilidade`.

### Tarefa 4: criar, baixar e cancelar a remessa

**Arquivos:** modificar `painel_agentes/remessa_itau_db.py`; testar em `test_remessa_itau_db.py`.

**Interfaces — consome:** `elegibilidade`, `montar_arquivo`, `ContaPagadora`. **Produz:**
`criar_remessa(chaves: list[str], data_inicio: str, data_fim: str, data_pagamento: date, usuario: str, conta: ContaPagadora, pasta: Path, conn=None, agora: datetime | None = None) -> dict` → `{remessa_id, arquivo_nome, qtd, valor_total}`; transação única; levanta `ErroRemessa(str)` (mensagem pronta para a tela).
`ler_arquivo(remessa_id, pasta, usuario, conn=None) -> bytes` (marca `baixada_em` na primeira leitura; confere o SHA-256 gravado).
`cancelar_remessa(remessa_id, usuario, conn=None) -> None` (só se `baixada_em IS NULL`; solta as rotas, pagamentos viram `REJEITADO` com motivo `"remessa cancelada"`).
`confirmar_pago_manual(pagamento_id, usuario, conn=None)`, `rejeitar_manual(pagamento_id, motivo, usuario, conn=None)` (só em `ENVIADO`).

- [ ] **Passo 1: testes que falham:** criar com 2 motoristas elegíveis → 2 pagamentos `ENVIADO`, rotas presas, arquivo gravado com SHA-256 igual ao do banco; motorista inelegível → `ErroRemessa` citando o nome e o motivo, **nada** gravado; Foco 4: segunda `criar_remessa` com o mesmo motorista → `ErroRemessa` e contagem de remessas e pagamentos inalterada; `ler_arquivo` marca `baixada_em`; `cancelar_remessa` antes do download solta as rotas, depois do download levanta `ErroRemessa`; `confirmar_pago_manual` vira `PAGO` e `pago_em` = agora; `data_pagamento` anterior a hoje → `ErroRemessa`.
- [ ] **Passo 2:** rodar; esperado: falha.
- [ ] **Passo 3:** implementar. Nome do arquivo `PIX_AAAAMMDD_NN.REM`, `NN` sequencial do dia; `Seu Número` = id do pagamento (insere os pagamentos primeiro, monta o arquivo depois, tudo dentro da mesma transação; erro desfaz tudo e remove o arquivo parcial).
- [ ] **Passo 4:** rodar; esperado: passa.
- [ ] **Passo 5:** commit `Remessa Itau: criar, baixar e cancelar remessa`.

### Tarefa 5: ler e aplicar o retorno

**Arquivos:** modificar `painel_agentes/remessa_itau.py` (`ler_retorno`, `OCORRENCIAS`) e `remessa_itau_db.py` (`aplicar_retorno`); testar em ambos os arquivos de teste.

**Interfaces — produz:**
`ler_retorno(texto: str) -> list[RegistroRetorno]` com `RegistroRetorno(pagamento_id: int, ocorrencias: list[str], data_efetiva: date | None, valor_efetivo: Decimal | None)`, lendo os segmentos A (pos. 74-93 `Seu Número`, 155-162 data efetiva, 163-177 valor efetivo, 231-240 ocorrências em pares de 2).
`OCORRENCIAS: dict[str, str]` com a tabela do manual (nota 8) completa.
`aplicar_retorno(texto: str, remessa_id: int, usuario: str, conn=None) -> dict` → `{pagos, rejeitados, agendados, ignorados: list[str]}`.

- [ ] **Passo 1: testes que falham** (retorno **sintético** montado pelo teste a partir de uma remessa gerada, trocando pos. 231-240 e datas): `00` → `PAGO` com `pago_em` = data efetiva; `BD` → continua `ENVIADO`; `NA`, `SS`, `BI`, `CD`, `CN` → `REJEITADO`, `ocorrencia` e `motivo` com o texto do manual, rotas liberadas; Foco 5: aplicar o mesmo arquivo duas vezes não muda nada (SHA-256 igual) e linha com `Seu Número` inexistente ou de outra remessa vai para `ignorados`, sem alterar estado; valor efetivo diferente do previsto → vai para `ignorados` com aviso e não paga.
- [ ] **Passo 2:** rodar; esperado: falha.
- [ ] **Passo 3:** implementar. Ocorrência desconhecida **nunca** vira pago: fica `ENVIADO` e entra em `ignorados`. Grava `retorno_em`, `retorno_por`, `retorno_sha256`.
- [ ] **Passo 4:** rodar; esperado: passa.
- [ ] **Passo 5:** commit `Remessa Itau: leitura do arquivo de retorno`.

### Tarefa 6: rotas, tela, configuração e acesso

**Arquivos:** modificar `painel_agentes/painel_agentes.py`, `templates/relatorio_esporadicos.html`, `acesso_telas.py`, `MAPA_DO_SISTEMA.txt`; testar em `test_relatorio_esporadicos.py::TestRotasEsporadicos` (login real, como o teste existente).

**Interfaces — consome:** `criar_remessa`, `ler_arquivo`, `cancelar_remessa`, `confirmar_pago_manual`, `rejeitar_manual`, `aplicar_retorno`. **Produz:** rotas `POST /api/financeiro/esporadicos/remessa` (JSON `{chaves, de, ate, data_pagamento}`), `GET /financeiro/esporadicos/remessa/<id>/arquivo`, `POST .../remessa/<id>/cancelar`, `POST .../remessa/<id>/retorno` (arquivo), `POST /api/financeiro/esporadicos/<pagamento_id>/confirmar-pago` e `/rejeitar`; `NIVEIS_REMESSA = ("total",)`; helper `_conta_itau(config) -> ContaPagadora | None`.

- [ ] **Passo 1: testes que falham:** com `itau_remessa.ativo` ausente/falso as rotas respondem 404 e a tela não mostra nada de remessa; com `atendimento` as rotas dão 403 e com `total` funcionam; POST sem `Origin` dá 403; gerar remessa devolve o arquivo na próxima chamada de download; cancelar após o download dá 400 com mensagem; a tela lista motoristas inelegíveis com o motivo e a data de pagamento padrão = próximo dia útil (`regras.feriados.proximo_dia_util`).
- [ ] **Passo 2:** rodar; esperado: falha.
- [ ] **Passo 3:** implementar. A configuração vem de `_carregar_config()["itau_remessa"]`; ausente ou `ativo` falso desliga tudo; nada de segredo em resposta ou log. Na tela: caixas de seleção, data de pagamento, botão **Gerar remessa Itaú**, aviso fixo sobre o valor dos fixos (tarifa por km), lista de remessas com **baixar**, **enviar retorno**, **cancelar** (some após baixar), e por pagamento `ENVIADO`: **Confirmar pago** e **Marcar rejeitado** com confirmação explícita. Filtro "Mostrar" ganha Enviados e Rejeitados. Atualizar o mapa e a regex em `acesso_telas.py`.
- [ ] **Passo 4:** rodar `py -3.11 -m unittest test_remessa_itau test_remessa_itau_db test_relatorio_esporadicos test_acesso_telas` (de dentro de `painel_agentes/`); esperado: tudo passa.
- [ ] **Passo 5:** commit `Remessa Itau: rotas, tela e configuracao (desligado por padrao)`.

### Tarefa 7: revisão, publicação e prova

- [ ] **Passo 1:** rodar a revisão do diff inteiro com um agente do modelo `fable` (Fable 5.2), pedido do Hugo em 28/11; tratar cada achado (corrigir ou justificar).
- [ ] **Passo 2:** publicar pelo worktree limpo (sem trazer mudanças de outras sessões), push por SHA literal, `deploy --forcar --restart painel-agentes` e conferir o horário de início do serviço **depois** do pull.
- [ ] **Passo 3:** prova em produção: chamar as views dentro do app (tela e CSV) e confirmar que, sem `itau_remessa` no config, a tela está idêntica à atual; log sem erros.
- [ ] **Passo 4:** entregar ao Hugo o trecho de `config.yaml` a preencher e o roteiro do primeiro teste (arquivo de teste ao Itaú; depois 1 pagamento de valor mínimo). **Não ligar** `itau_remessa.ativo`.
