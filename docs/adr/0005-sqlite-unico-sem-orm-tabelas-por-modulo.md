# ADR-0005: Um SQLite (`dados/dados.db`), sem ORM, cada módulo dono das próprias tabelas

## Status
Aceito para o legado (`dados/dados.db`). O pacote novo `freshhub/` segue ADR-0010 (Postgres gerenciado) e ADR-0011 (ORM + Alembic).

## Data
2026-07 (migração para a VPS); blindagem em 2026-09-15

## Contexto
O sistema nasceu como scripts agendados numa máquina Windows e foi para a VPS em julho. Um único arquivo `dados/dados.db` (SQLite, 90+ tabelas) é compartilhado por rotinas em lote (timers systemd) e por cinco serviços web (waitress). Não há ORM nem migrações centralizadas: cada módulo faz `CREATE TABLE IF NOT EXISTS` das próprias tabelas na subida.

Dois incidentes moldaram a prática: rodar script como `root` cria WAL do SQLite com dono errado e trava o serviço; transação longa (WMS do galpão) trava o banco para os outros.

## Decisão
- Banco único `dados/dados.db`, acesso direto por `sqlite3`, sem ORM.
- **Dono da tabela = módulo que faz o `CREATE TABLE IF NOT EXISTS`.** Para achar o dono: `grep "CREATE TABLE IF NOT EXISTS <nome>"`. Mudança de esquema é idempotente (`ALTER TABLE` guardado por `PRAGMA table_info`).
- Scripts na VPS rodam sempre como `www-data`.
- Backup: diário 03h (`VACUUM INTO` → GCS, 30 dias) + horário (:22, 48 h, com fotos) + ensaio semanal de restauração (dom 05:30, medido 1,8 s até o banco utilizável). Alerta por e-mail quando job crítico falha.
- Bancos separados só para componentes com deploy separado: `dados/atendimento.db` (central WhatsApp) e `confirmacoes.db` (confirmação de rota).

## Alternativas consideradas

### Postgres
- Prós: concorrência real, migrações, backup contínuo nativo.
- Contras: mais um serviço na VPS; 45+ módulos a adaptar.
- **Adiada**, não rejeitada: o Hugo decide antes do piloto real do app dos motoristas (decisão 3 do `DOC_EXECUCAO_CLAUDE_SAIDA_VUUPT.md`).

### ORM + migrações (SQLAlchemy/Alembic)
- Rejeitada: o código já tem dezenas de consultas SQL diretas; o ganho não paga a reescrita enquanto o banco for SQLite.

### Um banco por serviço
- Rejeitada: as telas cruzam dados de todos os módulos (Torre, vigia, consulta); dividir criaria sincronizações internas.

## Consequências
- Transação longa trava todo mundo: ler, soltar a conexão, só então chamar rede. O WMS do galpão já quebrou isso uma vez.
- Testes criam SQLite em memória ou arquivo temporário e chamam o `criar_tabelas` do módulo; não há fixture central.
- O banco local do desenvolvedor é cópia congelada (17/08); dado de produção só na VPS ou em cópia baixada do GCS.
- Quando o Postgres vier, o primeiro passo é um ponto único de conexão (hoje não existe; ver varredura de arquitetura de 08/10).
