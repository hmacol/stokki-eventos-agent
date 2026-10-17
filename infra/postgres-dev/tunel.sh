#!/usr/bin/env bash
# Tunel SSH: 127.0.0.1:5433 local -> 127.0.0.1:5432 da VPS (Postgres de dev).
# Uso: bash infra/postgres-dev/tunel.sh   (fica em primeiro plano; Ctrl+C encerra)
set -euo pipefail
exec ssh -i ~/.ssh/atendimento_vps -N -L 5433:127.0.0.1:5432 root@187.127.52.197
