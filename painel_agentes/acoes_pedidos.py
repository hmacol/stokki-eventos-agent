# -*- coding: utf-8 -*-
"""
Acoes da tela /pedidos (Hugo, 10/11). Cada acao confere o status na Vuupt na
hora (o nucleo pode estar ate 15 min atrasado) e chama a funcao que ja roda na
Torre / portal / planejamento. Nenhuma acao manda e-mail ou WhatsApp.

Hoje: Reenvio (novo servico #PS-N-R<k> copiado do existente).
"""
import json
import logging
import re
import sys
import threading
import uuid
from datetime import date, datetime
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
_FALTAS_SEGUIDAS_VUUPT = 3   # -Rk que nao existe: so para depois de 3 seguidos (R1 apagado, R2 vivo)
_MAX_PS_STOKKI = 5           # cada PS leva ~30 s na Stokki
_MAX_EXPEDIR = 30            # pedidos por clique no Expedir na Stokki (uma execucao so)
AGENTE_EXPEDICAO = "somente_expedicao"

_RE_REENTREGA = re.compile(r"-R\d.*$")

# Um reenvio por pedido por vez (revisao final, 10/11): dois cliques ao mesmo tempo passavam os dois
# pelas conferencias. Uma trava por (sender_id, codigo base); a do registro so protege o dicionario.
_TRAVA_REGISTRO = threading.Lock()
_TRAVAS_REENVIO: dict = {}


def _trava_reenvio(chave) -> threading.Lock:
    with _TRAVA_REGISTRO:
        return _TRAVAS_REENVIO.setdefault(chave, threading.Lock())


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
    base = _codigo_base(codigo)
    sender_id = grupo[0]["sender_id"] if grupo else None
    # Conferir e criar sob a trava do pedido: o segundo clique so entra depois que o primeiro criou
    # (e o duplicar_pedido_manual, aqui dentro, confere o fingerprint ja_duplicado).
    with _trava_reenvio((sender_id, base)):
        return _conferir_e_criar(service_id, codigo, base, servico, grupo, insucesso, motivo, usuario,
                                 vuupt, stokki)


def _conferir_e_criar(service_id, codigo, base, servico, grupo, insucesso, motivo, usuario, vuupt, stokki) -> dict:
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
    # o codigo base, depois -R1, -R2... Um -Rk que falta nao encerra a busca (R1 apagado, R2 vivo):
    # so para depois de _FALTAS_SEGUIDAS_VUUPT faltas seguidas.
    candidatos = [base] + [f"#{base}-R{k}" for k in range(1, _MAX_TENTATIVAS_VUUPT + 1)]
    faltas = 0
    for cand in candidatos:
        achado = vuupt.buscar_servico_por_code(cand)
        if not achado:
            faltas += 1
            if faltas >= _FALTAS_SEGUIDAS_VUUPT:
                break
            continue
        faltas = 0
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
                   cancelar=None, vuupt=None) -> dict:
    """Cancela o servico na Vuupt (tira a parada do rascunho/rota). Recusa motorista a caminho/no local.
    Le o status antes: buscar_servico_por_id devolve None em qualquer falha e o cancelar_servico_completo
    pularia a guarda de andamento."""
    if cancelar is None:
        from roteirizacao.cancelar_servico import cancelar_servico_completo as cancelar
    vuupt = vuupt or _vuupt_padrao()
    atual = vuupt.buscar_servico_por_id(service_id)
    if not atual:
        return {"ok": False, "ja_estava": False, "erro": "não achei o serviço na Vuupt agora, tente de novo.", "aviso": ""}
    if atual.get("status") in ("on_route", "arrived"):
        return {"ok": False, "ja_estava": False, "aviso": "",
                "erro": "motorista a caminho ou no local: fale com ele antes de cancelar"}
    if atual.get("status") == "done":
        return {"ok": False, "ja_estava": False, "aviso": "",
                "erro": "pedido já finalizado na Vuupt: não dá pra cancelar"}
    r = cancelar(token, service_id, vuupt=vuupt)
    if not r.get("ok"):
        erro = r.get("erro") or "Falha ao cancelar na Vuupt."
        if "on_route" in erro or "arrived" in erro:
            erro = "motorista a caminho ou no local: fale com ele antes de cancelar"
        elif "'done'" in erro:
            erro = "pedido já finalizado na Vuupt: não dá pra cancelar"
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
    if len(ids) > _MAX_PS_STOKKI:
        return {"ok": False, "resultados": [], "erro": f"no máximo {_MAX_PS_STOKKI} pedidos por vez."}
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
                if r.get("ok"):
                    try:
                        from stokki import devolucao_estoque
                        devolucao_estoque.registrar(id_stokki, "pedidos", torre_controle._carregar_config(),
                                                    ja_estava=bool(r.get("ja_estava")))
                    except Exception as e:  # noqa: BLE001 -- a fila de devolucao nunca trava o cancelamento
                        logger.warning(f"[acoes_pedidos] PS-{id_stokki}: nao registrou na fila de devolucao: {e}")
                if r.get("ok") and r.get("ja_estava"):
                    # repetir depois de um poll perdido: a tratativa ainda guarda quem pediu
                    tratativas.registrar_evento(
                        codigo.strip(), "PEDIDOS", "CANCELADO_STOKKI",
                        texto=f"{usuario}: cancelado na Stokki (PS-{id_stokki}, já estava cancelado): {motivo}")
                elif r.get("ok"):
                    tratativas.registrar_evento(
                        codigo.strip(), "PEDIDOS", "CANCELADO_STOKKI",
                        texto=f"{usuario}: cancelado na Stokki (PS-{id_stokki}, situação: "
                              f"{r.get('situacao_antes') or '?'} → {r.get('situacao') or '?'}): {motivo}")
    except RuntimeError as e:
        if not resultados:
            return {"ok": False, "resultados": [], "erro": f"{e}, tente em alguns minutos."}
        raise
    falhas = [x for x in resultados if not x["ok"]]
    return {"ok": not falhas, "resultados": resultados,
            "erro": "; ".join(f"PS-{x['id_stokki']}: {x['erro']}" for x in falhas)}


def expedir_stokki(codigos, usuario: str, *, iniciar, pasta, coleta: int | None = None) -> dict:
    """Expedir na Stokki (Hugo, 12/11): poe na fila de execucoes do painel o
    expedir_pedidos.py so dos pedidos pedidos, no modo --tela-pedidos (solta da
    Expedicao Programada e aceita retirada/coleta pela transportadora). Quem
    confere entrega, OE e transportadora e o script; o resultado por pedido sai
    em andamento_expedicao. iniciar(args_extra) -> (execucao_id, entrou_na_fila)."""
    if not isinstance(codigos, list) or not codigos:
        return {"ok": False, "erro": "codigos ausente."}
    bases = list(dict.fromkeys(m.upper() for c in codigos for m in re.findall(r"PS-\d+", str(c), re.I)))
    if not bases:
        return {"ok": False, "erro": "código sem PS-<número>: não dá pra achar o pedido na Stokki."}
    if len(bases) > _MAX_EXPEDIR:
        return {"ok": False, "erro": f"no máximo {_MAX_EXPEDIR} pedidos por vez."}
    token = uuid.uuid4().hex
    arquivo = Path(pasta) / f"expedir_tela_{token}.json"
    # coleta = COL-n da tela /expedicao/coletas, ja confirmada com foto: so assim retirada nova expede
    args_coleta = ["--coleta", str(int(coleta))] if coleta else []
    execucao_id, na_fila = iniciar(["--pedido", *bases, "--tela-pedidos", *args_coleta,
                                    "--resultado-json", str(arquivo)])
    for base in bases:
        tratativas.registrar_evento(base, "PEDIDOS", "EXPEDIR_STOKKI",
                                    texto=(f"{usuario}: confirmou a coleta COL-{int(coleta)} na Expedição (com foto)"
                                           if coleta else f"{usuario}: pediu a expedição na Stokki pela tela Pedidos"))
    return {"ok": True, "execucao_id": execucao_id, "token": token, "na_fila": bool(na_fila), "codigos": bases}


def andamento_expedicao(execucao: dict | None, token: str, pasta) -> dict:
    """Estado de um Expedir na Stokki: na_fila | rodando | fim (+ resultados {PS-N: {ok, mensagem}}).
    execucao = linha de painel_execucoes do execucao_id devolvido por expedir_stokki."""
    if not execucao or execucao.get("agente_id") != AGENTE_EXPEDICAO or not re.fullmatch(r"[0-9a-f]{32}", token or ""):
        return {"ok": False, "erro": "Execução não encontrada."}
    if execucao["status"] in ("NA_FILA", "RODANDO"):
        return {"ok": True, "estado": execucao["status"].lower()}
    try:
        resultados = json.loads((Path(pasta) / f"expedir_tela_{token}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"ok": True, "estado": "fim", "resultados": {},
                "erro": f"A expedição terminou sem devolver o resultado (execução {execucao['id']}: {execucao['status']})."}
    return {"ok": True, "estado": "fim", "resultados": resultados}


def _servico_vivo(service_id: int, vuupt):
    """(servico, erro): relê o status na Vuupt; erro se sumiu ou não está mais vivo (done/insucesso/cancelado/excluído)."""
    servico = (vuupt or _vuupt_padrao()).buscar_servico_por_id(service_id)
    if not servico:
        return None, "não achei o serviço na Vuupt agora, tente de novo."
    if _terminal(servico):
        return None, "pedido já finalizado ou cancelado na Vuupt: não dá pra alterar."
    return servico, ""


def alterar_endereco(service_id: int, endereco: str, usuario: str, *, vuupt=None) -> dict:
    endereco = (endereco or "").strip()
    if not endereco:
        return {"ok": False, "erro": "Informe o novo endereço."}
    servico, erro = _servico_vivo(service_id, vuupt)
    if erro:
        return {"ok": False, "erro": erro}
    from roteirizacao import cancelar_servico
    import planejamento_rotas
    rascunho_id, _status = cancelar_servico.rascunho_do_servico(service_id)
    r = planejamento_rotas.editar_endereco_pedido(service_id, endereco, rascunho_id)
    if not r.get("ok"):
        return {"ok": False, "erro": r.get("erro") or "Falha ao alterar o endereço."}
    tratativas.registrar_evento(servico.get("code") or _codigo_do_servico(service_id), "PEDIDOS",
                                "ENDERECO_ALTERADO", service_id=service_id,
                                texto=f"{usuario}: endereço alterado para {endereco}")
    return {"ok": True, "erro": ""}


def agendar(service_id: int, data: str, hora_inicio: str, hora_fim: str, usuario: str, *, vuupt=None) -> dict:
    try:
        d = datetime.strptime(data or "", "%Y-%m-%d")
    except ValueError:
        return {"ok": False, "erro": "Data inválida (use AAAA-MM-DD)."}
    if d.date() < date.today():   # o painel roda em America/Sao_Paulo
        return {"ok": False, "erro": "data no passado: escolha hoje ou um dia depois."}
    servico, erro = _servico_vivo(service_id, vuupt)
    if erro:
        return {"ok": False, "erro": erro}
    import planejamento_rotas
    r = planejamento_rotas.reagendar_pedido(service_id, data, hora_inicio, hora_fim)
    if not r.get("ok"):
        return {"ok": False, "erro": r.get("erro") or "Falha ao agendar."}
    tratativas.registrar_evento(servico.get("code") or _codigo_do_servico(service_id), "PEDIDOS", "REAGENDADO",
                                service_id=service_id,
                                texto=f"{usuario}: agendado {d:%d/%m} {hora_inicio}–{hora_fim}")
    return {"ok": True, "erro": ""}


def _checklist_id(servico: dict):
    """Copia de portal_cliente.dados_cliente.checklist_id_do_servico (importar o portal puxa o app dele)."""
    w = (servico or {}).get("checklistAnswers")
    lista = w.get("data") if isinstance(w, dict) else w
    if not lista:
        return None
    cl = lista[0] or {}
    if int(cl.get("images_quantity") or 0) <= 0:
        return None
    return cl.get("id")


def comprovantes(codigo_servico: str, *, token: str, conn) -> list[dict]:
    """Canhotos do pedido, em ordem VUUPT, MANUAL, APP: [{origem, rotulo, url}]. Precisa de contexto de request (url_for).
    Vuupt fora do ar nao derruba as outras origens."""
    from flask import url_for
    from nucleo import canhotos_manuais
    codigo = canhotos_manuais.normalizar(codigo_servico)
    itens: list[dict] = []
    try:
        from notificacao_entregas import vuupt_entregas
        vistos = set()
        for servico in vuupt_entregas.buscar_por_codigo(token, codigo):
            cid = _checklist_id(servico)
            if cid and cid not in vistos:
                vistos.add(cid)
                insucesso = servico.get("status_done") == "failed"
                itens.append({"origem": "VUUPT",
                              "rotulo": "Foto do insucesso (Vuupt)" if insucesso else "Canhoto da Vuupt (PDF)",
                              "url": url_for("pedidos_canhoto_vuupt", checklist_id=int(cid), codigo=codigo)})
    except Exception as e:  # noqa: BLE001
        logger.warning("comprovantes %s: Vuupt indisponivel: %s", codigo, e)
    if canhotos_manuais.caminho_canhoto_manual(codigo):
        itens.append({"origem": "MANUAL", "rotulo": "Canhoto manual (PDF)",
                      "url": url_for("pedidos_canhoto_manual", codigo=codigo)})
    linhas = conn.execute(
        """SELECT c.id, c.tipo FROM nucleo_comprovantes c JOIN nucleo_paradas p ON p.id = c.parada_id
           WHERE UPPER(REPLACE(p.codigo, '#', '')) = ? AND c.caminho_local IS NOT NULL AND c.caminho_local != ''
           ORDER BY c.id""", (codigo,)).fetchall()
    for r in linhas:
        itens.append({"origem": "APP", "rotulo": f"Foto do app ({(r['tipo'] or '').lower()})",
                      "url": url_for("canhoto_foto", comprovante_id=r["id"])})
    return itens
