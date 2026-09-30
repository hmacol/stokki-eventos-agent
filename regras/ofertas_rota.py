# -*- coding: utf-8 -*-
"""
regras/ofertas_rota.py

Tabela local do marketplace de rotas -- pedido do Hugo, 22/08: em vez de
só direcionar (manual) ou alocar (automático) um motorista pra uma rota
em rascunho, dá pra PUBLICAR a rota (sem motorista ainda) pro grupo de
motoristas elegíveis, cada um vendo um resumo (região/cidades/bairros,
paradas por nível, caixas, peso quando disponível) e podendo escolher 1
rota pra si.

Mesmo padrão de regras/confirmacao_rotas.py: tabela própria em
dados/dados.db, sincronizada por push/pull com o app standalone na VPS
(confirmacao_motoristas/app.py) -- a máquina local não expõe entrada de
internet, então a página que o motorista abre vive lá, não aqui. Essa
tabela local é o espelho de "o que foi publicado" + "o que já foi
escolhido"; rascunhos_rota (painel_agentes/rascunhos_rota.py) continua
sendo a fonte de verdade da rota em si (paradas, motorista, status).

Chaveada por rascunho_id (não por token): ao contrário da confirmação
de rota já enviada (1 rota real na VUUPT = 1 confirmação), aqui a oferta
é sobre o RASCUNHO (ainda pré-VUUPT), e só existe 1 oferta ativa por
rascunho por vez.
"""
import json
import sqlite3
from datetime import datetime
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
_DB_PATH = _RAIZ / "dados" / "dados.db"

STATUS_ABERTA = "ABERTA"
STATUS_ESCOLHIDA = "ESCOLHIDA"
STATUS_CANCELADA = "CANCELADA"


def _conectar():
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ofertas_rota (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            rascunho_id           INTEGER NOT NULL UNIQUE,
            data_alvo             TEXT NOT NULL,
            resumo_json           TEXT NOT NULL,
            agent_ids_elegiveis   TEXT NOT NULL,
            status                TEXT NOT NULL DEFAULT 'ABERTA',
            escolhido_por         INTEGER,
            escolhido_em          TEXT,
            criado_em             TEXT NOT NULL,
            sincronizado_em       TEXT,
            aplicado_em           TEXT
        )
    """)
    # Priorização em ondas (Hugo, 03/09 -- ver regras/prioridade_ofertas.py):
    # cada elegível da oferta tem um 'visivel_a_partir_de' dentro do JSON de
    # agent_ids_elegiveis; esta tabela registra quem JÁ FOI AVISADO de cada
    # oferta, pra o job periódico avisar a onda seguinte só quando ela
    # abrir, e nunca avisar o mesmo motorista 2x pela mesma oferta.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ofertas_avisos (
            rascunho_id  INTEGER NOT NULL,
            agent_id     INTEGER NOT NULL,
            onda         INTEGER NOT NULL DEFAULT 0,
            avisado_em   TEXT NOT NULL,
            PRIMARY KEY (rascunho_id, agent_id)
        )
    """)
    conn.commit()
    return conn


def criar_ou_atualizar_oferta(rascunho_id: int, data_alvo, resumo: dict, elegiveis: list[dict]) -> int:
    """Publica (ou republica) uma oferta pra esse rascunho -- sempre
    reabre em ABERTA, limpando qualquer escolha/aplicação anterior (só
    é chamada quando o rascunho está em RASCUNHO, ver
    painel_agentes/planejamento_rotas.py::publicar_oferta_rascunho; uma
    oferta anterior pra esse rascunho já foi consumida -- escolhida e
    aplicada -- antes dele voltar a ficar disponível pra publicar de
    novo).

    `elegiveis` é uma lista de {"agent_id": int, "telefone_ultimos4": str|None,
    "cpf": str|None} -- telefone e cpf vão junto (não só o agent_id)
    porque a VPS decide sozinha quem pode escolher o quê: telefone_ultimos4
    é a checagem leve da página pessoal (/escolher/<token>, mesmo padrão
    da confirmação de rota já enviada); cpf é o identificador completo
    da página compartilhada sem token (/escolher, Hugo 22/08 -- "dar a
    mesma oportunidade a todos", não depende de link individual chegar
    em quem tem telefone/e-mail cadastrado). Motorista sem CPF
    cadastrado simplesmente não aparece pra identificação na página
    compartilhada -- ver regras/preferencias_motoristas.py.

    Retorna o id da linha."""
    agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = _conectar()
    try:
        conn.execute("""
            INSERT INTO ofertas_rota
                (rascunho_id, data_alvo, resumo_json, agent_ids_elegiveis, status, criado_em)
            VALUES (?, ?, ?, ?, 'ABERTA', ?)
            ON CONFLICT(rascunho_id) DO UPDATE SET
                data_alvo           = excluded.data_alvo,
                resumo_json         = excluded.resumo_json,
                agent_ids_elegiveis = excluded.agent_ids_elegiveis,
                status              = 'ABERTA',
                escolhido_por       = NULL,
                escolhido_em        = NULL,
                criado_em           = excluded.criado_em,
                sincronizado_em     = NULL,
                aplicado_em         = NULL
        """, (rascunho_id, data_alvo.isoformat(), json.dumps(resumo, ensure_ascii=False),
              json.dumps(elegiveis), agora))
        # republicação = ondas recomeçam do zero, todo mundo pode ser
        # avisado de novo (a lista de elegíveis/liberações é nova).
        conn.execute("DELETE FROM ofertas_avisos WHERE rascunho_id = ?", (rascunho_id,))
        conn.commit()
        linha = conn.execute("SELECT id FROM ofertas_rota WHERE rascunho_id = ?", (rascunho_id,)).fetchone()
        return linha["id"]
    finally:
        conn.close()


def cancelar_oferta(rascunho_id: int) -> bool:
    """Despublica -- só cancela se ainda estiver ABERTA (não mexe numa
    oferta já ESCOLHIDA: se um motorista ganhou a corrida antes do
    clique de despublicar, a escolha dele prevalece, mesmo padrão de
    'nunca sobrescrever resposta já registrada' de confirmacao_rotas.py).
    Marca sincronizado_em = NULL pra o cancelamento ser empurrado pra
    VPS. Retorna True se cancelou de fato."""
    conn = _conectar()
    try:
        cur = conn.execute("""
            UPDATE ofertas_rota SET status = 'CANCELADA', sincronizado_em = NULL
            WHERE rascunho_id = ? AND status = 'ABERTA'
        """, (rascunho_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def listar_pendentes_de_envio() -> list[dict]:
    """Linhas ainda não empurradas pra VPS (novas, republicadas ou
    canceladas desde o último push)."""
    conn = _conectar()
    try:
        linhas = conn.execute(
            "SELECT * FROM ofertas_rota WHERE sincronizado_em IS NULL"
        ).fetchall()
    finally:
        conn.close()
    return [dict(linha) for linha in linhas]


def marcar_sincronizadas(ids: list[int]) -> None:
    if not ids:
        return
    agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = _conectar()
    try:
        marcadores = ",".join("?" * len(ids))
        conn.execute(
            f"UPDATE ofertas_rota SET sincronizado_em = ? WHERE id IN ({marcadores})",
            (agora, *ids),
        )
        conn.commit()
    finally:
        conn.close()


def aplicar_resposta_remota(rascunho_id: int, agent_id: int, escolhido_em: str) -> bool:
    """Espelha localmente a escolha registrada na VPS (pull). Escrita
    incondicional -- confia na VPS como fonte de verdade de quem
    escolheu (mesmo padrão de confirmacao_rotas.py::aplicar_resposta_remota).
    Retorna False se esse rascunho_id não tinha oferta local (dessincronia;
    quem chama decide como logar)."""
    conn = _conectar()
    try:
        cur = conn.execute("""
            UPDATE ofertas_rota
            SET status = 'ESCOLHIDA', escolhido_por = ?, escolhido_em = ?
            WHERE rascunho_id = ?
        """, (agent_id, escolhido_em, rascunho_id))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def listar_escolhidas_nao_aplicadas() -> list[dict]:
    """Ofertas ESCOLHIDA (já espelhadas da VPS) cuja escolha ainda não
    foi aplicada em rascunhos_rota -- ver
    painel_agentes/rascunhos_rota.py::aplicar_escolha_motorista."""
    conn = _conectar()
    try:
        linhas = conn.execute(
            "SELECT * FROM ofertas_rota WHERE status = 'ESCOLHIDA' AND aplicado_em IS NULL"
        ).fetchall()
    finally:
        conn.close()
    return [dict(linha) for linha in linhas]


def marcar_aplicada(rascunho_id: int) -> None:
    agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = _conectar()
    try:
        conn.execute(
            "UPDATE ofertas_rota SET aplicado_em = ? WHERE rascunho_id = ?",
            (agora, rascunho_id),
        )
        conn.commit()
    finally:
        conn.close()


def listar_aplicadas() -> list[dict]:
    """Ofertas ESCOLHIDA e JÁ aplicadas em rascunhos_rota (Hugo, 23/08)
    -- candidatas a checar contra a VPS se o motorista desistiu DEPOIS
    da aplicação (ver GET /api/sync/ofertas/status em
    confirmacao_motoristas/app.py e roteirizacao/
    sincronizar_respostas_confirmacao.py::_reconciliar_escolhas_revertidas).
    Sem isso, uma escolha desfeita pelo próprio motorista na página
    pública nunca chega de volta pra cá -- o rascunho continuaria
    mostrando o motorista errado, e a rota reaparece "disponível" pra
    outro motorista escolher sem o painel saber."""
    conn = _conectar()
    try:
        linhas = conn.execute(
            "SELECT * FROM ofertas_rota WHERE status = 'ESCOLHIDA' AND aplicado_em IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()
    return [dict(linha) for linha in linhas]


def marcar_revertida(rascunho_id: int) -> None:
    """A escolha foi desfeita na VPS depois de já aplicada aqui (ver
    listar_aplicadas) -- status vira CANCELADA pra não ser reprocessada
    de novo por engano (nem por listar_escolhidas_nao_aplicadas, que já
    exige aplicado_em IS NULL, nem por esta mesma checagem, que exige
    status='ESCOLHIDA'). sincronizado_em volta pra NULL -- sem isso o
    CANCELADA nunca seria empurrado de volta pra VPS (listar_pendentes_
    de_envio só pega quem tem sincronizado_em NULL), e a oferta
    continuaria "disponível" na página pública indefinidamente, mesmo
    com o rascunho já de volta a RASCUNHO comum aqui (achado 23/08,
    revisão do Hugo: "continua aparecendo pro usuário de teste")."""
    conn = _conectar()
    try:
        conn.execute(
            "UPDATE ofertas_rota SET status = 'CANCELADA', sincronizado_em = NULL WHERE rascunho_id = ?",
            (rascunho_id,),
        )
        conn.commit()
    finally:
        conn.close()


def buscar_por_rascunho(rascunho_id: int) -> dict | None:
    conn = _conectar()
    try:
        linha = conn.execute("SELECT * FROM ofertas_rota WHERE rascunho_id = ?", (rascunho_id,)).fetchone()
        return dict(linha) if linha else None
    finally:
        conn.close()


def listar_liberacoes_pendentes(agora_utc: str, apenas_agent_ids: set[int] | None = None) -> list[dict]:
    """Elegíveis de ofertas ABERTA cuja onda JÁ ABRIU (visivel_a_partir_de
    <= agora_utc, ou sem esse campo -- oferta gravada antes das ondas
    existirem, tratada como visível desde sempre) e que ainda não foram
    avisados dessa oferta (sem linha em ofertas_avisos). É a fila do
    aviso: quem publica avisa a onda 0 na hora, o job periódico
    (sincronizar_respostas_confirmacao.py) avisa as seguintes.

    `apenas_agent_ids` restringe ao conjunto que quem chama tem em mãos
    (o endpoint de publicação só conhece os elegíveis daquela rota).
    Retorna [{rascunho_id, data_alvo, agent_id, onda}] -- um item por
    (oferta, motorista); quem chama agrupa por motorista+dia."""
    conn = _conectar()
    try:
        ofertas = conn.execute("SELECT rascunho_id, data_alvo, agent_ids_elegiveis FROM ofertas_rota WHERE status = 'ABERTA'").fetchall()
        avisados = {
            (r["rascunho_id"], r["agent_id"])
            for r in conn.execute("SELECT rascunho_id, agent_id FROM ofertas_avisos")
        }
    finally:
        conn.close()

    pendentes = []
    for oferta in ofertas:
        try:
            elegiveis = json.loads(oferta["agent_ids_elegiveis"] or "[]")
        except ValueError:
            continue
        for e in elegiveis:
            agent_id = e.get("agent_id") if isinstance(e, dict) else e
            if agent_id is None or (apenas_agent_ids is not None and agent_id not in apenas_agent_ids):
                continue
            if (oferta["rascunho_id"], agent_id) in avisados:
                continue
            visivel = e.get("visivel_a_partir_de") if isinstance(e, dict) else None
            if visivel and visivel > agora_utc:
                continue
            pendentes.append({
                "rascunho_id": oferta["rascunho_id"], "data_alvo": oferta["data_alvo"],
                "agent_id": agent_id, "onda": (e.get("onda") if isinstance(e, dict) else 0) or 0,
            })
    return pendentes


def marcar_avisados(itens: list[dict]) -> None:
    """Registra em ofertas_avisos os pares (rascunho_id, agent_id) de
    `itens` (mesmo formato de listar_liberacoes_pendentes)."""
    if not itens:
        return
    agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = _conectar()
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO ofertas_avisos (rascunho_id, agent_id, onda, avisado_em) VALUES (?, ?, ?, ?)",
            [(i["rascunho_id"], i["agent_id"], i.get("onda") or 0, agora) for i in itens],
        )
        conn.commit()
    finally:
        conn.close()


def contar_escolhidas_no_dia(agent_id: int, data_alvo: str, conn: sqlite3.Connection | None = None) -> int:
    """Quantas ofertas desse dia esse motorista já ESCOLHEU (pra respeitar
    vagas_restantes no app do motorista -- ver nucleo/operacao.py)."""
    fechar = conn is None
    conn = conn or _conectar()
    try:
        linha = conn.execute(
            "SELECT COUNT(*) FROM ofertas_rota WHERE status = 'ESCOLHIDA' AND escolhido_por = ? AND data_alvo = ?",
            (agent_id, data_alvo),
        ).fetchone()
        return int(linha[0]) if linha else 0
    finally:
        if fechar:
            conn.close()
