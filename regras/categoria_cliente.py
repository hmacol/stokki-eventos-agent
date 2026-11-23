# -*- coding: utf-8 -*-
"""
regras/categoria_cliente.py

Categoria do DESTINATÁRIO a partir do CNAE da Receita (pedido do Hugo,
29/09): restaurante, comércio pequeno, supermercado, atacado, pessoa
física ou outros. Serve de base pra relatórios, tempo de parada na
roteirização e nível de dificuldade.

Regra:
  - documento que é CPF (11 dígitos, ou 14 com zeros à esquerda -- é
    assim que ~40% da tabela `clientes` guarda pessoa física) vira
    "pessoa_fisica" direto, sem consulta;
  - CNPJ: classifica pelo CNAE principal; se o principal cair em
    "outros", usa o primeiro CNAE secundário que tenha categoria
    (origem "cnae_secundario", pra ficar fácil de revisar);
  - correção manual (origem "manual") nunca é sobrescrita pela carga.

Guardado na tabela `clientes_cnae`, por documento só com dígitos (mesma
chave da tabela `clientes` e da planilha de complexidade). Quem consulta
a Receita e preenche é classificar_clientes_cnae.py (raiz).
"""
import json
import sqlite3

CATEGORIAS = (
    "restaurante", "comercio_pequeno", "supermercado", "atacado",
    "pessoa_fisica", "outros",
)

# Prefixo do CNAE (7 dígitos, sem máscara) -> categoria. O primeiro
# prefixo que casar vence.
_PREFIXOS = (
    ("561", "restaurante"),         # restaurantes, lanchonetes, bares, ambulantes
    ("562", "restaurante"),         # bufê, cozinha industrial, cantina
    ("4711", "supermercado"),       # hipermercados e supermercados
    ("4712", "comercio_pequeno"),   # minimercado, mercearia, armazém
    ("472", "comercio_pequeno"),    # padaria, açougue, bebidas, hortifrúti, outros alimentos
    ("10911", "comercio_pequeno"),  # padaria com fabricação própria
    ("463", "atacado"),             # atacado de alimentos e bebidas
    ("4691", "atacado"),            # atacado em geral com predominância de alimentos
)


def _so_digitos(s) -> str:
    return "".join(c for c in str(s or "") if c.isdigit())


def _cnpj_valido(d: str) -> bool:
    if len(d) != 14 or len(set(d)) == 1:
        return False

    def _dv(base, pesos):
        resto = sum(int(x) * p for x, p in zip(base, pesos)) % 11
        return "0" if resto < 2 else str(11 - resto)

    pesos1 = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    return d[12] == _dv(d[:12], pesos1) and d[13] == _dv(d[:13], [6] + pesos1)


def _cpf_valido(d: str) -> bool:
    if len(d) != 11 or len(set(d)) == 1:
        return False
    for n in (9, 10):
        dv = sum(int(d[i]) * (n + 1 - i) for i in range(n)) * 10 % 11 % 10
        if dv != int(d[n]):
            return False
    return True


def tipo_documento(documento) -> str:
    """'cnpj', 'cpf', 'ambiguo' ou 'invalido'. CPF guardado com 14
    dígitos (zeros à esquerda) conta como CPF.

    'ambiguo' = fecha a conta como CNPJ E como CPF com zeros à esquerda.
    Quase sempre é CPF (71 casos em 29/09, a Receita respondeu 404 pra
    todos os consultados), mas existe empresa de verdade nessa situação
    (00.000.000/0001-91, Banco do Brasil): só a consulta decide."""
    d = _so_digitos(documento)
    if _cpf_valido(d):
        return "cpf"
    cpf_com_zeros = len(d) == 14 and d.startswith("000") and _cpf_valido(d[3:])
    if _cnpj_valido(d):
        return "ambiguo" if cpf_com_zeros else "cnpj"
    return "cpf" if cpf_com_zeros else "invalido"


def _categoria_de_um_cnae(cnae) -> str | None:
    d = _so_digitos(cnae)
    if not d:
        return None
    d = d.zfill(7)
    for prefixo, categoria in _PREFIXOS:
        if d.startswith(prefixo):
            return categoria
    return None


def categoria_por_cnae(principal, secundarios=()) -> tuple[str, str]:
    """(categoria, origem), origem = 'cnae_principal' ou 'cnae_secundario'."""
    categoria = _categoria_de_um_cnae(principal)
    if categoria:
        return categoria, "cnae_principal"
    for secundario in secundarios or ():
        categoria = _categoria_de_um_cnae(secundario)
        if categoria:
            return categoria, "cnae_secundario"
    return "outros", "cnae_principal"


# ---------------------------------------------------------------------
# Tabela clientes_cnae
# ---------------------------------------------------------------------

def criar_tabela(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS clientes_cnae (
            documento          TEXT PRIMARY KEY,
            categoria          TEXT,
            origem             TEXT,
            cnae               TEXT,
            cnae_descricao     TEXT,
            cnaes_secundarios  TEXT,
            porte              TEXT,
            situacao           TEXT,
            erro               TEXT,
            atualizado_em      TEXT NOT NULL
        )
    """)
    conn.commit()


def gravar_consulta(conn: sqlite3.Connection, documento: str, resposta: dict) -> tuple[str, str]:
    """Grava o retorno da BrasilAPI (/api/cnpj/v1) e a categoria calculada.
    Linha com origem 'manual' mantém a categoria; só o dado bruto atualiza."""
    doc = _so_digitos(documento)
    secundarios = [s.get("codigo") for s in (resposta.get("cnaes_secundarios") or []) if s.get("codigo")]
    categoria, origem = categoria_por_cnae(resposta.get("cnae_fiscal"), secundarios)
    conn.execute("""
        INSERT INTO clientes_cnae (
            documento, categoria, origem, cnae, cnae_descricao,
            cnaes_secundarios, porte, situacao, erro, atualizado_em
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, datetime('now', 'localtime'))
        ON CONFLICT(documento) DO UPDATE SET
            categoria = CASE WHEN clientes_cnae.origem = 'manual'
                             THEN clientes_cnae.categoria ELSE excluded.categoria END,
            origem = CASE WHEN clientes_cnae.origem = 'manual'
                          THEN 'manual' ELSE excluded.origem END,
            cnae = excluded.cnae,
            cnae_descricao = excluded.cnae_descricao,
            cnaes_secundarios = excluded.cnaes_secundarios,
            porte = excluded.porte,
            situacao = excluded.situacao,
            erro = NULL,
            atualizado_em = excluded.atualizado_em
    """, (
        doc, categoria, origem, _so_digitos(resposta.get("cnae_fiscal")) or None,
        resposta.get("cnae_fiscal_descricao"), json.dumps(secundarios),
        resposta.get("porte"), resposta.get("descricao_situacao_cadastral"),
    ))
    conn.commit()
    return categoria, origem


def gravar_pessoa_fisica(conn: sqlite3.Connection, documento: str) -> None:
    conn.execute("""
        INSERT INTO clientes_cnae (documento, categoria, origem, atualizado_em)
        VALUES (?, 'pessoa_fisica', 'documento', datetime('now', 'localtime'))
        ON CONFLICT(documento) DO NOTHING
    """, (_so_digitos(documento),))
    conn.commit()


def gravar_erro(conn: sqlite3.Connection, documento: str, erro: str) -> None:
    """Consulta que falhou (404 na base, CNPJ inválido): fica sem
    categoria e entra de novo na próxima carga."""
    conn.execute("""
        INSERT INTO clientes_cnae (documento, erro, atualizado_em)
        VALUES (?, ?, datetime('now', 'localtime'))
        ON CONFLICT(documento) DO UPDATE SET
            erro = excluded.erro, atualizado_em = excluded.atualizado_em
    """, (_so_digitos(documento), erro))
    conn.commit()


def definir_categoria_manual(conn: sqlite3.Connection, documento: str, categoria: str) -> None:
    doc = _so_digitos(documento)
    if not doc:
        raise ValueError("Documento (CNPJ/CPF) vazio.")
    if categoria not in CATEGORIAS:
        raise ValueError(f"Categoria inválida: {categoria!r} (válidas: {', '.join(CATEGORIAS)}).")
    conn.execute("""
        INSERT INTO clientes_cnae (documento, categoria, origem, atualizado_em)
        VALUES (?, ?, 'manual', datetime('now', 'localtime'))
        ON CONFLICT(documento) DO UPDATE SET
            categoria = excluded.categoria, origem = 'manual',
            atualizado_em = excluded.atualizado_em
    """, (doc, categoria))
    conn.commit()


def documentos_ja_resolvidos(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT documento FROM clientes_cnae WHERE categoria IS NOT NULL")}


def carregar_categorias(conn: sqlite3.Connection) -> dict[str, str]:
    """{documento_só_dígitos: categoria} de quem já foi classificado."""
    return {r[0]: r[1] for r in conn.execute(
        "SELECT documento, categoria FROM clientes_cnae WHERE categoria IS NOT NULL")}
