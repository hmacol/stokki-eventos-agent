# Endereço de entrega conferido no envio do portal: plano de implementação

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Na prévia do envio por XML, o cliente vê o endereço de entrega e pode trocá-lo (casos 1 a 4 da spec). O endereço escolhido vale pra área, pro dia fixo e pra gravação, e entra como `<entrega>` no XML que vai pra Stokki.

**Architecture:** O módulo novo `portal_cliente/endereco_entrega.py` concentra as regras: validar o endereço pelo CEP, aplicar no `nfe`, inserir o `<entrega>` no XML, achar a "última entrega" no núcleo e classificar o caso. O resto só chama esse módulo:
- `envio_pedidos.confirmar_envios` e `avisos_fora_dia_fixo` aplicam o endereço no `nfe` logo depois do `ler_nfe`, como o trabalho de 24/09 já faz com o `<entrega>` da nota. Assim área, dia fixo e gravação mudam sem mexer em cada uma.
- `app.py` devolve o caso na prévia e consulta o CEP.
- O worker `enviar_stokki.py` insere o `<entrega>` no arquivo do lote.
- `_envios.html` desenha a coluna.

**Tech Stack:** Python 3.11, Flask, SQLite (`dados/dados.db`), `xml.etree`/texto pro XML, JS puro no template, `unittest`.

**Spec:** `docs/superpowers/specs/2026-10-27-endereco-entrega-portal-design.md` (aprovada pelo Hugo em 27/10). Leia a spec antes de cada task.

## Global Constraints

- Python sempre `py -3.11`. Testes com `py -3.11 -m unittest portal_cliente.<modulo> -v`, rodados da raiz da worktree. Nunca junte `painel_agentes` no mesmo comando.
- Trabalhe na worktree `.claude/worktrees/endereco-entrega-portal`, ramo `endereco-entrega-portal` a partir de `origin/master`. Faça commits locais por task, só dos arquivos da task (nunca `git add -A`). **Push e deploy, só quando o Hugo pedir.**
- Commits em português: `Portal: <o que mudou e por quê>`, com o rodapé `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Chave no config: `portal_cliente.envios.endereco_entrega_ativo`, **padrão desligado** (`False`). Desligada, nada desta entrega aparece nem é exigido: a tela fica igual à de hoje mais o trabalho de 24/09. Nunca editar o `config.yaml`.
- Só pedidos por XML. Planilha fica de fora.
- NF com transportadora TERCEIROS ou RETIRADA (`envio_pedidos._tipo_transportadora`): sem formulário e sem caso 4.
- A memória da "última entrega" é por embarcador (`cnpj_embarcador` + `sender_id` da empresa do envio) + documento do destinatário. Nunca atravessa embarcadores.
- Nunca confiar no navegador. No confirmar, o servidor recalcula o caso e valida o endereço pelo CEP. Município, UF e código IBGE vêm sempre da consulta do CEP.
- O arquivo XML original do cliente (`xml_path`) nunca é alterado. O `<entrega>` só entra na cópia do lote do worker.
- Textos da tela, exatos:
  - Cabeçalho da coluna: `Endereço de entrega`.
  - Aviso: `Se a entrega for em outro lugar, informe abaixo. Sem alteração, o pedido vai pro endereço fiscal da nota.`
  - Botões: `Alterar endereço de entrega`, `Usar este endereço`, `Cancelar`, `Editar`, `Voltar pro endereço fiscal`.
  - Selos: `alterado por você`, `informado na nota`.
  - Caso 4: `No último pedido pra este destinatário (dd/mm) a entrega foi em X. Onde entregar este?`, com os botões `Entregar lá de novo`, `Endereço fiscal`, `Outro endereço`.
  - Redespacho e retirada: `Redespacho: entregue à transportadora X` / `Retirada`.
- Comentários seguem o arquivo: `envio_pedidos.py` e `app.py` usam acento; `enviar_stokki.py` também. Testes com XML sintético, nunca dado real de cliente no repo.

## Review Focus

1. **CEP que não existe, ou ViaCEP e BrasilAPI fora do ar:** o formulário mostra "CEP não encontrado" e não deixa usar o endereço. Se mesmo assim chegar ao confirmar, o servidor devolve `ErroEnvio` com o rótulo da NF, nunca 500. O teste fica na Task 2 (`montar_endereco`) e na Task 4.
2. **Complemento com `&`, `<` ou aspas** (ex.: "Doca 2 & 3"): o XML do lote continua válido e a Stokki recebe o texto certo. O teste fica na Task 2 (`inserir_entrega_xml`).
3. **O mesmo destinatário em duas NFs do mesmo lote, ambas no caso 4, e o cliente escolhe só numa:** o confirmar recusa a outra, apontando a NF. A escolha de uma linha nunca vale pra outra. O teste fica na Task 4.
4. **Login de grupo enviando pela empresa B:** a "última entrega" procura só nos pedidos da empresa B, nunca nos da empresa A do mesmo grupo. O teste fica na Task 3.
5. **XML com o grupo `<retirada>` ou com prefixo de namespace** (`<nfe:dest>`): o `<entrega>` entra na posição do layout (depois de `</retirada>`, se houver; senão depois de `</dest>`). Se o XML já tiver `<entrega>`, é recusado. O teste fica na Task 2.

---

### Task 1: Resgatar o trabalho de 24/09 numa worktree nova

O trabalho de 24/09 (`ler_nfe` lendo o `<entrega>`, a coluna `endereco_fiscal` e o aviso na tela) está **sem commit** em `.claude/worktrees/endereco-entrega`, montado sobre e5cd18e. É a base das tasks seguintes.

**Files:**
- Modify: `portal_cliente/envio_pedidos.py`, `portal_cliente/templates/_envios.html` (o diff da worktree antiga)
- Create: `portal_cliente/test_endereco_entrega_xml.py` (copiado da worktree antiga)
- Create: `docs/superpowers/specs/2026-10-27-endereco-entrega-portal-design.md`, `docs/superpowers/plans/2026-10-27-endereco-entrega-portal.md` (copiados da master local, onde estão sem commit)

**Interfaces:**
- Produces: `ler_nfe(...)["endereco_fiscal"]`, vazio quando a nota não tem `<entrega>`, senão `"RUA X, 10, BAIRRO, CIDADE/UF, CEP 00000-000"`. Também a coluna `portal_envios.endereco_fiscal` e `_linha(r)["endereco_fiscal"]`.

- [ ] **Step 1:** `git fetch origin` e `git worktree add .claude/worktrees/endereco-entrega-portal -b endereco-entrega-portal origin/master`.
- [ ] **Step 2:** Gere o patch da worktree antiga (`git -C .claude/worktrees/endereco-entrega diff > <scratchpad>/entrega_2409.patch`) e aplique na nova com `git apply --3way`. Copie `portal_cliente/test_endereco_entrega_xml.py`. Resolva os conflitos mantendo o que a master tem hoje: depois de 24/09, `confirmar_envios` ganhou fluxo, XD e planilha, e `_envios.html` ganhou a coluna Fluxo. **Não apague a worktree antiga.**
- [ ] **Step 3:** Rode `py -3.11 -m unittest portal_cliente.test_endereco_entrega_xml -v`. Esperado: 8 OK.
- [ ] **Step 4:** Rode a regressão do envio: `py -3.11 -m unittest portal_cliente.test_fluxo_envio portal_cliente.test_fora_dia_fixo_envio portal_cliente.test_bloqueio_area portal_cliente.test_reenviar_erros -v`. Esperado: tudo OK. Rode também `py -3.11 -m pytest portal_cliente/test_envio_planilha.py -q`. Esperado: passed.
- [ ] **Step 5:** Copie a spec e este plano para a worktree. Commit: `Portal: <entrega> do XML vira o endereco do envio, fiscal guardado so pra tela (trabalho de 24/09)`, com os 3 arquivos de código e os 2 docs.

---

### Task 2: Regras puras do endereço de entrega

**Files:**
- Create: `portal_cliente/endereco_entrega.py`
- Modify: `portal_cliente/cotacao.py` (`consultar_cep` passa a devolver também `"ibge"`)
- Test: `portal_cliente/test_endereco_entrega.py`

**Interfaces:**
- Produces, em `endereco_entrega.py`:
  - `entrega_ativa(config: dict | None) -> bool`: lê `portal_cliente.envios.endereco_entrega_ativo`, padrão `False`.
  - `ErroEndereco(ValueError)`: mensagem pronta pro cliente.
  - `montar_endereco(campos: dict, consultar=cotacao.consultar_cep) -> dict`. A entrada vem do navegador: `{cep, numero, complemento?, logradouro?, bairro?}`. A saída é o **EnderecoEntrega**: `{cep: 8 dígitos, logradouro, numero, complemento, bairro, municipio, uf, cmun: 7 dígitos}`.
    - Município, UF e `cmun` vêm da consulta do CEP.
    - Logradouro e bairro vêm do cliente quando ele mandou, senão da consulta. CEP geral de cidade não traz rua.
    - `numero` é obrigatório (aceita `S/N`).
    - Levanta `ErroEndereco` com: `CEP não encontrado`, `CEP sem código de município: tente de novo em instantes` (sem `ibge`), `Informe o número` e `Informe a rua`.
  - `linha_endereco(end: dict) -> str`: no formato que `ler_nfe` grava em `destinatario_endereco` (`"Rua X, 10 - Compl"`).
  - `linha_fiscal(nfe: dict) -> str`: no mesmo formato de `endereco_fiscal` da Task 1. Extraia o helper da Task 1 pra cá e use nos dois lugares.
  - `aplicar_entrega(nfe: dict, end: dict) -> dict`: devolve uma cópia do `nfe` com `destinatario_endereco`, `destinatario_bairro`, `destinatario_municipio`, `destinatario_uf` e `destinatario_cep` trocados pelo `end`, e `endereco_fiscal = linha_fiscal(nfe original)`. O telefone não muda.
  - `inserir_entrega_xml(conteudo: bytes, end: dict, documento: str, nome: str) -> bytes`: devolve os bytes com o grupo inserido.
    - Formato do grupo: `<entrega><CNPJ|CPF>…</…><xNome>…</xNome><xLgr/><nro/>[<xCpl/>]<xBairro/><cMun/><xMun/><UF/><CEP/></entrega>`. Usa CNPJ se o documento tiver 14 dígitos e CPF se tiver 11. Textos escapados com `xml.sax.saxutils.escape`.
    - Insere **por texto**, pra não reescrever namespaces nem a assinatura: logo depois de `</retirada>` se existir, senão depois de `</dest>`, aceitando prefixo (`</(\w+:)?dest>`). Usa o mesmo prefixo da tag de fechamento encontrada.
    - Levanta `ValueError` se já houver `<entrega>` ou se não achar `</dest>`.
  - `cep_de_texto(texto: str) -> str` (8 dígitos ou `""`) e `numero_de_texto(texto: str) -> str` (número do imóvel no endereço da Vuupt `"Rua X, 123, Bairro, Cidade - UF, 00000-000, Brasil"`, ou `""`).
- Produces, em `cotacao.py`: `consultar_cep(cep)` devolve também `"ibge"` (do ViaCEP; `""` na BrasilAPI).

- [ ] **Step 1: Escreva os testes que falham**, em `test_endereco_entrega.py`, com `consultar` falso:
  - `test_montar_usa_municipio_uf_ibge_do_cep`: o cliente manda `municipio="Outro"`, e o resultado tem o município da consulta e `cmun == "3550308"`.
  - `test_montar_cep_geral_usa_rua_do_cliente`: a consulta volta com `logradouro=""`, e vale a rua digitada.
  - `test_montar_erros`: CEP não achado → `ErroEndereco("CEP não encontrado")`; sem `ibge` → mensagem do `cmun`; sem número → `"Informe o número"`.
  - `test_aplicar_troca_endereco_e_guarda_fiscal`: `destinatario_cep` passa a ser o novo, e `endereco_fiscal == "RUA IVAI, 207, TATUAPE, SAO PAULO/SP, CEP 03080-010"`. Reaproveite o XML sintético de `test_endereco_entrega_xml`.
  - `test_inserir_depois_de_dest`, `test_inserir_depois_de_retirada`, `test_inserir_com_prefixo`: o resultado volta com `ler_nfe` e dá `endereco_fiscal` preenchido e `destinatario_endereco == "Rua Haddock Lobo, 595 - loja 2"`.
  - `test_inserir_escapa_texto`: complemento `Doca 2 & 3 <fundos>` → `ET.fromstring` não falha e o `xCpl` lido é igual ao original.
  - `test_inserir_recusa_se_ja_tem_entrega` e `test_inserir_cpf`: com documento de 11 dígitos, o grupo leva `<CPF>`.
  - `test_cep_e_numero_de_texto`: `"Rua Bela Cintra, 1200, Consolação, São Paulo - SP, 01415-000, Brasil"` → `("01415000", "1200")`. Sem CEP → `""`.
  - `test_entrega_ativa_padrao_desligada`: `entrega_ativa({}) is False` e `entrega_ativa({"portal_cliente": {"envios": {"endereco_entrega_ativo": True}}}) is True`.
- [ ] **Step 2:** Rode `py -3.11 -m unittest portal_cliente.test_endereco_entrega -v`. Esperado: FAIL com `ModuleNotFoundError: endereco_entrega`.
- [ ] **Step 3:** Implemente as funções acima e o `"ibge"` no `consultar_cep`. No `ler_nfe`, troque a montagem do fiscal pela `linha_fiscal`.
- [ ] **Step 4:** Rode os testes desta task, `test_endereco_entrega_xml` e `py -3.11 -m pytest portal_cliente/test_cotacao.py -q`. Esperado: tudo verde.
- [ ] **Step 5:** Commit: `Portal: regras do endereco de entrega (CEP com IBGE, aplicar no pedido, <entrega> no XML)`.

---

### Task 3: "Última entrega" e o caso de cada NF

**Files:**
- Modify: `portal_cliente/endereco_entrega.py`
- Test: `portal_cliente/test_endereco_entrega_memoria.py`

**Interfaces:**
- Consumes: `cep_de_texto` e `numero_de_texto` (Task 2); `regras.transportadoras.CatalogoTransportadoras.carregar_do_banco(db).resolver_por_endereco(texto)`; `envio_pedidos._tipo_transportadora(nome, cnpj)`.
- Produces:
  - `ultima_entrega(conn, cnpj_embarcador: str, sender_id: int | None, destinatario_doc: str, catalogo=None) -> dict | None`. Devolve `{data: "YYYY-MM-DD", data_br: "dd/mm", texto: str, cep: str, numero: str, campos: dict | None, fonte: "vuupt" | "portal"}`. A regra está na seção 3 da spec:
    - **Vuupt:** junte `nucleo_paradas p` com `nucleo_rotas r` (pra `r.data_rota`) e com `nucleo_pedidos ped ON REPLACE(p.codigo,'#','') = ped.codigo`, filtrando:
      - `p.situacao IN ('ENTREGUE','PARCIAL','INSUCESSO')`;
      - `COALESCE(ped.sender_id, p.sender_id) = sender_id`;
      - documento de `ped.destinatario_codigo` só com dígitos `= destinatario_doc`.
      Descarte as paradas sem CEP no `p.endereco` e as que `resolver_por_endereco` reconhece como galpão de redespacho. `campos = None`.
    - **Portal:** linhas de `portal_envios` do mesmo `cnpj_embarcador` e `destinatario_doc`, com `endereco_entrega_origem IN ('cliente','ultima_entrega','nota')`, `status != 'CANCELADO'`, e cujo `codigo_pedido` **não** tenha parada concluída no conjunto Vuupt. A data é `criado_em[:10]`. `campos` sai de `endereco_entrega_json`; na origem `nota`, sai das colunas `destinatario_*` da linha. As colunas novas vêm da Task 4; aqui crie o teste com elas já na tabela (`ALTER` no setup).
    - Fica com a data mais recente. No empate, ganha a Vuupt. Sem `sender_id`, só a fonte portal.
  - `mesmo_endereco(ult: dict, nfe: dict) -> bool`: com fonte `vuupt`, compara só o CEP. Com fonte `portal`, compara CEP e número (número só com dígitos e letras, em maiúsculas).
  - `classificar_entrega(conn, nfe: dict, cnpj_embarcador: str, sender_id: int | None, catalogo=None) -> dict`. Devolve `{"modo": "nota" | "terceiros" | "retirada" | "livre" | "sugestao", "transportadora": str, "sugestao": dict | None}`, nesta ordem:
    1. `nfe["endereco_fiscal"]` preenchido → `nota`.
    2. Tipo da transportadora TERCEIROS ou RETIRADA → `terceiros` ou `retirada`, com o nome.
    3. Há `ultima_entrega` e ela não é o mesmo endereço → `sugestao`. A `sugestao` leva `{data_br, texto, campos}`. Quando `campos` é `None` (fonte Vuupt), monte `{cep, numero}` com `cep_de_texto` e `numero_de_texto`, pro formulário abrir preenchido.
    4. Senão → `livre`.

- [ ] **Step 1: Escreva os testes que falham.** O setup cria um banco temporário: `ep.conectar()` com `DB_PATH` trocado (como em `test_endereco_entrega_xml`) e `nucleo.banco.garantir_esquema(conn)` na mesma conexão. Passe um catálogo falso com `resolver_por_endereco`. Casos:
  - `test_vuupt_corrigida_pela_equipe_vira_sugestao`: entrega ENTREGUE ontem em CEP diferente do fiscal → `modo == "sugestao"`, `sugestao["data_br"]` de ontem, `campos == {"cep": ..., "numero": ...}`.
  - `test_ultima_no_fiscal_nao_sugere`: entrega antiga em outro CEP e a mais recente no fiscal → `livre`.
  - `test_correcao_so_de_numero_nao_sugere`: mesmo CEP e outro número, vindo da Vuupt → `livre`.
  - `test_pedido_aberto_sem_escolha_nao_apaga_memoria`: parada PENDENTE hoje no fiscal e ENTREGUE ontem em outro CEP → `sugestao`.
  - `test_escolha_no_portal_ainda_aberta_conta`: envio de hoje com origem `cliente`, sem parada → `sugestao` com `campos` estruturados.
  - `test_vuupt_vence_escolha_do_portal`: envio com origem `cliente` no CEP A, e a parada concluída desse `codigo_pedido` no CEP B → sugere B.
  - `test_cancelado_e_redespacho_nao_contam`: envio CANCELADO, e parada cujo endereço o catálogo reconhece → `livre`.
  - `test_outro_embarcador_nao_aparece` (Review Focus 4): o mesmo destinatário entregue em outro CEP com outro `sender_id` e outro `cnpj_embarcador` → `livre`.
  - `test_nota_com_entrega_e_terceiros`: `endereco_fiscal` preenchido → `nota`; `_tipo_transportadora` trocado pra devolver `"TERCEIROS"` → `terceiros`.
- [ ] **Step 2:** Rode `py -3.11 -m unittest portal_cliente.test_endereco_entrega_memoria -v`. Esperado: FAIL (`AttributeError: ultima_entrega`).
- [ ] **Step 3:** Implemente `ultima_entrega`, `mesmo_endereco` e `classificar_entrega`.
- [ ] **Step 4:** Rode os testes das Tasks 2 e 3. Esperado: tudo OK.
- [ ] **Step 5:** Commit: `Portal: ultima entrega do destinatario (Vuupt e portal, por embarcador) decide o caso do endereco`.

---

### Task 4: Confirmar o envio com o endereço escolhido

**Files:**
- Modify: `portal_cliente/envio_pedidos.py` (`conectar` com colunas novas, `confirmar_envios`, `avisos_fora_dia_fixo`, `_linha`)
- Test: `portal_cliente/test_endereco_entrega_envio.py`

**Interfaces:**
- Consumes: `entrega_ativa`, `montar_endereco`, `aplicar_entrega`, `classificar_entrega`, `ErroEndereco` (Tasks 2 e 3).
- Produces:
  - Colunas em `portal_envios` (via `_garantir_colunas`): `endereco_entrega_json TEXT` (EnderecoEntrega + `documento` + `nome` do destinatário) e `endereco_entrega_origem TEXT` (`fiscal` | `nota` | `cliente` | `ultima_entrega`; `NULL` em planilha).
  - O item do confirmar ganha `entrega_escolha` (`"fiscal"` | `"cliente"` | `"ultima_entrega"`; sem o campo, vale `fiscal`) e `entrega_endereco` (`{cep, numero, complemento?, logradouro?, bairro?}`).
  - Assinatura nova: `confirmar_envios(conn, cnpj_embarcador, itens, enviado_por, config, regra_xml, fluxos_disponiveis=None, sender_id: int | None = None)`.
  - `_linha(r)["endereco_entrega_origem"]`, com `""` quando nulo.
- Regra, no laço de leitura de `confirmar_envios`, só para XML e só com `entrega_ativa(config)`, **antes** de `classificar_area_envio`:
  1. `caso = classificar_entrega(conn, nfe, emb, sender_id)`.
  2. Com `caso["modo"]` igual a `nota`, `terceiros` ou `retirada`: escolha diferente de `fiscal` → `ErroEnvio("<rótulo>: este pedido não aceita troca de endereço.")`.
  3. Com modo `sugestao` e sem `entrega_escolha` → `ErroEnvio("<rótulo>: escolha onde entregar (no último pedido a entrega foi em outro endereço).")`.
  4. Com escolha `cliente` ou `ultima_entrega`: `end = montar_endereco(item["entrega_endereco"])`, com `ErroEndereco` virando `ErroEnvio("<rótulo>: <mensagem>")`, e depois `nfe = aplicar_entrega(nfe, end)`. `ultima_entrega` só é aceito quando o modo é `sugestao`.
  5. Grava `endereco_entrega_origem`: `nota` quando o modo é `nota`, a escolha quando houve uma, senão `fiscal`. Grava `endereco_entrega_json` só nas origens `cliente` e `ultima_entrega`.
  6. Com a chave desligada, ignora os campos novos e grava origem `nota` ou `fiscal` (pra memória começar a se formar já).
  - Em `avisos_fora_dia_fixo(itens, config=None)`: aplique o mesmo `montar_endereco` + `aplicar_entrega` quando a chave está ligada e a escolha é `cliente`/`ultima_entrega`, antes de `regra_dia_fixo_do_servico`. Endereço inválido: pule o aviso (o confirmar recusa depois).

- [ ] **Step 1: Escreva os testes que falham.** Use o setup de `GravaELista` em `test_endereco_entrega_xml` (mocks de `validar_item`, `info_destinatarios`, `gravar_destinatario`) e mock de `endereco_entrega.consultar_cep` (ou parâmetro). Casos:
  - `test_cliente_altera_grava_entrega_e_fiscal`: `destinatario_cep` com o novo, `endereco_fiscal` com o fiscal, `endereco_entrega_origem == "cliente"` e o json com `cmun`.
  - `test_area_usa_endereco_escolhido`: `classificar_area_envio` (mock) recebe `nfe` com o CEP novo.
  - `test_sugestao_sem_escolha_recusa` e `test_sugestao_duas_nfs_mesmo_destinatario` (Review Focus 3): duas NFs do mesmo destinatário no caso 4 e só a primeira com escolha → `ErroEnvio` citando a NF da segunda, sem gravar nada.
  - `test_cep_invalido_vira_erro_envio` (Review Focus 1): a consulta devolve `None` → `ErroEnvio` com `"CEP não encontrado"` e o rótulo da NF.
  - `test_nota_com_entrega_recusa_troca` e `test_terceiros_recusa_troca`.
  - `test_chave_desligada_ignora_campos`: o item traz `entrega_escolha="cliente"` com a chave desligada → grava o fiscal e `origem="fiscal"`.
  - `test_fora_dia_fixo_olha_endereco_escolhido`: estenda o padrão de `test_fora_dia_fixo_envio` com um XML. O fiscal está em região de quarta, o escolhido em região de quinta, e a data é uma quinta → sem aviso.
- [ ] **Step 2:** Rode `py -3.11 -m unittest portal_cliente.test_endereco_entrega_envio -v`. Esperado: FAIL.
- [ ] **Step 3:** Implemente conforme a regra acima.
- [ ] **Step 4:** Rode os testes desta task e toda a regressão da Task 1 Step 4. Esperado: tudo OK.
- [ ] **Step 5:** Commit: `Portal: confirmar envio aplica o endereco de entrega escolhido (area, dia fixo e gravacao) e exige escolha no caso 4`.

---

### Task 5: API da prévia e consulta de CEP

**Files:**
- Modify: `portal_cliente/app.py` (`api_envios_analisar`, `api_envios_confirmar` e rota nova)
- Test: `portal_cliente/test_endereco_entrega_api.py`

**Interfaces:**
- Consumes: `classificar_entrega`, `entrega_ativa`, `cotacao.consultar_cep`; `cfg = envios.config_stokki_cliente(...)`, que já tem `sender_id`.
- Produces:
  - Em `analisar`, para cada item XML com a chave ligada: `item["entrega"] = classificar_entrega(conn, nfe, cnpj, cfg["sender_id"])`. Calcule uma vez por destinatário distinto do lote e reaproveite. Chave desligada: sem a chave `entrega`. Resposta da prévia: `"entrega_ativa": bool`.
  - Em `confirmar`: passa `sender_id=cfg["sender_id"]` para `confirmar_envios` e `config` para `avisos_fora_dia_fixo`.
  - `GET /api/envios/cep/<cep>`, com `@requer_cliente` e `_exige_pode_enviar()`: devolve `{"cep", "logradouro", "bairro", "cidade", "uf"}` ou 404 com `{"erro": "CEP não encontrado"}`. Sem `ibge` na resposta (é só pra preencher a tela).

- [ ] **Step 1: Escreva os testes que falham** com `app.test_client()`, no padrão da fixture `cliente_http` de `test_cotacao.py` (sessão com `cnpj` e `v`, `auth.sessao_valida` trocado, `_CONFIG` com a chave):
  - `test_analisar_devolve_modo_por_item`: sobe 2 XMLs sintéticos, um com `<entrega>` e outro sem → `modo` `nota` e `livre`.
  - `test_analisar_sem_chave_nao_devolve_entrega`.
  - `test_cep_ok_e_404`.
  - `test_confirmar_passa_sender_id` (mock de `envios.confirmar_envios` conferindo o kwarg).
- [ ] **Step 2:** Rode `py -3.11 -m unittest portal_cliente.test_endereco_entrega_api -v`. Esperado: FAIL.
- [ ] **Step 3:** Implemente.
- [ ] **Step 4:** Rode os testes desta task e a regressão. Esperado: OK.
- [ ] **Step 5:** Commit: `Portal: previa do envio devolve o caso do endereco de entrega e consulta CEP`.

---

### Task 6: Worker insere o `<entrega>` no XML que vai pra Stokki

**Files:**
- Modify: `portal_cliente/enviar_stokki.py` (no laço de preparo, linhas ~580-588)
- Test: `portal_cliente/test_endereco_entrega_worker.py`

**Interfaces:**
- Consumes: `inserir_entrega_xml` (Task 2); as colunas `endereco_entrega_origem` e `endereco_entrega_json` (Task 4).
- Produces: `aplicar_entrega_no_lote(e: dict, destino: Path) -> None`, em `enviar_stokki.py`. Com origem `cliente` ou `ultima_entrega`, reescreve `destino` com `inserir_entrega_xml(destino.read_bytes(), end, end["documento"], end["nome"])`. Nas outras origens, não faz nada. Chame depois do `copyfile`/`transformar_xml`, nos dois ramos (simular e real). Uma falha cai no `except` que já existe e marca ERRO com "Falha ao preparar o XML".

- [ ] **Step 1: Escreva os testes que falham:**
  - `test_origem_cliente_insere_e_original_intacto`: o `destino` volta com `ler_nfe` e dá o endereço novo, e o arquivo de `xml_path` continua byte a byte igual.
  - `test_origem_fiscal_e_nota_nao_mexem`.
  - `test_reenvio_de_erro_insere_de_novo`: chamar duas vezes sobre uma cópia nova do original dá o mesmo resultado (o reenvio parte sempre do original).
- [ ] **Step 2:** Rode `py -3.11 -m unittest portal_cliente.test_endereco_entrega_worker -v`. Esperado: FAIL.
- [ ] **Step 3:** Implemente e ligue no laço.
- [ ] **Step 4:** Rode os testes desta task e `py -3.11 -m unittest portal_cliente.test_enviar_stokki_cross -v`. Esperado: OK.
- [ ] **Step 5:** Commit: `Portal: worker insere o <entrega> escolhido no XML do lote da Stokki (original intacto)`.

---

### Task 7: Tela: coluna "Endereço de entrega" na prévia e na lista

**Files:**
- Modify: `portal_cliente/templates/_envios.html` (CSS no topo, `renderPrevia`, `confirmar`, lista `env-tabela`)
- Modify: `MAPA_DO_SISTEMA.txt` (linha "Envio de pedido (XML) pelo cliente": citar `endereco_entrega.py` e a chave)

**Interfaces:**
- Consumes: `item.entrega` e `entrega_ativa` da prévia (Task 5), `GET api/envios/cep/<cep>` (pelo `request.script_root`, nunca caminho absoluto), e os campos `entrega_escolha` e `entrega_endereco` do confirmar (Task 4). Na lista, `endereco_entrega_origem` e `endereco_fiscal`.
- Comportamento (a prévia aprovada pelo Hugo está descrita na seção 2 da spec; use os textos exatos das Global Constraints):
  - Com `entrega_ativa`, o cabeçalho da coluna vira `Endereço de entrega`.
  - `livre`: fiscal, aviso e botão. O botão abre o formulário na linha (CEP, rua, bairro, número, cidade/UF só leitura, complemento). Com 8 dígitos no CEP, consulta e preenche; erro → `CEP não encontrado` na linha. `Usar este endereço` passa a linha ao estado "alterado" (selo, fiscal riscado, `Editar`, `Voltar pro endereço fiscal`).
  - `nota`: selo `informado na nota` e o fiscal embaixo.
  - `terceiros` / `retirada`: o texto informativo.
  - `sugestao`: o quadro com os 3 botões. `Entregar lá de novo` e `Outro endereço` abrem o formulário (o primeiro preenchido com `sugestao.campos`). `Endereço fiscal` marca a escolha `fiscal`.
  - `confirmar()`: com linha `sugestao` sem escolha, para com `<rótulo>: escolha onde entregar este pedido.` (mesmo padrão da mensagem de horário). Manda `entrega_escolha` e `entrega_endereco` por item.
  - Na lista: com `endereco_entrega_origem` igual a `cliente` ou `ultima_entrega`, mostre `Endereço de entrega alterado no envio`, com o fiscal no `title`. Com `nota`, mantenha o texto de 24/09.
  - Funciona no tema escuro (use só os tokens `var(--…)` que o arquivo já usa) e no celular (a célula quebra linha; o formulário vira uma coluna abaixo de 560 px).
- [ ] **Step 1:** Implemente o comportamento acima.
- [ ] **Step 2:** Verifique no navegador. Suba o portal local em outra porta, com a chave ligada só no processo: de dentro de `portal_cliente/`, `py -3.11 -c "import app; app._CONFIG.setdefault('portal_cliente', {}).setdefault('envios', {})['endereco_entrega_ativo'] = True; app.app.run(host='127.0.0.1', port=8098)"`. Entre com o cliente teste (memória `reference_cliente_teste_portal`), suba 4 XMLs sintéticos (sem `<entrega>`, com `<entrega>`, com transportadora TERCEIROS e um destinatário com parada ENTREGUE em outro CEP no banco local) e confira os 4 casos. Confira também que o Confirmar barra o caso 4 sem escolha. Tire screenshots com Playwright (memória `feedback_testar_painel_playwright_local`) em claro, escuro e 390 px. **Não confirme o envio de verdade**: o worker local não deve estar rodando; confira com `Get-Process`.
- [ ] **Step 3:** Rode `py -3.11 -m unittest portal_cliente.test_endereco_entrega portal_cliente.test_endereco_entrega_memoria portal_cliente.test_endereco_entrega_envio portal_cliente.test_endereco_entrega_api portal_cliente.test_endereco_entrega_worker portal_cliente.test_endereco_entrega_xml -v` e a regressão da Task 1. Esperado: tudo OK.
- [ ] **Step 4:** Atualize o `MAPA_DO_SISTEMA.txt`. Commit: `Portal: previa do envio mostra e deixa trocar o endereco de entrega (casos 1 a 4, chave endereco_entrega_ativo)`.

---

## Depois do plano (com o Hugo, fora das tasks)

1. **Revisão do ramo inteiro** antes do merge.
2. **Prova na Stokki** (spec, seção 5, item 2): com a chave ligada só pro cliente teste, mande um XML com `<entrega>` **inserido por nós**. Confira o endereço gravado na Stokki e no serviço da Vuupt, e cancele depois. Se a Stokki recusar o XML, ou gravar o fiscal ou o documento errado, pare e volte pro Hugo (plano B na spec).
3. **Deploy** (skill `deploy-vps`), só quando o Hugo pedir: reinicie `portal-cliente` e `portal-cliente-envios`. A chave fica desligada até o Hugo ligar.
