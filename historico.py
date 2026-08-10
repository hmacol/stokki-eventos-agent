# -*- coding: utf-8 -*-
"""
historico.py

Registro de auditoria das execuções do pipeline de importação
(Stokki → VUUPT). Mesmo padrão do agente de importação por e-mail
(historico_execucoes.jsonl + STATUS.md), com um nível a mais de
detalhe: o resultado por pedido de cada execução.

Gera três coisas a cada execução:

1. dados/logs/historico_execucoes.jsonl
   Uma linha JSON por execução — resumo: quando rodou, modo, duração,
   contagem por ação (criado, atualizado, pulado, ignorado, erro...).
   Nunca é sobrescrito; é o histórico completo, fácil de consultar
   por script.

2. dados/logs/importacoes/importacao_YYYYMMDD_HHMMSS.csv
   Detalhe por pedido daquela execução: código PS, embarcador (ref),
   fonte de coleta, ação, fonte do endereço, observação, erro.
   Separador ';' e BOM UTF-8 — abre direto no Excel pt-BR.

3. STATUS_IMPORTACAO.md (raiz do projeto)
   Regenerado a cada execução: resumo da última rodada + tabela das
   últimas 10. É o "bati o olho e sei se rodou e o que aconteceu".

Uso (no final do main() do pipeline.py):

    from historico import registrar_execucao
    registrar_execucao(
        modo_teste=modo_teste,
        filtro=filtro_pedido or filtro_embarcador or "",
        resultados=resultados,          # lista de dicts do processar_pedido
        duracao_seg=duracao,
    )
"""
import csv
import json
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

RAIZ         = Path(__file__).parent
DIR_LOGS     = RAIZ / "dados" / "logs"
DIR_DETALHES = DIR_LOGS / "importacoes"
ARQ_HISTORICO = DIR_LOGS / "historico_execucoes.jsonl"
ARQ_STATUS    = RAIZ / "STATUS_IMPORTACAO.md"

# Ordem fixa das ações no resumo (as que não ocorreram aparecem com 0
# nas colunas principais; as demais só quando > 0)
ACOES_PRINCIPAIS = ["criado", "atualizado", "pulado_sem_alteracao",
                    "pulado_atribuido", "ignorado_retirada", "simulado"]

ROTULOS = {
    "criado":               "Criados",
    "atualizado":           "Atualizados",
    "pulado_sem_alteracao": "Sem alteração (fingerprint)",
    "pulado_atribuido":     "Pulados (já atribuídos/concluídos)",
    "ignorado_retirada":    "Ignorados (RETIRADA)",
    "simulado":             "Simulados (modo teste)",
    "erro":                 "Erros",
    "revisao":              "Revisão manual",
}


def _contar(resultados: list[dict]) -> dict:
    """Conta os resultados por ação, mais erros e revisão manual."""
    contagem = {a: 0 for a in ACOES_PRINCIPAIS}
    contagem["erro"] = 0
    contagem["revisao"] = 0
    for r in resultados:
        if r.get("erro"):
            contagem["erro"] += 1
            continue
        acao = r.get("acao") or "desconhecida"
        contagem[acao] = contagem.get(acao, 0) + 1
        if r.get("requer_revisao"):
            contagem["revisao"] += 1
    return contagem


def _gravar_resumo_jsonl(registro: dict):
    DIR_LOGS.mkdir(parents=True, exist_ok=True)
    with open(ARQ_HISTORICO, "a", encoding="utf-8") as f:
        f.write(json.dumps(registro, ensure_ascii=False) + "\n")


def _gravar_detalhes_csv(carimbo: str, resultados: list[dict]) -> Path:
    DIR_DETALHES.mkdir(parents=True, exist_ok=True)
    caminho = DIR_DETALHES / f"importacao_{carimbo}.csv"
    colunas = ["codigo_ps", "id_stokki", "referencia", "fonte_coleta",
               "acao", "fonte_endereco", "fonte_telefone",
               "fonte_tipo_carga", "fonte_nivel_complexidade", "skill_aplicada",
               "tem_agendamento", "fonte_agendamento",
               "requer_revisao", "observacao", "erro"]
    # utf-8-sig (BOM) + ';' → abre certinho no Excel pt-BR
    with open(caminho, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=colunas, delimiter=";",
                           extrasaction="ignore")
        w.writeheader()
        for r in resultados:
            linha = dict(r)
            if linha.get("codigo_ps"):
                linha["codigo_ps"] = f"#{linha['codigo_ps']}"
            linha["requer_revisao"] = "sim" if r.get("requer_revisao") else ""
            linha["erro"] = str(r.get("erro") or "")
            w.writerow(linha)
    return caminho


def _ler_ultimas_execucoes(n: int = 10) -> list[dict]:
    if not ARQ_HISTORICO.exists():
        return []
    try:
        registros = []
        with open(ARQ_HISTORICO, encoding="utf-8") as f:
            for linha in f:
                linha = linha.strip()
                if not linha:
                    continue
                try:
                    registros.append(json.loads(linha))
                except json.JSONDecodeError:
                    continue  # linha corrompida não derruba o status
        return registros[-n:]
    except Exception as e:
        logger.debug(f"Erro ao ler histórico: {e}")
        return []


def _regenerar_status(ultimo: dict, resultados: list[dict], arq_csv: Path):
    """Reescreve o STATUS_IMPORTACAO.md com a última execução + histórico."""
    c = ultimo["contagem"]
    linhas_md = []
    linhas_md.append("# Status — Importação Stokki → VUUPT")
    linhas_md.append("")
    linhas_md.append(f"*Regenerado automaticamente a cada execução do pipeline.*")
    linhas_md.append("")
    linhas_md.append("## Última execução")
    linhas_md.append("")
    modo = "TESTE (nada gravado no VUUPT)" if ultimo["modo_teste"] else "REAL"
    linhas_md.append(f"- **Quando:** {ultimo['quando']}")
    linhas_md.append(f"- **Modo:** {modo}")
    if ultimo.get("filtro"):
        linhas_md.append(f"- **Filtro:** `{ultimo['filtro']}`")
    linhas_md.append(f"- **Duração:** {ultimo['duracao_seg']:.0f}s")
    linhas_md.append(f"- **Pedidos processados:** {ultimo['total']}")
    linhas_md.append(f"- **Resultado:** {'COM ERROS' if c.get('erro') else 'OK'}")
    linhas_md.append("")
    linhas_md.append("| Resultado | Qtde |")
    linhas_md.append("|---|---|")
    for acao in ACOES_PRINCIPAIS + ["erro", "revisao"]:
        qtd = c.get(acao, 0)
        if acao in ("erro", "revisao") and qtd == 0:
            continue  # linhas de problema só aparecem quando existem
        if acao == "simulado" and qtd == 0:
            continue
        linhas_md.append(f"| {ROTULOS.get(acao, acao)} | {qtd} |")
    # Ações inesperadas (fora da lista fixa) também aparecem
    for acao, qtd in sorted(c.items()):
        if acao not in ACOES_PRINCIPAIS and acao not in ("erro", "revisao") and qtd:
            linhas_md.append(f"| {acao} | {qtd} |")
    linhas_md.append("")
    linhas_md.append(f"Detalhe por pedido: `{arq_csv.relative_to(RAIZ)}`")
    linhas_md.append("")

    # Problemas da última execução, visíveis sem abrir o CSV
    erros   = [r for r in resultados if r.get("erro")]
    revisao = [r for r in resultados if not r.get("erro") and r.get("requer_revisao")]
    if erros:
        linhas_md.append("### Erros")
        linhas_md.append("")
        for r in erros[:20]:
            linhas_md.append(f"- `#{r.get('codigo_ps','?')}`: {str(r['erro'])[:120]}")
        if len(erros) > 20:
            linhas_md.append(f"- ... e mais {len(erros) - 20} (ver CSV)")
        linhas_md.append("")
    if revisao:
        linhas_md.append("### Revisão manual")
        linhas_md.append("")
        for r in revisao[:20]:
            linhas_md.append(f"- `#{r.get('codigo_ps','?')}`: {str(r.get('observacao',''))[:120]}")
        if len(revisao) > 20:
            linhas_md.append(f"- ... e mais {len(revisao) - 20} (ver CSV)")
        linhas_md.append("")

    # Tabela das últimas 10 execuções
    ultimas = _ler_ultimas_execucoes(10)
    if ultimas:
        linhas_md.append("## Últimas execuções")
        linhas_md.append("")
        linhas_md.append("| Quando | Modo | Total | Criados | Atualizados | Sem alt. | Ignorados | Erros |")
        linhas_md.append("|---|---|---|---|---|---|---|---|")
        for reg in reversed(ultimas):
            cc = reg.get("contagem", {})
            ignorados = cc.get("ignorado_retirada", 0) + cc.get("pulado_atribuido", 0)
            linhas_md.append(
                f"| {reg.get('quando','?')} "
                f"| {'teste' if reg.get('modo_teste') else 'real'} "
                f"| {reg.get('total', 0)} "
                f"| {cc.get('criado', 0)} "
                f"| {cc.get('atualizado', 0)} "
                f"| {cc.get('pulado_sem_alteracao', 0)} "
                f"| {ignorados} "
                f"| {cc.get('erro', 0)} |"
            )
        linhas_md.append("")

    ARQ_STATUS.write_text("\n".join(linhas_md), encoding="utf-8")


def ultima_execucao() -> dict | None:
    """Retorna o registro da execução mais recente do pipeline (o mesmo
    conteúdo salvo em historico_execucoes.jsonl), ou None se não houver
    nenhuma ainda. Usado por notificar_execucao_agente.py pra montar o
    resumo por e-mail sem duplicar a leitura do JSONL."""
    execucoes = _ler_ultimas_execucoes(1)
    return execucoes[-1] if execucoes else None


def registrar_execucao(modo_teste: bool, filtro: str,
                       resultados: list[dict], duracao_seg: float):
    """
    Registra a execução completa: resumo no JSONL, detalhe por pedido
    em CSV, e regenera o STATUS_IMPORTACAO.md.

    Nunca levanta exceção — auditoria não pode derrubar o pipeline.
    """
    try:
        agora   = datetime.now()
        carimbo = agora.strftime("%Y%m%d_%H%M%S")
        # Garante nome único: se já existe CSV com esse carimbo (execuções
        # no mesmo segundo — raro, mas auditoria não pode sobrescrever),
        # acrescenta sufixo incremental.
        DIR_DETALHES.mkdir(parents=True, exist_ok=True)
        candidato, n = carimbo, 2
        while (DIR_DETALHES / f"importacao_{candidato}.csv").exists():
            candidato = f"{carimbo}_{n}"
            n += 1
        carimbo = candidato
        contagem = _contar(resultados)

        registro = {
            "quando":      agora.strftime("%d/%m/%Y %H:%M:%S"),
            "carimbo":     carimbo,
            "modo_teste":  modo_teste,
            "filtro":      filtro or "",
            "duracao_seg": round(duracao_seg, 1),
            "total":       len(resultados),
            "contagem":    contagem,
        }

        arq_csv = _gravar_detalhes_csv(carimbo, resultados)
        _gravar_resumo_jsonl(registro)
        _regenerar_status(registro, resultados, arq_csv)

        logger.info("=" * 60)
        logger.info("Arquivos desta execução:")
        logger.info(f"  Detalhe por pedido : {arq_csv.resolve()}")
        logger.info(f"  Status resumido    : {ARQ_STATUS.resolve()}")
        logger.info("=" * 60)
    except Exception as e:
        logger.warning(f"Falha ao registrar auditoria da execução (pipeline não afetado): {e}")
