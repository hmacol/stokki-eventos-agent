# Motoristas sem app (iPhone): rota não é cancelada e baixa pela Torre

Data: 08/10/2026. Pedido do Hugo depois da baixa retroativa de 07/10
(memória `project_motoristas_iphone_sem_vuupt`).

## Problema

Iago Mendes (agent 50191), Watson Kaue (51469) e Rafael Batista Ribeiro
(50199) usam iPhone: não acessam o app da Vuupt e o nosso app de motorista
é só Android. Eles fazem a rota, mas nada é registrado: nenhum serviço é
aceito, iniciado ou concluído.

No dia seguinte, às 15h45, `roteirizacao/cancelar_rotas_sem_motorista.py`
(passo 2, `devolver_pendentes_de_rotas_passadas`) vê a rota de dia anterior
com todos os serviços "não iniciados", cancela a rota e devolve os pedidos
ao pool. Os pedidos são roteirizados de novo, aparecem como abertos e a
baixa vira trabalho manual (em 08/10: 30 pedidos de 07/10 baixados à mão).

## Objetivo

- A rota de motorista sem app **não é cancelada** pelo job das 15h45.
- No dia seguinte a Torre mostra **"rota aguardando baixa"**, e uma tela
  curta deixa o Hugo/time marcar o que voltou e concluir a rota na Vuupt:
  entregues como sucesso, os que voltaram como **insucesso com motivo**
  (entram no fluxo normal de insucesso).

Sucesso: nenhuma rota desses motoristas é cancelada pelo job; toda rota
deles de dia anterior com serviço aberto aparece na Torre até ser baixada;
a baixa de uma rota de 12 pedidos leva poucos cliques.

## Decisões do Hugo (08/10)

| Pergunta | Decisão |
|---|---|
| Onde marcar | Coluna nova `SEM_APP` (SIM/vazio) no `dados/BD_MOTORISTAS.xlsx`, mesmo padrão de `APENAS_CARGA_SECA`. |
| Como baixar | Item na Torre + tela de baixa (não fica comigo/script). |
| O que voltou | Concluir como **insucesso** na Vuupt com motivo escolhido na tela (fluxo normal de insucesso). |
| Já "done" em outra rota | Não mexe (decisão de 08/10). |

## 1. Marcação (`regras/preferencias_motoristas.py`)

- `MotoristaPreferencias` ganha `sem_app: bool` lido de `SEM_APP`
  (`_parse_bool`; vazio = NAO), como `apenas_carga_seca`.
- Função nova `agent_ids_sem_app(caminho=None) -> set[int]`: carrega o
  catálogo e devolve os `agent_id` com `sem_app`. Planilha ausente ou sem a
  coluna: conjunto vazio (comportamento de hoje).
- A planilha recebe a coluna com SIM para 50191, 51469 e 50199 (commit
  junto). A rotina mensal do BD_MOTORISTAS não mexe nessa coluna.

## 2. Job das 15h45 (`cancelar_rotas_sem_motorista.py`)

- `devolver_pendentes_de_rotas_passadas` recebe `sem_app: set[int]` e pula
  rota cujo `agent_id` está nele, antes de `plano_devolucao`.
- O resumo ganha `aguardando_baixa: ["<nome da rota> (<motorista>, dd/mm, N pedidos)"]`,
  que vai no e-mail de execução como "Rotas de motorista sem app aguardando
  baixa (não devolvidas)".
- Se ler a planilha falhar, o job segue como hoje (conjunto vazio) e loga
  aviso: preferir devolver ao pool a travar pedidos.

## 3. Torre (`painel_agentes/torre_controle.py`)

Fonte: núcleo (sem chamada à Vuupt na Torre).
`nucleo_rotas` com `agent_id ∈ sem_app`, `data_rota < hoje`,
`status NOT IN ('CONCLUIDA','CANCELADA')`, e pelo menos uma parada com
`situacao` pendente (não ENTREGUE/INSUCESSO/CANCELADA).

Um item por rota, no formato de `_montar_excecoes`:

```
{"id": f"semapp:{vuupt_route_id}",
 "severidade": "atencao", "tipo": "Sem app",
 "descricao": f"Rota de {motorista} de {dd/mm} aguardando baixa ({n} pedido(s)).",
 "quando": None,
 "acao": {"tipo": "link", "url": f"/baixa-sem-app?rota={vuupt_route_id}", "rotulo": "Dar baixa"}}
```

Rota sem `vuupt_route_id` fica de fora (não há o que baixar na Vuupt).
O item some sozinho quando a rota fecha (núcleo sincroniza a cada 15 min);
"Tratar" na Torre só esconde, como os demais.

## 4. Tela de baixa (`/baixa-sem-app`)

Nova rota no painel, níveis `total` e `operador`
(`@requer_auth(niveis=("total", "operador"))`; GET de leitura sem
`@bloqueia_planejamento_passado`, porque baixa de dia passado é o objetivo).

- **GET `/baixa-sem-app?rota=<vuupt_route_id>`:** lê a rota na Vuupt
  (`rotas_client.buscar_rota(..., include=["services"])`) e mostra
  motorista, data e, por pedido, código, destinatário, remetente e status
  atual. Serviço aberto vem marcado "Entregue"; o operador desmarca os que
  voltaram e escolhe o **motivo** de cada um numa lista de
  `insucesso_entrega/motivos_falha.MOTIVOS_FALHA` (failed_reason_id →
  texto). Serviço já `done`/cancelado aparece cinza, sem ação. Rota de
  motorista que não é `SEM_APP` → mensagem e nada a fazer (a tela não vira
  atalho para baixar rota de motorista com app).
- **POST `/api/baixa-sem-app`** (`@exige_mesma_origem`), body
  `{"rota": id, "itens": [{"service_id", "entregue": bool, "failed_reason_id"}]}`:
  valida que a rota é de motorista sem app e que todo item "não entregue"
  tem motivo; grava um lote em `baixas_sem_app` e dispara a execução em
  **thread de fundo** (não segura a requisição). Responde `{"lote": id}`.
- **GET `/api/baixa-sem-app/<lote>`:** andamento para a tela fazer polling
  (pendente / concluído / erro por item).
- Execução por item: mesma lógica de `lalamove_integracao._concluir_na_vuupt`
  (libera rota agendada; `concluir_como_agente(sucesso=..., failed_reason_id=...)`),
  extraída para uma função compartilhada em `vuupt_client.py` ou
  `nucleo/baixa_sem_app.py` para não duplicar. Serviço já fechado é pulado
  (idempotente: repetir a baixa não faz nada). 429 já é tratado pelo
  `chamar_com_retry`.
- Ao terminar: `ressincronizar_ids` do núcleo para a Torre atualizar logo,
  e evento em `tratativas` por pedido (`BAIXA_SEM_APP`, quem, entregue ou
  motivo).

Tabela `baixas_sem_app` (em `dados/dados.db`): `id, vuupt_route_id,
agent_id, criado_por, criado_em, itens_json` (com status por item),
`terminado_em`. Serve de auditoria e de andamento.

## 5. Efeitos (aceitos)

- Entregues: a expedição automática expede na Stokki sem canhoto; o
  batimento os mostra como "Entregue sem canhoto".
- Insucesso: segue o fluxo atual de insucesso (pergunta ao embarcador
  conforme a configuração vigente, reentrega se confirmar), igual a um
  insucesso registrado pelo motorista no app.
- A data de conclusão na Vuupt é a da baixa, não a do dia da rota.

## 6. Testes

- `regras/test_preferencias_motoristas.py`: `SEM_APP` lido; ausente = False;
  `agent_ids_sem_app`.
- `roteirizacao/test_cancelar_rotas_sem_motorista.py` (novo ou existente):
  rota de agent sem app não é devolvida e entra em `aguardando_baixa`;
  outras seguem iguais.
- Torre: item aparece para rota pendente de dia anterior; não aparece para
  rota de hoje, concluída, cancelada, de motorista com app ou sem
  `vuupt_route_id`.
- Baixa: POST recusa motorista com app, item não entregue sem motivo e
  nível leitura; execução chama `concluir_como_agente` com sucesso/insucesso
  certos (Vuupt mockada), pula já fechado, registra lote.
- Playwright local: Torre → tela → desmarcar 1 → confirmar → andamento.

## 7. Canhoto na tela de baixa (Hugo, 09/10)

Decisões: canhoto **opcional** (quem não tiver baixa assim mesmo); guardado
por nós e anexado na Stokki pela expedição (não pelo checklist da Vuupt).

Restrição que manda no desenho: a Stokki **não aceita anexo em pedido já
expedido**, e a expedição roda a cada 30 min logo depois da baixa. O
canhoto precisa existir no servidor **antes** de o serviço ser concluído
na Vuupt.

- **Tela:** cada pedido marcado "Entregue" ganha um campo de arquivo
  "Canhoto" (`accept="image/*,application/pdf"`, `capture` no celular).
  Desmarcar "Entregue" limpa o arquivo.
- **POST `/api/baixa-sem-app`** passa a aceitar `multipart/form-data`: o
  JSON de antes vai no campo `dados`; cada arquivo no campo
  `canhoto_<service_id>`. Limite 15 MB por arquivo; tipos JPEG, PNG, HEIC
  ou PDF. Os canhotos são **gravados antes** de criar o lote e disparar a
  thread.
- **Gravação** (`nucleo/canhotos_manuais.py`, novo):
  - imagem vira PDF de 1 página (Pillow; HEIC via `pillow-heif` se
    instalado, senão recusa HEIC com mensagem); PDF é guardado como veio;
  - arquivo em `dados/canhotos_manuais/{PS}.pdf` (substitui se reenviar);
  - cópia best-effort no GCS em `pedidos/{PS}/canhoto/manual_{data}.pdf`
    (mesmo bucket do app; falha só loga);
  - tabela `canhotos_manuais` (`codigo` PK, `service_id`, `caminho`,
    `caminho_gcs`, `enviado_por`, `enviado_em`, `origem='BAIXA_SEM_APP'`).
  - `caminho_canhoto_manual(codigo) -> Path | None` para a expedição.
- **Expedição** (`expedir_pedidos.py` da **raiz**, o que roda em
  produção): onde hoje faz
  `pdf_path = baixar_canhoto_pdf(...) if checklist_id else None`, cai para
  `caminho_canhoto_manual(codigo_ps)` quando não houver PDF da Vuupt. O
  anexo segue o caminho de hoje (`anexar_canhoto`), e o pedido não entra na
  lista "expedido sem comprovante" do e-mail interno.
- **Batimento** (`batimento/medir.py::ler_banco`): `canhotos_manuais`
  conta como canhoto, `canhoto_fonte = "painel"`, depois do app e da foto
  da Vuupt, antes de `expedicao_anexou`.
- Pedido marcado "voltou" não leva canhoto (campo some).

Testes: gravação (imagem→PDF, PDF passa, tipo inválido/grande recusado,
reenvio substitui); POST multipart grava antes do lote e recusa arquivo
inválido com 400; expedição usa o manual quando a Vuupt não tem (função
isolada testável); batimento reconhece `painel`.

Fora: os 30 pedidos baixados em 08/10 já foram (ou serão) expedidos sem
canhoto — a Stokki não aceita anexo depois; se precisar, é anexar à mão na
Stokki antes da expedição, o que já não é possível para os expedidos.

## Fora do escopo

- App de motorista para iOS.
- Editar `SEM_APP` pela tela de motoristas do painel (fica na planilha).
