# -*- coding: utf-8 -*-
"""
expedicao.py

Dados e geração de PDF da tela de Expedição (`/expedicao`) -- pedido do
Hugo, 18/08: uma página simples pro time de expedição imprimir a
papelada (romaneio: NFs + boletos + canhoteira) de cada rota do dia
antes do motorista sair, sem precisar entender Torre/Planejamento.

Reaproveita 100% o motor de roteirizacao/gerar_pdf_romaneios.py (mesmo
usado no job automático das 04h e no botão "Imprimir rota" do
planejamento) -- aqui só lista as rotas JÁ CRIADAS na VUUPT (não
rascunhos, diferente de planejamento_rotas.py::gerar_romaneio_pdf) e
gera o PDF sob demanda, sempre fresco (mesmo padrão do botão "Imprimir
rota": reflete o estado atual dos documentos, não o que o job das 04h
viu), gravando no MESMO caminho que o job das 04h usaria -- o
print-agent local não precisa saber a diferença.
"""
import re
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(_RAIZ / "roteirizacao"))

import yaml

from avisar_motoristas_rotas import buscar_rotas_do_dia, _extrair_servicos_da_rota
from regras.preferencias_motoristas import CatalogoMotoristas
import gerar_pdf_romaneios as gpr

_PADRAO_NUMERO = re.compile(r"(\d+)")


def _chave_ordem_natural(texto: str) -> list:
    """Mesmo ajuste feito em torre_controle.py e rascunhos_rota.py, 23/08:
    todas as rotas do dia nascem com o mesmo start_at (13:00 fixo, ver
    roteirizacao/criar_rotas_diarias.py), então esta lista na prática
    ordena só por nome -- e nome tem o formato 'Planejamento - DD/MM -
    #N', onde ordenação de string pura colocava '#11' antes de '#2'."""
    pedacos = _PADRAO_NUMERO.split(texto)
    return [(0, int(p)) if p.isdigit() else (1, p.lower()) for p in pedacos if p != ""]

# Mesma normalização de reentrega ('PS-36327-R1' -> 'PS-36327') que
# roteirizacao/gerar_pdf_romaneios.py::_codigo_base e
# painel_agentes/planejamento_rotas.py::_codigo_base -- documentos e o
# pedido na Stokki vivem sob o código BASE, sem sufixo de reentrega.
_PADRAO_CODIGO_BASE = re.compile(r"PS-?\d{4,6}", re.IGNORECASE)


def _codigo_base(codigo: str) -> str:
    """Extrai o PREFIXO 'PS-NNNNN' em vez de remover sufixo do FIM da
    string: reentrega de reentrega (insucesso de novo numa entrega já
    reentregue) empilha sufixo -- 'PS-36741-R1-R1' -- e uma regex
    ancorada em '$' só tira o ÚLTIMO '-R\\d+', devolvendo 'PS-36741-R1'
    em vez do código base (achado 20/08: NF/boleto certos no banco sob
    'PS-36741' somiam do romaneio pra esses casos). Casar pelo prefixo
    é imune a qualquer sufixo/combinação que apareça depois (-R1, -C1,
    -R1-R1, -R2-C1...)."""
    m = _PADRAO_CODIGO_BASE.match((codigo or "").lstrip("#").strip())
    return m.group(0) if m else (codigo or "").lstrip("#")


def _codigos_base_lista(codigo: str) -> list[str]:
    """'code' da VUUPT pode agrupar mais de um pedido combinado por
    vírgula (achado 20/08, ver roteirizacao/gerar_pdf_romaneios.py::
    _codigos_base_lista) -- quebra em códigos individuais antes de
    normalizar, senão a busca de documentos nunca casa nada pro grupo."""
    return [_codigo_base(c.strip()) for c in (codigo or "").split(",") if c.strip()]


def _carregar_config() -> dict:
    with open(_RAIZ / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


# Rota (VUUPT) neste conjunto de status ainda não iniciou deslocamento --
# mesma trava de painel_agentes/rascunhos_rota.py::STATUS_ROTA_NAO_INICIADA
# (duplicado aqui, não importado: aquele módulo é sobre RASCUNHOS locais,
# expedição só lida com rota já criada de verdade na VUUPT).
_STATUS_ROTA_NAO_INICIADA = {"not_started", "assigned", "accepted", "not_assigned", "scheduled"}

MOTIVOS_EXCLUSAO = ["Não Encontrado", "Não coube", "Já enviado (Stokki)", "Já enviado (Vuupt)", "Outros"]

_DB_PATH = _RAIZ / "dados" / "dados.db"


def _rota_do_corpo(dados_rota) -> dict:
    """Mesmo helper de rascunhos_rota.py::_rota_do_corpo -- GET
    /routes/{id} vem embrulhado de formas diferentes ({"route": {...}},
    {"data": {...}} ou sem envelope), duplicado aqui pelo mesmo motivo
    de _STATUS_ROTA_NAO_INICIADA acima."""
    if not isinstance(dados_rota, dict):
        return {}
    for chave in ("route", "data"):
        valor = dados_rota.get(chave)
        if isinstance(valor, dict):
            return valor
    return dados_rota


def _conectar_db() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS expedicao_exclusoes (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            data_alvo       TEXT NOT NULL,
            rota_id         INTEGER NOT NULL,
            rota_nome       TEXT,
            service_id      INTEGER NOT NULL,
            codigo_pedido   TEXT,
            motivo          TEXT NOT NULL,
            observacao      TEXT,
            usuario         TEXT,
            criado_em       TEXT NOT NULL
        )
    """)
    return conn


def _registrar_exclusao(data_alvo: date, rota_id: int, rota_nome: str, service_id: int,
                        codigo_pedido: str, motivo: str, observacao: str, usuario: str) -> None:
    """Registro do botão "Excluir da Rota" (Hugo, 23/08) em
    dados/dados.db -- é o que listar_rotas_do_dia usa (via
    _exclusoes_por_rota) pra continuar mostrando o chip do pedido na
    rota depois de excluído (só marcado como excluído, nunca some da
    tela), então isso não é só auditoria "morta": é a fonte da verdade
    de quais pedidos dessa rota foram tirados e por quê."""
    conn = _conectar_db()
    try:
        conn.execute("""
            INSERT INTO expedicao_exclusoes
                (data_alvo, rota_id, rota_nome, service_id, codigo_pedido, motivo, observacao, usuario, criado_em)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (data_alvo.isoformat(), rota_id, rota_nome, service_id, codigo_pedido,
              motivo, observacao or None, usuario or None, datetime.now().isoformat(timespec="seconds")))
        conn.commit()
    finally:
        conn.close()


def _exclusoes_por_rota(data_alvo: date, rota_id: int) -> list[dict]:
    """Pedidos já excluídos dessa rota nesse dia, mais recente primeiro
    -- listar_rotas_do_dia usa isso pra continuar mostrando o chip
    (marcado como excluído) mesmo depois que o pedido já saiu da lista
    de serviços ao vivo da rota na VUUPT (pedido do Hugo, 23/08: o chip
    nunca soma, só muda de estado)."""
    conn = _conectar_db()
    try:
        linhas = conn.execute("""
            SELECT service_id, codigo_pedido, motivo, observacao, usuario, criado_em, rota_nome
            FROM expedicao_exclusoes
            WHERE data_alvo = ? AND rota_id = ?
            ORDER BY id DESC
        """, (data_alvo.isoformat(), rota_id)).fetchall()
        return [
            {"service_id": sid, "codigo_pedido": codigo, "motivo": motivo,
             "observacao": observacao, "usuario": usuario, "criado_em": criado_em, "rota_nome": rota_nome}
            for sid, codigo, motivo, observacao, usuario, criado_em, rota_nome in linhas
        ]
    finally:
        conn.close()


def _catalogo_motoristas() -> dict:
    """agent_id -> MotoristaPreferencias, pra resolver nome e placa."""
    cfg_motoristas = _carregar_config().get("motoristas", {})
    catalogo = CatalogoMotoristas.carregar(
        cfg_motoristas.get("planilha", ""), cfg_motoristas.get("json_fallback", ""))
    return {m.agent_id: m for m in catalogo.motoristas}


def _montar_pedidos_chip(data_alvo: date, rota_id: int, servicos: list[dict]) -> list[dict]:
    """Chip por pedido (Hugo, 23/08) -- pro time de expedição conferir o
    que tem na rota e, se algo não foi carregado no caminhão, tirar da
    rota ali mesmo (menu de contexto do chip -> "Excluir da Rota" ->
    excluir_pedido_da_rota). O chip de um pedido excluído NUNCA some da
    tela (pedido do Hugo, 23/08) -- só fica marcado como excluído
    (motivo no hover) -- por isso entra tanto o que está de verdade na
    rota agora (`servicos`, ao vivo na VUUPT) quanto o que já foi
    excluído dela hoje (_exclusoes_por_rota, histórico local). Um
    pedido reexcluído mais de uma vez conta só a tentativa mais
    recente; um que voltou a ficar ativo na MESMA rota depois de
    excluído (raro) prevalece como ativo, não como excluído.

    `ordem` é a posição da parada dentro da rota (1, 2, 3...), a MESMA
    numeração exibida no planejamento -- é o que o Hugo chama de
    "Número da Ordem de Entrega" (CORRIGIDO 23/08: a 1ª versão tentava
    extrair uma "referência" do título do serviço via regex, mas isso
    não é a Ordem de Entrega -- é outra coisa, às vezes um número de
    NF). Pedido excluído não tem posição na rota atual, fica sem ordem.
    """
    ids_ativos = {s.get("id") for s in servicos}
    pedidos_chip = [
        {"service_id": s.get("id"), "codigo": s.get("code") or "", "titulo": (s.get("title") or "")[:70],
         "ordem": i, "excluido": False}
        for i, s in enumerate(servicos, start=1)
    ]
    vistos_excluidos = set()
    for ex in _exclusoes_por_rota(data_alvo, rota_id):
        sid = ex["service_id"]
        if sid in ids_ativos or sid in vistos_excluidos:
            continue
        vistos_excluidos.add(sid)
        pedidos_chip.append({
            "service_id": sid, "codigo": ex["codigo_pedido"] or "", "titulo": "",
            "ordem": None, "excluido": True, "motivo": ex["motivo"], "observacao": ex["observacao"],
        })
    return pedidos_chip


def _rota_ids_com_exclusao_no_dia(data_alvo: date) -> set[int]:
    """Todo rota_id que teve pelo menos 1 exclusão nesse dia -- usado
    pra achar rota CANCELADA por ter perdido a última parada (ver
    excluir_pedido_da_rota) que por isso já não aparece mais em
    buscar_rotas_do_dia (filtra status=canceled), mas cujo chip
    excluído não pode sumir da tela."""
    conn = _conectar_db()
    try:
        linhas = conn.execute(
            "SELECT DISTINCT rota_id FROM expedicao_exclusoes WHERE data_alvo = ?",
            (data_alvo.isoformat(),),
        ).fetchall()
        return {rota_id for (rota_id,) in linhas}
    finally:
        conn.close()


def _montar_card_rota_cancelada(token: str, data_alvo: date, rota_id: int,
                                motorista_por_agent_id: dict) -> dict:
    """Reconstrói o card de uma rota que sumiu de buscar_rotas_do_dia
    porque foi cancelada de vez (última parada excluída -- ver
    excluir_pedido_da_rota) -- sem paradas ativas, só o(s) chip(s)
    excluído(s) (pedido do Hugo, 23/08: o pedido não pode sumir da tela
    só porque a rota em si deixou de existir). Busca a rota direto por
    ID pra pegar nome/motorista atualizados; GET /routes/{id} devolve a
    rota mesmo cancelada (só a LISTAGEM filtra, ver rotas_client.
    buscar_rota) -- se nem isso responder (rota apagada de vez, não só
    cancelada), cai pro nome gravado na própria exclusão."""
    from rotas_client import buscar_rota

    nome, agent_id, inicio = None, None, ""
    try:
        rota = _rota_do_corpo(buscar_rota(token, rota_id, include=["agent"]))
        nome = rota.get("name")
        agent_id = rota.get("agent_id")
        inicio = (rota.get("start_at") or "")[11:16]
    except Exception:
        pass

    exclusoes = _exclusoes_por_rota(data_alvo, rota_id)
    if not nome:
        nome = (exclusoes[0]["rota_nome"] if exclusoes and exclusoes[0].get("rota_nome") else f"Rota {rota_id}")
    motorista = motorista_por_agent_id.get(agent_id)

    return {
        "id": rota_id,
        "nome": nome,
        "motorista": motorista.nome if motorista else "(sem motorista)",
        "placa": motorista.placa if motorista else None,
        "pedidos": 0,
        "inicio": inicio,
        "pedidos_chip": _montar_pedidos_chip(data_alvo, rota_id, []),
        "cancelada": True,
    }


def listar_rotas_do_dia(data_alvo: date) -> list[dict]:
    """Rotas reais (VUUPT) da data, uma linha por rota: nome, motorista,
    placa, quantidade de pedidos e horário de saída -- ordenadas por
    horário de saída. Rota sem serviço (vazia) fica fora, mesmo
    critério do job das 04h -- EXCETO rota que ficou vazia porque um
    "Excluir da Rota" tirou a última parada dela hoje: essa aparece
    mesmo assim, marcada "cancelada": True e com 0 pedidos ativos, só
    pra manter visível o chip do que foi excluído (ver
    _montar_card_rota_cancelada)."""
    config = _carregar_config()
    token = config.get("vuupt_api", {}).get("token", "")
    motorista_por_agent_id = _catalogo_motoristas()

    rotas = []
    ids_do_dia = set()
    for r in buscar_rotas_do_dia(token, data_alvo):
        servicos = _extrair_servicos_da_rota(r)
        if not servicos:
            continue
        rota_id = r.get("id")
        ids_do_dia.add(rota_id)
        motorista = motorista_por_agent_id.get(r.get("agent_id"))
        rotas.append({
            "id": rota_id,
            "nome": r.get("name") or f"Rota {rota_id}",
            "motorista": motorista.nome if motorista else "(sem motorista)",
            "placa": motorista.placa if motorista else None,
            "pedidos": len(servicos),
            "inicio": (r.get("start_at") or "")[11:16],
            "pedidos_chip": _montar_pedidos_chip(data_alvo, rota_id, servicos),
            "cancelada": False,
        })

    for rota_id in _rota_ids_com_exclusao_no_dia(data_alvo) - ids_do_dia:
        rotas.append(_montar_card_rota_cancelada(token, data_alvo, rota_id, motorista_por_agent_id))

    rotas.sort(key=lambda r: (r["inicio"], _chave_ordem_natural(r["nome"])))
    return rotas


def gerar_romaneio_rota(data_alvo: date, rota_id: int) -> Path:
    """Gera (sempre fresco -- mesmo padrão do botão "Imprimir rota" do
    planejamento) e devolve o caminho do PDF de romaneio de uma rota já
    criada na VUUPT. Levanta ValueError (404 pro chamador) se a rota
    não existir mais na data ou não tiver paradas."""
    config = _carregar_config()
    token = config.get("vuupt_api", {}).get("token", "")
    nome_por_agent_id = {aid: m.nome for aid, m in _catalogo_motoristas().items()}

    rotas = buscar_rotas_do_dia(token, data_alvo)
    rota = next((r for r in rotas if r.get("id") == rota_id), None)
    if rota is None:
        raise ValueError(f"Rota {rota_id} não encontrada em {data_alvo.isoformat()} (ou está cancelada).")

    servicos = _extrair_servicos_da_rota(rota)
    if not servicos:
        raise ValueError("Rota sem paradas -- nada pra imprimir.")

    nome_motorista = nome_por_agent_id.get(rota.get("agent_id"), "(sem motorista)")
    codigos = {c for s in servicos for c in _codigos_base_lista(s.get("code") or "")}
    docs_por_pedido, _em_revisao = gpr.carregar_documentos_por_pedido(codigos)
    embarcadores, fatores = gpr.carregar_embarcadores()

    pasta = gpr.PASTA_ROMANEIOS / data_alvo.isoformat()
    caminho_saida = pasta / gpr.nome_arquivo_saida(rota, nome_motorista, data_alvo)
    gpr.montar_pdf_rota(rota, servicos, docs_por_pedido, embarcadores, fatores,
                        nome_motorista, data_alvo, caminho_saida)
    return caminho_saida


def excluir_pedido_da_rota(data_alvo: date, rota_id: int, service_id: int,
                           motivo: str, observacao: str, usuario: str) -> dict:
    """Tira 1 pedido de uma rota JÁ CRIADA na VUUPT -- botão "Excluir da
    Rota" do menu de contexto de um chip de pedido, tela de expedição
    (Hugo, 23/08): o motorista foi carregar o caminhão e o pedido não
    coube / não foi encontrado / etc, então sai da rota SEM ser
    cancelado (sempre "desatribui", qualquer que seja o motivo -- fica
    'not_assigned' na VUUPT, disponível pra alguém decidir depois:
    reroteirizar, investigar, cancelar de vez manualmente). Motivo +
    observação só viram auditoria (_registrar_exclusao), não mudam o
    que acontece com o pedido.

    Mesmo mecanismo (e mesma trava) de rascunhos_rota.
    preparar_cancelamento_de_parada: só mexe em rota que, checada AO
    VIVO contra a API, ainda não iniciou deslocamento -- não faz
    sentido tirar parada de rota que o motorista já está rodando. Se
    for a ÚLTIMA parada, cancela a ROTA INTEIRA com
    services_action="unassign" (não dá pra ter rota com 0 paradas); o
    pedido em si continua 'not_assigned', só a rota que deixa de
    existir.

    Além disso (pedido do Hugo, 23/08): só mexe em rota de HOJE em
    diante -- rota de dia anterior é só consulta aqui, nunca alterada,
    mesma regra de planejamento_rotas.py (ver
    bloqueia_planejamento_passado). O status ao vivo checado acima
    NÃO cobre isso sozinho: uma rota antiga que nunca chegou a sair
    (ex: criada e esquecida, sem motorista) pode continuar com status
    "not_started"/"scheduled" dias depois, e mesmo assim não pode ser
    mexida por aqui.

    Retorna {"ok": True, "rota_cancelada": bool} ou
    {"ok": False, "erro": "..."}.
    """
    if data_alvo < date.today():
        return {"ok": False,
                "erro": f"Rota de {data_alvo.strftime('%d/%m/%Y')} já passou -- só é possível "
                        f"alterar rotas de hoje em diante."}
    if motivo not in MOTIVOS_EXCLUSAO:
        return {"ok": False, "erro": f"Motivo inválido: {motivo!r}"}

    config = _carregar_config()
    token = config.get("vuupt_api", {}).get("token", "")

    from rotas_client import atualizar_rota, buscar_rota, cancelar_rota

    try:
        # include=["services"] é obrigatório aqui -- ao contrário da
        # listagem (buscar_rotas_do_dia), GET /routes/{id} sozinho NÃO
        # embute os serviços da rota (ver rotas_client.buscar_rota), e é
        # exatamente a lista viva de serviços que decide ids_restantes
        # abaixo (expedição não tem rascunho local com as paradas, ao
        # contrário de rascunhos_rota.preparar_cancelamento_de_parada).
        dados_rota = buscar_rota(token, rota_id, include=["services"])
    except Exception as e:
        resposta = getattr(e, "response", None)
        if resposta is not None and resposta.status_code == 404:
            return {"ok": False, "erro": f"Rota #{rota_id} não existe mais na VUUPT."}
        return {"ok": False, "erro": f"Falha ao consultar a rota #{rota_id} na VUUPT: {e}"}

    rota = _rota_do_corpo(dados_rota)
    status_atual = rota.get("status")
    if status_atual not in _STATUS_ROTA_NAO_INICIADA:
        return {"ok": False,
                "erro": f"Rota #{rota_id} não pode ser alterada por aqui (status atual na VUUPT: "
                        f"'{status_atual}') -- só rotas que ainda não iniciaram deslocamento."}

    servicos = _extrair_servicos_da_rota(rota)
    servico_alvo = next((s for s in servicos if s.get("id") == service_id), None)
    if servico_alvo is None:
        return {"ok": False, "erro": f"Pedido {service_id} não está mais nessa rota."}
    ids_restantes = [s.get("id") for s in servicos if s.get("id") != service_id]

    try:
        if not ids_restantes:
            cancelar_rota(token, rota_id, services_action="unassign")
        else:
            atualizar_rota(token, rota_id, ids_restantes)
    except Exception as e:
        return {"ok": False, "erro": str(e)}

    _registrar_exclusao(data_alvo, rota_id, rota.get("name") or "", service_id,
                        servico_alvo.get("code") or "", motivo, observacao, usuario)
    return {"ok": True, "rota_cancelada": not ids_restantes}
