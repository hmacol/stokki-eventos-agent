# ADR-0010: Postgres gerenciado como banco único do sistema novo

## Status
Aceito

## Data
2026-10-17

## Contexto
O ADR-0005 adiou o Postgres "até o piloto real do app". Em 17/10/2026 o Hugo decidiu construir um sistema 100% próprio (plano mestre em `docs/superpowers/specs/2026-10-17-freshhub-sistema-proprio-design.md`), com robustez acima de velocidade, time de uma pessoa + Claude, e aplicação na VPS com banco gerenciado.

## Decisão
- O pacote `freshhub/` usa **só Postgres** (`postgresql+psycopg://`); a configuração recusa qualquer outra URL.
- Um banco por ambiente (`prod`, `hml`), com **schemas por módulo de domínio**: `identidade`, `cadastros`, `pedidos`, `armazem`, `transporte`, `financeiro`, `comunicacao`, `legado`.
- Provedor recomendado: Google Cloud SQL em `southamerica-east1` (billing, bucket e Maps já estão no Google; PITR nativo). Supabase em `sa-east-1` como reserva. Critério que decide, independente do provedor: restaurar pra um instante qualquer dos últimos 7 dias em menos de 30 min, ensaiado.
- Desenvolvimento e testes usam Postgres 16 em Docker na VPS (`infra/postgres-dev/`), por túnel SSH; nunca com dado de produção.
- Histórico é append-only e toda data é `timestamptz` em UTC (spec 2.3).
- Toda conexão nasce com `connect_timeout` curto: banco fora não pode prender API nem job.

## Alternativas consideradas
- **Continuar no SQLite**: rejeitada. Transação longa trava todo mundo (incidente do WMS), sem PITR, sem usuário por papel, e o sistema novo terá API, jobs e três áreas de tela escrevendo ao mesmo tempo.
- **Postgres auto-hospedado na VPS**: rejeitada pelo Hugo. Backup, upgrade e disco por conta de uma pessoa só.
- **Nuvem completa (containers + banco + storage)**: rejeitada por ora. Mais peças novas do que o time comporta; a VPS com systemd é conhecida e provada.

## Consequências
- O `dados/dados.db` continua sendo o banco do legado até a Fase 4; a convivência é por sincronizador (spec 2.4).
- Custo recorrente novo (R$ 150 a 300/mês) com alerta de orçamento.
- Snapshot diário exportado pro GCS além do PITR do provedor; ensaio de restauração mensal completo.
