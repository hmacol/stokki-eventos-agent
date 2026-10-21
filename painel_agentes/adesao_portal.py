"""
Adesão ao portal do cliente (21/10, pedido do Hugo): quem já entrou no
portal e quem está importando pedidos por lá (aba "Enviar pedidos"), pra
equipe de atendimento saber quem ainda precisa ser convidado/cobrado.

Lê só dados/dados.db (clientes_portal, portal_envios, portal_grupos), ao
vivo a cada abertura da tela. Login de grupo econômico soma os envios dos
membros.
"""

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from portal_cliente.auth_cliente import formatar_cnpj

DB_PATH = Path(__file__).parent.parent / "dados" / "dados.db"

CNPJ_TESTE = "00000000000191"
# Sem envio nesta janela, quem já importou cai pra "parou de importar".
DIAS_ATIVO = 14
# DUPLICADO/CANCELADO não contam como pedido importado.
_STATUS_IGNORADOS = ("DUPLICADO", "CANCELADO")

IMPORTANDO = "IMPORTANDO"
PAROU = "PAROU"
SO_ENTROU = "SO_ENTROU"
NUNCA_ENTROU = "NUNCA_ENTROU"
ROTULOS = {
    IMPORTANDO: "Importando",
    PAROU: "Parou de importar",
    SO_ENTROU: "Entrou, não importa",
    NUNCA_ENTROU: "Nunca entrou",
}
_ORDEM = [IMPORTANDO, PAROU, SO_ENTROU, NUNCA_ENTROU]


def listar(caminho=None, agora=None):
    """Uma linha por conta ativa do portal (sem o cliente teste), na ordem
    importando → parou → só entrou → nunca entrou."""
    agora = agora or datetime.now()
    corte = (agora - timedelta(days=DIAS_ATIVO)).strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(f"file:{caminho or DB_PATH}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        contas = conn.execute(
            "SELECT cnpj, nome, trocar_pin, ultimo_login_em FROM clientes_portal "
            "WHERE ativo = 1 AND cnpj != ?", (CNPJ_TESTE,)).fetchall()
        membros = {}
        for g in conn.execute("SELECT cnpj_login, cnpj_membro FROM portal_grupos"):
            membros.setdefault(g["cnpj_login"], set()).add(g["cnpj_membro"])
        envios = {}
        marcas = ",".join("?" * len(_STATUS_IGNORADOS))
        for e in conn.execute(
                f"SELECT cnpj_embarcador AS cnpj, COUNT(*) AS total, SUM(criado_em >= ?) AS recentes, "
                f"MAX(criado_em) AS ultimo FROM portal_envios WHERE status NOT IN ({marcas}) "
                f"GROUP BY cnpj_embarcador", (corte, *_STATUS_IGNORADOS)):
            envios[e["cnpj"]] = e
    finally:
        conn.close()

    linhas = []
    for c in contas:
        dos_cnpjs = [envios[x] for x in {c["cnpj"], *membros.get(c["cnpj"], ())} if x in envios]
        total = sum(e["total"] for e in dos_cnpjs)
        recentes = sum(e["recentes"] or 0 for e in dos_cnpjs)
        ultimo = max((e["ultimo"] for e in dos_cnpjs), default=None)
        if recentes:
            situacao = IMPORTANDO
        elif total:
            situacao = PAROU
        elif c["ultimo_login_em"]:
            situacao = SO_ENTROU
        else:
            situacao = NUNCA_ENTROU
        linhas.append({
            "nome": c["nome"] or "(sem nome)",
            "cnpj": formatar_cnpj(c["cnpj"]),
            "situacao": situacao,
            "rotulo": ROTULOS[situacao],
            "ultimo_login": c["ultimo_login_em"],
            "pin_pendente": bool(c["trocar_pin"]),
            "pedidos_total": total,
            "pedidos_recentes": recentes,
            "ultimo_envio": ultimo,
        })
    linhas.sort(key=lambda l: (_ORDEM.index(l["situacao"]), -l["pedidos_total"], l["nome"].upper()))
    return linhas


def resumo(linhas):
    return [{"situacao": s, "rotulo": ROTULOS[s], "qtd": sum(1 for l in linhas if l["situacao"] == s)}
            for s in _ORDEM]
