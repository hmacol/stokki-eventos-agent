# -*- coding: utf-8 -*-
"""
Acoes da tela /pedidos (Hugo, 10/11). Cada acao confere o status na Vuupt na
hora (o nucleo pode estar ate 15 min atrasado) e chama a funcao que ja roda na
Torre / portal / planejamento. Nenhuma acao manda e-mail ou WhatsApp.

Hoje: Reenvio (novo servico #PS-N-R<k> copiado do existente).
"""
import logging
import re
import sys
from pathlib import Path

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from nucleo import banco  # noqa: E402
import torre_controle  # noqa: E402

logger = logging.getLogger(__name__)

NIVEIS_ACAO = ("total", "operador", "logistica", "atendimento")
# Status da Vuupt em que o servico nao esta mais vivo.
VIVOS_VUUPT_FIM = ("done", "failed", "canceled")

_RE_REENTREGA = re.compile(r"-R\d.*$")


def _codigo_base(codigo: str) -> str:
    """Mesma regra de nucleo/tabela_pedidos: sem '#'/espacos a esquerda, maiusculo, sem -R<digito>..."""
    return _RE_REENTREGA.sub("", (codigo or "").lstrip("# ").upper())


def servicos_do_grupo(conn, service_id: int) -> list[dict]:
    """Linhas de nucleo_pedidos do mesmo pedido (empresa + codigo base) do servico."""
    atual = conn.execute("SELECT codigo, sender_id FROM nucleo_pedidos WHERE vuupt_service_id = ?",
                         (service_id,)).fetchone()
    if not atual:
        return []
    base = _codigo_base(atual["codigo"])
    linhas = conn.execute(
        "SELECT codigo, status, vuupt_service_id, sender_id, reentrega_de_codigo FROM nucleo_pedidos "
        "WHERE COALESCE(sender_id, -1) = COALESCE(?, -1) AND UPPER(codigo) LIKE ?",
        (atual["sender_id"], f"%{base}%")).fetchall()
    return [dict(l) for l in linhas if _codigo_base(l["codigo"]) == base]


def situacao_stokki(codigo: str) -> str | None:
    """Situacao do pedido na Stokki (primeiro PS-<id> do codigo), pela conta admin.
    None se a Stokki estiver ocupada ou a consulta falhar."""
    from stokki import sessao_uso
    m = re.search(r"PS-(\d+)", codigo or "", re.I)
    if not m:
        return None
    dono = "painel-pedidos-acao"
    conta = sessao_uso.adquirir(dono, ttl_segundos=300, esperar_segundos=60, alternativa=True)
    if not conta:
        return None
    try:
        from stokki.auth import StokkiSession
        from stokki.cancelar import situacao_e_token
        sessao = StokkiSession(torre_controle._carregar_config(), conta=conta)
        return situacao_e_token(sessao, int(m.group(1)))[0]
    except Exception as e:
        logger.warning(f"[acoes_pedidos] situacao na Stokki de {codigo} falhou: {e}")
        return None
    finally:
        sessao_uso.liberar(dono)


def _vuupt_padrao():
    from vuupt_client import VuuptClient
    return VuuptClient(torre_controle._carregar_config().get("vuupt_api", {}).get("token", ""))


def _erro(msg: str) -> dict:
    return {"ok": False, "novo_code": "", "erro": msg}


def reenviar(service_id: int, motivo: str, usuario: str, *, vuupt=None, stokki=situacao_stokki) -> dict:
    motivo = (motivo or "").strip()
    if not 5 <= len(motivo) <= 200:
        return _erro("Informe o motivo do reenvio (de 5 a 200 caracteres).")
    vuupt = vuupt or _vuupt_padrao()

    servico = vuupt.buscar_servico_por_id(service_id)
    if not servico:
        return _erro("não achei o serviço na Vuupt agora, tente de novo.")
    status = servico.get("status")
    if status not in VIVOS_VUUPT_FIM:
        return _erro("pedido ainda aberto: já existe um serviço vivo na Vuupt.")

    conn = banco.conectar()
    try:
        grupo = servicos_do_grupo(conn, service_id)
    finally:
        conn.close()
    for g in grupo:
        sid = g["vuupt_service_id"]
        if not sid or sid == service_id:
            continue
        outro = vuupt.buscar_servico_por_id(sid)
        if not outro:
            return _erro("não achei o serviço na Vuupt agora, tente de novo.")
        if outro.get("status") not in VIVOS_VUUPT_FIM:
            return _erro(f"a tentativa {g['codigo']} ainda está aberta na Vuupt.")

    codigo = servico.get("code") or ""
    situacao = stokki(codigo)
    if situacao is None:
        return _erro("não deu pra conferir a Stokki, tente de novo.")
    if "cancel" in situacao.lower():
        return _erro("pedido cancelado na Stokki.")

    r = torre_controle.duplicar_pedido_manual(
        service_id, codigo, origem="PEDIDOS", servico_original=servico,
        texto=f"{usuario}: Reenvio pela tela Pedidos → {{novo}}: {motivo}",
        marcar_tratado=(status == "failed"))
    if not r.get("ok"):
        return _erro(r.get("erro") or "Falha ao criar a reentrega.")
    return {"ok": True, "novo_code": r.get("novo_code", ""), "erro": ""}
