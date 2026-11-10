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
import uuid
from pathlib import Path

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from nucleo import banco  # noqa: E402
import torre_controle  # noqa: E402
import tratativas  # noqa: E402

logger = logging.getLogger(__name__)

NIVEIS_ACAO = ("total", "operador", "logistica", "atendimento")
# Status da Vuupt em que o servico nao esta mais vivo.
VIVOS_VUUPT_FIM = ("done", "failed", "canceled", "cancelled")
_MAX_TENTATIVAS_VUUPT = 20

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
        "SELECT codigo, status, vuupt_service_id, sender_id, reentrega_de_codigo, excluido_em FROM nucleo_pedidos "
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
    dono = f"painel-pedidos-acao-{uuid.uuid4().hex[:8]}"  # dono unico: o mesmo dono nao trava a si mesmo
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


def _terminal(servico: dict) -> bool:
    """Servico que nao esta mais vivo na Vuupt: excluido, concluido ou cancelado."""
    return bool(servico.get("deleted_at")) or servico.get("status") in VIVOS_VUUPT_FIM


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
    if not _terminal(servico):
        return _erro("pedido ainda aberto: já existe um serviço vivo na Vuupt.")
    codigo = servico.get("code") or ""
    if (servico.get("title") or "").lstrip("# ").upper().startswith("[RETIRADA]"):
        return _erro("retirada não vai pra Vuupt: reenvio não se aplica.")
    # insucesso = done com status_done failed (a Vuupt nao tem status top-level failed)
    insucesso = servico.get("status") == "done" and servico.get("status_done") == "failed"

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
            if g.get("excluido_em"):
                continue  # apagado na Vuupt: morto
            return _erro("não achei o serviço na Vuupt agora, tente de novo.")
        if not _terminal(outro):
            return _erro(f"a tentativa {g['codigo']} ainda está aberta na Vuupt.")

    # Pergunta direto a Vuupt por todas as tentativas (o nucleo pode nao conhecer alguma):
    # o codigo base, depois -R1, -R2... (mesmo laco de duplicar_servico_por_insucesso).
    base = _codigo_base(codigo)
    candidatos = [base] + [f"#{base}-R{k}" for k in range(1, _MAX_TENTATIVAS_VUUPT + 1)]
    for i, cand in enumerate(candidatos):
        achado = vuupt.buscar_servico_por_code(cand)
        if not achado:
            if i == 0:
                continue  # sem o base na Vuupt, ainda pode haver -R1
            break
        if achado.get("id") == service_id:
            continue
        if not _terminal(achado):
            return _erro(f"a tentativa {achado.get('code') or cand} ainda está aberta na Vuupt.")
    situacao = stokki(codigo)
    if situacao is None:
        return _erro("não deu pra conferir a Stokki, tente de novo.")
    if "cancel" in situacao.lower():
        return _erro("pedido cancelado na Stokki.")

    r = torre_controle.duplicar_pedido_manual(
        service_id, codigo, origem="PEDIDOS", servico_original=servico,
        texto=f"{usuario}: Reenvio pela tela Pedidos → {{novo}}: {motivo}",
        marcar_tratado=insucesso)
    if not r.get("ok"):
        return _erro(r.get("erro") or "Falha ao criar a reentrega.")
    return {"ok": True, "novo_code": r.get("novo_code", ""), "erro": ""}


def _codigo_do_servico(service_id: int) -> str:
    conn = banco.conectar()
    try:
        r = conn.execute("SELECT codigo FROM nucleo_pedidos WHERE vuupt_service_id = ?", (service_id,)).fetchone()
        return r["codigo"] if r else ""
    finally:
        conn.close()


def _agente_da_ultima_rota(service_id: int):
    conn = banco.conectar()
    try:
        r = conn.execute(
            "SELECT r.agent_id FROM nucleo_paradas p JOIN nucleo_rotas r ON r.id = p.rota_id "
            "WHERE p.service_id = ? ORDER BY r.data_rota DESC, r.id DESC LIMIT 1", (service_id,)).fetchone()
        return r["agent_id"] if r else None
    finally:
        conn.close()


def cancelar_vuupt(service_id: int, usuario: str, *, token: str, agent_lalamove: int = 0,
                   cancelar=None) -> dict:
    """Cancela o servico na Vuupt (tira a parada do rascunho/rota). Recusa motorista a caminho/no local."""
    if cancelar is None:
        from roteirizacao.cancelar_servico import cancelar_servico_completo as cancelar
    r = cancelar(token, service_id)
    if not r.get("ok"):
        erro = r.get("erro") or "Falha ao cancelar na Vuupt."
        if "on_route" in erro or "arrived" in erro:
            erro = "motorista a caminho ou no local: fale com ele antes de cancelar"
        return {"ok": False, "ja_estava": False, "erro": erro, "aviso": ""}
    aviso = ""
    if agent_lalamove and _agente_da_ultima_rota(service_id) == agent_lalamove:
        aviso = "Pedido de rota Lalamove: cancelar aqui NÃO cancela na Lalamove, avise por lá."
    if not r.get("ja_estava"):
        tratativas.registrar_evento(_codigo_do_servico(service_id), "PEDIDOS", "CANCELADO_MANUAL",
                                    service_id=service_id,
                                    texto=f"{usuario}: cancelado na Vuupt pela tela Pedidos")
    return {"ok": True, "ja_estava": bool(r.get("ja_estava")), "erro": "", "aviso": aviso}


def _abrir_sessao_stokki():
    """Gerenciador de contexto: pega a trava da Stokki (dono unico por chamada, ate 60 s de espera,
    conta principal ou reserva) e entrega a StokkiSession. RuntimeError se ocupada."""
    import contextlib
    from stokki import sessao_uso

    @contextlib.contextmanager
    def _ctx():
        dono = f"painel-pedidos-acao-{uuid.uuid4().hex[:8]}"
        conta = sessao_uso.adquirir(dono, ttl_segundos=300, esperar_segundos=60, alternativa=True)
        if not conta:
            raise RuntimeError("Stokki ocupada por outra rotina")
        try:
            from stokki.auth import StokkiSession
            yield StokkiSession(torre_controle._carregar_config(), conta=conta)
        finally:
            sessao_uso.liberar(dono)
    return _ctx()


def cancelar_stokki(codigo: str, motivo: str, usuario: str, *, abrir_sessao=None) -> dict:
    """Cancela na Stokki os pedidos PS-<id> do codigo (so a Stokki; a Vuupt e outra acao)."""
    motivo = (motivo or "").strip()
    if len(motivo) < 15:
        return {"ok": False, "resultados": [], "erro": "Informe o motivo do cancelamento (mínimo de 15 caracteres)."}
    ids = list(dict.fromkeys(int(i) for i in re.findall(r"PS-(\d+)", (codigo or "").upper())))
    if not ids:
        return {"ok": False, "resultados": [], "erro": "código sem PS-<número>: não dá pra achar o pedido na Stokki."}
    from stokki import cancelar
    abrir_sessao = abrir_sessao or _abrir_sessao_stokki
    resultados = []
    try:
        with abrir_sessao() as sessao:
            for id_stokki in ids:
                try:
                    r = cancelar.cancelar_pedido(sessao, id_stokki, motivo,
                                                 prefixo=f"Fresh Hub Pedidos ({usuario})")
                except Exception as e:
                    logger.warning(f"[acoes_pedidos] cancelar PS-{id_stokki} na Stokki falhou: {e}")
                    r = {"ok": False, "situacao": "", "erro": f"falha ao falar com a Stokki: {e}"}
                resultados.append({"id_stokki": id_stokki, "ok": bool(r.get("ok")),
                                   "situacao": r.get("situacao") or "", "erro": r.get("erro") or ""})
                if r.get("ok") and not r.get("ja_estava"):
                    tratativas.registrar_evento(
                        codigo.strip(), "PEDIDOS", "CANCELADO_STOKKI",
                        texto=f"{usuario}: cancelado na Stokki (PS-{id_stokki}, situação depois: "
                              f"{r.get('situacao') or '?'}): {motivo}")
    except RuntimeError as e:
        if not resultados:
            return {"ok": False, "resultados": [], "erro": f"{e}, tente em alguns minutos."}
        raise
    falhas = [x for x in resultados if not x["ok"]]
    return {"ok": not falhas, "resultados": resultados,
            "erro": "; ".join(f"PS-{x['id_stokki']}: {x['erro']}" for x in falhas)}
