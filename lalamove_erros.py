# -*- coding: utf-8 -*-
"""
lalamove_erros.py

Nomenclatura das falhas de lancamento da corrida na Lalamove (Hugo, 27/11):
em vez do texto cru da excecao ("HTTP 422: ERR_..."), o card do /planejamento
e o log mostram "LAL-04 Telefone invalido -- o que fazer", com o texto cru
no fim, entre colchetes, pra quem precisar investigar.

A classificacao e por palavra-chave/status HTTP (a API da Lalamove nao tem
lista fechada de erros que a gente controle); o que nao casar vira LAL-99.
Ordem da tabela importa: o primeiro que casar ganha.
"""
import re

# (codigo, titulo, o que fazer, status HTTP ou None, regex no texto cru)
_REGRAS = [
    ("LAL-11", "Corrida criada, mas titulo da Vuupt nao atualizado",
     "a corrida JA existe na Lalamove; so falta o [LALAMOVE id] no titulo dos servicos na Vuupt -- nao lance de novo",
     None, r"t[ií]tulo n[aã]o atualizado"),
    ("LAL-01", "Configuracao da Lalamove incompleta",
     "ajustar a secao lalamove do config.yaml (chaves, remetente_telefone em +55...)",
     None, r"n[aã]o configurados|remetente_telefone"),
    ("LAL-02", "Rota nao esta pronta pra lancar",
     "conferir se a rota foi enviada a Vuupt, tem paradas e esta com o motorista virtual LALAMOVE",
     None, r"n[aã]o encontrado|sem paradas|ainda n[aã]o enviada|n[aã]o est[aá] com o motorista virtual"),
    ("LAL-08", "Lalamove recusou as credenciais",
     "conferir api_key/secret e se sandbox/producao estao corretos no config.yaml",
     None, r"HTTP 40[13]\b|unauthori[sz]ed|forbidden"),
    ("LAL-09", "Limite de requisicoes da Lalamove",
     "esperar alguns minutos e lancar de novo",
     None, r"HTTP 429\b|too.?many|too_frequent"),
    ("LAL-07", "Saldo insuficiente na Lalamove",
     "recarregar o credito da conta Lalamove e lancar de novo",
     None, r"credit|insufficient|saldo|balance"),
    ("LAL-04", "Telefone invalido",
     "corrigir o telefone do destinatario na Vuupt (ou o do remetente no config) e lancar de novo",
     None, r"phone|telefone"),
    ("LAL-03", "Endereco fora da area ou sem coordenadas",
     "conferir o endereco das paradas; fora da area de servico a Lalamove nao atende",
     None, r"out_of_service|service.?area|geocodific|coordenad|latitude|longitude"),
    ("LAL-05", "Veiculo ou opcional invalido",
     "trocar o veiculo/opcionais no card (nem todo opcional existe pra todo veiculo)",
     None, r"service.?type|special.?request|veiculo|ve[ií]culo"),
    ("LAL-06", "Cotacao inconsistente ou expirada",
     "lancar de novo (a cotacao vale 5 min e e refeita a cada tentativa)",
     None, r"quotation|stopids|cota[cç][aã]o"),
    ("LAL-10", "Lalamove fora do ar ou sem conexao",
     "tentar de novo mais tarde; se persistir, ver o status da Lalamove",
     None, r"HTTP 5\d\d\b|timeout|timed out|connection|max retries|ssl"),
]

CODIGO_NAO_CLASSIFICADO = "LAL-99"


def classificar(erro) -> dict:
    """Recebe a excecao (ou o texto) e devolve {codigo, titulo, acao, detalhe}."""
    detalhe = str(erro or "").strip()
    for codigo, titulo, acao, _, padrao in _REGRAS:
        if re.search(padrao, detalhe, re.IGNORECASE):
            return {"codigo": codigo, "titulo": titulo, "acao": acao, "detalhe": detalhe}
    return {"codigo": CODIGO_NAO_CLASSIFICADO, "titulo": "Falha nao classificada",
            "acao": "ver o detalhe abaixo e o log do painel; se repetir, avisar o time de TI",
            "detalhe": detalhe}


def formatar(erro) -> str:
    """Texto pro card/log: 'LAL-04 Titulo -- o que fazer [detalhe]'. Idempotente."""
    texto = str(erro or "").strip()
    if re.match(r"LAL-\d\d ", texto):
        return texto
    c = classificar(texto)
    saida = f"{c['codigo']} {c['titulo']} -- {c['acao']}"
    return f"{saida} [{c['detalhe']}]" if c["detalhe"] else saida
