"""
Adesão dos motoristas ao app novo (23/11, saída da Vuupt, etapa 5): quem já opera
pelo app, quem entrou, quem tem PIN e nunca entrou e quem nem conta tem, mais a
leva em que cada um entra. Só leitura de dados/dados.db (bd_motoristas, motoristas,
motoristas_fila_app, nucleo_rotas), ao vivo a cada abertura da tela. Quem está na
ponte vem do config (api_motorista.ponte_vuupt.agentes).

Leva (regra do plano de 23/11):
  0 já na ponte · 1 já tem login e ainda fora da ponte · 2 tem PIN e nunca entrou,
  ou é FIXO sem login · 3 esporádico sem login que rodou nos últimos 14 dias ·
  4 sem login e sem rota nos últimos 14 dias (entra quando voltar a rodar).
"""

import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "dados" / "dados.db"

VERSAO_ALVO = (1, 2, 0)   # igual a nucleo.fila_app_motoristas.VERSAO_ALVO
DIAS_RECENTE = 14
AGENTE_LALAMOVE = 50258
_TESTE = re.compile(r"\bteste\b", re.IGNORECASE)

NA_PONTE = "NA_PONTE"
USANDO = "USANDO"
DESATUALIZADO = "DESATUALIZADO"
PIN_SEM_ENTRAR = "PIN_SEM_ENTRAR"
SEM_LOGIN = "SEM_LOGIN"
ROTULOS = {
    NA_PONTE: "Na ponte",
    USANDO: "Usando o app novo",
    DESATUALIZADO: "Entrou, versão antiga/desconhecida",
    PIN_SEM_ENTRAR: "Tem PIN, nunca entrou",
    SEM_LOGIN: "Sem login",
}
_ORDEM = [NA_PONTE, USANDO, DESATUALIZADO, PIN_SEM_ENTRAR, SEM_LOGIN]
LEVAS = {0: "Leva 0 (já na ponte)", 1: "Leva 1", 2: "Leva 2", 3: "Leva 3", 4: "Leva 4 (quando voltar a rodar)"}


def agentes_da_ponte():
    """Agentes liberados na ponte, do config (vazio se não der pra ler)."""
    try:
        from nucleo import api_motorista
        cfg = api_motorista._carregar_config() or {}
        return {int(a) for a in ((cfg.get("api_motorista") or {}).get("ponte_vuupt") or {}).get("agentes") or []}
    except Exception:
        return set()


def _versao(texto):
    try:
        return tuple(int(p) for p in str(texto or "").split("."))
    except ValueError:
        return ()


def _digitos(x):
    return re.sub(r"\D", "", x or "")


def _tabela_existe(conn, nome):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (nome,)).fetchone() is not None


def listar(caminho=None, agora=None, ponte=None):
    """Uma linha por motorista ativo (sem teste e sem Lalamove), na ordem leva → rotas recentes."""
    agora = agora or datetime.now()
    corte = (agora - timedelta(days=DIAS_RECENTE)).strftime("%Y-%m-%d")
    ponte = agentes_da_ponte() if ponte is None else set(ponte)
    conn = sqlite3.connect(f"file:{caminho or DB_PATH}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        bd = conn.execute("SELECT * FROM bd_motoristas WHERE UPPER(COALESCE(ATIVO, '')) = 'SIM'").fetchall()
        logins = {r["cpf"]: r for r in conn.execute("SELECT cpf, ultimo_login_em, app_versao, plataforma FROM motoristas")}
        fila = ({r["cpf"]: r for r in conn.execute("SELECT * FROM motoristas_fila_app")}
                if _tabela_existe(conn, "motoristas_fila_app") else {})
        rotas = {r["agent_id"]: r for r in conn.execute(
            "SELECT agent_id, COUNT(*) AS n, SUM(COALESCE(operada_app, 0)) AS app, MAX(data_rota) AS ultima "
            "FROM nucleo_rotas WHERE data_rota >= ? AND status != 'CANCELADA' GROUP BY agent_id", (corte,))}
    finally:
        conn.close()

    linhas = []
    for b in bd:
        nome = b["NOME_MOTORISTA"] or "(sem nome)"
        agente = b["AGENT_ID_VUUPT"]
        if agente == AGENTE_LALAMOVE or _TESTE.search(nome):
            continue
        cpf = _digitos(b["CPF_MOTORISTA"])
        conta = logins.get(cpf) if cpf else None
        envio = fila.get(cpf) if cpf else None
        r = rotas.get(agente)
        n_rotas = r["n"] if r else 0
        versao = _versao(conta["app_versao"]) if conta else ()
        if agente in ponte:
            situacao = NA_PONTE
        elif conta and conta["ultimo_login_em"]:
            situacao = USANDO if versao and versao >= VERSAO_ALVO else DESATUALIZADO
        elif conta:
            situacao = PIN_SEM_ENTRAR
        else:
            situacao = SEM_LOGIN
        posicao = (b["POSICAO"] or "").upper()
        if situacao == NA_PONTE:
            leva = 0
        elif situacao in (USANDO, DESATUALIZADO):
            leva = 1
        elif situacao == PIN_SEM_ENTRAR or posicao == "FIXO":
            leva = 2
        elif n_rotas:
            leva = 3
        else:
            leva = 4
        if b["SEM_APP"] == "SIM":
            plataforma = "iPhone"
        elif conta and conta["plataforma"] == "web":
            plataforma = "PWA"
        else:
            plataforma = "—"
        linhas.append({
            "id": b["id"], "tem_cpf": bool(cpf), "tem_login": conta is not None,
            "nome": nome, "posicao": posicao.title() or "—", "plataforma": plataforma,
            "situacao": situacao, "rotulo": ROTULOS[situacao], "leva": leva, "leva_rotulo": LEVAS[leva],
            "ultimo_login": (conta["ultimo_login_em"] or "")[:10] if conta else "",
            "versao": (conta["app_versao"] or "") if conta else "",
            "rotas_recentes": n_rotas, "rotas_app": (r["app"] or 0) if r else 0,
            "ultima_rota": r["ultima"] if r else "",
            "msg_envios": envio["envios"] if envio else 0,
            "msg_ultimo": (envio["ultimo_envio_em"] or "")[:16] if envio else "",
            "msg_situacao": envio["situacao"] if envio else "",
        })
    linhas.sort(key=lambda l: (l["leva"], -l["rotas_recentes"], l["nome"].upper()))
    return linhas


def resumo(linhas):
    return [{"situacao": s, "rotulo": ROTULOS[s], "qtd": sum(1 for l in linhas if l["situacao"] == s)}
            for s in _ORDEM]


def resumo_levas(linhas):
    return [{"leva": n, "rotulo": rot, "qtd": sum(1 for l in linhas if l["leva"] == n)} for n, rot in LEVAS.items()]
