# Recebimento por etapas, plano 2: Receber (checklist da chegada)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Antes de conferir, o galpão registra a chegada do caminhão num checklist com fotos e temperatura; carga fora da faixa fica parada até a liberação formal do cliente por link; o cliente recebe o e-mail "sua carga chegou" com o Relatório de Recebimento em PDF.

**Architecture:** Um módulo de dados sem Flask, `painel_agentes/wms_chegada.py`, guarda checklist, fotos, temperaturas e a máquina de estados da chegada. O painel expõe as rotas e as telas no `/wms`; o PDF sai de `wms_chegada_pdf.py` (Pillow, como os outros PDFs do projeto); os e-mails de `wms_chegada_avisos.py`; a página pública de liberação fica no portal do cliente (`portal_cliente/link_liberacao.py`, no molde de `link_ciencia.py`). Só vale para embarcador com `etapas: true`.

**Tech Stack:** Python 3.11, Flask, SQLite, Pillow, itsdangerous, `email_utils.enviar_email`, JS puro no `wms.html`, `unittest`.

**Spec:** `docs/superpowers/specs/2026-11-22-recebimento-duas-etapas-design.md`, seções "Etapa 1: Receber" e "Cliente: portal e e-mail". **Depende do plano 1** (`2026-11-22-recebimento-etapas-1-doca-conferir-guardar.md`): usa `wms_pilotos.ids_com_etapas`, `_wms_ids_etapas()`, `usa_etapas`, `wms_produtos.temperatura` e `wms_doca.temperatura_do_produto`. Executar no mesmo ramo, depois dele.

## Global Constraints

- Tudo o que está em "Global Constraints" do plano 1 vale aqui.
- Faixas padrão: refrigerado **0 a 7 °C**; congelado **-18 a -12 °C**. Produto pode ter faixa própria.
- Receber é **obrigatório**: recebimento de embarcador com etapas só pode ser conferido com chegada `LIBERADA`. A trava é no servidor, não só na tela.
- Fora da faixa: a carga fica `AGUARDANDO_ESCRITORIO`. O galpão não libera nem recusa. Só o escritório (`_NIVEIS_WMS_ADMIN`) age.
- Receber fora da faixa exige liberação: do cliente pelo link, ou registrada pelo escritório com texto obrigatório.
- E-mail a cliente nasce desligado e redirecionado, no mesmo mecanismo de `wms_faltas_recebimento.py` (`controle_notificacoes.decidir`, `forcar_destino`, `EMAIL_TESTE`). Ligar é do Hugo. Em teste, sempre injetar `enviar` falso: **`--modo-teste` manda e-mail de verdade**.
- E-mail ao escritório é interno e sai sempre, para `email_escritorio` do config (padrão `entregas@freshlogbr.com`).
- Nenhuma tela nova de checklist no portal. A única página nova fora do `/wms` é a do link de liberação.
- Fotos: JPEG regravado (EXIF corrigido, lado maior 2200 px, qualidade 85), limite de 12 MB na entrada, em `dados/chegadas/`. Mesmo tratamento de `_coletas_salvar_foto` em `painel_agentes.py` (~l.1216).
- PDF: Pillow puro, logo em `assets/logo_freshlog.png`, A4 1240×1754 a 150 dpi, no molde de `comprovante_entrega/gerar_pdf.py`.
- Deploy (quando o Hugo pedir) reinicia `painel-agentes` **e** `portal-cliente`.

## Review Focus

1. **Cliente responde duas vezes** (ou duas pessoas abrem o link): a segunda vê "já respondido"; o estado não muda. (Tasks 4 e 7)
2. **Arquivo que não é foto, grande demais ou HEIC do iPhone**: erro claro, nenhum arquivo órfão em disco, nenhuma linha em `wms_chegada_fotos`. (Task 2)
3. **Temperatura digitada como `6,5`, `-18`, vazia ou texto**: vírgula e negativo são aceitos; vazio ou texto dá erro citando a classe. (Task 3)
4. **Carga só com produto sem temperatura cadastrada**: não pede temperatura, conclui e libera. (Tasks 1 e 3)
5. **Link vencido, adulterado, ou clicado depois que o escritório já decidiu**: página de aviso, nada muda. (Task 7)

---

## Estrutura de arquivos

| Arquivo | Papel |
|---|---|
| `painel_agentes/wms.py` (modificar) | colunas `temp_min`/`temp_max` do produto |
| `painel_agentes/wms_chegada.py` (criar) | tabelas, checklist, fotos, estados. Só stdlib + Pillow, **sem importar `wms`** (o portal também usa) |
| `painel_agentes/wms_chegada_pdf.py` (criar) | Relatório de Recebimento em PDF |
| `painel_agentes/wms_chegada_avisos.py` (criar) | e-mails: escritório, cliente, pedido de liberação, pendentes |
| `portal_cliente/link_liberacao.py` (criar) | token e página pública de liberação |
| `portal_cliente/templates/liberacao.html`, `liberacao_erro.html` (criar) | página do cliente |
| `portal_cliente/app.py` (modificar) | registrar o link; liberar os endpoints em `_LIVRES_ANTES_DA_TROCA` |
| `painel_agentes/painel_agentes.py` (modificar) | rotas `/api/wms/chegadas/*`, trava da conferência |
| `painel_agentes/templates/wms.html` (modificar) | telas Receber |
| `sincronizar_recebimentos_wms.py` (modificar) | chama o envio de pendentes a cada rodada |
| testes | `painel_agentes/test_wms_chegada.py`, `test_wms_chegada_pdf.py`, `test_wms_chegada_avisos.py`, `test_wms_chegada_tela.py`; `portal_cliente/test_link_liberacao.py` |

O portal precisa importar `wms_chegada`. Use o mesmo caminho que o portal já usa para ler código de `painel_agentes/` (procure em `portal_cliente/` por imports de `wms_`); se não houver nenhum, carregue por `importlib.util.spec_from_file_location`, como `painel_agentes/test_fila_etiquetas_tela.py` faz. Por isso `wms_chegada.py` não pode importar `wms`.

### Estados da chegada

```
RASCUNHO --concluir, tudo na faixa--------------------> LIBERADA
RASCUNHO --concluir, alguma classe fora da faixa------> AGUARDANDO_ESCRITORIO
AGUARDANDO_ESCRITORIO --cliente autoriza pelo link----> LIBERADA        (liberacao_via = CLIENTE)
AGUARDANDO_ESCRITORIO --escritorio registra por fora--> LIBERADA        (liberacao_via = ESCRITORIO)
AGUARDANDO_ESCRITORIO --cliente nao autoriza----------> AGUARDANDO_ESCRITORIO (resposta_cliente = NAO_AUTORIZADO)
AGUARDANDO_ESCRITORIO --escritorio: nao receber-------> NAO_RECEBIDA
```

Um recebimento pertence a no máximo uma chegada que não seja `NAO_RECEBIDA`. Depois de `NAO_RECEBIDA` ele volta para a lista do Receber (continua esperado na Stokki) e pode ganhar uma chegada nova.

---

### Task 1: Faixas de temperatura

**Files:**
- Modify: `painel_agentes/wms.py`
- Create: `painel_agentes/wms_chegada.py`
- Modify: `painel_agentes/painel_agentes.py` (rota de temperatura do produto, do plano 1), `painel_agentes/templates/wms.html` (mesmo cartão)
- Test: `painel_agentes/test_wms_chegada.py`

**Interfaces:**
- Consumes: `wms_produtos.temperatura` (plano 1).
- Produces:
  - colunas `wms_produtos.temp_min REAL`, `temp_max REAL` (nulas = usa o padrão); `wms.definir_temperatura_produto(conn, produto_id, temperatura, temp_min=None, temp_max=None)` passa a gravar as duas (as duas juntas ou nenhuma; `temp_min > temp_max` → `ErroWMS`).
  - `wms_chegada.FAIXAS_PADRAO = {"REFRIGERADO": (0.0, 7.0), "CONGELADO": (-18.0, -12.0)}`
  - `wms_chegada.faixas(config: dict) -> dict[str, tuple[float, float]]`: o padrão, sobrescrito por `config["wms"]["temperatura_faixas"]` no formato `{"REFRIGERADO": [0, 7], "CONGELADO": [-18, -12]}`; entrada malformada é ignorada.
  - `wms_chegada.classes_da_carga(conn, recebimento_ids: list[int]) -> list[str]`: classes entre `REFRIGERADO` e `CONGELADO` presentes nos produtos dos itens, nessa ordem. Usa só o cadastro (`wms_produtos.temperatura`); produto sem temperatura não conta.
  - `wms_chegada.faixa_da_carga(conn, recebimento_ids, classe: str, config: dict) -> tuple[float, float]`: interseção da faixa da classe com as faixas próprias dos produtos dessa classe na carga (maior dos mínimos, menor dos máximos).
  - `wms_chegada.conectar(caminho=None)`, `wms_chegada.garantir_tabelas(conn)`.

- [ ] **Step 1: Testes que falham**

```python
def test_faixas_padrao_e_config(self):
    self.assertEqual(wms_chegada.faixas({}), {"REFRIGERADO": (0.0, 7.0), "CONGELADO": (-18.0, -12.0)})
    f = wms_chegada.faixas({"wms": {"temperatura_faixas": {"REFRIGERADO": [2, 8], "CONGELADO": "x"}}})
    self.assertEqual(f, {"REFRIGERADO": (2.0, 8.0), "CONGELADO": (-18.0, -12.0)})

def test_classes_da_carga(self):
    # rec com um produto REFRIGERADO, um CONGELADO e um sem temperatura
    self.assertEqual(wms_chegada.classes_da_carga(self.conn, [self.rec]), ["REFRIGERADO", "CONGELADO"])

def test_carga_sem_temperatura_nao_tem_classe(self):   # Review Focus 4
    self.assertEqual(wms_chegada.classes_da_carga(self.conn, [self.rec_seco]), [])

def test_faixa_propria_do_produto_estreita(self):
    wms.definir_temperatura_produto(self.conn, self.p_refri, "REFRIGERADO", temp_min=2, temp_max=5)
    self.assertEqual(wms_chegada.faixa_da_carga(self.conn, [self.rec], "REFRIGERADO", {}), (2.0, 5.0))
```

- [ ] **Step 2: Rodar e ver falhar** — de dentro de `painel_agentes/`: `py -3.11 -m unittest test_wms_chegada -v`.
- [ ] **Step 3: Implementar.** Na tela do produto, dois campos numéricos "Mín °C" e "Máx °C" ao lado do select de temperatura, visíveis quando a temperatura é refrigerado ou congelado.
- [ ] **Step 4: Rodar e ver passar**, junto com `test_wms_doca test_wms_doca_tela`.
- [ ] **Step 5: Commit** — `WMS: faixas de temperatura por classe e por produto`

---

### Task 2: Abrir a chegada e guardar fotos

**Files:**
- Modify: `painel_agentes/wms_chegada.py`
- Test: `painel_agentes/test_wms_chegada.py`

**Interfaces:**
- Produces:
  - `class wms_chegada.ErroChegada(ValueError)`.
  - Tabelas (em `garantir_tabelas`):
    - `wms_chegadas`: `id`, `uuid` UNIQUE, `estado`, `motorista`, `placa`, `transportadora`, `volumes`, `lacre`, `integra` (1/0/NULL), `observacao`, `chegada_em`, `operador_id`, `operador_nome`, `concluida_em`, `liberacao_via`, `liberacao_nome`, `liberacao_texto`, `liberacao_em`, `resposta_cliente`, `resposta_cliente_nome`, `resposta_cliente_em`, `nao_recebida_motivo`, `nao_recebida_por`, `criado_em`. Textos `NOT NULL DEFAULT ''`.
    - `wms_chegada_recebimentos`: `chegada_id`, `recebimento_id`, PK das duas.
    - `wms_chegada_fotos`: `id`, `chegada_id`, `tipo`, `arquivo`, `criado_em`.
    - `wms_chegada_temperaturas`: `chegada_id`, `classe`, `medida_c`, `faixa_min`, `faixa_max`, `fora` (1/0), PK (`chegada_id`, `classe`).
    - `wms_chegada_envios`: `chave` PK, `enviado_em` (idempotência dos e-mails, Task 6).
  - `wms_chegada.TIPOS_FOTO = ("CAMINHAO_ABERTO", "PLACA", "NOTA_FISCAL", "TERMOMETRO", "CARGA", "AVARIA")`
  - `wms_chegada.PASTA_FOTOS` = `DB_PATH.parent / "chegadas"`; `MAX_FOTO_BYTES = 12 * 1024 * 1024`.
  - `wms_chegada.abrir(conn, *, uuid: str, recebimento_ids: list[int], operador: dict) -> dict` (a chegada; idempotente por `uuid`; `chegada_em` = agora).
  - `wms_chegada.salvar_foto(conn, chegada_id: int, tipo: str, conteudo: bytes, pasta: Path | None = None) -> dict` → `{"id", "tipo", "arquivo"}`; nome `CHG-{chegada_id}-{8 hex}.jpg`.
  - `wms_chegada.remover_foto(conn, chegada_id: int, foto_id: int) -> None` (só em `RASCUNHO`; apaga linha e arquivo).
  - `wms_chegada.caminho_foto(conn, chegada_id: int, foto_id: int, pasta: Path | None = None) -> Path` (levanta `ErroChegada` se a foto não é dessa chegada).
  - `wms_chegada.detalhe(conn, chegada_id: int) -> dict`: a chegada + `"recebimentos": [{"id", "codigo", "embarcador"}]`, `"fotos": [...]`, `"temperaturas": [...]`.

Regras de `abrir`: lista vazia → erro; recebimento inexistente ou com `estado != "ESPERADO"` → erro; recebimento já numa chegada que não é `NAO_RECEBIDA` → erro `Esse recebimento já tem chegada registrada.`, exceto se for a mesma `uuid`.

- [ ] **Step 1: Testes que falham**: `test_abrir_cria_rascunho`, `test_abrir_mesmo_uuid_devolve_a_mesma`, `test_abrir_com_dois_recebimentos`, `test_abrir_recebimento_ja_em_outra_chegada_recusa`, `test_abrir_de_novo_depois_de_nao_recebida`, `test_abrir_recebimento_encerrado_recusa`, `test_salvar_foto_regrava_jpeg` (entra PNG 3000×2000, sai JPEG com lado maior 2200), `test_foto_que_nao_e_imagem` , `test_foto_grande_demais`, `test_foto_tipo_invalido`, `test_remover_foto_so_em_rascunho`, `test_caminho_foto_de_outra_chegada_recusa`.

  Os três de foto inválida (Review Focus 2) terminam com:

```python
self.assertEqual(self.n("wms_chegada_fotos"), 0)
self.assertEqual(list(self.pasta.glob("*")), [])
```

  HEIC: bytes que o Pillow não abre caem no mesmo erro, com a mensagem `Não consegui ler a foto. Tire de novo pela câmera do aparelho.`

- [ ] **Step 2: Rodar e ver falhar.**
- [ ] **Step 3: Implementar.**
- [ ] **Step 4: Rodar e ver passar.**
- [ ] **Step 5: Commit** — `WMS: chegada do caminhao (tabelas, abrir, fotos)`

---

### Task 3: Concluir o checklist e travar a conferência

**Files:**
- Modify: `painel_agentes/wms_chegada.py`
- Test: `painel_agentes/test_wms_chegada.py`

**Interfaces:**
- Consumes: `classes_da_carga`, `faixa_da_carga` (Task 1).
- Produces:
  - `wms_chegada.concluir(conn, chegada_id: int, dados: dict, config: dict, operador: dict) -> dict` → `{"chegada": detalhe, "fora_da_faixa": bool, "avaria": bool}`. Repetir numa chegada já concluída devolve o estado atual sem mudar nada.
  - `dados`: `{"motorista": str, "placa": str, "transportadora": str, "volumes": str, "lacre": str, "integra": bool, "observacao": str, "temperaturas": {"REFRIGERADO": "6,5", ...}}`.
  - `wms_chegada.estado_do_recebimento(conn, recebimento_id: int) -> str`: `"SEM_CHEGADA"` ou o estado da chegada viva (a que não é `NAO_RECEBIDA`).
  - `wms_chegada.pode_conferir(conn, recebimento_id: int) -> bool`: `estado_do_recebimento == "LIBERADA"`.

Validação de `concluir` (cada falha é `ErroChegada` com a mensagem ao operador):

| Exigência | Mensagem |
|---|---|
| `motorista` com 3+ caracteres | `Informe o nome do motorista.` |
| `placa`: sem espaço/hífen, maiúscula, 7 caracteres alfanuméricos | `Placa inválida.` |
| 1+ foto `CAMINHAO_ABERTO` | `Falta a foto do caminhão aberto.` |
| 1+ foto `CARGA` | `Falta pelo menos uma foto da carga.` |
| `integra` é `True` ou `False` | `Diga se a carga chegou inteira.` |
| `integra False`: 1+ foto `AVARIA` e `observacao` com 5+ caracteres | `Avaria precisa de foto e de observação.` |
| para cada classe da carga: temperatura numérica | `Informe a temperatura do {refrigerado\|congelado}.` |
| havendo classe: 1+ foto `TERMOMETRO` | `Falta a foto do termômetro.` |

Fotos `PLACA` e `NOTA_FISCAL` são opcionais. Temperatura aceita vírgula e sinal (`"6,5"`, `"-18"`). Para cada classe grava uma linha em `wms_chegada_temperaturas` com a faixa usada e `fora = medida < min or medida > max` (os limites estão dentro). Alguma `fora` → `AGUARDANDO_ESCRITORIO`; senão `LIBERADA`.

- [ ] **Step 1: Testes que falham**: um por linha da tabela acima, mais `test_concluir_na_faixa_libera`, `test_limite_da_faixa_esta_dentro` (7,0 e 0,0 liberam; -12 e -18 liberam), `test_fora_da_faixa_aguarda_escritorio` (`-9,0` no congelado), `test_temperatura_com_virgula_e_negativa` e `test_temperatura_vazia_ou_texto` (Review Focus 3), `test_carga_seca_nao_pede_temperatura` (Review Focus 4), `test_carga_mista_pede_as_duas`, `test_concluir_duas_vezes_nao_muda`, `test_pode_conferir_so_liberada` (`SEM_CHEGADA`, `RASCUNHO` e `AGUARDANDO_ESCRITORIO` → `False`).
- [ ] **Step 2: Rodar e ver falhar.**
- [ ] **Step 3: Implementar.**
- [ ] **Step 4: Rodar e ver passar.**
- [ ] **Step 5: Commit** — `WMS: concluir checklist da chegada e faixa de temperatura`

---

### Task 4: Decisões sobre carga fora da faixa

**Files:**
- Modify: `painel_agentes/wms_chegada.py`
- Test: `painel_agentes/test_wms_chegada.py`

**Interfaces:**
- Produces (todas devolvem `detalhe`; todas exigem `AGUARDANDO_ESCRITORIO`, senão `ErroChegada("Essa carga não está mais aguardando decisão.")`):
  - `wms_chegada.responder_cliente(conn, chegada_id: int, nome: str, autorizado: bool) -> dict`. Nome com 3+ caracteres. Já havendo `resposta_cliente`: `ErroChegada("Esse pedido de liberação já foi respondido.")`. Autorizado: `LIBERADA`, `liberacao_via="CLIENTE"`, `liberacao_nome=nome`. Não autorizado: continua aguardando, `resposta_cliente="NAO_AUTORIZADO"`.
  - `wms_chegada.liberar_por_fora(conn, chegada_id: int, texto: str, usuario: str) -> dict`. `texto` com 10+ caracteres (`Descreva quem autorizou e por onde.`). `LIBERADA`, `liberacao_via="ESCRITORIO"`, `liberacao_nome=usuario`, `liberacao_texto=texto`.
  - `wms_chegada.nao_receber(conn, chegada_id: int, motivo: str, usuario: str) -> dict`. `motivo` com 5+ caracteres. `NAO_RECEBIDA`.
- A troca de estado é um `UPDATE ... WHERE id = ? AND estado = 'AGUARDANDO_ESCRITORIO'` (e, na resposta do cliente, `AND resposta_cliente = ''`), conferindo `rowcount`, para duas respostas simultâneas não passarem as duas.

- [ ] **Step 1: Testes que falham**: `test_cliente_autoriza_libera`, `test_cliente_nao_autoriza_continua_aguardando`, `test_cliente_responde_duas_vezes` (Review Focus 1: a segunda levanta e o estado é o da primeira), `test_cliente_responde_depois_do_escritorio_decidir` (Review Focus 5), `test_liberar_por_fora_exige_texto`, `test_liberar_por_fora_depois_de_nao_autorizado_e_permitido`, `test_nao_receber_exige_motivo`, `test_nao_receber_devolve_recebimento_para_a_lista` (`estado_do_recebimento == "SEM_CHEGADA"`), `test_decisao_em_chegada_liberada_recusa`.
- [ ] **Step 2: Rodar e ver falhar.**
- [ ] **Step 3: Implementar.**
- [ ] **Step 4: Rodar e ver passar.**
- [ ] **Step 5: Commit** — `WMS: liberacao e recusa de carga fora da faixa`

---

### Task 5: Relatório de Recebimento em PDF

**Files:**
- Create: `painel_agentes/wms_chegada_pdf.py`
- Test: `painel_agentes/test_wms_chegada_pdf.py`

**Interfaces:**
- Consumes: `wms_chegada.detalhe`.
- Produces: `wms_chegada_pdf.gerar(detalhe: dict, fotos: list[tuple[str, bytes]], emitido_em: datetime | None = None) -> bytes` (`fotos` = `(tipo, bytes do jpeg)`); `wms_chegada_pdf.do_banco(conn, chegada_id: int, pasta: Path | None = None) -> bytes` (monta `detalhe` e lê as fotos do disco; foto ausente no disco é pulada).

Conteúdo, nesta ordem: cabeçalho com o logo e o título `Relatório de Recebimento`; carga (códigos dos recebimentos, embarcador); data e hora da chegada; motorista, placa, transportadora; volumes e lacre; temperatura por classe (`medida`, `faixa`, `dentro`/`fora da faixa`); condição da carga (`íntegra` / `com avaria`) e observação; operador; quando houver, o bloco `Liberação`: via cliente (`Autorizado por {nome} em {data}, pelo link de liberação`) ou via escritório (`Registrado por {usuário} em {data}: {texto}`); fotos com legenda do tipo, em grade, com páginas extras se preciso; rodapé `Emitido em {data} · Fresh Log`. O resultado da conferência não entra. Layout, fontes e helpers: copiar o jeito de `comprovante_entrega/gerar_pdf.py` (`_colar_logo`, A4 a 150 dpi).

- [ ] **Step 1: Testes que falham**

```python
def test_gera_pdf(self):
    pdf = wms_chegada_pdf.gerar(self.detalhe, [("CAMINHAO_ABERTO", self.jpeg), ("CARGA", self.jpeg)])
    self.assertTrue(pdf.startswith(b"%PDF"))

def test_muitas_fotos_viram_mais_paginas(self):
    um = wms_chegada_pdf.gerar(self.detalhe, [("CARGA", self.jpeg)])
    muitas = wms_chegada_pdf.gerar(self.detalhe, [("CARGA", self.jpeg)] * 14)
    self.assertGreater(self.paginas(muitas), self.paginas(um))

def test_sem_foto_e_sem_temperatura_nao_quebra(self):
    self.assertTrue(wms_chegada_pdf.gerar(self.detalhe_seco, []).startswith(b"%PDF"))

def test_do_banco_pula_foto_que_sumiu_do_disco(self): ...
```

  O texto do PDF é imagem (Pillow), então o teste não lê palavras; `paginas()` conta `/Type /Page` nos bytes.

- [ ] **Step 2: Rodar e ver falhar.**
- [ ] **Step 3: Implementar.**
- [ ] **Step 4: Rodar e ver passar**, e abrir um PDF gerado com dados de exemplo para conferir o layout a olho (salvar no diretório temporário da sessão, não no repo).
- [ ] **Step 5: Commit** — `WMS: Relatorio de Recebimento em PDF`

---

### Task 6: E-mails

**Files:**
- Create: `painel_agentes/wms_chegada_avisos.py`
- Modify: `sincronizar_recebimentos_wms.py`
- Test: `painel_agentes/test_wms_chegada_avisos.py`

**Interfaces:**
- Consumes: `wms_chegada.detalhe`, `wms_chegada_pdf.do_banco`, `email_utils.enviar_email(destinatarios, assunto, corpo_html, config_email, cc=None, anexos=None)`, e de `wms_faltas_recebimento`: `embarcador_do_recebimento`, `resolver_destinos`, o uso de `controle_notificacoes.decidir`.
- Produces:
  - `SECAO_CONFIG = "notificacao_chegada_recebimento"`; chaves `forcar_destino` (ausente = preso em `EMAIL_TESTE`; `""` = envio real) e `email_escritorio` (padrão `entregas@freshlogbr.com`).
  - `avisar_escritorio(conn, chegada_id: int, config: dict, motivo: str, enviar=None) -> dict` com `motivo` em `TEMPERATURA`, `AVARIA`, `RESPOSTA_CLIENTE`. Sempre para `email_escritorio`. Idempotente pela chave `escritorio-{motivo}-{chegada_id}`.
  - `avisar_cliente_chegada(conn, chegada_id: int, config: dict, enviar=None, modo_teste: bool = False) -> list[dict]`: um e-mail por recebimento da chegada, assunto `Sua carga chegou: {código}`, resumo do checklist no corpo, PDF anexado como `Relatorio-de-Recebimento-{código}.pdf`. Só com a chegada `LIBERADA`. Idempotente pela chave `chegou-{recebimento_id}` (ou `chegou-{recebimento_id}-teste` quando redirecionado, como o módulo de faltas faz).
  - `pedir_liberacao(conn, chegada_id: int, config: dict, link: str, enviar=None) -> dict`: e-mail ao cliente com medição, faixa e o `link`. Só em `AGUARDANDO_ESCRITORIO`. Não é idempotente (o escritório pode pedir de novo).
  - `enviar_pendentes(conn, config: dict, enviar=None) -> dict` → `{"chegou": n, "escritorio": n}`: manda `avisar_cliente_chegada` para toda chegada `LIBERADA` sem envio registrado e `avisar_escritorio` que tenha falhado.
  - Cada retorno segue o formato de `notificar_faltas`: `{"enviado": bool, "motivo": str, "destinos": [...]}`.
- O tipo de notificação novo tem que aparecer na tela Notificações do painel. Registre do mesmo jeito que `wms_faltas_recebimento.py` registra o `TIPO_NOTIFICACAO` dele em `preferencias_notificacao` / `controle_notificacoes`.

- [ ] **Step 1: Testes que falham** (todos com `enviar` falso que grava as chamadas): `test_escritorio_recebe_temperatura_fora`, `test_escritorio_nao_repete`, `test_cliente_chegou_sai_preso_por_padrao` (sem a seção no config o destino é `EMAIL_TESTE`), `test_cliente_chegou_leva_pdf_anexo` (`anexos[0][1].endswith(".pdf")`), `test_cliente_chegou_um_por_recebimento`, `test_cliente_chegou_nao_sai_aguardando_escritorio`, `test_cliente_chegou_nao_repete`, `test_pedir_liberacao_leva_o_link`, `test_pedir_liberacao_fora_do_estado_recusa`, `test_pendentes_manda_o_que_faltou`, `test_falha_de_envio_nao_registra` (`enviar` devolve `False` → sem linha em `wms_chegada_envios`, e `enviar_pendentes` tenta de novo).
- [ ] **Step 2: Rodar e ver falhar.**
- [ ] **Step 3: Implementar.** Em `sincronizar_recebimentos_wms.py`, no fim da rodada e fora do `--modo-teste`, chamar `enviar_pendentes` dentro de `try/except` com log: e-mail nunca derruba a sincronização.
- [ ] **Step 4: Rodar e ver passar** — `test_wms_chegada_avisos`, e da raiz `py -3.11 -m unittest test_sincronizar_recebimentos_wms`.
- [ ] **Step 5: Commit** — `WMS: e-mails da chegada (escritorio, cliente e pedido de liberacao)`

---

### Task 7: Página pública de liberação (portal)

**Files:**
- Create: `portal_cliente/link_liberacao.py`, `portal_cliente/templates/liberacao.html`, `portal_cliente/templates/liberacao_erro.html`
- Modify: `portal_cliente/app.py` (registro ~l.1823; `_LIVRES_ANTES_DA_TROCA` ~l.182)
- Test: `portal_cliente/test_link_liberacao.py`

**Interfaces:**
- Consumes: `wms_chegada.detalhe`, `responder_cliente`, `caminho_foto`; `email_utils.enviar_email`.
- Produces (mesmo desenho de `portal_cliente/link_ciencia.py`):
  - `_SALT = "liberacao-recebimento"`, `VALIDADE_DIAS = 7`.
  - `gerar(chegada_id: int, segredo: str) -> str`; `ler(token: str, segredo: str, validade_dias: int = VALIDADE_DIAS) -> int | None`; `url(chegada_id: int, segredo: str, url_base: str) -> str`.
  - `registrar(app, *, config, secret, db_path=None, enviar=None)` cria:
    - `GET /liberacao/<token>`: carga, temperatura medida e faixa, fotos, campo Nome e os botões `Autorizo o recebimento` e `Não autorizo`.
    - `POST /liberacao/<token>/responder` (form `nome`, `resposta` = `autorizo` | `nao_autorizo`): grava, avisa o escritório por e-mail (assunto `Cliente {autorizou|não autorizou} o recebimento: {código}`) e redireciona com 303 para o GET.
    - `GET /liberacao/<token>/foto/<int:foto_id>`: serve a foto, só se for da chegada do token.
  - Token inválido ou vencido: `liberacao_erro.html`, status 404. Chegada que já não aguarda ou já respondida: a mesma página do GET em modo leitura, dizendo o que foi registrado e por quem, sem botões.

- [ ] **Step 1: Testes que falham**: `test_token_ida_e_volta`, `test_token_adulterado_ou_vencido_404` (Review Focus 5), `test_pagina_mostra_medicao_e_faixa`, `test_autorizar_libera_e_avisa_escritorio`, `test_nao_autorizar_registra_e_avisa`, `test_responder_duas_vezes` (Review Focus 1: o segundo POST não muda nada e a página mostra a primeira resposta), `test_responder_depois_do_escritorio_decidir` (Review Focus 5), `test_nome_vazio_volta_com_erro`, `test_foto_de_outra_chegada_404`, `test_rota_livre_sem_login` (sem sessão, o GET responde 200 e não redireciona para o login nem para a troca de PIN). Siga `portal_cliente/test_link_ciencia.py` e `test_link_ciencia_pagina.py`.
- [ ] **Step 2: Rodar e ver falhar** — da raiz: `py -3.11 -m unittest portal_cliente.test_link_liberacao -v`.
- [ ] **Step 3: Implementar**, registrar em `app.py` e incluir os três endpoints em `_LIVRES_ANTES_DA_TROCA`.
- [ ] **Step 4: Rodar e ver passar**, mais `portal_cliente.test_link_ciencia portal_cliente.test_link_ciencia_pagina` (o registro novo não quebra o vizinho).
- [ ] **Step 5: Commit** — `Portal: pagina de liberacao de recebimento fora da faixa`

---

### Task 8: Rotas do painel e trava da conferência

**Files:**
- Modify: `painel_agentes/painel_agentes.py`
- Modify: `MAPA_DO_SISTEMA.txt`
- Test: `painel_agentes/test_wms_chegada_tela.py`

**Interfaces:**
- Consumes: `wms_chegada.*`, `wms_chegada_avisos.*`, `wms_chegada_pdf.do_banco`, `link_liberacao.url` (com `portal_cliente.secret_key` e a URL base do portal, do mesmo jeito que o painel já monta o link de `link_ciencia`, ~l.2032), `_wms_ids_etapas()`.
- Produces:
  - `_wms_recebimento_usa_etapas(conn, recebimento_id) -> bool`: algum item do recebimento tem produto de embarcador com etapas.
  - `GET /api/wms/recebimentos` e `/<id>`: cada recebimento ganha `"usa_etapas": bool`, `"chegada_estado": str`, `"chegada_id": int | None`.
  - **Trava**: `POST /api/wms/caixas/<serial>/ativar` (com `recebimento_id`), `POST /api/wms/recebimentos/<id>/itens/<item>/enderecar` e `POST /api/wms/recebimentos/<id>/encerrar` devolvem 400 `Faça o Receber (checklist da chegada) antes de conferir.` quando `_wms_recebimento_usa_etapas` e não `wms_chegada.pode_conferir`.
  - Galpão (`_NIVEIS_WMS`; POSTs com `@exige_mesma_origem` e `_wms_exige_operador()`):
    - `POST /api/wms/chegadas` `{"uuid", "recebimento_ids"}` → `{"chegada": detalhe, "classes": [...], "faixas": {classe: [min, max]}}`
    - `GET /api/wms/chegadas/<id>` → o mesmo formato
    - `POST /api/wms/chegadas/<id>/fotos` multipart (`tipo`, `foto`) → `{"foto": {...}}`
    - `DELETE /api/wms/chegadas/<id>/fotos/<foto_id>`
    - `GET /wms/chegadas/<id>/foto/<foto_id>` (níveis `_NIVEIS_WMS_CONSULTA`)
    - `POST /api/wms/chegadas/<id>/concluir` (corpo = `dados`) → retorno de `concluir` + `"avisos": {...}`. Depois de concluir: fora da faixa → `avisar_escritorio(TEMPERATURA)`; avaria → `avisar_escritorio(AVARIA)`; liberada → `avisar_cliente_chegada`. Falha de e-mail não desfaz a conclusão.
    - `GET /wms/chegadas/<id>/relatorio.pdf` (níveis `_NIVEIS_WMS_CONSULTA`)
  - Escritório (`_NIVEIS_WMS_ADMIN`, `@exige_mesma_origem`):
    - `POST /api/wms/chegadas/<id>/pedir-liberacao` → `{"link": str, "envio": {...}}`
    - `POST /api/wms/chegadas/<id>/liberar-por-fora` `{"texto"}` → `{"chegada": detalhe}`, e dispara `avisar_cliente_chegada`
    - `POST /api/wms/chegadas/<id>/nao-receber` `{"motivo"}` → `{"chegada": detalhe}`
  - `ErroChegada` → 400 com `{"erro": str}`.

- [ ] **Step 1: Testes que falham**: `test_lista_marca_chegada_estado`, `test_conferir_sem_chegada_e_barrado` (as três rotas da trava), `test_conferir_com_chegada_liberada_passa`, `test_embarcador_sem_etapas_nao_tem_trava`, `test_fluxo_completo_na_faixa` (abrir, 2 fotos, concluir → `LIBERADA`, `enviar` do cliente chamado), `test_fora_da_faixa_avisa_escritorio_e_nao_o_cliente`, `test_galpao_nao_decide` (nível `galpao` recebe 403 em `pedir-liberacao`, `liberar-por-fora` e `nao-receber`), `test_pedir_liberacao_devolve_link`, `test_liberar_por_fora_libera_e_avisa_cliente`, `test_nao_receber`, `test_foto_grande_demais_400`, `test_relatorio_pdf` (`content_type == "application/pdf"`), `test_foto_de_outra_chegada_404`. Os e-mails são trocados por `mock.patch.object(wms_chegada_avisos, ...)`.
- [ ] **Step 2: Rodar e ver falhar.**
- [ ] **Step 3: Implementar** e registrar no `MAPA_DO_SISTEMA.txt`: os três módulos novos, as rotas `/api/wms/chegadas/*` e `/wms/chegadas/*`, a pasta `dados/chegadas/`, a seção de config `notificacao_chegada_recebimento`, `wms.temperatura_faixas`, a página `/liberacao/<token>` do portal e o envio de pendentes dentro do timer `wms-recebimentos`.
- [ ] **Step 4: Rodar e ver passar** — `test_wms_chegada_tela test_wms_caixas_tela test_wms_doca_tela test_wms_faltas_recebimento`.
- [ ] **Step 5: Commit** — `WMS: rotas da chegada e trava da conferencia sem checklist`

---

### Task 9: Telas Receber

**Files:**
- Modify: `painel_agentes/templates/wms.html`
- Test: `painel_agentes/test_wms_chegada_tela.py`

**Interfaces:**
- Consumes: rotas da Task 8; `S.podeAdministrar`, `api()`, `toast()`, `uuid()`, `ir()` existentes.
- Produces: `TELAS.receberChegada` (lista), `TELAS.chegada1`, `TELAS.chegada2`, `TELAS.chegadaAguardando`.

Com `etapas_ativo`:

- **Início**: botão `Receber` em largura inteira, com a contagem das cargas esperadas que ainda não foram liberadas e o texto `cargas esperadas · checklist da chegada do caminhão`. O botão `Conferir Recebimento` (plano 1) passa a contar e listar só recebimento com `chegada_estado == "LIBERADA"` ou sem etapas.
- **Lista do Receber**: recebimentos esperados com etapas e `chegada_estado` em `SEM_CHEGADA`, `RASCUNHO` ou `AGUARDANDO_ESCRITORIO`. Selo por estado (`aguardando o escritório` em destaque). Dá para marcar mais de um antes de começar (`+ outro recebimento no mesmo caminhão`).
- **`chegada1` (quem chegou)**: carga, motorista, placa, transportadora (opcional), fotos `Caminhão aberto` (obrigatória), `Placa` e `Nota fiscal` (opcionais), botão `Continuar`. Foto: `<input type="file" accept="image/*" capture="environment">`, enviada na hora; a miniatura vem da rota de foto; toque longo ou um `×` remove.
- **`chegada2` (como chegou)**: para cada classe devolvida pela rota, campo `Temperatura (°C)` com a faixa esperada escrita embaixo e a foto do termômetro; volumes/pallets; lacre; `A carga chegou inteira?` (Sim / Tem avaria); fotos da carga e da avaria; observação; botão `Concluir recebimento`. Erro do servidor aparece no `toast` com a mensagem dele.
- Resultado: liberada → toast `Recebido. A carga está em Conferir Recebimento.` e volta à lista. Fora da faixa → `chegadaAguardando`.
- **`chegadaAguardando`**: cartão `Temperatura fora da faixa` (medido e faixa), cartão `Aguardando o escritório` com o texto `Não descarregue para o estoque até a decisão.` Para `S.podeAdministrar`: `Pedir liberação ao cliente` (mostra o link num campo com botão `Copiar` e o resultado do envio), `Registrar liberação recebida por fora` (texto obrigatório), `Não receber` (motivo obrigatório). Mostra também a resposta do cliente quando houver. A tela se atualiza sozinha a cada 30 s enquanto aberta.
- **Dentro de Conferir Recebimento**: cartão `Chegada` com motorista, placa, hora, temperatura e o link `Relatório de Recebimento (PDF)`.
- Sem rede: os campos de texto ficam guardados no aparelho (`localStorage`, em `try/catch`) e voltam ao reabrir; foto e conclusão exigem rede e avisam `Sem rede: a foto não foi enviada. Tente de novo.`

- [ ] **Step 1: Teste que falha** — o HTML de `/wms` contém `TELAS.receberChegada`, `TELAS.chegada1`, `TELAS.chegada2`, `TELAS.chegadaAguardando`, `Pedir liberação ao cliente`, `capture="environment"`.
- [ ] **Step 2: Rodar e ver falhar.**
- [ ] **Step 3: Implementar.**
- [ ] **Step 4: Rodar e ver passar**, e provar na tela (porta 8099, Playwright, banco de teste, e-mails trocados por falso): (a) carga seca do começo ao fim até aparecer em Conferir Recebimento; (b) carga congelada a -9 °C: fica aguardando, nível `galpao` não vê os botões, nível `total` pede a liberação e recebe o link; (c) abrir o link num portal local em outra porta, autorizar, e ver a carga liberada no `/wms`; (d) baixar o PDF.
- [ ] **Step 5: Commit** — `WMS: telas Receber com checklist da chegada`

---

### Task 10: Regressão

- [ ] **Step 1:** `py -3.11 -m py_compile` em todos os arquivos `.py` criados ou alterados neste plano → sem saída.
- [ ] **Step 2:** de dentro de `painel_agentes/`: `py -3.11 -m unittest test_wms_chegada test_wms_chegada_pdf test_wms_chegada_avisos test_wms_chegada_tela test_wms_doca test_wms_doca_tela test_wms_caixas test_wms_caixas_tela test_wms_pedidos test_wms_separacao test_wms_separacao_tela test_wms_telas test_wms_faltas_recebimento test_wms_pilotos` → OK.
- [ ] **Step 3:** da raiz: `py -3.11 -m unittest portal_cliente.test_link_liberacao portal_cliente.test_link_ciencia portal_cliente.test_link_ciencia_pagina test_sincronizar_recebimentos_wms` → OK.
- [ ] **Step 4:** conferir o `MAPA_DO_SISTEMA.txt` contra a lista da Task 8; commit se mudou — `Mapa: checklist da chegada do recebimento`.
- [ ] **Step 5:** Parar aqui. Push, homologação e deploy são do Hugo. No deploy: reiniciar `painel-agentes` e `portal-cliente`; criar `dados/chegadas/` como `www-data`; o e-mail ao cliente continua preso até o Hugo soltar na tela Notificações.
