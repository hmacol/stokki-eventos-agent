"""Configuracao por variaveis de ambiente (prefixo FH_). O pacote novo
nunca le o arquivo de segredos do legado (spec 5.7)."""
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
            return Configuracao(_env_file=arquivo)
        return Configuracao()
    except ValidationError as e:
        faltando = [f"FH_{str(err['loc'][0]).upper()}: {err['msg']}" for err in e.errors()]
        raise ConfiguracaoInvalida("Configuracao invalida -- " + "; ".join(faltando)) from e
