# ADR-0011: ORM (SQLAlchemy 2) e migrações versionadas (Alembic) no pacote novo

## Status
Aceito

## Data
2026-10-17

## Contexto
O ADR-0005 rejeitou ORM e migrações "enquanto o banco for SQLite", porque o legado tem dezenas de consultas SQL diretas e cada módulo faz `CREATE TABLE IF NOT EXISTS` na subida. Com o Postgres (ADR-0010) e um modelo de dados novo (spec 2.2), o cálculo muda: não há SQL legado a reescrever no pacote novo, e esquema sem versionamento não passa pela homologação exigida pela spec (5.1).

## Decisão
- Tabelas do pacote `freshhub/` são declaradas em SQLAlchemy 2 (`freshhub/modelos/`), com convenção de nomes de constraint (`pk_`, `fk_`, `uq_`, `ix_`, `ck_`) fixada em `Base.metadata`.
- Toda mudança de esquema é uma revisão Alembic em `freshhub/migracoes/versoes/`, numerada `NNNN_descricao.py`, escrita ou revisada à mão (autogenerate é rascunho, nunca commit direto). **Nenhum `CREATE TABLE IF NOT EXISTS` no pacote novo.**
- Migração sobe em homologação antes da produção, pela Action de deploy (plano 0.3). `downgrade` existe pra dev e teste; em produção corrige-se pra frente.
- A tabela `alembic_version` fica no schema `public`.
- A URL do banco vem só de `FH_DATABASE_URL` (nunca do `alembic.ini`).

## Alternativas consideradas
- **SQL direto como no legado**: rejeitada. Sem versionamento não há como provar que hml e prod têm o mesmo esquema.
- **Migrações em SQL puro (sem Alembic)**: consideradas; rejeitadas porque o Alembic dá a ordem, o estado atual e o autogenerate como rascunho de graça.
- **ORM mais leve (SQLModel, Peewee)**: rejeitadas. SQLAlchemy 2 é o padrão do FastAPI, tem tipos (`Mapped[]`) e o Alembic é dele.

## Consequências
- O ADR-0005 passa a valer **só para o legado**; status atualizado lá.
- Regras de domínio não importam `modelos` (contrato do import-linter): a lógica não depende do ORM.
- Consulta pesada de relatório pode usar SQL textual via SQLAlchemy quando o ORM atrapalhar; o que não muda é que o esquema vem da migração.
