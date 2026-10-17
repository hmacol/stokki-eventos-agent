import os
import subprocess
import sys

import pytest

from freshhub.config import Configuracao, ConfiguracaoInvalida, carregar_config


def test_subir_api_com_url_malformada_nao_vaza_senha_nem_traceback():
    # Erro mais provavel na pratica: esquecer o +psycopg (revisao, 17/10)
    env = {k: v for k, v in os.environ.items() if not k.startswith("FH_")}
    env.update({"FH_AMBIENTE": "dev", "FH_DATABASE_URL": "postgres://freshhub:SENHA-XYZ@h/x"})
    r = subprocess.run(
        [sys.executable, "-m", "freshhub"], env=env, capture_output=True, text=True, timeout=60
    )
    assert r.returncode == 2
    assert "FH_DATABASE_URL" in r.stderr
    assert "SENHA-XYZ" not in r.stderr + r.stdout
    assert "Traceback" not in r.stderr


def test_le_variaveis_fh(monkeypatch):
    monkeypatch.setenv("FH_AMBIENTE", "teste")
    monkeypatch.setenv("FH_DATABASE_URL", "postgresql+psycopg://u:s@127.0.0.1:5433/x")
    cfg = carregar_config()
    assert isinstance(cfg, Configuracao)
    assert cfg.ambiente == "teste"
    assert cfg.database_url_texto.startswith("postgresql+psycopg://")
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
