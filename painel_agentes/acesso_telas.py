# -*- coding: utf-8 -*-
"""
Acesso personalizado por usuario (21/10, Hugo): em vez de um dos niveis
fixos, um login nominal pode ter uma lista de telas escolhida na tela
/acessos. Tela liberada vale completa: ver e todos os botoes, com o poder
do nivel total dentro dela.

Logins nominais (23/10, Hugo): moram na tabela painel_usuarios (login,
nome de exibicao, hash da senha, nivel). A tela /acessos cadastra, muda
nome e nivel e redefine senha (provisoria: a pessoa troca no 1o acesso).
Os usuarios_extras do config.yaml foram copiados uma vez pra tabela por
migrar_usuarios_extras.py e deixaram de valer. Os pares fixos (usuario,
usuario_operador...) continuam so no config.

Como funciona:
- A lista mora na tabela painel_acesso_telas (dados/dados.db). Usuario sem
  linha segue o nivel dele, como sempre. Apagar a linha devolve o nivel.
- requer_auth (painel_agentes.py) le nivel e lista a cada request (muda na
  hora, sem novo login) e libera a rota se o caminho cair numa das telas.
- Cada tela abaixo lista os caminhos (regex sobre request.path, sem o
  prefixo /painel) da pagina e das APIs que ela chama. Rota nova no painel
  precisa entrar numa tela daqui, senao o usuario personalizado leva 403:
  o teste test_acesso_telas confere isso pra toda rota do app.
- A chave de cada tela e o endpoint da pagina (o mesmo 'rota' do menu em
  _menu_lateral_nav.html), que serve de link e de filtro do menu.
"""
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from werkzeug.security import check_password_hash, generate_password_hash

DB_PATH = Path(__file__).parent.parent / "dados" / "dados.db"

# "logistica" (19/10, Hugo) so existe pra login nominal: poderes do operador,
# mas so nas telas da logistica (as rotas com "logistica" no niveis=).
NIVEIS = ("total", "operador", "leitura", "expedicao", "galpao", "atendimento", "logistica")
_PADRAO_LOGIN = re.compile(r"^[a-z0-9._-]{2,40}$")
SENHA_MINIMA = 6

# (chave = endpoint da pagina, rotulo, grupo do menu, caminhos)
TELAS = [
    ("inicio", "Início", "", [r"^/inicio$", r"^/api/inicio/"]),
    ("torre", "Torre de Controle", "Acompanhar", [r"^/torre(/|$)", r"^/api/torre/"]),
    ("vigia_pedidos", "Vigia", "Acompanhar", [r"^/vigia$", r"^/api/batimento/"]),
    ("canhotos_pendentes", "Canhotos pendentes", "Acompanhar", [r"^/canhotos-pendentes$", r"^/api/canhotos-pendentes$"]),
    ("pedidos_parados", "Pedidos Parados", "Acompanhar", [r"^/pedidos-parados$", r"^/api/pedidos-parados/"]),
    ("historico_tratativas", "Tratativas", "Acompanhar", [r"^/historico-tratativas$"]),
    ("consulta", "Consulta", "Acompanhar", [r"^/consulta(/|$)", r"^/canhotos/\d+/foto$",
                                            r"^/financeiro/pedagios/\d+/foto$"]),
    ("baixa_sem_app", "Baixa sem app", "Acompanhar", [r"^/baixa-sem-app$", r"^/api/baixa-sem-app(/|$)"]),
    ("retornos_tela", "Retornos", "Operar", [r"^/retornos(/|$)", r"^/api/retornos(/|$)"]),
    ("planejamento", "Planejamento", "Planejar", [r"^/planejamento(/|$)", r"^/api/planejamento/",
                                                  r"^/api/torre/marcar-entregue$"]),
    ("mapa_rotas", "Mapa", "Planejar", [r"^/mapa-rotas$"]),
    ("laboratorio_rotas", "Laboratório", "Planejar", [r"^/laboratorio-rotas$"]),
    ("expedicao", "Expedição", "Operar", [r"^/expedicao$", r"^/api/expedicao/(romaneio|excluir-pedido)(/|$)"]),
    ("expedicao_consulta", "Consultar pedido", "Operar", [r"^/expedicao/consulta$", r"^/api/expedicao/consulta$"]),
    ("wms", "Galpão", "Operar", [r"^/wms(?!/estoque$)(/|$)", r"^/api/wms/"]),
    ("wms_estoque", "Estoque", "Operar", [r"^/wms/estoque$", r"^/api/wms/(pedidos|pendencias|reservas-antigas)(/|$)",
                                          r"^/api/wms/produtos(/\d+/saldo)?$"]),
    ("atendimento", "Atendimento", "Operar", [r"^/atendimento(/|$)", r"^/api/atendimento/"]),
    ("adesao_portal", "Adesão ao portal", "Operar", [r"^/adesao-portal$"]),
    ("motoristas", "Motoristas", "Motoristas", [r"^/motoristas$", r"^/api/motoristas(/|$)"]),
    ("pedagios", "Pedágios", "Motoristas", [r"^/financeiro/pedagios(/|$)", r"^/api/financeiro/pedagios/"]),
    ("canhotos", "Canhotos", "Motoristas", [r"^/canhotos(/|$)", r"^/api/canhotos/"]),
    ("index", "Agentes", "Sistema", [r"^/$", r"^/rodar/", r"^/execucao/", r"^/api/agentes/progresso$",
                                     r"^/encerrar-tudo$"]),
    ("historico", "Execuções", "Sistema", [r"^/historico$", r"^/execucao/"]),
    ("clientes_agenda", "Clientes AGENDA", "Sistema", [r"^/clientes-agenda$", r"^/api/clientes-agenda/"]),
    ("transportadoras", "Transportadoras", "Sistema", [r"^/transportadoras$", r"^/api/transportadoras(/|$)"]),
]
PAGINAS = [t[0] for t in TELAS]

# Acoes so do total que moram dentro de uma tela comum: liberar a tela nao
# libera a acao (revisao de seguranca 21/10 -- liberar o Inicio dava
# aprovar embarcador sem ninguem perceber). Sem pagina propria.
PERMISSOES = [
    ("aprovar_embarcadores", "Aprovar embarcadores", "Ações", [r"^/api/cadastro-embarcador/"]),
]
CHAVES = PAGINAS + [p[0] for p in PERMISSOES]
_PADROES = {chave: [re.compile(p) for p in caminhos] for chave, _, _, caminhos in TELAS + PERMISSOES}

# Usadas por qualquer tela (badges do menu, deslogar por inatividade).
CAMINHOS_COMUNS = [re.compile(r"^/api/sidebar/contadores$"), re.compile(r"^/api/sessao/(atividade|estado)$")]


def caminho_liberado(caminho: str, telas) -> bool:
    if any(p.match(caminho) for p in CAMINHOS_COMUNS):
        return True
    return any(p.match(caminho) for chave in telas for p in _PADROES.get(chave, ()))


def conectar(caminho=None) -> sqlite3.Connection:
    conn = sqlite3.connect(caminho or DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS painel_acesso_telas (
            usuario        TEXT PRIMARY KEY,
            telas          TEXT NOT NULL,
            atualizado_por TEXT,
            atualizado_em  TEXT NOT NULL
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS painel_usuarios (
            usuario        TEXT PRIMARY KEY,
            nome           TEXT NOT NULL DEFAULT '',
            senha_hash     TEXT NOT NULL,
            nivel          TEXT NOT NULL,
            trocar_senha   INTEGER NOT NULL DEFAULT 0,
            criado_por     TEXT,
            criado_em      TEXT NOT NULL,
            atualizado_por TEXT,
            atualizado_em  TEXT,
            versao_senha   INTEGER NOT NULL DEFAULT 0,
            ativo          INTEGER NOT NULL DEFAULT 1
        )""")
    # Tabela criada antes dessas colunas (deploys de 23/10).
    colunas = {r["name"] for r in conn.execute("PRAGMA table_info(painel_usuarios)")}
    if "versao_senha" not in colunas:
        conn.execute("ALTER TABLE painel_usuarios ADD COLUMN versao_senha INTEGER NOT NULL DEFAULT 0")
    if "ativo" not in colunas:
        conn.execute("ALTER TABLE painel_usuarios ADD COLUMN ativo INTEGER NOT NULL DEFAULT 1")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS painel_sessoes_rodada (
            id     INTEGER PRIMARY KEY CHECK (id = 1),
            rodada INTEGER NOT NULL,
            por    TEXT,
            em     TEXT NOT NULL
        )""")
    return conn


def _agora() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _telas_da_linha(r):
    return [c for c in json.loads(r["telas"]) if c in CHAVES] if r["telas"] is not None else None


def conta(usuario: str, conn=None):
    """Login nominal com nivel, nome, trocar_senha e telas (None = segue o
    nivel), ou None quando nao e login nominal (par fixo do config)."""
    if not usuario:
        return None
    proprio = conn is None
    conn = conn or conectar()
    try:
        r = conn.execute(
            "SELECT u.usuario, u.nome, u.nivel, u.trocar_senha, u.senha_hash, u.versao_senha, u.ativo, t.telas "
            "FROM painel_usuarios u "
            "LEFT JOIN painel_acesso_telas t ON t.usuario = u.usuario WHERE u.usuario = ?", (usuario,)).fetchone()
    finally:
        if proprio:
            conn.close()
    if r is None:
        return None
    return {"usuario": r["usuario"], "nome": r["nome"], "nivel": r["nivel"], "trocar_senha": bool(r["trocar_senha"]),
            "senha_hash": r["senha_hash"], "versao_senha": r["versao_senha"], "ativo": bool(r["ativo"]),
            "telas": _telas_da_linha(r)}


# Login inexistente confere contra este hash, pra responder no mesmo tempo
# que senha errada (nao entrega quais logins existem).
_HASH_FICTICIO = generate_password_hash("login-inexistente")


def conferir_senha(usuario: str, senha: str, conn=None):
    """Conta do login nominal se a senha bate e ele esta ativo, senao None."""
    c = conta(usuario, conn)
    if not check_password_hash(c["senha_hash"] if c else _HASH_FICTICIO, senha or "") or c is None \
            or not c["ativo"]:
        return None
    return c


def telas_do_usuario(usuario: str, conn=None):
    """Lista de telas do usuario, ou None quando ele segue o nivel. So
    login nominal pode ser personalizado: os pares fixos (total,
    operador...) nunca."""
    c = conta(usuario, conn)
    return c["telas"] if c else None


def listar(conn) -> list[dict]:
    """Um item por login nominal, em ordem de nome."""
    resultado = []
    for r in conn.execute(
            "SELECT u.*, t.telas, t.atualizado_por AS telas_por, t.atualizado_em AS telas_em FROM painel_usuarios u "
            "LEFT JOIN painel_acesso_telas t ON t.usuario = u.usuario "
            "ORDER BY u.ativo DESC, lower(COALESCE(NULLIF(u.nome, ''), u.usuario))"):
        ultimo = max((r["atualizado_em"] or "", r["atualizado_por"] or ""), (r["telas_em"] or "", r["telas_por"] or ""))
        resultado.append({
            "usuario": r["usuario"],
            "nome": r["nome"],
            "nivel": r["nivel"],
            "trocar_senha": bool(r["trocar_senha"]),
            "ativo": bool(r["ativo"]),
            "telas": _telas_da_linha(r),
            "atualizado_por": ultimo[1] or None,
            "atualizado_em": ultimo[0] or None,
        })
    return resultado


def _validar_senha(senha: str) -> None:
    if len(senha or "") < SENHA_MINIMA:
        raise ValueError(f"A senha precisa ter pelo menos {SENHA_MINIMA} caracteres.")


def _validar_nivel(nivel: str) -> None:
    if nivel not in NIVEIS:
        raise ValueError(f"Nível desconhecido: {nivel}")


def criar(conn, usuario: str, nome: str, nivel: str, senha: str, por: str, logins_fixos) -> str:
    """Login novo com senha provisoria: a pessoa troca no 1o acesso.
    logins_fixos = os pares do config (usuario, usuario_operador...), que
    nao podem ser repetidos. Devolve o login normalizado."""
    usuario = (usuario or "").strip().lower()
    if not _PADRAO_LOGIN.match(usuario):
        raise ValueError("Login inválido: use de 2 a 40 letras minúsculas, números, ponto, hífen ou _.")
    if usuario in logins_fixos or conta(usuario, conn):
        raise ValueError(f"Já existe o login {usuario}.")
    _validar_nivel(nivel)
    _validar_senha(senha)
    conn.execute(
        "INSERT INTO painel_usuarios (usuario, nome, senha_hash, nivel, trocar_senha, criado_por, criado_em) "
        "VALUES (?, ?, ?, ?, 1, ?, ?)",
        (usuario, (nome or "").strip(), generate_password_hash(senha), nivel, por, _agora()))
    conn.commit()
    return usuario


def _exigir_outro(conn, usuario: str, por: str) -> None:
    if not conta(usuario, conn):
        raise ValueError("Login nominal não encontrado.")
    if usuario == por:
        raise ValueError("Você não pode mudar o seu próprio acesso.")


def editar(conn, usuario: str, nome: str, nivel: str, por: str) -> None:
    _exigir_outro(conn, usuario, por)
    _validar_nivel(nivel)
    conn.execute("UPDATE painel_usuarios SET nome = ?, nivel = ?, atualizado_por = ?, atualizado_em = ? WHERE usuario = ?",
                 ((nome or "").strip(), nivel, por, _agora(), usuario))
    conn.commit()


def mudar_ativo(conn, usuario: str, ativo: bool, por: str) -> None:
    """Desativado nao entra e a sessao aberta cai no proximo clique
    (requer_auth). Nao apaga: o login segue no historico e nao e reusado.
    Desativar sobe a versao_senha: reativar depois nao ressuscita sessao velha."""
    _exigir_outro(conn, usuario, por)
    conn.execute("UPDATE painel_usuarios SET ativo = ?, versao_senha = versao_senha + ?, atualizado_por = ?, "
                 "atualizado_em = ? WHERE usuario = ?", (1 if ativo else 0, 0 if ativo else 1, por, _agora(), usuario))
    conn.commit()


def redefinir_senha(conn, usuario: str, senha: str, por: str) -> None:
    """Senha provisoria dada por outro total: troca obrigatoria no proximo
    acesso. Sobe a versao_senha: sessao aberta antes cai pro login."""
    _exigir_outro(conn, usuario, por)
    _validar_senha(senha)
    conn.execute("UPDATE painel_usuarios SET senha_hash = ?, trocar_senha = 1, versao_senha = versao_senha + 1, "
                 "atualizado_por = ?, atualizado_em = ? WHERE usuario = ?",
                 (generate_password_hash(senha), por, _agora(), usuario))
    conn.commit()


def trocar_senha(conn, usuario: str, nova: str, versao_sessao: int):
    """A propria pessoa troca a provisoria pela dela. Devolve a versao_senha
    nova, que a sessao de quem trocou passa a carregar, ou None quando a
    senha foi redefinida de novo nesse meio tempo (a sessao nao vale mais)."""
    c = conta(usuario, conn)
    if c is None:
        raise ValueError("Login nominal não encontrado.")
    _validar_senha(nova)
    if check_password_hash(c["senha_hash"], nova):
        raise ValueError("A senha nova precisa ser diferente da provisória.")
    cur = conn.execute("UPDATE painel_usuarios SET senha_hash = ?, trocar_senha = 0, versao_senha = versao_senha + 1 "
                       "WHERE usuario = ? AND versao_senha = ? AND trocar_senha = 1",
                       (generate_password_hash(nova), usuario, versao_sessao))
    conn.commit()
    return versao_sessao + 1 if cur.rowcount else None


def rodada_sessoes(conn=None) -> dict:
    """Rodada atual das sessoes do painel (24/10): cada sessao nasce com a
    rodada do login e cai quando ela muda. Sem linha = rodada 0, que e
    tambem o que vale pra sessao sem o numero."""
    proprio = conn is None
    conn = conn or conectar()
    try:
        r = conn.execute("SELECT rodada, por, em FROM painel_sessoes_rodada WHERE id = 1").fetchone()
    finally:
        if proprio:
            conn.close()
    return dict(r) if r else {"rodada": 0, "por": None, "em": None}


def derrubar_sessoes(conn, por: str) -> int:
    """Derruba todas as sessoes abertas do painel, de qualquer login (pares
    fixos, nominais, galpao e expedicao): todo mundo loga de novo."""
    conn.execute(
        "INSERT INTO painel_sessoes_rodada (id, rodada, por, em) VALUES (1, 1, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET rodada = rodada + 1, por = excluded.por, em = excluded.em",
        (por, _agora()))
    conn.commit()
    return rodada_sessoes(conn)["rodada"]


def importar_do_config(conn, cfg_painel: dict) -> list[str]:
    """Copia os usuarios_extras do config pra tabela, com a mesma senha
    (sem troca obrigatoria). Quem ja existe na tabela fica como esta."""
    importados = []
    for extra in cfg_painel.get("usuarios_extras") or []:
        u, s, n = extra.get("usuario"), extra.get("senha"), extra.get("nivel")
        if not (u and s and n in NIVEIS) or conta(u, conn):
            continue
        conn.execute(
            "INSERT INTO painel_usuarios (usuario, nome, senha_hash, nivel, trocar_senha, criado_por, criado_em) "
            "VALUES (?, '', ?, ?, 0, 'config.yaml', ?)", (u, generate_password_hash(str(s)), n, _agora()))
        importados.append(u)
    conn.commit()
    return importados


def salvar(conn, usuario: str, telas, por: str) -> None:
    """telas=None apaga a linha (volta ao nivel). Lista vazia vale: o
    usuario fica sem tela nenhuma (nao consegue entrar)."""
    _exigir_outro(conn, usuario, por)
    if telas is None:
        conn.execute("DELETE FROM painel_acesso_telas WHERE usuario = ?", (usuario,))
    else:
        desconhecidas = [t for t in telas if t not in CHAVES]
        if desconhecidas:
            raise ValueError(f"Tela desconhecida: {', '.join(desconhecidas)}")
        ordenadas = [c for c in CHAVES if c in telas]
        conn.execute(
            "INSERT INTO painel_acesso_telas (usuario, telas, atualizado_por, atualizado_em) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(usuario) DO UPDATE SET telas = excluded.telas, atualizado_por = excluded.atualizado_por, "
            "atualizado_em = excluded.atualizado_em",
            (usuario, json.dumps(ordenadas), por, _agora()))
    conn.commit()
