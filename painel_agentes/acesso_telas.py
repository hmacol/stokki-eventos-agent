# -*- coding: utf-8 -*-
"""
Acesso personalizado por usuario (21/10, Hugo): em vez de um dos niveis
fixos, um login nominal (usuarios_extras do config.yaml) pode ter uma
lista de telas escolhida na tela /acessos. Tela liberada vale completa:
ver e todos os botoes, com o poder do nivel total dentro dela.

Como funciona:
- A lista mora na tabela painel_acesso_telas (dados/dados.db). Usuario sem
  linha segue o nivel do config, como sempre. Apagar a linha devolve o nivel.
- requer_auth (painel_agentes.py) le a lista a cada request (muda na hora,
  sem novo login) e libera a rota se o caminho cair numa das telas.
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

DB_PATH = Path(__file__).parent.parent / "dados" / "dados.db"

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

# Usadas por qualquer tela (badges do menu).
CAMINHOS_COMUNS = [re.compile(r"^/api/sidebar/contadores$")]


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
    return conn


def nomes_extras(cfg_painel: dict) -> list[str]:
    return [e.get("usuario") for e in cfg_painel.get("usuarios_extras") or [] if e.get("usuario")]


def telas_do_usuario(usuario: str, cfg_painel: dict, conn=None):
    """Lista de telas do usuario, ou None quando ele segue o nivel do
    config. So login nominal (usuarios_extras) pode ser personalizado:
    os pares fixos (total, operador...) nunca."""
    if not usuario or usuario not in nomes_extras(cfg_painel):
        return None
    proprio = conn is None
    conn = conn or conectar()
    try:
        linha = conn.execute("SELECT telas FROM painel_acesso_telas WHERE usuario = ?", (usuario,)).fetchone()
    finally:
        if proprio:
            conn.close()
    if linha is None:
        return None
    return [c for c in json.loads(linha["telas"]) if c in CHAVES]


def listar(cfg_painel: dict, conn) -> list[dict]:
    """Um item por login nominal, na ordem do config."""
    linhas = {r["usuario"]: r for r in conn.execute("SELECT * FROM painel_acesso_telas")}
    resultado = []
    for extra in cfg_painel.get("usuarios_extras") or []:
        u = extra.get("usuario")
        if not u:
            continue
        r = linhas.get(u)
        resultado.append({
            "usuario": u,
            "nivel_config": extra.get("nivel") or "",
            "telas": [c for c in json.loads(r["telas"]) if c in CHAVES] if r else None,
            "atualizado_por": r["atualizado_por"] if r else None,
            "atualizado_em": r["atualizado_em"] if r else None,
        })
    return resultado


def salvar(conn, usuario: str, telas, por: str, cfg_painel: dict) -> None:
    """telas=None apaga a linha (volta ao nivel do config). Lista vazia
    vale: o usuario fica sem tela nenhuma (nao consegue entrar)."""
    if usuario not in nomes_extras(cfg_painel):
        raise ValueError("Só login nominal (usuarios_extras do config) pode ter acesso personalizado.")
    if usuario == por:
        raise ValueError("Você não pode mudar o seu próprio acesso.")
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
            (usuario, json.dumps(ordenadas), por, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
