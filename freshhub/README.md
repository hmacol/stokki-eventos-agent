# freshhub — sistema proprio

Plano mestre: `docs/superpowers/specs/2026-10-17-freshhub-sistema-proprio-design.md`.
Plano desta fase: `docs/superpowers/plans/2026-10-17-fase0-1-esqueleto-freshhub.md`.

Camadas: `dominio` (regras puras) -> `servicos` (casos de uso) -> `api` / `jobs`.
`modelos` = tabelas e sessao; `migracoes` = Alembic; `adaptadores` = integracoes
atras de interfaces. O contrato de camadas e verificado pelo import-linter.

Configuracao so por variaveis `FH_*` (nunca pelo arquivo de segredos do legado).

## Rodar local

    py -3.11 -m pip install -e ".[dev]"
    cp freshhub-dev.env.exemplo freshhub-dev.env     # e preencha
    bash infra/postgres-dev/tunel.sh                  # tunel SSH pro Postgres de dev na VPS
    FH_ENV_ARQUIVO=freshhub-dev.env py -3.11 -m alembic upgrade head
    FH_ENV_ARQUIVO=freshhub-dev.env py -3.11 -m freshhub   # API em http://127.0.0.1:8100

## Verificar (o CI roda o mesmo)

    py -3.11 -m ruff check freshhub tests
    py -3.11 -m mypy
    lint-imports        # script do import-linter; no Windows fica em $(py -3.11 -c "import sysconfig;print(sysconfig.get_path('scripts'))")
    FH_TEST_DATABASE_URL=postgresql+psycopg://freshhub:...@127.0.0.1:5433/freshhub_test py -3.11 -m pytest
