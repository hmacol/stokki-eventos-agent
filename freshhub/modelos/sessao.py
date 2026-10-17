"""Fabrica de motor e sessao. Unico lugar que recebe a URL do banco."""
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

PREFIXO_URL = "postgresql+psycopg://"


def criar_motor(url: str) -> Engine:
    if not url.startswith(PREFIXO_URL):
        raise ValueError(f"URL do banco precisa comecar com {PREFIXO_URL}")
    return create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=5)


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
