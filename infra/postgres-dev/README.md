# Postgres de desenvolvimento (VPS, Docker)

Primeira subida (como root na VPS, uma vez):

    mkdir -p /etc/freshhub && chmod 700 /etc/freshhub
    printf 'POSTGRES_PASSWORD=%s\n' "$(openssl rand -base64 24)" > /etc/freshhub/postgres-dev.env
    chmod 600 /etc/freshhub/postgres-dev.env
    cd /opt/stokki-eventos/infra/postgres-dev && docker compose up -d
    docker compose ps   # healthy

A senha fica so em /etc/freshhub/postgres-dev.env (ler na VPS quando
precisar montar a URL local; nunca colar no chat). URLs:

    dev:   postgresql+psycopg://freshhub:<senha>@127.0.0.1:5433/freshhub_dev
    teste: postgresql+psycopg://freshhub:<senha>@127.0.0.1:5433/freshhub_test

Local: `bash infra/postgres-dev/tunel.sh` num terminal e as URLs acima em
`freshhub-dev.env` (FH_DATABASE_URL) e na variavel FH_TEST_DATABASE_URL.
Zerar o banco de teste: `alembic downgrade base` + `upgrade head` (o
conftest faz isso sozinho).

Enquanto o ramo freshhub-fase0 nao estiver em master, a pasta foi copiada
pra /opt/freshhub-dev na VPS; o volume nomeado e o mesmo quando o compose
passar a rodar de /opt/stokki-eventos/infra/postgres-dev.
