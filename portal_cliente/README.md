# Portal do cliente — acompanhamento de entregas

Tela em que o embarcador acompanha os pedidos dele no dia (pedido do Hugo,
08/09/2026). App Flask separado do painel interno, publicado em
`https://app.freshhub.com.br/cliente`.

Desenho aprovado no canvas: https://claude.ai/code/artifact/1932a3f8-bc08-4184-97ef-65812b445147
(página 1 = tela unificada claro/escuro; página 2 = opções A/B/C).

## O que a tela mostra

- **Precisa da sua atenção** — insucessos cujo motivo pede decisão do
  embarcador (`insucessos_aguardando_resposta` com status PENDENTE),
  agrupados por motivo, com os botões *Reentregar na próxima rota* /
  *Escolher outra data* / *Não reenviar*. A resposta passa por
  `insucesso_entrega/aplicar_resposta_insucesso.aplicar_decisao`, o MESMO
  caminho da página `/insucesso` (por grupo `sender_id + failed_reason_id`).
- **Tiles** (pedidos no dia, entregues, em rota, aguardando saída, insucesso),
  **abas** (todos / em rota / entregues / pendentes / insucesso / agendados),
  busca e **tabela** com seletor de colunas (visíveis, ordem e disponíveis,
  salvo no navegador em `localStorage`).
- **Lateral**: progresso do dia, aguardando saída, próximos dias
  (agendados pra frente) e resumo dos últimos 30 dias.
- Comprovante (canhoto) em PDF por pedido entregue, planilha do dia (xlsx),
  navegação por data, tema claro/escuro, atualização a cada 5 min.

## Fontes de dados (`dados_cliente.py`)

| O quê | De onde |
|---|---|
| Pedidos em rota / entregues / insucesso do dia | VUUPT `/routes` por `start_at` do dia, `include=services,services.customer,agent`, filtrando `sender_id` |
| Aguardando saída / agendados | VUUPT `/services` `status=not_assigned` + `sender_id` (mesmos critérios de `torre_controle._coletar_backlog`) |
| Motorista / placa | `regras.preferencias_motoristas.CatalogoMotoristas` (fallback: `agent.name` da rota) |
| Número da NF | `documentos_processados` por código base (mesma query de `rascunhos_rota`) |
| Insucessos aguardando resposta | `insucessos_aguardando_resposta` |
| Últimos 30 dias | VUUPT `/services` `status=done` + `sender_id` + `completed_at >= hoje-30` (cache 1 h) |
| Canhoto | `GET /services/{id}?include=checklistAnswers` → `GET /checklists/{id}/print` (cache em `dados/canhotos/`) |

Cache do dia: 5 min por `(sender_id, data)`; o botão *Atualizar* força.

## Acesso (`auth_cliente.py`)

- CNPJ + PIN de 6 dígitos (PBKDF2-HMAC-SHA256 200k, salt por conta, 5 erros →
  15 min de bloqueio). Tabela `clientes_portal` em `dados/dados.db`.
- Só pode ter conta quem está em `interno` com `sender_id` — é o `sender_id`
  que liga o embarcador aos pedidos na VUUPT. E-mail vem de `interno.email`.
- Primeiro acesso / esqueci o PIN: link assinado (24 h, uso único) enviado
  pelo `email_utils.enviar_email` ao(s) e-mail(s) de `interno`.
- Sessão: cookie assinado do Flask, 14 dias, invalidado ao trocar o PIN.

### Gestão pela linha de comando

```
py -3 portal_cliente/gerenciar_clientes.py listar
py -3 portal_cliente/gerenciar_clientes.py enviar-link <cnpj>     # manda o e-mail de primeiro acesso
py -3 portal_cliente/gerenciar_clientes.py link <cnpj>            # só imprime o link
py -3 portal_cliente/gerenciar_clientes.py definir-pin <cnpj> <pin>
py -3 portal_cliente/gerenciar_clientes.py desativar|ativar <cnpj>
```

## Rodar local

```
PORTAL_CLIENTE_DEV=1 py -3 portal_cliente/app.py     # http://127.0.0.1:8074 (cookie sem Secure)
```

Precisa da seção `portal_cliente:` no `config.yaml` (`secret_key`, `porta`,
`url_base`).

## Deploy na VPS (mesmo roteiro do /insucesso e /motorista)

1. `cd /opt/stokki-eventos && git pull`
2. Colar a seção `portal_cliente:` no `/opt/stokki-eventos/config.yaml`
   (MESMO `secret_key` do local, senão os links de PIN gerados de um lado
   não valem do outro).
3. `cp portal_cliente/infra/portal-cliente.service /etc/systemd/system/ && systemctl daemon-reload && systemctl enable --now portal-cliente`
4. Colar o bloco de `portal_cliente/infra/Caddyfile-cliente` dentro do site
   `app.freshhub.com.br` em `/etc/caddy/Caddyfile` e `systemctl reload caddy`.
5. `curl -s https://app.freshhub.com.br/cliente/saude` → `{"ok": true}`.
6. Liberar o primeiro embarcador: `venv/bin/python portal_cliente/gerenciar_clientes.py enviar-link <cnpj>`.

## Máscara de envio de pedidos (XML → fila → Stokki) — 08/09

Segunda vista da mesma tela (`Enviar pedidos`, `?aba=envios`): o embarcador
(ou a equipe Fresh Log em nome dele, via `/equipe` com usuário do painel)
sobe os XMLs das NF-e; a gente valida, guarda o XML original e enfileira a
criação do pedido na Stokki. É a primeira camada do "sistema próprio por
cima da infra da Stokki" (decisões do Hugo em 08/09, ver memória
`project_mascara_envio_pedidos_stokki`).

| Peça | Arquivo |
|---|---|
| Tela (vista de envio, prévia, ações) | `templates/_envios.html` (incluído por `acompanhamento.html`) |
| Rotas `/api/envios/*`, `/api/destinatarios`, `/equipe` | `app.py` |
| Dados: tabelas `portal_envios`, `portal_destinatarios`, `portal_solicitacoes`, `portal_clientes_envio`; leitura/validação do XML | `envio_pedidos.py` |
| Worker da fila (cria na Stokki pelo wizard do importador) | `enviar_stokki.py` (`--loop` no serviço `infra/portal-cliente-envios.service`) |
| Trava "quem está usando a Stokki" | `../stokki/sessao_uso.py` (tabela `stokki_sessao_uso`) |

Fluxo: `analisar` (lê NF-e, valida: é NF-e de saída, emitente = CNPJ logado,
chave não enviada antes; guarda temporário 2 h) → cliente informa, uma vez
por destinatário, o horário de recebimento (vai também pra
`ajustes_complexidade_cliente`, que o Planejamento usa) e, se o
destinatário só recebe com agendamento, a data (ou "pendente") →
`confirmar` grava o XML em `dados/portal_envios/<cnpj>/<chave>.xml` e a
linha `NA_FILA` → worker: transforma o XML pela regra do cliente
(`emporio_quatro_estrelas` consolida itens, `muai` kg→caixas, `nenhuma`),
espera a Stokki desocupar (trava), roda o wizard, marca `CRIADO` /
`DUPLICADO` / `ERRO` (erro = tela + e-mail pro cliente com cópia pra
`email.email_atendimento`), tenta descobrir o `PS-xxxxx` (listagem da
Stokki por NF; depois `documentos_processados`/`pedidos_historico`) e, com o
código, grava o agendamento em `agendamentos_pedido` (status RESPONDIDO)
pra `atualizar_agendamentos_confirmados.py` aplicar no VUUPT.

Ações do cliente: **Cancelar** (na fila = na hora; já na Stokki = pendência
pra operação), **Tirar da rota** (pendência), **Reagendar** (na hora quando
já há código; senão pendência), **Reenviar** (volta pra fila). Pendências
saem por e-mail pro atendimento e se fecham pela CLI (`solicitacoes` /
`concluir`).

Config (`portal_cliente:`): `caminho_importador`, `espera_stokki_minutos`,
`lote_maximo`, `stokki_padrao` (warehouse/transporte/embalagem/url) e
`stokki.usuario/senha` próprios (vazio = `stokki.*`). `client_id` da Stokki
vem de `interno.stkkc_id`. O importador por e-mail (`agente_importacao_stokki/main.py`)
também respeita a trava (importa `stokki.sessao_uso` do repo principal).

Deploy adicional: `cp portal_cliente/infra/portal-cliente-envios.service /etc/systemd/system/ && systemctl daemon-reload && systemctl enable --now portal-cliente-envios`
(precisa do repo `agente_importacao_stokki` em `/opt/agente-importacao-stokki` e do Chromium do Playwright no venv).
Rodar `enviar_stokki.py --uma-vez --simular` NÃO toca a Stokki (marca como criado) — só pra teste.

## Limites conhecidos (v1)

- Máscara de envio: o `PS-xxxxx` do pedido recém-criado depende da busca na
  listagem da Stokki por NF (best-effort) ou da conciliação posterior; até
  lá o reagendamento vira pendência pra operação. Cancelar/tirar da rota de
  pedido já criado ainda é manual (pendência + e-mail). Ainda não testado
  contra a Stokki real (só em `--simular`).

- Não há previsão de horário de chegada — a tela mostra "parada N de M" da
  rota e o horário de saída/entrega reais.
- "Pedidos no dia" soma o que está nas rotas do dia com o que ainda aguarda
  roteirização (pool sem data); pedido com agendamento futuro fica na aba
  *Agendados* e em *Próximos dias*.
- Agendamento pendente de confirmação pelo destinatário (fluxo
  `agendamentos_pedido`) ainda não aparece no bloco de atenção.
- Sem tela interna de gestão de contas — usar a CLI acima.
