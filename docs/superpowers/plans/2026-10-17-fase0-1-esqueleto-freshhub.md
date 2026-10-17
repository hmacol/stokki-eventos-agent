# Fase 0.1 · Esqueleto do pacote `freshhub` — Plano de execução

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deixar no repositório o esqueleto do sistema novo: pacote `freshhub/` com configuração por ambiente, Postgres de desenvolvimento, migrações Alembic, API com rota de saúde, log estruturado, contrato de camadas, lint, tipos, testes e CI — tudo verde — sem tocar no legado além de renomear a pasta do Lovable.

**Architecture:** O pacote novo nasce ao lado do legado (ADR a criar: ORM + Alembic; Postgres gerenciado). Camadas `dominio → servicos → api/jobs`, adaptadores atrás de interfaces, um dono por tabela em schemas por módulo. Nesta etapa só existem a fundação e a primeira migração (os 8 schemas vazios); modelos de verdade vêm nos planos 0.2 (identidade) e 0.4 (cadastros e pedidos).

**Tech Stack:** Python ≥ 3.11 (local 3.11.7, VPS 3.12.3), FastAPI, uvicorn, SQLAlchemy 2, Alembic, psycopg 3, pydantic-settings, pytest, httpx, ruff, mypy, import-linter, pip-audit. Postgres 16 em container Docker na VPS para desenvolvimento e testes (acessado por túnel SSH); `postgres:16` como service container no GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-10-17-freshhub-sistema-proprio-design.md` (seções 1, 2.5, 5.2, 5.4, 5.5, 5.7, 7.5).

## Planos da Fase 0 (este é o primeiro)

| Plano | Entrega | Depende de |
|---|---|---|
| **0.1 Esqueleto** (este) | pacote, config, Postgres dev, Alembic, `/api/saude`, log JSON, camadas, lint/tipos/testes, CI, ADRs 0010 e 0011 | — |
| 0.2 Identidade e acesso | usuário, papel, senha/PIN, sessão, CSRF, limite de taxa, auditoria, testes de fronteira | 0.1 |
| 0.3 Infra gerenciada | Cloud SQL prod e hml, `hml.freshhub.com.br`, deploy por Action com prova de fumaça e volta, observabilidade, bucket, secrets, ensaio de restauração | 0.1 |
| 0.4 Modelos e carga inicial | Cadastros e Pedidos, carga do `dados.db` e do arquivo da Vuupt, relatório de divergências, sincronizador SQLite → Postgres | 0.2, 0.3 |
| 0.5 Front | React + Vite + Tailwind/shadcn, shell das 3 áreas, login, tema, cliente gerado do OpenAPI, ADR 0012 | 0.2 |
| 0.6 Fatia vertical | Consulta de pedido e Torre leitura no sistema novo, comparador diário, portão da Fase 0 | 0.4, 0.5 |

## Global Constraints

- Idioma: código, comentários, commits e documentação em **português**; strings de log **sem acento** (CLAUDE.md).
- Python: local sempre `py -3.11`; VPS sempre `/opt/stokki-eventos/venv/bin/python`. `requires-python = ">=3.11"`.
- Nunca ler, copiar, sobrescrever ou colar `config.yaml` nem `sessao_*.json`. O pacote novo **não lê `config.yaml`**: configuração só por variáveis de ambiente (spec 5.7).
- Nenhuma tabela nova no `dados/dados.db`. Nenhum `CREATE TABLE IF NOT EXISTS` no pacote novo (spec 2.5).
- Segredos nunca no git: o arquivo de ambiente de desenvolvimento chama `freshhub-dev.env` (coberto por `*.env` no `.gitignore`).
- Trabalho em **worktree** próprio a partir de `origin/master` (hook bloqueia `checkout`, `stash`, `rebase`, `reset`). Commits locais no ramo `freshhub-fase0` são o checkpoint de cada tarefa; **push e merge em `master` só quando o Hugo pedir**.
- Mensagem de commit: `Área: o que mudou e por quê`, terminando com `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- Só `git add` dos arquivos da tarefa, nunca `git add -A`.
- Legado continua rodando: nenhuma tarefa deste plano muda comportamento de produção além do caminho do arquivo de sessão do Lovable (Task 2, passo de deploy).
- Na VPS, comandos de aplicação como `www-data`; `docker` como root é aceitável (container, não cria arquivo no repo).

## Review Focus

1. `FH_DATABASE_URL` ausente ou malformada ao subir a API → mensagem clara em português dizendo qual variável falta, sem traceback de SQLAlchemy. (Task 4)
2. `/api/saude` com o banco fora do ar → HTTP 503 com `{"banco": "falha"}` e **sem** a URL ou a senha do banco no corpo nem no log. (Task 7)
3. `alembic upgrade head` rodado duas vezes seguidas → segunda vez sem erro e sem mudança (idempotente). (Task 6)
4. Texto com acento ou caractere fora do ASCII dentro de um log → linha JSON válida, não `UnicodeEncodeError` (o legado já quebrou assim em processo sem console). (Task 8)
5. Depois da renomeação, `lovable.auth.SESSAO_PATH` aponta pra `lovable/sessao_freshhub.json`; na VPS o arquivo precisa ser movido no deploy, senão o Lovable refaz login Playwright a cada chamada. (Task 2)

---

### Task 1: Worktree e ramo de trabalho

**Files:**
- Nenhum arquivo do repo; cria o diretório `C:\agente_stokki_eventos-fase0`.

**Interfaces:**
- Produces: worktree em `C:\agente_stokki_eventos-fase0`, ramo `freshhub-fase0` a partir de `origin/master`. Todas as tarefas seguintes rodam **de dentro dele**.

- [ ] **Step 1: Conferir o estado do repo principal**

Run (Git Bash):
```bash
cd /c/agente_stokki_eventos && git fetch -q origin && git log --oneline -1 origin/master && git worktree list
```
Expected: mostra o SHA atual de `origin/master`; nenhum worktree chamado `fase0`.

- [ ] **Step 2: Criar o worktree**

```bash
cd /c/agente_stokki_eventos && git worktree add -b freshhub-fase0 /c/agente_stokki_eventos-fase0 origin/master
```
Expected: `Preparing worktree (new branch 'freshhub-fase0')`.

- [ ] **Step 3: Verificar**

```bash
cd /c/agente_stokki_eventos-fase0 && git status --short && git log --oneline -1
```
Expected: status vazio; SHA igual ao de `origin/master`.

---

### Task 2: Renomear `freshhub/` (Lovable) para `lovable/`

**Files:**
- Rename: `freshhub/` → `lovable/` (`__init__.py`, `auth.py`, `pedidos_parados.py`, `tasks.py`)
- Modify (imports `from freshhub.` → `from lovable.`): `atendimento/alertas.py`, `atendimento/app.py`, `atendimento/monitorar_saude_evolution.py`, `batimento/bater.py`, `embarcadores_stokki.py`, `executar_tudo.py`, `expedir_pedidos.py`, `gerar_informativo_mensal.py`, `insucesso_entrega/notificar_insucesso_aguardando_resposta.py`, `integracao_chatwoot.py`, `investigacao/testar_freshhub_pedidos_parados.py`, `marcar_clientes_agenda.py`, `notificacao_entregas/notificar_entrega_concluida.py`, `painel_agentes/painel_agentes.py`, `portal_cliente/app.py`, `portal_cliente/chamados.py`, `portal_cliente/enviar_stokki.py`, e os testes `notificacao_entregas/test_montar_email_entrega.py`, `notificacao_entregas/test_notificar_entrega_concluida.py`, `painel_agentes/test_wms_faltas_recebimento.py`, `portal_cliente/test_chamados_whatsapp.py`, `portal_cliente/test_link_canhoto.py`, `test_avisar_cliente_sem_resposta.py`, `test_embarcadores_stokki.py`, `test_marcar_clientes_agenda.py`, `test_notificar_whatsapp.py` (a lista exata vem do grep do passo 1; vale ela, não esta).
- Modify: `MAPA_DO_SISTEMA.txt` (20 ocorrências de `freshhub/`), `.gitignore` (linha `**/sessao_freshhub.json` já cobre o caminho novo; conferir).

**Interfaces:**
- Produces: pacote `lovable` com os mesmos nomes públicos (`lovable.auth.FreshHubSession`, `lovable.auth.SUPABASE_URL`, `lovable.pedidos_parados.listar_pedidos_parados`, `lovable.pedidos_parados.normalizar_order_number`, `lovable.tasks.criar_demanda_para_tratativa`). O nome `freshhub` fica livre para a Task 3.

- [ ] **Step 1: Listar quem importa (fonte da verdade da tarefa)**

```bash
cd /c/agente_stokki_eventos-fase0 && grep -rln "from freshhub\b\|import freshhub\b\|freshhub\.\(auth\|tasks\|pedidos_parados\)" --include=*.py . | grep -v "^./freshhub/" | sort
```
Expected: ~27 arquivos (18 módulos + 9 testes). Guardar a lista.

- [ ] **Step 2: Escrever o teste de caracterização (antes de renomear)**

Criar `tests/legado/test_lovable_rename.py`:
```python
# -*- coding: utf-8 -*-
"""Garante que o pacote do Lovable continua importavel com os mesmos
nomes publicos depois da renomeacao freshhub/ -> lovable/ (Fase 0.1)."""
import importlib
import subprocess
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]


def test_nomes_publicos_do_lovable():
    sys.path.insert(0, str(RAIZ))
    auth = importlib.import_module("lovable.auth")
    parados = importlib.import_module("lovable.pedidos_parados")
    tasks = importlib.import_module("lovable.tasks")
    assert hasattr(auth, "FreshHubSession")
    assert hasattr(auth, "SUPABASE_URL")
    assert auth.SESSAO_PATH == RAIZ / "lovable" / "sessao_freshhub.json"
    assert hasattr(parados, "listar_pedidos_parados")
    assert hasattr(parados, "normalizar_order_number")
    assert hasattr(tasks, "criar_demanda_para_tratativa")


def test_ninguem_mais_importa_freshhub_do_lovable():
    saida = subprocess.run(
        ["git", "grep", "-l", "-E", r"from freshhub\.(auth|tasks|pedidos_parados)|import freshhub\.(auth|tasks|pedidos_parados)", "--", "*.py"],
        cwd=RAIZ, capture_output=True, text=True,
    )
    assert saida.stdout.strip() == "", f"ainda importam o Lovable pelo nome antigo:\n{saida.stdout}"
```
Criar também `tests/__init__.py` e `tests/legado/__init__.py` vazios.

- [ ] **Step 3: Rodar o teste e ver falhar**

```bash
cd /c/agente_stokki_eventos-fase0 && py -3.11 -m pytest tests/legado/test_lovable_rename.py -q
```
Expected: 2 FAILED (`ModuleNotFoundError: No module named 'lovable'` e lista de arquivos no segundo).

- [ ] **Step 4: Renomear com `git mv` e trocar os imports**

```bash
cd /c/agente_stokki_eventos-fase0 && git mv freshhub lovable && rm -rf lovable/__pycache__
grep -rl "from freshhub\b\|import freshhub\b\|freshhub\.\(auth\|tasks\|pedidos_parados\)" --include=*.py . | grep -v "^./lovable/" | xargs sed -i -E 's/\bfrom freshhub\./from lovable./g; s/\bimport freshhub\./import lovable./g; s/\bimport freshhub\b/import lovable/g; s/\bfreshhub\.(auth|tasks|pedidos_parados)/lovable.\1/g'
```
Depois, à mão, trocar comentários e docstrings que digam "pasta freshhub/" nos arquivos tocados (grep `freshhub/` neles) para "pasta lovable/", sem mexer em texto que fale do domínio `freshhub.com.br`.

- [ ] **Step 5: Atualizar o MAPA**

Em `MAPA_DO_SISTEMA.txt`: trocar cada menção à pasta `freshhub/` (seção 0 "pasta freshhub/", seção 1, seção 14) por `lovable/`, e acrescentar na seção 14:
```
 lovable/                  Scraping do Fresh Hub do Lovable (Supabase): auth
                           (Playwright CPF+PIN -> token), pedidos parados,
                           tasks. Era freshhub/ ate 17/10/2026; o nome
                           freshhub/ passou a ser o pacote do sistema novo.
                           Sai de uso na Fase 1 do plano mestre.
```

- [ ] **Step 6: Rodar o teste novo e os testes legados afetados**

```bash
cd /c/agente_stokki_eventos-fase0 && py -3.11 -m pytest tests/legado/test_lovable_rename.py -q
py -3.11 -m unittest test_embarcadores_stokki test_marcar_clientes_agenda test_notificar_whatsapp test_avisar_cliente_sem_resposta 2>&1 | tail -3
(cd portal_cliente && py -3.11 -m unittest test_chamados_whatsapp test_link_canhoto 2>&1 | tail -3)
(cd notificacao_entregas && py -3.11 -m unittest test_montar_email_entrega test_notificar_entrega_concluida 2>&1 | tail -3)
(cd painel_agentes && py -3.11 -m unittest test_wms_faltas_recebimento 2>&1 | tail -3)
py -3.11 -m py_compile $(git diff --name-only --diff-filter=AMR | grep '\.py$')
```
Expected: pytest 2 passed; cada unittest `OK` (ou as mesmas falhas pré-existentes do `master` limpo — anotar quais, comparando com o checkout principal antes de concluir); `py_compile` sem saída.

- [ ] **Step 7: Registrar o passo de deploy**

Acrescentar em `MAPA_DO_SISTEMA.txt`, seção 17 (armadilhas):
```
 - Renomeacao freshhub/ -> lovable/ (17/10/2026): no deploy, mover na VPS o
   arquivo gitignored `freshhub/sessao_freshhub.json` para
   `lovable/sessao_freshhub.json` (como www-data) ANTES de reiniciar
   painel-agentes e portal-cliente; sem isso o Lovable refaz login Playwright
   a cada chamada.
```

- [ ] **Step 8: Commit**

```bash
cd /c/agente_stokki_eventos-fase0 && git add -A lovable tests/__init__.py tests/legado MAPA_DO_SISTEMA.txt $(git diff --name-only | grep '\.py$') && git status --short && git commit -q -F - <<'EOF'
Lovable: pasta freshhub/ vira lovable/ pra liberar o nome do pacote novo

O sistema proprio (plano mestre de 17/10) nasce no pacote freshhub/.
A pasta com o scraping do Fresh Hub do Lovable passa a se chamar
lovable/, com os mesmos nomes publicos; 18 modulos e 9 testes trocam
o import. Teste de caracterizacao garante que ninguem importa o Lovable
pelo nome antigo. Deploy: mover sessao_freshhub.json na VPS (MAPA 17).

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
EOF
```
(`git add -A lovable` é restrito ao diretório renomeado, não ao repo.)

---

### Task 3: Pacote `freshhub`, `pyproject.toml` e configuração por ambiente

**Files:**
- Create: `pyproject.toml`, `freshhub/__init__.py`, `freshhub/config.py`, `freshhub/dominio/__init__.py`, `freshhub/servicos/__init__.py`, `freshhub/adaptadores/__init__.py`, `freshhub/modelos/__init__.py`, `freshhub/api/__init__.py`, `freshhub/jobs/__init__.py`, `freshhub/README.md`, `freshhub-dev.env.exemplo`
- Test: `tests/freshhub/__init__.py`, `tests/freshhub/test_config.py`

**Interfaces:**
- Produces: `freshhub.config.Configuracao` (pydantic-settings) com campos `ambiente: Literal["dev","teste","hml","prod"]`, `database_url: str`, `log_nivel: str = "INFO"`; `freshhub.config.carregar_config() -> Configuracao` (lê variáveis `FH_*` e, se existir, o arquivo apontado por `FH_ENV_ARQUIVO`); `freshhub.config.ConfiguracaoInvalida(RuntimeError)`; `freshhub.__version__ = "0.1.0"`.

- [ ] **Step 1: Escrever o teste que falha**

`tests/freshhub/test_config.py`:
```python
# -*- coding: utf-8 -*-
import pytest

from freshhub.config import Configuracao, ConfiguracaoInvalida, carregar_config


def test_le_variaveis_fh(monkeypatch):
    monkeypatch.setenv("FH_AMBIENTE", "teste")
    monkeypatch.setenv("FH_DATABASE_URL", "postgresql+psycopg://u:s@127.0.0.1:5433/x")
    cfg = carregar_config()
    assert isinstance(cfg, Configuracao)
    assert cfg.ambiente == "teste"
    assert cfg.database_url.startswith("postgresql+psycopg://")
    assert cfg.log_nivel == "INFO"


def test_database_url_ausente_da_mensagem_clara(monkeypatch):
    monkeypatch.delenv("FH_DATABASE_URL", raising=False)
    monkeypatch.setenv("FH_AMBIENTE", "teste")
    with pytest.raises(ConfiguracaoInvalida) as erro:
        carregar_config()
    assert "FH_DATABASE_URL" in str(erro.value)


def test_database_url_que_nao_e_postgres_e_recusada(monkeypatch):
    monkeypatch.setenv("FH_AMBIENTE", "teste")
    monkeypatch.setenv("FH_DATABASE_URL", "sqlite:///dados.db")
    with pytest.raises(ConfiguracaoInvalida) as erro:
        carregar_config()
    assert "postgresql+psycopg://" in str(erro.value)


def test_ambiente_desconhecido_e_recusado(monkeypatch):
    monkeypatch.setenv("FH_AMBIENTE", "producao")
    monkeypatch.setenv("FH_DATABASE_URL", "postgresql+psycopg://u:s@h/x")
    with pytest.raises(ConfiguracaoInvalida):
        carregar_config()


def test_segredo_nao_aparece_no_repr(monkeypatch):
    monkeypatch.setenv("FH_AMBIENTE", "teste")
    monkeypatch.setenv("FH_DATABASE_URL", "postgresql+psycopg://u:SENHA123@h/x")
    assert "SENHA123" not in repr(carregar_config())
```

- [ ] **Step 2: Rodar e ver falhar**

```bash
cd /c/agente_stokki_eventos-fase0 && py -3.11 -m pytest tests/freshhub/test_config.py -q
```
Expected: erro de import `No module named 'freshhub.config'` (o pacote `freshhub` não existe mais após a Task 2).

- [ ] **Step 3: Criar o `pyproject.toml`**

```toml
[build-system]
requires = ["setuptools>=69"]
build-backend = "setuptools.build_meta"

[project]
name = "freshhub"
version = "0.1.0"
description = "Sistema proprio da Fresh Log: pedidos, armazem, transporte, financeiro"
requires-python = ">=3.11"
dependencies = [
  "fastapi>=0.115,<1",
  "uvicorn[standard]>=0.30,<1",
  "sqlalchemy>=2.0,<3",
  "alembic>=1.13,<2",
  "psycopg[binary]>=3.2,<4",
  "pydantic>=2.8,<3",
  "pydantic-settings>=2.4,<3",
]

[project.optional-dependencies]
dev = [
  "pytest>=8",
  "httpx>=0.27",
  "ruff>=0.6",
  "mypy>=1.11",
  "import-linter>=2.0",
  "pip-audit>=2.7",
]

[tool.setuptools.packages.find]
include = ["freshhub*"]

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["."]
addopts = "-ra"

[tool.ruff]
target-version = "py311"
line-length = 110
src = ["freshhub", "tests"]
extend-exclude = ["lovable", "painel_agentes", "portal_cliente", "nucleo", "roteirizacao", "regras", "stokki", "atendimento", "app_motorista", "infra", "docs", "dados", "investigacao", "insucesso_entrega", "notificacao_entregas", "batimento", "documentos_pedido", "retiradas", "wms", "confirmacao_motoristas", "resposta_insucesso", "print_agent", "*.py"]
extend-include = ["freshhub/**/*.py", "tests/**/*.py"]

[tool.ruff.lint]
select = ["E", "F", "I", "B", "UP", "N", "S", "T20"]
ignore = ["S101"]  # assert e normal em teste

[tool.ruff.lint.per-file-ignores]
"tests/**" = ["S105", "S106"]  # senhas de mentira em teste

[tool.mypy]
python_version = "3.11"
strict = true
files = ["freshhub"]
plugins = ["pydantic.mypy"]

[tool.importlinter]
root_package = "freshhub"

[[tool.importlinter.contracts]]
name = "dominio nao depende de nada do pacote"
type = "forbidden"
source_modules = ["freshhub.dominio"]
forbidden_modules = ["freshhub.servicos", "freshhub.modelos", "freshhub.adaptadores", "freshhub.api", "freshhub.jobs", "freshhub.config"]

[[tool.importlinter.contracts]]
name = "camadas: api e jobs so chamam servicos"
type = "layers"
layers = ["freshhub.api | freshhub.jobs", "freshhub.servicos", "freshhub.dominio"]

[[tool.importlinter.contracts]]
name = "api e jobs nao falam direto com modelos nem adaptadores"
type = "forbidden"
source_modules = ["freshhub.api", "freshhub.jobs"]
forbidden_modules = ["freshhub.modelos", "freshhub.adaptadores"]
ignore_imports = ["freshhub.api.saude -> freshhub.modelos.sessao"]
```
(O `extend-exclude` com `*.py` mais o `extend-include` faz o ruff olhar só o pacote novo e os testes novos; o legado continua fora do lint. A exceção `api.saude -> modelos.sessao` existe porque a rota de saúde faz `SELECT 1` direto; é a única.)

- [ ] **Step 4: Criar os pacotes e o `config.py`**

`freshhub/__init__.py`:
```python
# -*- coding: utf-8 -*-
"""Sistema proprio da Fresh Log (plano mestre de 17/10/2026).

Camadas: dominio (regras puras) -> servicos (casos de uso) -> api / jobs.
modelos = tabelas e migracoes; adaptadores = Stokki, Vuupt, Lalamove,
Google, e-mail, WhatsApp atras de interfaces. Ver freshhub/README.md.
"""
__version__ = "0.1.0"
```

`freshhub/dominio/__init__.py`, `servicos/__init__.py`, `adaptadores/__init__.py`, `modelos/__init__.py`, `api/__init__.py`, `jobs/__init__.py`: cada um só com a docstring de uma linha dizendo o papel da camada (ex.: `"""Regras puras: sem banco, sem rede, sem config."""`).

`freshhub/config.py`:
```python
# -*- coding: utf-8 -*-
"""Configuracao por variaveis de ambiente (prefixo FH_). O pacote novo
nunca le config.yaml (spec 5.7)."""
import os
from typing import Literal

from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Ambiente = Literal["dev", "teste", "hml", "prod"]


class ConfiguracaoInvalida(RuntimeError):
    """Variavel de ambiente ausente ou invalida; a mensagem diz qual."""


class Configuracao(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FH_", extra="ignore")

    ambiente: Ambiente
    database_url: SecretStr = Field(description="postgresql+psycopg://usuario:senha@host:porta/banco")
    log_nivel: str = "INFO"

    @field_validator("database_url")
    @classmethod
    def _so_postgres(cls, v: SecretStr) -> SecretStr:
        if not v.get_secret_value().startswith("postgresql+psycopg://"):
            raise ValueError("FH_DATABASE_URL precisa comecar com postgresql+psycopg://")
        return v

    @property
    def database_url_texto(self) -> str:
        return self.database_url.get_secret_value()


def carregar_config() -> Configuracao:
    """Le FH_* do ambiente; se FH_ENV_ARQUIVO apontar pra um arquivo
    (formato KEY=valor), ele e lido antes, sem sobrescrever o ambiente."""
    arquivo = os.environ.get("FH_ENV_ARQUIVO")
    try:
        if arquivo:
            return Configuracao(_env_file=arquivo)  # type: ignore[call-arg]
        return Configuracao()  # type: ignore[call-arg]
    except ValidationError as e:
        faltando = [f"FH_{str(err['loc'][0]).upper()}: {err['msg']}" for err in e.errors()]
        raise ConfiguracaoInvalida("Configuracao invalida -- " + "; ".join(faltando)) from e
```
(`SecretStr` esconde a senha no `repr`; a URL em texto só sai por `database_url_texto`, usada apenas pelo `modelos/sessao.py` da Task 5.)

`freshhub-dev.env.exemplo` (este é commitado; a cópia sem `.exemplo` é gitignored):
```
# Copie para freshhub-dev.env e preencha. FH_ENV_ARQUIVO=freshhub-dev.env
FH_AMBIENTE=dev
FH_DATABASE_URL=postgresql+psycopg://freshhub:TROQUE@127.0.0.1:5433/freshhub_dev
FH_LOG_NIVEL=DEBUG
```

`freshhub/README.md`:
```markdown
# freshhub — sistema proprio

Plano mestre: docs/superpowers/specs/2026-10-17-freshhub-sistema-proprio-design.md

## Rodar local
    py -3.11 -m pip install -e ".[dev]"
    cp freshhub-dev.env.exemplo freshhub-dev.env   # e preencha
    bash infra/postgres-dev/tunel.sh                # tunel SSH pro Postgres de dev na VPS
    FH_ENV_ARQUIVO=freshhub-dev.env py -3.11 -m alembic upgrade head
    FH_ENV_ARQUIVO=freshhub-dev.env py -3.11 -m freshhub      # API em http://127.0.0.1:8100

## Verificar (o CI roda o mesmo)
    py -3.11 -m ruff check freshhub tests
    py -3.11 -m mypy
    py -3.11 -m lint_imports
    FH_TEST_DATABASE_URL=postgresql+psycopg://freshhub:...@127.0.0.1:5433/freshhub_test py -3.11 -m pytest
```

- [ ] **Step 5: Instalar e rodar o teste**

```bash
cd /c/agente_stokki_eventos-fase0 && py -3.11 -m pip install -e ".[dev]" -q && py -3.11 -m pytest tests/freshhub/test_config.py -q
```
Expected: 5 passed.

- [ ] **Step 6: Lint e tipos**

```bash
cd /c/agente_stokki_eventos-fase0 && py -3.11 -m ruff check freshhub tests && py -3.11 -m mypy
```
Expected: `All checks passed!` e `Success: no issues found`. Corrigir o que apontar antes de seguir (não suprimir).

- [ ] **Step 7: Commit**

```bash
cd /c/agente_stokki_eventos-fase0 && git add pyproject.toml freshhub freshhub-dev.env.exemplo tests/freshhub && git commit -q -F - <<'EOF'
Freshhub: pacote novo com configuracao por ambiente e ferramentas de qualidade

Esqueleto do sistema proprio (plano mestre 17/10, Fase 0.1): pacote
freshhub/ com as camadas vazias, pyproject (ruff, mypy strict,
import-linter com o contrato de camadas, pytest) e configuracao so por
variaveis FH_* (nunca config.yaml). URL do banco e segredo e so aceita
Postgres; variavel ausente da mensagem clara em portugues.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
EOF
```

---

### Task 4: Postgres de desenvolvimento na VPS e túnel SSH

**Files:**
- Create: `infra/postgres-dev/compose.yaml`, `infra/postgres-dev/README.md`, `infra/postgres-dev/tunel.sh`
- Test: `tests/freshhub/test_banco_dev.py` (só roda com `FH_TEST_DATABASE_URL` definida)

**Interfaces:**
- Produces: Postgres 16 em `127.0.0.1:5432` **da VPS** (nunca exposto na internet), bancos `freshhub_dev` e `freshhub_test`, usuário `freshhub`; túnel local em `127.0.0.1:5433`. Variável `FH_TEST_DATABASE_URL` usada pelo `conftest` da Task 5.
- Decisão registrada: sem Docker nem Postgres na máquina do Hugo; a VPS já tem Docker 29. Dados de desenvolvimento e teste **nunca** são dados de produção.

- [ ] **Step 1: Escrever o teste (pula sem a variável)**

`tests/freshhub/test_banco_dev.py`:
```python
# -*- coding: utf-8 -*-
import os

import pytest
from sqlalchemy import create_engine, text

URL = os.environ.get("FH_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="FH_TEST_DATABASE_URL nao definida")


def test_postgres_de_teste_responde():
    motor = create_engine(URL, pool_pre_ping=True)
    with motor.connect() as conexao:
        versao = conexao.execute(text("select version()")).scalar_one()
        assert versao.startswith("PostgreSQL 16")
        assert conexao.execute(text("select current_database()")).scalar_one() == "freshhub_test"
```

- [ ] **Step 2: Rodar sem a variável e ver pular; com a variável e ver falhar**

```bash
cd /c/agente_stokki_eventos-fase0 && py -3.11 -m pytest tests/freshhub/test_banco_dev.py -q
FH_TEST_DATABASE_URL=postgresql+psycopg://freshhub:x@127.0.0.1:5433/freshhub_test py -3.11 -m pytest tests/freshhub/test_banco_dev.py -q
```
Expected: `1 skipped`; depois `1 failed` (conexão recusada).

- [ ] **Step 3: Criar o compose e o README**

`infra/postgres-dev/compose.yaml`:
```yaml
# Postgres de DESENVOLVIMENTO e TESTE do pacote freshhub (Fase 0.1).
# So escuta em 127.0.0.1 da VPS; acesso local por tunel SSH (tunel.sh).
# Nunca recebe dado de producao. Producao e homologacao usam Postgres
# gerenciado (plano 0.3).
services:
  postgres:
    image: postgres:16
    container_name: freshhub-postgres-dev
    restart: unless-stopped
    ports:
      - "127.0.0.1:5432:5432"
    env_file:
      - /etc/freshhub/postgres-dev.env   # POSTGRES_PASSWORD=...
    environment:
      POSTGRES_USER: freshhub
      POSTGRES_DB: freshhub_dev
      TZ: America/Sao_Paulo
    volumes:
      - freshhub-postgres-dev:/var/lib/postgresql/data
      - ./init:/docker-entrypoint-initdb.d:ro
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U freshhub -d freshhub_dev"]
      interval: 10s
      timeout: 5s
      retries: 5
volumes:
  freshhub-postgres-dev:
```

`infra/postgres-dev/init/01-bancos.sql`:
```sql
-- roda so na primeira subida do volume
CREATE DATABASE freshhub_test OWNER freshhub;
```

`infra/postgres-dev/tunel.sh`:
```bash
#!/usr/bin/env bash
# Tunel SSH: 127.0.0.1:5433 local -> 127.0.0.1:5432 da VPS (Postgres de dev).
# Uso: bash infra/postgres-dev/tunel.sh   (fica em primeiro plano; Ctrl+C encerra)
set -euo pipefail
exec ssh -i ~/.ssh/atendimento_vps -N -L 5433:127.0.0.1:5432 root@187.127.52.197
```

`infra/postgres-dev/README.md`:
```markdown
# Postgres de desenvolvimento (VPS, Docker)

Primeira subida (como root na VPS, uma vez):

    mkdir -p /etc/freshhub && chmod 700 /etc/freshhub
    printf 'POSTGRES_PASSWORD=%s\n' "$(openssl rand -base64 24)" > /etc/freshhub/postgres-dev.env
    chmod 600 /etc/freshhub/postgres-dev.env
    cd /opt/stokki-eventos/infra/postgres-dev && docker compose up -d
    docker compose ps   # healthy

A senha fica so em /etc/freshhub/postgres-dev.env (ler com `cat` na VPS
quando precisar montar a URL local). URLs:

    dev:   postgresql+psycopg://freshhub:<senha>@127.0.0.1:5433/freshhub_dev
    teste: postgresql+psycopg://freshhub:<senha>@127.0.0.1:5433/freshhub_test

Local: `bash infra/postgres-dev/tunel.sh` num terminal e as URLs acima em
`freshhub-dev.env` (FH_DATABASE_URL) e na variavel FH_TEST_DATABASE_URL.
Zerar o banco de teste: `alembic downgrade base` + `upgrade head` (o
conftest faz isso sozinho).
```

- [ ] **Step 4: Subir na VPS (uma vez) e abrir o túnel**

Copiar a pasta para a VPS ainda sem commit/pull (o worktree não está em `master`):
```bash
cd /c/agente_stokki_eventos-fase0 && ssh -i ~/.ssh/atendimento_vps root@187.127.52.197 "mkdir -p /opt/freshhub-dev" && scp -q -r -i ~/.ssh/atendimento_vps infra/postgres-dev/. root@187.127.52.197:/opt/freshhub-dev/ && ssh -i ~/.ssh/atendimento_vps root@187.127.52.197 "mkdir -p /etc/freshhub && chmod 700 /etc/freshhub && test -f /etc/freshhub/postgres-dev.env || printf 'POSTGRES_PASSWORD=%s\n' \"\$(openssl rand -base64 24)\" > /etc/freshhub/postgres-dev.env; chmod 600 /etc/freshhub/postgres-dev.env; cd /opt/freshhub-dev && docker compose up -d && sleep 8 && docker compose ps"
```
Expected: `freshhub-postgres-dev ... healthy`. (Quando o ramo for mesclado, o compose passa a viver em `/opt/stokki-eventos/infra/postgres-dev`; o volume nomeado é o mesmo.)

Abrir o túnel em outro terminal: `bash infra/postgres-dev/tunel.sh`. Pegar a senha com `ssh ... "cat /etc/freshhub/postgres-dev.env"` e **não colar no chat**: exportar direto no terminal.

- [ ] **Step 5: Rodar o teste com o túnel aberto**

```bash
cd /c/agente_stokki_eventos-fase0 && export FH_TEST_DATABASE_URL="postgresql+psycopg://freshhub:${SENHA}@127.0.0.1:5433/freshhub_test" && py -3.11 -m pytest tests/freshhub/test_banco_dev.py -q
```
Expected: `1 passed`.

- [ ] **Step 6: Commit**

```bash
cd /c/agente_stokki_eventos-fase0 && git add infra/postgres-dev tests/freshhub/test_banco_dev.py && git commit -q -F - <<'EOF'
Infra: Postgres 16 de desenvolvimento em Docker na VPS, acessado por tunel SSH

A maquina local nao tem Docker nem Postgres; a VPS tem Docker. O
container escuta so em 127.0.0.1 da VPS, senha em /etc/freshhub, bancos
freshhub_dev e freshhub_test. Nunca recebe dado de producao; producao e
homologacao ficam no Postgres gerenciado (plano 0.3).

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
EOF
```

---

### Task 5: Base de modelos, fábrica de sessão e `conftest` com banco limpo

**Files:**
- Create: `freshhub/modelos/base.py`, `freshhub/modelos/sessao.py`, `tests/conftest.py`
- Test: `tests/freshhub/test_sessao.py`

**Interfaces:**
- Produces: `freshhub.modelos.base.Base` (DeclarativeBase com `metadata` e convenção de nomes de constraint), `freshhub.modelos.base.SCHEMAS: tuple[str, ...]` com os 8 schemas da spec 2.1; `freshhub.modelos.sessao.criar_motor(url: str) -> Engine`; `freshhub.modelos.sessao.FabricaSessao` (sessionmaker); `freshhub.modelos.sessao.sessao_banco(fabrica) -> Iterator[Session]` (contexto que faz commit/rollback). Fixtures: `motor_teste` (sessão de teste, migra `base → head` uma vez), `sessao` (transação por teste com rollback).

- [ ] **Step 1: Escrever os testes**

`tests/freshhub/test_sessao.py`:
```python
# -*- coding: utf-8 -*-
import pytest
from sqlalchemy import text

from freshhub.modelos.base import SCHEMAS, Base
from freshhub.modelos.sessao import criar_motor


def test_schemas_da_spec():
    assert SCHEMAS == ("identidade", "cadastros", "pedidos", "armazem",
                       "transporte", "financeiro", "comunicacao", "legado")


def test_convencao_de_nomes_de_constraint():
    conv = Base.metadata.naming_convention
    assert conv["pk"] == "pk_%(table_name)s"
    assert conv["fk"] == "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"


def test_criar_motor_recusa_sqlite():
    with pytest.raises(ValueError, match="postgresql\\+psycopg"):
        criar_motor("sqlite:///x.db")


def test_sessao_de_teste_roda_em_transacao_que_volta(sessao):
    sessao.execute(text("create table if not exists legado.tmp_prova (id int)"))
    sessao.execute(text("insert into legado.tmp_prova values (1)"))
    assert sessao.execute(text("select count(*) from legado.tmp_prova")).scalar_one() == 1


def test_a_tabela_do_teste_anterior_nao_existe_mais(sessao):
    existe = sessao.execute(text(
        "select 1 from information_schema.tables where table_schema='legado' and table_name='tmp_prova'"
    )).first()
    assert existe is None
```
(Os dois últimos só passam com a fixture `sessao` fazendo rollback e com o schema `legado` existindo, que é a migração da Task 6: até lá eles falham, e é o esperado.)

- [ ] **Step 2: Rodar e ver falhar**

```bash
cd /c/agente_stokki_eventos-fase0 && py -3.11 -m pytest tests/freshhub/test_sessao.py -q
```
Expected: erro de import `freshhub.modelos.base`.

- [ ] **Step 3: Implementar `base.py` e `sessao.py`**

`freshhub/modelos/base.py`:
```python
# -*- coding: utf-8 -*-
"""Base declarativa e convencao de nomes. Um schema por modulo de dominio
(spec 2.1); 'legado' guarda linhas brutas do SQLite so pra auditoria."""
from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

SCHEMAS: tuple[str, ...] = (
    "identidade", "cadastros", "pedidos", "armazem",
    "transporte", "financeiro", "comunicacao", "legado",
)

CONVENCAO_NOMES = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=CONVENCAO_NOMES)
```

`freshhub/modelos/sessao.py`:
```python
# -*- coding: utf-8 -*-
"""Fabrica de motor e sessao. Unico lugar que recebe a URL do banco."""
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

PREFIXO_URL = "postgresql+psycopg://"


def criar_motor(url: str) -> Engine:
    if not url.startswith(PREFIXO_URL):
        raise ValueError(f"URL do banco precisa comecar com {PREFIXO_URL}")
    return create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=5, future=True)


FabricaSessao = sessionmaker[Session]


def criar_fabrica(motor: Engine) -> FabricaSessao:
    return sessionmaker(bind=motor, expire_on_commit=False)


@contextmanager
def sessao_banco(fabrica: FabricaSessao) -> Iterator[Session]:
    """Commit no fim; rollback se der excecao. Nunca segurar a sessao
    aberta durante chamada de rede (licao do ADR-0005)."""
    sessao = fabrica()
    try:
        yield sessao
        sessao.commit()
    except Exception:
        sessao.rollback()
        raise
    finally:
        sessao.close()
```

`tests/conftest.py`:
```python
# -*- coding: utf-8 -*-
"""Fixtures do pacote novo. Precisa de FH_TEST_DATABASE_URL (Postgres de
teste, Task 4). Migra base -> head uma vez por sessao de teste; cada
teste roda numa transacao que volta no fim."""
import os
import subprocess
import sys
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from freshhub.modelos.sessao import criar_motor

URL_TESTE = os.environ.get("FH_TEST_DATABASE_URL")


def _alembic(*args: str) -> None:
    env = {**os.environ, "FH_AMBIENTE": "teste", "FH_DATABASE_URL": URL_TESTE or ""}
    subprocess.run([sys.executable, "-m", "alembic", *args], check=True, env=env)


@pytest.fixture(scope="session")
def motor_teste() -> Iterator[Engine]:
    if not URL_TESTE:
        pytest.skip("FH_TEST_DATABASE_URL nao definida")
    _alembic("downgrade", "base")
    _alembic("upgrade", "head")
    motor = criar_motor(URL_TESTE)
    yield motor
    motor.dispose()


@pytest.fixture
def sessao(motor_teste: Engine) -> Iterator[Session]:
    conexao = motor_teste.connect()
    transacao = conexao.begin()
    sessao = Session(bind=conexao, join_transaction_mode="create_savepoint")
    try:
        yield sessao
    finally:
        sessao.close()
        transacao.rollback()
        conexao.close()
```

- [ ] **Step 4: Rodar; os 3 primeiros passam, os 2 últimos falham por falta de migração**

```bash
cd /c/agente_stokki_eventos-fase0 && py -3.11 -m pytest tests/freshhub/test_sessao.py -q
```
Expected: 3 passed, 2 errors (`alembic` ainda não existe → `No module named alembic.__main__` ou `alembic.ini` ausente). Seguir pra Task 6, que fecha isso.

- [ ] **Step 5: Lint e tipos**

```bash
cd /c/agente_stokki_eventos-fase0 && py -3.11 -m ruff check freshhub tests && py -3.11 -m mypy
```
Expected: limpo.

- [ ] **Step 6: Commit (parcial, testes dependentes marcados na mensagem)**

```bash
cd /c/agente_stokki_eventos-fase0 && git add freshhub/modelos/base.py freshhub/modelos/sessao.py tests/conftest.py tests/freshhub/test_sessao.py && git commit -q -F - <<'EOF'
Freshhub: base declarativa, schemas por modulo e fabrica de sessao

Os 8 schemas da spec (2.1) numa constante, convencao de nomes de
constraint (migracoes previsiveis) e um unico lugar que recebe a URL do
banco. conftest migra o banco de teste base->head uma vez e roda cada
teste numa transacao que volta. Os testes que dependem da migracao
passam na proxima tarefa (Alembic).

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
EOF
```

---

### Task 6: Alembic e a migração 0001 (os 8 schemas)

**Files:**
- Create: `alembic.ini`, `freshhub/migracoes/env.py`, `freshhub/migracoes/script.py.mako`, `freshhub/migracoes/versoes/0001_schemas.py`
- Test: `tests/freshhub/test_migracoes.py`

**Interfaces:**
- Consumes: `freshhub.config.carregar_config`, `freshhub.modelos.base.Base`, `SCHEMAS`.
- Produces: comando `py -3.11 -m alembic upgrade head` funcional lendo `FH_DATABASE_URL`; revisão `0001` cria os schemas, `downgrade` apaga (com `CASCADE`); nomes de arquivo `NNNN_descricao.py` com revisão = `NNNN`.

- [ ] **Step 1: Escrever o teste**

`tests/freshhub/test_migracoes.py`:
```python
# -*- coding: utf-8 -*-
import os
import subprocess
import sys

import pytest
from sqlalchemy import text

from freshhub.modelos.base import SCHEMAS

URL = os.environ.get("FH_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="FH_TEST_DATABASE_URL nao definida")


def _alembic(*args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "FH_AMBIENTE": "teste", "FH_DATABASE_URL": URL or ""}
    return subprocess.run([sys.executable, "-m", "alembic", *args], env=env, capture_output=True, text=True)


def test_upgrade_head_cria_os_schemas(motor_teste):
    with motor_teste.connect() as c:
        existentes = {r[0] for r in c.execute(text("select schema_name from information_schema.schemata"))}
    assert set(SCHEMAS) <= existentes


def test_upgrade_duas_vezes_e_idempotente(motor_teste):
    r = _alembic("upgrade", "head")
    assert r.returncode == 0, r.stderr
    assert "Running upgrade" not in r.stderr + r.stdout


def test_downgrade_base_apaga_os_schemas_e_upgrade_recria(motor_teste):
    assert _alembic("downgrade", "base").returncode == 0
    with motor_teste.connect() as c:
        existentes = {r[0] for r in c.execute(text("select schema_name from information_schema.schemata"))}
    assert not (set(SCHEMAS) & existentes)
    assert _alembic("upgrade", "head").returncode == 0


def test_alembic_sem_database_url_da_mensagem_clara():
    env = {k: v for k, v in os.environ.items() if k != "FH_DATABASE_URL"}
    env["FH_AMBIENTE"] = "teste"
    r = subprocess.run([sys.executable, "-m", "alembic", "current"], env=env, capture_output=True, text=True)
    assert r.returncode != 0
    assert "FH_DATABASE_URL" in r.stderr
```

- [ ] **Step 2: Rodar e ver falhar**

```bash
cd /c/agente_stokki_eventos-fase0 && py -3.11 -m pytest tests/freshhub/test_migracoes.py -q
```
Expected: erros (`alembic.ini` não existe / `No 'script_location'`).

- [ ] **Step 3: Criar `alembic.ini` e o `env.py`**

`alembic.ini` (raiz):
```ini
[alembic]
script_location = freshhub/migracoes
version_locations = freshhub/migracoes/versoes
file_template = %%(rev)s_%%(slug)s
prepend_sys_path = .
timezone = America/Sao_Paulo

[loggers]
keys = root,sqlalchemy,alembic
[handlers]
keys = console
[formatters]
keys = generic
[logger_root]
level = WARN
handlers = console
[logger_sqlalchemy]
level = WARN
handlers =
qualname = sqlalchemy.engine
[logger_alembic]
level = INFO
handlers =
qualname = alembic
[handler_console]
class = StreamHandler
args = (sys.stderr,)
level = NOTSET
formatter = generic
[formatter_generic]
format = %(levelname)-5.5s [%(name)s] %(message)s
```

`freshhub/migracoes/env.py`:
```python
# -*- coding: utf-8 -*-
"""Ambiente do Alembic: a URL vem de FH_DATABASE_URL (nunca do ini)."""
import sys

from alembic import context
from sqlalchemy import engine_from_config, pool

from freshhub.config import ConfiguracaoInvalida, carregar_config
from freshhub.modelos.base import SCHEMAS, Base

try:
    _cfg = carregar_config()
except ConfiguracaoInvalida as e:
    print(f"alembic: {e}", file=sys.stderr)
    raise SystemExit(2) from e

config = context.config
config.set_main_option("sqlalchemy.url", _cfg.database_url_texto)
target_metadata = Base.metadata


def _incluir(name: str | None, type_: str, parent_names: dict[str, str | None]) -> bool:
    # autogenerate so enxerga os nossos schemas
    if type_ == "schema":
        return name in SCHEMAS
    return True


def run_migrations_online() -> None:
    conectavel = engine_from_config(config.get_section(config.config_ini_section) or {},
                                    prefix="sqlalchemy.", poolclass=pool.NullPool)
    with conectavel.connect() as conexao:
        context.configure(connection=conexao, target_metadata=target_metadata,
                          include_schemas=True, include_name=_incluir,
                          version_table_schema="identidade" if False else None,
                          compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    raise SystemExit("modo offline nao e usado neste projeto")
run_migrations_online()
```
(A tabela `alembic_version` fica no schema `public` de propósito: ela precisa existir antes de qualquer schema nosso ser criado.)

`freshhub/migracoes/script.py.mako`:
```mako
# -*- coding: utf-8 -*-
"""${message}

Revisao: ${up_revision}
Anterior: ${down_revision | comma,n}
Data: ${create_date}
"""
from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
```

- [ ] **Step 4: Escrever a migração 0001 à mão (não autogenerate: schema não é autogerado)**

`freshhub/migracoes/versoes/0001_schemas.py`:
```python
# -*- coding: utf-8 -*-
"""Schemas por modulo de dominio (spec 2.1).

Revisao: 0001
Anterior: None
Data: 2026-10-17
"""
from alembic import op

from freshhub.modelos.base import SCHEMAS

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    for schema in SCHEMAS:
        op.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')


def downgrade() -> None:
    for schema in reversed(SCHEMAS):
        op.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
```

- [ ] **Step 5: Rodar os testes de migração e os de sessão pendentes**

```bash
cd /c/agente_stokki_eventos-fase0 && py -3.11 -m pytest tests/freshhub/test_migracoes.py tests/freshhub/test_sessao.py -q
```
Expected: 9 passed (4 + 5). Se `test_upgrade_duas_vezes_e_idempotente` falhar por texto de log, conferir se o Alembic escreveu `Running upgrade` em stderr: a asserção cobre stdout e stderr juntos.

- [ ] **Step 6: Lint, tipos e contrato de camadas**

```bash
cd /c/agente_stokki_eventos-fase0 && py -3.11 -m ruff check freshhub tests && py -3.11 -m mypy && py -3.11 -m lint_imports
```
Expected: tudo limpo; `lint_imports` mostra `Contracts: 3 kept, 0 broken`. (`migracoes` importa `config` e `modelos`; nenhum contrato proíbe isso.) Se o mypy reclamar de `alembic` sem stubs, adicionar em `pyproject.toml`:
```toml
[[tool.mypy.overrides]]
module = ["alembic.*"]
ignore_missing_imports = true
```

- [ ] **Step 7: Commit**

```bash
cd /c/agente_stokki_eventos-fase0 && git add alembic.ini freshhub/migracoes tests/freshhub/test_migracoes.py pyproject.toml && git commit -q -F - <<'EOF'
Freshhub: Alembic lendo FH_DATABASE_URL e migracao 0001 com os 8 schemas

Migracoes versionadas desde a primeira tabela (spec 2.5): nenhum CREATE
TABLE IF NOT EXISTS no pacote novo. A 0001 cria os schemas por modulo;
downgrade apaga em cascata (so faz sentido em dev/teste). Upgrade
repetido e idempotente; sem URL o alembic sai com mensagem clara.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
EOF
```

---

### Task 7: API FastAPI com `GET /api/saude` e `python -m freshhub`

**Files:**
- Create: `freshhub/api/app.py`, `freshhub/api/saude.py`, `freshhub/__main__.py`
- Test: `tests/freshhub/test_api_saude.py`

**Interfaces:**
- Consumes: `carregar_config`, `criar_motor`, `__version__`.
- Produces: `freshhub.api.app.criar_app(config: Configuracao | None = None) -> FastAPI`; rota `GET /api/saude` → 200 `{"status":"ok","versao":"0.1.0","ambiente":"...","banco":"ok"}` ou 503 `{"status":"degradado", ..., "banco":"falha"}`; `app.state.motor` (Engine) e `app.state.config`; `python -m freshhub` sobe uvicorn em `127.0.0.1:8100` (porta nova; o legado usa 8070–8095).

- [ ] **Step 1: Escrever os testes**

`tests/freshhub/test_api_saude.py`:
```python
# -*- coding: utf-8 -*-
import os

import pytest
from fastapi.testclient import TestClient

from freshhub import __version__
from freshhub.api.app import criar_app
from freshhub.config import Configuracao

URL = os.environ.get("FH_TEST_DATABASE_URL")


def _config(url: str) -> Configuracao:
    return Configuracao(ambiente="teste", database_url=url)  # type: ignore[arg-type]


@pytest.mark.skipif(not URL, reason="FH_TEST_DATABASE_URL nao definida")
def test_saude_ok_com_banco(motor_teste):
    with TestClient(criar_app(_config(URL or ""))) as cliente:
        r = cliente.get("/api/saude")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "versao": __version__, "ambiente": "teste", "banco": "ok"}


def test_saude_503_sem_banco_e_sem_vazar_segredo(caplog):
    url = "postgresql+psycopg://freshhub:SENHA-SECRETA@127.0.0.1:1/nao_existe"
    with TestClient(criar_app(_config(url))) as cliente:
        r = cliente.get("/api/saude")
    assert r.status_code == 503
    assert r.json()["banco"] == "falha"
    assert r.json()["status"] == "degradado"
    assert "SENHA-SECRETA" not in r.text
    assert "SENHA-SECRETA" not in caplog.text


def test_rota_inexistente_e_404_json():
    url = "postgresql+psycopg://u:s@127.0.0.1:1/x"
    with TestClient(criar_app(_config(url))) as cliente:
        r = cliente.get("/api/nao-existe")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/json")
```

- [ ] **Step 2: Rodar e ver falhar**

```bash
cd /c/agente_stokki_eventos-fase0 && py -3.11 -m pytest tests/freshhub/test_api_saude.py -q
```
Expected: erro de import `freshhub.api.app`.

- [ ] **Step 3: Implementar**

`freshhub/api/saude.py`:
```python
# -*- coding: utf-8 -*-
"""Rota de saude: a unica que fala com o banco sem passar por servicos
(excecao declarada no import-linter)."""
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from freshhub import __version__

logger = logging.getLogger(__name__)
roteador = APIRouter()


@roteador.get("/api/saude")
def saude(request: Request) -> JSONResponse:
    config = request.app.state.config
    motor = request.app.state.motor
    banco = "ok"
    try:
        with motor.connect() as conexao:
            conexao.execute(text("select 1"))
    except Exception as e:  # noqa: BLE001 - qualquer falha de banco vira 'falha', sem a URL
        banco = "falha"
        logger.warning("saude: banco indisponivel (%s)", type(e).__name__)
    corpo = {"status": "ok" if banco == "ok" else "degradado", "versao": __version__,
             "ambiente": config.ambiente, "banco": banco}
    return JSONResponse(corpo, status_code=200 if banco == "ok" else 503)
```
(Só o **tipo** da exceção vai pro log: a mensagem do psycopg traz host e usuário, e a URL traria a senha.)

`freshhub/api/app.py`:
```python
# -*- coding: utf-8 -*-
"""Fabrica da aplicacao FastAPI."""
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from freshhub import __version__
from freshhub.api.saude import roteador as roteador_saude
from freshhub.config import Configuracao, carregar_config
from freshhub.log import configurar_log
from freshhub.modelos.sessao import criar_motor


def criar_app(config: Configuracao | None = None) -> FastAPI:
    cfg = config or carregar_config()
    configurar_log(cfg.log_nivel)

    @asynccontextmanager
    async def ciclo(app: FastAPI) -> AsyncIterator[None]:
        app.state.config = cfg
        app.state.motor = criar_motor(cfg.database_url_texto)
        yield
        app.state.motor.dispose()

    app = FastAPI(title="Fresh Hub", version=__version__, lifespan=ciclo,
                  docs_url="/api/docs" if cfg.ambiente != "prod" else None,
                  openapi_url="/api/openapi.json")
    app.include_router(roteador_saude)
    return app
```
(`freshhub.log` é a Task 8; até lá, criar `freshhub/log.py` com `def configurar_log(nivel: str) -> None: logging.basicConfig(level=nivel)` e substituir na Task 8. O contrato `api → modelos` é a exceção `freshhub.api.saude -> freshhub.modelos.sessao` já declarada; `app.py` importa `criar_motor` também, então acrescentar `"freshhub.api.app -> freshhub.modelos.sessao"` ao `ignore_imports` do terceiro contrato no `pyproject.toml`.)

`freshhub/__main__.py`:
```python
# -*- coding: utf-8 -*-
"""python -m freshhub: sobe a API local (dev). Em producao o systemd
chama o uvicorn direto (plano 0.3)."""
import uvicorn

from freshhub.api.app import criar_app

if __name__ == "__main__":
    uvicorn.run(criar_app(), host="127.0.0.1", port=8100, log_config=None)
```

- [ ] **Step 4: Rodar os testes**

```bash
cd /c/agente_stokki_eventos-fase0 && py -3.11 -m pytest tests/freshhub/test_api_saude.py -q
```
Expected: 3 passed (com o túnel aberto; sem ele, 2 passed 1 skipped).

- [ ] **Step 5: Subir de verdade e chamar**

```bash
cd /c/agente_stokki_eventos-fase0 && (FH_ENV_ARQUIVO=freshhub-dev.env py -3.11 -m freshhub & echo $! > /tmp/fh.pid; sleep 3; curl -s -i http://127.0.0.1:8100/api/saude | head -12; kill $(cat /tmp/fh.pid))
```
Expected: `HTTP/1.1 200 OK` e `{"status":"ok","versao":"0.1.0","ambiente":"dev","banco":"ok"}` (exige `freshhub-dev.env` preenchido e o túnel; sem banco, 503 com `"banco":"falha"`, que também prova o caminho).

- [ ] **Step 6: Lint, tipos, camadas**

```bash
cd /c/agente_stokki_eventos-fase0 && py -3.11 -m ruff check freshhub tests && py -3.11 -m mypy && py -3.11 -m lint_imports
```
Expected: limpo, 3 contratos mantidos.

- [ ] **Step 7: Commit**

```bash
cd /c/agente_stokki_eventos-fase0 && git add freshhub/api freshhub/__main__.py freshhub/log.py pyproject.toml tests/freshhub/test_api_saude.py && git commit -q -F - <<'EOF'
Freshhub: API FastAPI com rota de saude e python -m freshhub

GET /api/saude responde 200 com versao, ambiente e banco ok, ou 503
degradado quando o banco nao responde -- sem a URL nem a senha no corpo
ou no log (so o tipo da excecao). Docs do OpenAPI fora do ar em prod.
Porta 8100 local, fora da faixa do legado.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
EOF
```

---

### Task 8: Log estruturado em JSON com id de requisição

**Files:**
- Modify: `freshhub/log.py` (substitui o provisório da Task 7), `freshhub/api/app.py` (middleware)
- Test: `tests/freshhub/test_log.py`

**Interfaces:**
- Produces: `freshhub.log.configurar_log(nivel: str) -> None` (handler único em stderr, uma linha JSON por registro com `hora` ISO em UTC, `nivel`, `logger`, `mensagem`, `request_id` quando houver, `extra` achatado); `freshhub.log.request_id_atual: contextvars.ContextVar[str | None]`; middleware que gera `request_id` (uuid4 curto) por requisição, aceita `X-Request-ID` de entrada e devolve o mesmo cabeçalho na resposta.

- [ ] **Step 1: Escrever os testes**

`tests/freshhub/test_log.py`:
```python
# -*- coding: utf-8 -*-
import io
import json
import logging

from fastapi.testclient import TestClient

from freshhub.api.app import criar_app
from freshhub.config import Configuracao
from freshhub.log import configurar_log, request_id_atual


def _capturar() -> io.StringIO:
    configurar_log("INFO")
    buffer = io.StringIO()
    handler = logging.getLogger().handlers[0]
    handler.stream = buffer  # type: ignore[attr-defined]
    return buffer


def test_linha_json_com_campos_basicos():
    buffer = _capturar()
    logging.getLogger("teste.x").info("ola", extra={"pedido": "PS-1"})
    linha = json.loads(buffer.getvalue().strip().splitlines()[-1])
    assert linha["nivel"] == "INFO"
    assert linha["logger"] == "teste.x"
    assert linha["mensagem"] == "ola"
    assert linha["pedido"] == "PS-1"
    assert linha["hora"].endswith("+00:00")


def test_acento_nao_quebra_e_sai_legivel():
    buffer = _capturar()
    logging.getLogger("teste.y").warning("endereço não encontrado: São Paulo")
    linha = json.loads(buffer.getvalue().strip().splitlines()[-1])
    assert linha["mensagem"] == "endereço não encontrado: São Paulo"


def test_configurar_duas_vezes_nao_duplica_handler():
    configurar_log("INFO")
    configurar_log("DEBUG")
    assert len(logging.getLogger().handlers) == 1
    assert logging.getLogger().level == logging.DEBUG


def test_request_id_vai_pro_log_e_pro_cabecalho():
    buffer = _capturar()
    cfg = Configuracao(ambiente="teste", database_url="postgresql+psycopg://u:s@127.0.0.1:1/x")  # type: ignore[arg-type]
    with TestClient(criar_app(cfg)) as cliente:
        r = cliente.get("/api/saude", headers={"X-Request-ID": "abc-123"})
    assert r.headers["X-Request-ID"] == "abc-123"
    linhas = [json.loads(l) for l in buffer.getvalue().strip().splitlines()]
    assert any(l.get("request_id") == "abc-123" for l in linhas)
    assert request_id_atual.get() is None
```

- [ ] **Step 2: Rodar e ver falhar**

```bash
cd /c/agente_stokki_eventos-fase0 && py -3.11 -m pytest tests/freshhub/test_log.py -q
```
Expected: falhas (`request_id_atual` não existe; saída não é JSON).

- [ ] **Step 3: Implementar `log.py` e o middleware**

`freshhub/log.py`:
```python
# -*- coding: utf-8 -*-
"""Log estruturado: uma linha JSON por registro, em stderr (spec 5.5).
Campos extras do `extra=` sao achatados na linha."""
import contextvars
import json
import logging
import sys
from datetime import UTC, datetime

request_id_atual: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)

_CAMPOS_PADRAO = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}


class FormatadorJson(logging.Formatter):
    def format(self, registro: logging.LogRecord) -> str:
        linha: dict[str, object] = {
            "hora": datetime.fromtimestamp(registro.created, tz=UTC).isoformat(),
            "nivel": registro.levelname,
            "logger": registro.name,
            "mensagem": registro.getMessage(),
        }
        rid = request_id_atual.get()
        if rid:
            linha["request_id"] = rid
        for chave, valor in registro.__dict__.items():
            if chave not in _CAMPOS_PADRAO and not chave.startswith("_"):
                linha[chave] = valor
        if registro.exc_info:
            linha["excecao"] = self.formatException(registro.exc_info)
        return json.dumps(linha, ensure_ascii=False, default=str)


def configurar_log(nivel: str) -> None:
    raiz = logging.getLogger()
    raiz.setLevel(nivel.upper())
    for h in list(raiz.handlers):
        raiz.removeHandler(h)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(FormatadorJson())
    raiz.addHandler(handler)
    logging.getLogger("uvicorn.access").disabled = True  # o middleware loga a requisicao
```

Em `freshhub/api/app.py`, dentro de `criar_app`, depois de criar `app`:
```python
    @app.middleware("http")
    async def _request_id(request: Request, chamar_proximo: Callable[[Request], Awaitable[Response]]) -> Response:
        rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
        token = request_id_atual.set(rid)
        inicio = time.perf_counter()
        try:
            resposta = await chamar_proximo(request)
        finally:
            request_id_atual.reset(token)
        resposta.headers["X-Request-ID"] = rid
        logging.getLogger("freshhub.api").info(
            "requisicao", extra={"request_id": rid, "metodo": request.method, "caminho": request.url.path,
                                 "status": resposta.status_code, "ms": round((time.perf_counter() - inicio) * 1000, 1)})
        return resposta
```
com os imports `import logging, time, uuid`, `from collections.abc import Awaitable, Callable`, `from fastapi import Request, Response`, `from freshhub.log import configurar_log, request_id_atual`. (O log da requisição passa `request_id` explícito no `extra` porque o `reset` já aconteceu.)

- [ ] **Step 4: Rodar todos os testes do pacote**

```bash
cd /c/agente_stokki_eventos-fase0 && py -3.11 -m pytest -q
```
Expected: todos passando (config 5, banco 1, sessão 5, migrações 4, API 3, log 4, legado 2 = 24).

- [ ] **Step 5: Lint, tipos, camadas**

```bash
cd /c/agente_stokki_eventos-fase0 && py -3.11 -m ruff check freshhub tests && py -3.11 -m mypy && py -3.11 -m lint_imports
```
Expected: limpo.

- [ ] **Step 6: Commit**

```bash
cd /c/agente_stokki_eventos-fase0 && git add freshhub/log.py freshhub/api/app.py tests/freshhub/test_log.py && git commit -q -F - <<'EOF'
Freshhub: log em JSON com id de requisicao em toda linha

Uma linha JSON por registro em stderr (spec 5.5), extras achatados,
acento preservado (ensure_ascii=False -- o legado ja quebrou com
UnicodeEncodeError em processo sem console). Middleware gera ou aceita
X-Request-ID, devolve no cabecalho e loga metodo, caminho, status e ms.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
EOF
```

---

### Task 9: CI no GitHub Actions

**Files:**
- Create: `.github/workflows/freshhub.yml`

**Interfaces:**
- Produces: workflow `freshhub` disparado em push e pull request quando mudam `freshhub/**`, `tests/**`, `pyproject.toml`, `alembic.ini` ou o próprio workflow; jobs: `qualidade` (ruff, mypy, lint-imports, pip-audit) e `testes` (pytest com Postgres 16 como service, `FH_TEST_DATABASE_URL` apontando pra ele). Verde é pré-requisito dos planos seguintes (0.3 acrescenta deploy).

- [ ] **Step 1: Escrever o workflow**

`.github/workflows/freshhub.yml`:
```yaml
name: freshhub

on:
  push:
    paths: ["freshhub/**", "tests/**", "pyproject.toml", "alembic.ini", ".github/workflows/freshhub.yml"]
  pull_request:
    paths: ["freshhub/**", "tests/**", "pyproject.toml", "alembic.ini", ".github/workflows/freshhub.yml"]

jobs:
  qualidade:
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"   # mesma versao da VPS
          cache: pip
      - run: python -m pip install -e ".[dev]"
      - run: python -m ruff check freshhub tests
      - run: python -m mypy
      - run: python -m lint_imports
      - run: python -m pip_audit --strict --progress-spinner off

  testes:
    runs-on: ubuntu-24.04
    services:
      postgres:
        image: postgres:16
        env:
          POSTGRES_USER: freshhub
          POSTGRES_PASSWORD: ci
          POSTGRES_DB: freshhub_test
        ports: ["5432:5432"]
        options: >-
          --health-cmd "pg_isready -U freshhub -d freshhub_test"
          --health-interval 5s --health-timeout 5s --health-retries 10
    env:
      FH_TEST_DATABASE_URL: postgresql+psycopg://freshhub:ci@127.0.0.1:5432/freshhub_test
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: pip
      - run: python -m pip install -e ".[dev]"
      - run: python -m pytest -q
```

- [ ] **Step 2: Validar a sintaxe localmente**

```bash
cd /c/agente_stokki_eventos-fase0 && py -3.11 -c "import yaml,sys; d=yaml.safe_load(open('.github/workflows/freshhub.yml', encoding='utf-8')); print(sorted(d['jobs']))"
```
Expected: `['qualidade', 'testes']`.

- [ ] **Step 3: Rodar localmente a mesma sequência do job `qualidade`** (o `pip_audit` pode apontar CVE em dependência; se apontar, subir a versão mínima no `pyproject.toml` em vez de ignorar)

```bash
cd /c/agente_stokki_eventos-fase0 && py -3.11 -m ruff check freshhub tests && py -3.11 -m mypy && py -3.11 -m lint_imports && py -3.11 -m pip_audit --strict --progress-spinner off
```
Expected: tudo limpo.

- [ ] **Step 4: Commit**

```bash
cd /c/agente_stokki_eventos-fase0 && git add .github/workflows/freshhub.yml && git commit -q -F - <<'EOF'
CI: workflow freshhub com lint, tipos, camadas, auditoria e testes em Postgres 16

Primeira automacao do repo (spec 5.4, passo 1): vermelho nao entra.
Roda so quando o pacote novo, os testes ou o pyproject mudam; o legado
continua fora ate migrar. Python 3.12 como na VPS.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
EOF
```

A prova real do workflow só acontece no primeiro push (decisão do Hugo). Registrar no handoff: "CI escrito, validado em sintaxe e simulado local; prova no push".

---

### Task 10: ADRs 0010 e 0011, MAPA e índice de ADRs

**Files:**
- Create: `docs/adr/0010-postgres-gerenciado-como-banco-unico-do-sistema-novo.md`, `docs/adr/0011-orm-e-migracoes-no-pacote-novo.md`
- Modify: `docs/adr/README.md` (índice), `docs/adr/0005-sqlite-unico-sem-orm-tabelas-por-modulo.md` (status), `MAPA_DO_SISTEMA.txt` (seção 14 e seção 16)

**Interfaces:**
- Produces: decisões registradas pra quem chegar depois (spec 1.6).

- [ ] **Step 1: ADR 0010**

```markdown
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

## Alternativas consideradas
- **Continuar no SQLite**: rejeitada — transação longa trava todo mundo (incidente do WMS), sem PITR, sem usuário por papel, e o sistema novo terá API, jobs e três áreas de tela escrevendo ao mesmo tempo.
- **Postgres auto-hospedado na VPS**: rejeitada pelo Hugo — backup, upgrade e disco por conta de uma pessoa só.
- **Nuvem completa (containers + banco + storage)**: rejeitada por ora — mais peças novas do que o time comporta; a VPS com systemd é conhecida e provada.

## Consequências
- O `dados/dados.db` continua sendo o banco do legado até a Fase 4; a convivência é por sincronizador (spec 2.4).
- Custo recorrente novo (R$ 150 a 300/mês) com alerta de orçamento.
- Snapshot diário exportado pro GCS além do PITR do provedor; ensaio de restauração mensal completo.
```

- [ ] **Step 2: ADR 0011**

```markdown
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

## Alternativas consideradas
- **SQL direto como no legado**: rejeitada — sem versionamento não há como provar que hml e prod têm o mesmo esquema.
- **Migrações em SQL puro (sem Alembic)**: consideradas; rejeitadas porque o Alembic dá a ordem, o estado atual e o autogenerate como rascunho de graça.
- **ORM mais leve (SQLModel, Peewee)**: rejeitadas — SQLAlchemy 2 é o padrão do FastAPI, tem tipos (`Mapped[]`) e o Alembic é dele.

## Consequências
- O ADR-0005 passa a valer **só para o legado**; status atualizado lá.
- Regras de domínio não importam `modelos` (contrato do import-linter): a lógica não depende do ORM.
- Consulta pesada de relatório pode usar SQL textual via SQLAlchemy quando o ORM atrapalhar; o que não muda é que o esquema vem da migração.
```

- [ ] **Step 3: Atualizar ADR-0005, o índice e o MAPA**

Em `docs/adr/0005-...md`, linha de Status: `Aceito para o legado (dados/dados.db). O pacote novo freshhub/ segue ADR-0010 e ADR-0011.`

Em `docs/adr/README.md`, acrescentar as duas linhas no índice (mesmo formato das existentes).

Em `MAPA_DO_SISTEMA.txt`:
- Seção 0 (visão geral), em "O que é nosso", acrescentar: `- freshhub/: sistema novo (Fase 0 do plano mestre de 17/10/2026). API em 127.0.0.1:8100 (dev), Postgres proprio, migracoes Alembic. Ver freshhub/README.md.`
- Seção 14, entrada nova:
```
 freshhub/                 Sistema proprio (plano mestre 17/10/2026,
                           docs/superpowers/specs/2026-10-17-freshhub-sistema-proprio-design.md).
                           Camadas dominio -> servicos -> api/jobs; modelos +
                           migracoes (Alembic, alembic.ini na raiz);
                           adaptadores. Config so por FH_* (nunca config.yaml).
                           Testes em tests/ (pytest). CI: .github/workflows/freshhub.yml.
 infra/postgres-dev/       Postgres 16 em Docker na VPS pra dev/teste do
                           freshhub (tunel SSH 5433 -> 5432). Nunca producao.
 lovable/                  (ver acima) scraping do Fresh Hub do Lovable.
```
- Seção 16 (como rodar e testar), acrescentar o bloco de comandos do `freshhub/README.md` (instalar, túnel, alembic, pytest, ruff, mypy, lint_imports).

- [ ] **Step 4: Conferir que o MAPA não ficou com `freshhub/` ambíguo**

```bash
cd /c/agente_stokki_eventos-fase0 && grep -n "freshhub/" MAPA_DO_SISTEMA.txt
```
Expected: toda ocorrência se refere ao pacote novo; as do Lovable dizem `lovable/`.

- [ ] **Step 5: Commit**

```bash
cd /c/agente_stokki_eventos-fase0 && git add docs/adr/0010-postgres-gerenciado-como-banco-unico-do-sistema-novo.md docs/adr/0011-orm-e-migracoes-no-pacote-novo.md docs/adr/README.md docs/adr/0005-sqlite-unico-sem-orm-tabelas-por-modulo.md MAPA_DO_SISTEMA.txt && git commit -q -F - <<'EOF'
Docs: ADR-0010 (Postgres gerenciado) e ADR-0011 (ORM + Alembic) pro pacote novo

Decisoes do plano mestre de 17/10 registradas; ADR-0005 passa a valer
so pro legado. MAPA ganha freshhub/, infra/postgres-dev/ e os comandos
de rodar e testar o pacote novo.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
EOF
```

---

### Task 11: Verificação final do plano 0.1

**Files:** nenhum novo.

- [ ] **Step 1: Tudo verde, do zero**

Com o túnel aberto e `FH_TEST_DATABASE_URL` exportada:
```bash
cd /c/agente_stokki_eventos-fase0 && py -3.11 -m ruff check freshhub tests && py -3.11 -m mypy && py -3.11 -m lint_imports && py -3.11 -m pip_audit --strict --progress-spinner off && py -3.11 -m pytest -q
```
Expected: 4 ferramentas limpas e `24 passed`.

- [ ] **Step 2: Legado intacto**

```bash
cd /c/agente_stokki_eventos-fase0 && py -3.11 -m unittest test_embarcadores_stokki test_marcar_clientes_agenda test_notificar_whatsapp test_avisar_cliente_sem_resposta 2>&1 | tail -2 && (cd regras && py -3.11 -m unittest discover -p "test_*.py" 2>&1 | tail -2) && (cd roteirizacao && py -3.11 -m unittest discover -p "test_*.py" 2>&1 | tail -2)
```
Expected: `OK` em cada bloco, ou exatamente as mesmas falhas do checkout principal em `origin/master` (anotar no handoff).

- [ ] **Step 3: Histórico do ramo**

```bash
cd /c/agente_stokki_eventos-fase0 && git log --oneline origin/master..HEAD
```
Expected: 9 commits (Tasks 2 a 10), do mais novo pro mais velho: Docs ADRs → CI → log JSON → API saúde → Alembic → base/sessão → Postgres dev → pacote/config → Lovable rename.

- [ ] **Step 4: Handoff pro Hugo**

Relatar: ramo `freshhub-fase0` pronto no worktree, sem push; o que pedir dele: (a) push e merge quando quiser (o CI prova no push); (b) no deploy, mover `sessao_freshhub.json` na VPS (MAPA 17); (c) ler ADR-0010 e 0011. Próximo plano: **0.2 Identidade e acesso**.

---

## Self-review (feito ao escrever)

- **Cobertura da spec (seções que este plano promete):** 1.1 repo e pacote (T2, T3) · 1.3 camadas com contrato (T3, T6) · 1.6 ADRs 0010/0011 (T10; o 0012 fica pro plano 0.5 com o front) · 2.1 schemas (T5, T6) · 2.5 Alembic (T6) · 5.2 uvicorn (T7) · 5.4 passo 1 do CI (T9; passos 2 e 3 no plano 0.3) · 5.5 log estruturado (T8; métricas e alertas no 0.3) · 5.7 segredos por ambiente (T3, T4) · 7.5 lint/tipos/auditoria (T3, T9).
- **Sem placeholders:** todo passo tem comando ou código.
- **Nomes consistentes:** `carregar_config`, `Configuracao`, `ConfiguracaoInvalida`, `database_url_texto`, `criar_motor`, `criar_fabrica`, `sessao_banco`, `SCHEMAS`, `Base`, `criar_app`, `configurar_log`, `request_id_atual` usados com a mesma assinatura em T3–T8.
- **Review Focus:** 1 → T3 (`test_database_url_ausente_da_mensagem_clara`) e T6 (`test_alembic_sem_database_url_da_mensagem_clara`); 2 → T7 (`test_saude_503_sem_banco_e_sem_vazar_segredo`); 3 → T6 (`test_upgrade_duas_vezes_e_idempotente`); 4 → T8 (`test_acento_nao_quebra_e_sai_legivel`); 5 → T2 (`SESSAO_PATH` no teste + passo de deploy no MAPA).
