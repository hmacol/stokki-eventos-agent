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
