# -*- coding: utf-8 -*-
"""
Botao "Cancelar e notificar" do Planejamento (Hugo, 27/10/2026): depois que a
rota e cancelada na Vuupt (rascunhos_rota.cancelar_rota_enviada), avisa

  - a equipe, no grupo "Rotas canceladas" do /atendimento (criado se faltar):
    rota, motorista, motivo, todos os pedidos e os embarcadores afetados;
  - cada embarcador, numa conversa nova do chat do portal com os pedidos dele
    (fica em "Aguardando Fresh Log" na fila do /atendimento pra equipe voltar
    com a nova previsao).

O aviso ao embarcador passa pela tela Notificacoes (tipo "rota_cancelada"):
ENVIAR = chat do proprio embarcador; PILOTO = chat do cliente teste, com o
destino real no texto; NAO = so o grupo interno. Sem linha na tela vale o
forcar_destino dos chamados (preenchido = piloto). Falha aqui nunca desfaz o
cancelamento.

    py -3.11 -m unittest test_aviso_rota_cancelada   (de dentro de painel_agentes/)
"""
import logging
import sys
from datetime import date
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
for _p in (_RAIZ, _RAIZ / "portal_cliente"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import auth_cliente  # noqa: E402  (portal_cliente/auth_cliente.py)
import chamados as ch  # noqa: E402  (portal_cliente/chamados.py)
import controle_notificacoes as cn  # noqa: E402  (raiz)
import grupos_equipe  # noqa: E402

logger = logging.getLogger("painel.aviso_rota_cancelada")

NOME_GRUPO = "Rotas canceladas"
AUTOR_AVISO = "Planejamento"
TIPO_NOTIFICACAO = "rota_cancelada"
CNPJ_TESTE = "00000000000191"  # cliente teste do portal (reference_cliente_teste_portal)


def _data_br(valor) -> str:
    try:
        return date.fromisoformat(str(valor)[:10]).strftime("%d/%m")
    except ValueError:
        return str(valor or "")


def agrupar_por_embarcador(paradas: list[dict]) -> list[dict]:
    """[{sender_id, nome, codigos}] na ordem em que aparecem na rota."""
    grupos: dict = {}
    for p in paradas:
        chave = p.get("sender_id") or f"sem:{p.get('remetente_nome') or ''}"
        g = grupos.setdefault(chave, {"sender_id": p.get("sender_id"),
                                      "nome": p.get("remetente_nome") or "sem embarcador", "codigos": []})
        if p.get("codigo") and p["codigo"] not in g["codigos"]:
            g["codigos"].append(p["codigo"])
    return list(grupos.values())


def texto_embarcador(rascunho: dict, motivo: str, codigos: list[str]) -> str:
    plural = len(codigos) > 1
    return (f"A rota de entrega de {_data_br(rascunho.get('data_alvo'))} foi cancelada: {motivo}.\n"
            f"{'Pedidos afetados' if plural else 'Pedido afetado'}: {', '.join(codigos)}.\n"
            f"Nossa equipe está reprogramando a entrega e retorna por aqui com a nova previsão.")


def texto_grupo(rascunho: dict, motivo: str, por: str, embarcadores: list[dict]) -> str:
    n = sum(len(e["codigos"]) for e in embarcadores)
    linhas = [f"Rota cancelada · {rascunho.get('nome')} · {rascunho.get('motorista_nome') or 'sem motorista'} · "
              f"{_data_br(rascunho.get('data_alvo'))}",
              f"Motivo: {motivo}",
              f"Cancelada por {por}",
              f"{n} pedido(s) · {len(embarcadores)} embarcador(es):"]
    for e in embarcadores:
        linhas.append(f"{e['nome']} ({len(e['codigos'])}): {', '.join(e['codigos'])} · {e['aviso']}")
    return "\n".join(linhas)


def _embarcador_do_sender(conn, sender_id) -> dict | None:
    if not sender_id:
        return None
    row = conn.execute("SELECT cnpj_embarcador FROM interno WHERE sender_id = ?", (sender_id,)).fetchone()
    return auth_cliente.buscar_embarcador(conn, row["cnpj_embarcador"]) if row and row["cnpj_embarcador"] else None


def _avisar_embarcador(conn_ch, rascunho: dict, motivo: str, emb: dict, config: dict, db_path) -> str:
    """Abre a conversa no chat do portal; devolve o texto de situacao pro grupo."""
    cliente = _embarcador_do_sender(conn_ch, emb["sender_id"])
    if not cliente:
        return "NÃO avisado (sem cadastro em interno)"
    forcar = ch.cfg_chamados(config).get("forcar_destino") or ""
    decisao = cn.decidir(TIPO_NOTIFICACAO, cliente["cnpj"], fallback_acao=cn.acao_do_config(True, forcar),
                         fallback_piloto=forcar, db_path=db_path)
    texto = texto_embarcador(rascunho, motivo, emb["codigos"])
    if decisao.acao == "NAO":
        return f"NÃO avisado ({decisao.motivo})"
    if decisao.acao == "PILOTO":
        teste = auth_cliente.buscar_embarcador(conn_ch, CNPJ_TESTE)
        if not teste:
            return "NÃO avisado (piloto sem cliente teste)"
        texto = f"[Piloto · destino real: {cliente['nome']}]\n{texto}"
        cliente = teste
    chamado = ch.criar_chamado(conn_ch, cliente, ch.ORIGEM_SISTEMA, ch.STATUS_AGUARDANDO_FL,
                               assunto="Rota cancelada", area="entrega", pedido_ref=emb["codigos"][0])
    ch.mensagem_sistema(conn_ch, chamado, texto)
    return f"avisado no chat #{chamado['id']}" + (" (piloto: cliente teste)" if decisao.acao == "PILOTO" else "")


def _postar_grupo(texto: str, db_path) -> None:
    conn_g = grupos_equipe.conectar(db_path or grupos_equipe.DB_PATH)
    try:
        row = conn_g.execute("SELECT id FROM equipe_grupos WHERE lower(nome) = lower(?)", (NOME_GRUPO,)).fetchone()
        grupo_id = row["id"] if row else grupos_equipe.criar_grupo(conn_g, NOME_GRUPO, "sistema", AUTOR_AVISO)["id"]
        grupos_equipe.postar(conn_g, grupo_id, "sistema", AUTOR_AVISO, texto[:grupos_equipe.TEXTO_MAX])
    finally:
        conn_g.close()


def avisar(rascunho: dict, motivo: str, por: str, config: dict, db_path=None) -> dict:
    """Chamar so depois do cancelamento ter dado certo. Devolve
    {"grupo": bool, "embarcadores": [{nome, codigos, aviso}]}."""
    embarcadores = [e for e in agrupar_por_embarcador(rascunho.get("paradas") or []) if e["codigos"]]
    try:
        conn_ch = ch.conectar()
        try:
            for e in embarcadores:
                try:
                    e["aviso"] = _avisar_embarcador(conn_ch, rascunho, motivo, e, config, db_path)
                except Exception as erro:  # noqa: BLE001
                    logger.warning("rota %s: aviso a %s falhou: %s", rascunho.get("id"), e["nome"], erro)
                    e["aviso"] = f"NÃO avisado (erro: {erro})"
        finally:
            conn_ch.close()
    except Exception as erro:  # noqa: BLE001
        logger.warning("rota %s: chat do portal indisponivel: %s", rascunho.get("id"), erro)
        for e in embarcadores:
            e.setdefault("aviso", f"NÃO avisado (erro: {erro})")

    grupo_ok = True
    try:
        _postar_grupo(texto_grupo(rascunho, motivo, por, embarcadores), db_path)
    except Exception as erro:  # noqa: BLE001
        logger.warning("rota %s: aviso no grupo %s falhou: %s", rascunho.get("id"), NOME_GRUPO, erro)
        grupo_ok = False
    return {"grupo": grupo_ok, "embarcadores": [{k: e[k] for k in ("nome", "codigos", "aviso")} for e in embarcadores]}
