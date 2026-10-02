# -*- coding: utf-8 -*-
"""
painel_agentes/pagina_inicial.py

Dados da pagina inicial do painel (/inicio, pedido do Hugo 29/09/2026):
o que esta esperando acao de quem logou, os numeros da operacao de hoje
e, so pro nivel total, as rotinas que falharam ou estao rodando.

Regra que manda (a mesma de contadores_menu.py): a pagina nunca espera
fonte cara. Tudo aqui le o que ja esta pronto -- os contadores do menu
(com cache e renovacao em segundo plano), o snapshot que a Torre publica
a cada coleta e o SQLite das execucoes do painel. Nenhuma chamada nova a
VUUPT, Stokki ou Fresh Hub.

As tres fontes entram por parametro em montar_dados() pra que os testes
rodem sem banco nem rede; as rotas do Flask chamam sem argumento e caem
nas fontes de verdade.
"""
import logging
from datetime import date, datetime, timedelta

logger = logging.getLogger("painel.inicio")

# Cartoes de "Esperando voce". A chave e a mesma de contadores_menu
# (NIVEIS_POR_CONTADOR decide quem ve o que -- aqui so o visual).
CARTOES = [
    {"chave": "torre", "rota": "torre", "rotulo": "Torre de Controle",
     "apoio": "ocorrências na fila de ação", "apoio_critico": "críticas"},
    {"chave": "pedidos_parados", "rota": "pedidos_parados", "rotulo": "Pedidos Parados",
     "apoio": "esperando triagem", "apoio_critico": "urgentes"},
    {"chave": "atendimento", "rota": "atendimento", "rotulo": "Atendimento",
     "apoio": "chamados na fila", "apoio_critico": None},
    {"chave": "pedagios", "rota": "pedagios", "rotulo": "Pedágios",
     "apoio": "aguardando aprovação", "apoio_critico": None},
    {"chave": "canhotos", "rota": "canhotos", "rotulo": "Canhotos",
     "apoio": "reprovados na conferência", "apoio_critico": None},
]

FALHAS_MAX = 5
JANELA_FALHAS = timedelta(hours=24)
_FORMATO_SQLITE = "%Y-%m-%d %H:%M:%S"


def montar_pendencias(nivel_acesso: str, contadores: dict, niveis_por_contador: dict, url_de) -> list[dict]:
    """Um cartao por fila que o nivel pode abrir. Contador que nao veio
    (fonte cara ainda sem leitura, ou que falhou) vira qtd=None -- a tela
    mostra "carregando" em vez de um zero mentiroso.

    Ordem: com critica, depois com pendencia, depois sem leitura, depois
    zerado. Empate segue a ordem de CARTOES, salvo pro nivel atendimento,
    cuja fila propria vem antes."""
    cartoes = []
    for i, modelo in enumerate(CARTOES):
        if nivel_acesso not in niveis_por_contador.get(modelo["chave"], ()):
            continue
        valor = contadores.get(modelo["chave"])
        qtd = valor["qtd"] if valor else None
        criticas = (valor.get("criticas") or 0) if valor else 0
        cartoes.append({
            "chave": modelo["chave"], "rotulo": modelo["rotulo"], "url": url_de(modelo["rota"]),
            "apoio": modelo["apoio"], "apoio_critico": modelo["apoio_critico"],
            "qtd": qtd, "criticas": criticas, "_pos": i,
        })

    def grupo(c):
        if c["criticas"] > 0:
            return 0
        if c["qtd"] is None:
            return 2
        return 1 if c["qtd"] > 0 else 3

    def desempate(c):
        if nivel_acesso == "atendimento" and c["chave"] == "atendimento":
            return -1
        return c["_pos"]

    cartoes.sort(key=lambda c: (grupo(c), desempate(c)))
    for c in cartoes:
        del c["_pos"]
    return cartoes


def montar_operacao(snapshot: dict | None) -> dict | None:
    """Numeros do dia a partir do snapshot da Torre (torre_controle.
    snapshot_resumo_dia). None quando ainda nao ha leitura de hoje."""
    if not snapshot:
        return None
    return {
        "gerado_em": snapshot["gerado_em"][:5],
        "rotas": dict(snapshot["rotas_resumo"]),
        "pedidos": dict(snapshot["pedidos"]),
    }


def _hora(texto: str | None) -> str:
    return texto[11:16] if texto else ""


def montar_rotinas(execucoes: list[dict], url_de, agora: datetime | None = None) -> dict:
    """O que esta rodando agora e as falhas (qualquer status fora de
    SUCESSO/RODANDO/NA_FILA) das ultimas 24 h, mais recentes primeiro,
    no maximo FALHAS_MAX. `execucoes` ja vem em ordem decrescente de id."""
    agora = agora or datetime.now()
    limite = agora - JANELA_FALHAS
    rodando, falhas = [], []
    for ex in execucoes:
        status = ex["status"]
        if status == "RODANDO":
            rodando.append({"nome": ex["agente_nome"], "iniciado_em": _hora(ex["iniciado_em"])})
            continue
        if status in ("SUCESSO", "NA_FILA") or len(falhas) >= FALHAS_MAX:
            continue
        quando_bruto = ex.get("finalizado_em") or ex["iniciado_em"]
        try:
            quando = datetime.strptime(quando_bruto, _FORMATO_SQLITE)
        except (TypeError, ValueError):
            continue
        if quando < limite:
            continue
        falhas.append({
            "nome": ex["agente_nome"], "status": status,
            "quando": quando.strftime("%d/%m %H:%M"),
            "url": url_de("execucao", execucao_id=ex["id"]),
        })
    return {"rodando": rodando, "falhas": falhas}


def montar_dados(nivel_acesso: str, url_de, *, contadores=None, niveis_por_contador=None,
                 snapshot=None, execucoes=None) -> dict:
    """JSON de /api/inicio/dados. Sem argumentos de fonte, usa as reais.
    Falha numa fonte nao derruba as outras: o bloco vem vazio e o erro
    vai pro log."""
    if contadores is None or niveis_por_contador is None:
        import contadores_menu
        contadores = contadores or contadores_menu.contadores
        niveis_por_contador = niveis_por_contador or contadores_menu.NIVEIS_POR_CONTADOR
    if snapshot is None:
        import torre_controle
        snapshot = torre_controle.snapshot_resumo_dia
    if execucoes is None:
        from executor import listar_execucoes_recentes
        execucoes = listar_execucoes_recentes

    try:
        valores = contadores(nivel_acesso)
    except Exception as e:
        logger.warning(f"[inicio] Falha nos contadores: {e}")
        valores = {}
    dados = {
        "pendencias": montar_pendencias(nivel_acesso, valores, niveis_por_contador, url_de),
        "operacao": None,
    }
    try:
        dados["operacao"] = montar_operacao(snapshot(date.today()))
    except Exception as e:
        logger.warning(f"[inicio] Falha no resumo do dia: {e}")

    if nivel_acesso == "total":
        try:
            dados["rotinas"] = montar_rotinas(execucoes(), url_de)
        except Exception as e:
            logger.warning(f"[inicio] Falha ao ler execucoes: {e}")
            dados["rotinas"] = {"rodando": [], "falhas": []}
    return dados
