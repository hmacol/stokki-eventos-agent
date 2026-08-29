# Retiradas no galpão — serviço avulso na VUUPT

Pedido do Hugo, 28/08/2026. Implementado e testado em 28/08 (ainda não deployado).

## O que muda

Até 28/08 o pipeline **ignorava** todo pedido cuja transportadora resolve pra
`RETIRADA` (cliente retira / transportadora terceira coleta no galpão —
`regras/transportadoras.py`). Agora:

1. **pipeline.py** (`processar_pedido`, ramo RETIRADA) importa o pedido na VUUPT
   como **serviço avulso**: título `[RETIRADA] #PS-x - ref / Embarcador /
   Destinatário / via Transportadora`, `code` `#PS-x`, `customer_id` = o próprio
   galpão (`retiradas.customer_id`), nota explicando, e **atribui ao agente fixo**
   (`retiradas.agent_id`) via `PUT /services/{id}/assign-agent/{agent_id}`.
   Status vira `assigned` sem rota. Ações no histórico: `retirada_criada` /
   `retirada_atualizada`.
2. Como o pool do `/planejamento`, `criar_rotas_diarias.py` e
   `incrementar_rotas.py` só olham `not_assigned`, a retirada **nunca entra em
   rota**. Na 2ª rodada do pipeline ela é pulada pela checagem antecipada de
   status (`pulado_ja_atribuido`).
3. **retiradas/acompanhar_retiradas.py** lista os serviços do agente fixo com
   título `[RETIRADA]`, consulta a situação de cada pedido na Stokki
   (`/administrator/inventory/outbound/show/{id}`, 1º `badge-status`):
   - `Enviado` → fecha como **entregue com sucesso** por API
     (`accept` → `start` → `check-in` → `check-out accomplished=true`);
   - `Cancelado` → `PUT /services/{id}/cancel`;
   - outro → aguarda. Roda a cada 30 min (08:05–17:35,
     `infra/stokki-acompanhar-retiradas.timer`) e como **Etapa 4** do
     `executar_tudo.py` (18h/22h, mesma sequência → sem login concorrente).
     Fora das sequências, recusa rodar se houver agente do painel RODANDO.
     Só manda e-mail de execução quando fechou/cancelou/errou algo.
4. **/planejamento**: 4º bloco "A retirar no galpão", só leitura (sem drag,
   seleção nem mapa), com destinatário, quem retira, status e NF; atualiza
   junto com o botão "Atualizar". Vem de `buscar_pool_e_agendados` →
   `pool_retiradas` (também no `/api/planejamento/pool`).

## Config

```yaml
retiradas:
  ativo: true
  agent_id: null        # agente "RETIRADA - TERCEIROS" — criar com retiradas/criar_agente_retirada.py --criar
  customer_id: 22265695 # FRESHLOG LOGISTICA LTDA, Rua Zilda 288 (galpão)
```

Sem `agent_id` o pipeline mantém o comportamento antigo (log "retiradas
desativadas no config") e a tela fica sem o bloco.

## Endpoints da VUUPT usados (vuupt_client.py)

Documentados: `assign-agent`, `unassign-agent`, `cancel`. **Não documentados**
(funcionam com o token de conta, validados 28/08): `accept`, `start`,
`check-in`, `check-out` — este último **só em form-urlencoded com
`accomplished=true` string**; em JSON/1 a VUUPT fecha como insucesso "Local
fechado". Ver `VuuptClient._acao_servico` / `concluir_como_agente`.

## Ativar em produção

1. Confirmar com a VUUPT se agente novo conta licença; então
   `python retiradas/criar_agente_retirada.py --criar` e colar o id em
   `retiradas.agent_id`.
2. Deploy (git pull na VPS), copiar `infra/stokki-acompanhar-retiradas.*` pra
   `/etc/systemd/system/`, `systemctl daemon-reload && systemctl enable --now
   stokki-acompanhar-retiradas.timer`.
3. Na 1ª rodada do pipeline as retiradas paradas em "Aguardando Transportador"
   (ex.: PS-36657, desde 14/08) viram serviços avulsos e ficam no bloco até a
   Stokki marcar Enviado.

## Testes feitos (28/08)

- `pipeline.py --pedido PS-36657 --modo-teste` com agente temporário: ramo
  RETIRADA monta `[RETIRADA] #PS-36657 - 7121 / MARCHEF - GOURMAR / GUMERCINDO / via CLIENTE RETIRA`.
- Ciclo real com serviços de teste (cliente "usuario teste", agente Hugo):
  criar+atribuir → `assigned`; `concluir_como_agente` de `assigned` e de
  `arrived` → `done/success`; `cancelar_servico_oficial` → `canceled`; tudo
  excluído depois.
- `status_stokki`: 36657 → "Aguardando Transportador" (aguardar), 36672 →
  "Enviado" (concluir).
