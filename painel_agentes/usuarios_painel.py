# -*- coding: utf-8 -*-
"""
painel_agentes/usuarios_painel.py

Usuários do painel cadastrados pela tela /usuarios (pedido do Hugo,
29/09/2026): cada pessoa tem login próprio e, pra CADA tela do painel,
um de três acessos -- "total" (vê e age), "leitura" (só vê) ou sem
acesso (nem aparece no menu).

Convive com os logins fixos do config.yaml (total, operador, leitura,
expedicao, galpao, atendimento): esses continuam funcionando igual, com
o `niveis=` de cada rota. Usuário daqui entra na sessão com nivel_acesso
"personalizado" e o requer_auth troca a checagem de nível pela checagem
de tela (ver acesso_da_requisicao).

REGRA DE ACESSO (uma só, pra todas as rotas):
  - cada rota pertence a uma ou mais telas (PAGINAS[*].caminhos/endpoints);
  - GET/HEAD precisa de "leitura" ou "total" na tela;
  - qualquer outro método (POST/DELETE...) precisa de "total";
  - rota que não pertence a nenhuma tela é negada (só login fixo total).
Rota nova no painel: inclua o caminho na tela certa aqui, senão o
usuário cadastrado leva 403 nela.

Permissões são relidas do banco a cada requisição: mudar o acesso ou
desativar alguém vale no próximo clique, sem esperar a sessão expirar.
Trocar senha, login ou desativar incrementa `versao` e derruba as
sessões abertas daquele usuário.
"""
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from werkzeug.security import check_password_hash, generate_password_hash

_RAIZ = Path(__file__).parent.parent
DB_PATH = _RAIZ / "dados" / "dados.db"

ACESSOS = ("total", "leitura")
ROTULO_ACESSO = {"total": "Acesso total", "leitura": "Somente leitura", "": "Sem acesso"}

SENHA_MINIMA = 8
_PADRAO_LOGIN = re.compile(r"^[a-z0-9][a-z0-9._@-]{2,39}$")

# Catálogo das telas, na ordem e nos grupos do menu lateral
# (_menu_lateral_nav.html usa a mesma `chave` no campo 'pagina').
# caminhos: prefixo de URL (casa o próprio caminho e tudo abaixo dele,
#           "/" casa só a raiz). endpoints: nomes de rota do Flask,
#           pra quando o caminho sozinho não separa as telas (WMS).
# inicial: endpoint aberto depois do login, se for a primeira tela liberada.
PAGINAS = [
    {"chave": "torre", "rotulo": "Torre de Controle", "grupo": "Acompanhar", "inicial": "torre",
     "caminhos": ("/torre", "/api/torre")},
    {"chave": "vigia", "rotulo": "Vigia", "grupo": "Acompanhar", "inicial": "vigia_pedidos",
     "caminhos": ("/vigia",)},
    {"chave": "pedidos_parados", "rotulo": "Pedidos Parados", "grupo": "Acompanhar", "inicial": "pedidos_parados",
     "caminhos": ("/pedidos-parados", "/api/pedidos-parados")},
    {"chave": "tratativas", "rotulo": "Tratativas", "grupo": "Acompanhar", "inicial": "historico_tratativas",
     "caminhos": ("/historico-tratativas",)},
    # A consulta de rota/pedido mostra as fotos de canhoto e pedágio.
    {"chave": "consulta", "rotulo": "Consulta", "grupo": "Acompanhar", "inicial": "consulta",
     "caminhos": ("/consulta",), "endpoints": ("canhoto_foto", "pedagio_foto")},
    {"chave": "planejamento", "rotulo": "Planejamento", "grupo": "Planejar", "inicial": "planejamento",
     "caminhos": ("/planejamento", "/api/planejamento")},
    {"chave": "mapa", "rotulo": "Mapa", "grupo": "Planejar", "inicial": "mapa_rotas",
     "caminhos": ("/mapa-rotas",)},
    {"chave": "laboratorio", "rotulo": "Laboratório", "grupo": "Planejar", "inicial": "laboratorio_rotas",
     "caminhos": ("/laboratorio-rotas",)},
    {"chave": "expedicao", "rotulo": "Expedição", "grupo": "Operar", "inicial": "expedicao",
     "caminhos": ("/expedicao", "/api/expedicao")},
    {"chave": "galpao", "rotulo": "Galpão", "grupo": "Operar", "inicial": "wms",
     "caminhos": ("/api/wms",), "endpoints": ("wms", "wms_etiquetas_pdf", "wms_etiqueta_produto_pdf")},
    # /wms/estoque usa só estas APIs do WMS (ver wms_estoque.html).
    {"chave": "estoque", "rotulo": "Estoque", "grupo": "Operar", "inicial": "wms_estoque",
     "caminhos": (), "endpoints": ("wms_estoque", "api_wms_pedidos", "api_wms_reservas_antigas",
                                   "api_wms_liberar_reservas", "api_wms_pendencias", "api_wms_produtos",
                                   "api_wms_produto_saldo")},
    {"chave": "atendimento", "rotulo": "Atendimento", "grupo": "Operar", "inicial": "atendimento",
     "caminhos": ("/atendimento", "/api/atendimento")},
    {"chave": "motoristas", "rotulo": "Motoristas", "grupo": "Motoristas", "inicial": "motoristas",
     "caminhos": ("/motoristas", "/api/motoristas")},
    {"chave": "pedagios", "rotulo": "Pedágios", "grupo": "Motoristas", "inicial": "pedagios",
     "caminhos": ("/financeiro/pedagios", "/api/financeiro/pedagios")},
    {"chave": "canhotos", "rotulo": "Canhotos", "grupo": "Motoristas", "inicial": "canhotos",
     "caminhos": ("/canhotos", "/api/canhotos")},
    {"chave": "agentes", "rotulo": "Agentes", "grupo": "Sistema", "inicial": "index",
     "caminhos": ("/", "/rodar", "/execucao", "/api/agentes", "/encerrar-tudo")},
    {"chave": "execucoes", "rotulo": "Execuções", "grupo": "Sistema", "inicial": "historico",
     "caminhos": ("/historico", "/execucao")},
    # Quem tem "total" aqui cadastra usuários e dá qualquer acesso --
    # inclusive a si mesmo. Dar só pra quem já é administrador.
    {"chave": "usuarios", "rotulo": "Usuários", "grupo": "Sistema", "inicial": "usuarios",
     "caminhos": ("/usuarios",)},
]
CHAVES_PAGINAS = tuple(p["chave"] for p in PAGINAS)

# Rotas que qualquer usuário logado usa, seja qual for a tela
# (o badge do menu já filtra pelo que a pessoa pode ver).
ENDPOINTS_LIVRES = ("api_sidebar_contadores",)

# Badge do menu -> tela de que ele depende (contadores_menu.py).
PAGINA_DO_CONTADOR = {
    "torre": "torre", "pedidos_parados": "pedidos_parados", "pedagios": "pedagios",
    "canhotos": "canhotos", "atendimento": "atendimento",
}

_DDL = """
CREATE TABLE IF NOT EXISTS painel_usuarios (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    usuario       TEXT NOT NULL UNIQUE,
    nome          TEXT NOT NULL,
    senha_hash    TEXT NOT NULL,
    ativo         INTEGER NOT NULL DEFAULT 1,
    versao        INTEGER NOT NULL DEFAULT 1,
    criado_em     TEXT NOT NULL,
    criado_por    TEXT,
    atualizado_em TEXT,
    atualizado_por TEXT,
    ultimo_login  TEXT
);
CREATE TABLE IF NOT EXISTS painel_usuario_permissoes (
    usuario_id INTEGER NOT NULL REFERENCES painel_usuarios(id) ON DELETE CASCADE,
    pagina     TEXT NOT NULL,
    acesso     TEXT NOT NULL CHECK (acesso IN ('total', 'leitura')),
    PRIMARY KEY (usuario_id, pagina)
);
"""


class ErroUsuario(ValueError):
    """Dado inválido no cadastro -- a mensagem vai direto pra tela."""


def conectar(caminho: Path | None = None) -> sqlite3.Connection:
    caminho = Path(caminho) if caminho else DB_PATH
    caminho.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(caminho, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_DDL)
    return conn


def _agora() -> str:
    return datetime.now().isoformat(timespec="seconds")


def normalizar_login(usuario: str) -> str:
    return (usuario or "").strip().lower()


# ── Leitura ───────────────────────────────────────────────────────────────────

def _permissoes(conn, usuario_id: int) -> dict:
    linhas = conn.execute("SELECT pagina, acesso FROM painel_usuario_permissoes WHERE usuario_id = ?",
                          (usuario_id,)).fetchall()
    return {l["pagina"]: l["acesso"] for l in linhas if l["pagina"] in CHAVES_PAGINAS and l["acesso"] in ACESSOS}


def _montar(conn, linha) -> dict | None:
    if linha is None:
        return None
    u = dict(linha)
    u.pop("senha_hash", None)
    u["ativo"] = bool(u["ativo"])
    u["permissoes"] = _permissoes(conn, u["id"])
    return u


def buscar_usuario(conn, usuario_id: int) -> dict | None:
    return _montar(conn, conn.execute("SELECT * FROM painel_usuarios WHERE id = ?", (usuario_id,)).fetchone())


def listar_usuarios(conn) -> list[dict]:
    linhas = conn.execute("SELECT * FROM painel_usuarios ORDER BY ativo DESC, nome COLLATE NOCASE").fetchall()
    return [_montar(conn, l) for l in linhas]


def autenticar(conn, usuario: str, senha: str) -> dict | None:
    """Usuário ativo com essa senha, ou None. Grava o último login."""
    login = normalizar_login(usuario)
    if not login or not senha:
        return None
    linha = conn.execute("SELECT * FROM painel_usuarios WHERE usuario = ?", (login,)).fetchone()
    if linha is None or not linha["ativo"] or not check_password_hash(linha["senha_hash"], senha):
        return None
    conn.execute("UPDATE painel_usuarios SET ultimo_login = ? WHERE id = ?", (_agora(), linha["id"]))
    conn.commit()
    return _montar(conn, linha)


# ── Cadastro ──────────────────────────────────────────────────────────────────

def limpar_permissoes(bruto: dict) -> dict:
    """Só telas conhecidas e acessos válidos; o resto vira "sem acesso"."""
    return {p: a for p, a in (bruto or {}).items() if p in CHAVES_PAGINAS and a in ACESSOS}


def salvar_usuario(conn, *, usuario_id: int | None, usuario: str, nome: str, senha: str,
                   ativo: bool, permissoes: dict, por: str, logins_reservados=(),
                   editor_id: int | None = None) -> int:
    """Cria (usuario_id None) ou atualiza. Senha vazia na edição mantém a
    atual. `logins_reservados` = logins fixos do config.yaml, que não podem
    ser reaproveitados (o login fixo ganharia e o cadastro nunca entraria).
    `editor_id` = id de quem está salvando, se for usuário cadastrado:
    impede que a pessoa se tranque pra fora da própria tela de usuários."""
    login = normalizar_login(usuario)
    nome = (nome or "").strip()
    permissoes = limpar_permissoes(permissoes)
    if not _PADRAO_LOGIN.match(login):
        raise ErroUsuario("Login inválido: 3 a 40 caracteres, só letras minúsculas, números e . _ - @")
    if login in {normalizar_login(r) for r in logins_reservados if r}:
        raise ErroUsuario("Esse login já é usado por um acesso fixo do painel. Escolha outro.")
    if not nome:
        raise ErroUsuario("Informe o nome.")
    if senha and len(senha) < SENHA_MINIMA:
        raise ErroUsuario(f"A senha precisa ter pelo menos {SENHA_MINIMA} caracteres.")
    existente = conn.execute("SELECT id FROM painel_usuarios WHERE usuario = ?", (login,)).fetchone()
    if existente and existente["id"] != usuario_id:
        raise ErroUsuario("Já existe um usuário com esse login.")

    agora = _agora()
    if usuario_id is None:
        if not senha:
            raise ErroUsuario("Informe a senha do novo usuário.")
        cur = conn.execute(
            "INSERT INTO painel_usuarios (usuario, nome, senha_hash, ativo, criado_em, criado_por) VALUES (?, ?, ?, ?, ?, ?)",
            (login, nome, generate_password_hash(senha), int(bool(ativo)), agora, por))
        usuario_id = cur.lastrowid
    else:
        atual = conn.execute("SELECT * FROM painel_usuarios WHERE id = ?", (usuario_id,)).fetchone()
        if atual is None:
            raise ErroUsuario("Usuário não encontrado.")
        if editor_id is not None and editor_id == usuario_id and (not ativo or permissoes.get("usuarios") != "total"):
            raise ErroUsuario("Você não pode desativar o próprio usuário nem tirar seu acesso total à tela Usuários.")
        # Sessões abertas caem quando muda o que identifica a pessoa.
        derruba = bool(senha) or login != atual["usuario"] or (atual["ativo"] and not ativo)
        conn.execute(
            "UPDATE painel_usuarios SET usuario = ?, nome = ?, ativo = ?, atualizado_em = ?, atualizado_por = ?,"
            " versao = versao + ? WHERE id = ?",
            (login, nome, int(bool(ativo)), agora, por, int(derruba), usuario_id))
        if senha:
            conn.execute("UPDATE painel_usuarios SET senha_hash = ? WHERE id = ?",
                         (generate_password_hash(senha), usuario_id))
    conn.execute("DELETE FROM painel_usuario_permissoes WHERE usuario_id = ?", (usuario_id,))
    conn.executemany("INSERT INTO painel_usuario_permissoes (usuario_id, pagina, acesso) VALUES (?, ?, ?)",
                     [(usuario_id, p, a) for p, a in permissoes.items()])
    conn.commit()
    return usuario_id


# ── Regra de acesso ───────────────────────────────────────────────────────────

def _casa_caminho(caminho: str, base: str) -> bool:
    if base == "/":
        return caminho == "/"
    return caminho == base or caminho.startswith(base + "/")


def paginas_da_rota(caminho: str, endpoint: str | None) -> list[str]:
    """Telas a que a rota pertence (caminho SEM o prefixo /painel, ou seja,
    request.path do Flask)."""
    return [p["chave"] for p in PAGINAS
            if (endpoint and endpoint in p.get("endpoints", ()))
            or any(_casa_caminho(caminho, b) for b in p["caminhos"])]


def acesso_exigido(metodo: str) -> str:
    return "leitura" if (metodo or "").upper() in ("GET", "HEAD", "OPTIONS") else "total"


def acesso_na_rota(permissoes: dict, caminho: str, endpoint: str | None) -> str | None:
    """Maior acesso que o usuário tem entre as telas da rota ("total",
    "leitura") ou None se não tem nenhuma delas."""
    acessos = {permissoes.get(p) for p in paginas_da_rota(caminho, endpoint)}
    if "total" in acessos:
        return "total"
    if "leitura" in acessos:
        return "leitura"
    return None


def pode(permissoes: dict, caminho: str, endpoint: str | None, metodo: str) -> tuple[bool, str | None]:
    """(liberado, acesso na tela). Endpoint livre passa com qualquer login."""
    if endpoint in ENDPOINTS_LIVRES:
        return True, None
    acesso = acesso_na_rota(permissoes, caminho, endpoint)
    if acesso is None:
        return False, None
    if acesso_exigido(metodo) == "total" and acesso != "total":
        return False, acesso
    return True, acesso


def endpoint_inicial(permissoes: dict) -> str | None:
    """Primeira tela liberada, na ordem do menu."""
    for p in PAGINAS:
        if permissoes.get(p["chave"]):
            return p["inicial"]
    return None


def contadores_permitidos(permissoes: dict) -> set[str]:
    return {c for c, p in PAGINA_DO_CONTADOR.items() if permissoes.get(p)}


def grupos_paginas() -> list[tuple[str, list[dict]]]:
    grupos: dict[str, list[dict]] = {}
    for p in PAGINAS:
        grupos.setdefault(p["grupo"], []).append(p)
    return list(grupos.items())
