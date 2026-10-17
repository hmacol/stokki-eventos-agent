"""A fixture de banco faz downgrade base (DROP SCHEMA CASCADE): so pode
apontar pra banco cujo nome termina em _test (revisao, 17/10)."""
from tests.conftest import banco_e_de_teste


def test_aceita_banco_com_sufixo_test():
    assert banco_e_de_teste("postgresql+psycopg://u:s@127.0.0.1:5433/freshhub_test")


def test_recusa_dev_hml_e_prod():
    for nome in ("freshhub_dev", "freshhub", "freshhub_hml", "freshhub_prod", "freshhub_test2"):
        assert not banco_e_de_teste(f"postgresql+psycopg://u:s@h/{nome}"), nome


def test_recusa_url_sem_nome_de_banco():
    assert not banco_e_de_teste("postgresql+psycopg://u:s@h")
