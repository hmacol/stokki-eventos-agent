# -*- coding: utf-8 -*-
"""
Dados da tela /ciencias-dedicado (equipe acompanha a ciencia do dedicado fora do
dia fixo; spec docs/superpowers/specs/2026-11-17-ciencia-dedicado-dia-fixo-design.md, secao 6).
"""
import sys
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
if str(_RAIZ) not in sys.path:
    sys.path.insert(0, str(_RAIZ))
import ciencia_dedicado as cd  # noqa: E402
import preferencias_notificacao as pn  # noqa: E402

ROTULOS = {
    cd.PENDENTE: "Aguardando resposta",
    cd.CIENTE: "Ciente do dedicado",
    cd.REAGENDADO: "Reagendado",
    cd.ENCERRADO: "Encerrado",
}
CANAIS = ("email", "whatsapp", "chat")
_ORIGENS = {"email": "pelo link do e-mail", "whatsapp": "pelo link do WhatsApp", "chat": "pelo link do chat",
            "equipe": "pelo link do alerta da equipe"}


def rotulo_origem(origem) -> str:
    """De qual link veio a resposta (portal_cliente/link_ciencia: claim "o" do token)."""
    if not origem:
        return ""
    if origem.startswith("painel:"):
        return f"pelo link copiado do painel por {origem[len('painel:'):]}"
    return _ORIGENS.get(origem, f"pelo link ({origem})")


def nomes_embarcadores(db_path) -> dict:
    """{sender_id: nome}; banco sem o cadastro vale vazio (a tela cai pro CNPJ)."""
    try:
        embs = pn.carregar_embarcadores("agendamento", db_path=db_path)
    except Exception:
        return {}
    return {k: e.get("nome") or "" for k, e in embs.items()}


def _rotulo_situacao(c: dict) -> str:
    rotulo = ROTULOS.get(c["situacao"], c["situacao"])
    if c["situacao"] == cd.ENCERRADO and c.get("motivo_encerramento"):
        rotulo += f": {c['motivo_encerramento']}"
    return rotulo


def montar(conn, nomes: dict, situacao: str = "", embarcador: str = "") -> list[dict]:
    """Linhas da tabela: ciencia + nome do embarcador + avisos enviados por canal
    (so resultado 'enviado') + resposta do embarcador (so CIENTE/REAGENDADO)."""
    filtro = (embarcador or "").strip().lower()
    linhas = []
    for c in cd.listar(conn, situacao=situacao or None):
        nome = nomes.get(c["sender_id"]) or c.get("cnpj") or "—"
        if filtro and filtro not in nome.lower():
            continue
        contagem = {canal: 0 for canal in CANAIS}
        for e in cd.envios(conn, c["id"]):
            if e["resultado"] == "enviado" and e["canal"] in contagem:
                contagem[e["canal"]] += 1
        respondeu = c["situacao"] in (cd.CIENTE, cd.REAGENDADO)
        linhas.append({
            **c,
            "embarcador": nome,
            "situacao_rotulo": _rotulo_situacao(c),
            "avisos": contagem,
            "resposta_em": c.get("respondido_em") if respondeu else "",
            "resposta_por": c.get("respondido_por") if respondeu else "",
            "encerrado_em": c.get("respondido_em") if c["situacao"] == cd.ENCERRADO else "",
        })
    return linhas
