# -*- coding: utf-8 -*-
"""
Aviso do bloqueio por area nao atendida (Hugo, 23/09/2026): 1 chamado por
confirmacao do cliente (nao 1 por NF), mensagem de sistema no chat, e-mail
pro cliente e e-mail interno. Tudo redirecionado por
portal_cliente.envios.forcar_destino (default hugo@) ate o Hugo liberar
(forcar_destino: "" no config = envio real).
"""
import html
import logging
import sys
from pathlib import Path

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from email_utils import enviar_email, envelope_html  # noqa: E402

logger = logging.getLogger(__name__)
EMAIL_TESTE = "hugo@freshlogbr.com"
URL_PORTAL = "https://app.freshhub.com.br/cliente"
STATUS_BLOQUEADO = "AGUARDANDO_LIBERACAO"
AREA_CHAMADO = "envios"


def _chamados():
    import chamados as ch
    return ch


def _secao(config: dict) -> dict:
    return ((config or {}).get("portal_cliente", {}) or {}).get("envios", {}) or {}


def forcar_destino(config: dict) -> str:
    """Sem a chave no config o padrao e redirecionar pro Hugo (piloto)."""
    secao = _secao(config)
    if "forcar_destino" not in secao:
        return EMAIL_TESTE
    return str(secao.get("forcar_destino") or "").strip()


def _rotulo(c: dict) -> str:
    return c.get("numero_nf") or c.get("referencia") or f"#{c['id']}"


def texto_bloqueio(bloqueados: list[dict]) -> str:
    destinos = sorted({f"{c.get('destinatario_municipio') or '?'}/{c.get('destinatario_uf') or '?'}" for c in bloqueados})
    nfs = ", ".join(_rotulo(c) for c in bloqueados)
    plural = len(bloqueados) > 1
    return (f"Não atendemos a região de {', '.join(destinos)}. "
            f"{'As NFs' if plural else 'A NF'} {nfs} {'estão' if plural else 'está'} aguardando liberação e não "
            f"{'foram enviadas' if plural else 'foi enviada'} à Stokki.\n"
            f"Você pode solicitar uma cotação de envio dedicado, falar com a equipe Fresh Log ou cancelar o envio, "
            f"pela aba Envios do portal.")


def avisar_cancelamento(envio: dict, por: str) -> bool:
    """Cliente cancelou um envio que estava bloqueado: mensagem de sistema no
    chamado do bloqueio (spec, casos de borda). Falha nao derruba a acao."""
    chamado_id = envio.get("bloqueio_chamado_id")
    if not chamado_id:
        return False
    try:
        ch = _chamados()
        conn_ch = ch.conectar()
        try:
            chamado = ch.buscar_chamado(conn_ch, chamado_id)
            if not chamado:
                return False
            quem = "pela equipe Fresh Log" if str(por).startswith("equipe") else "pelo cliente"
            ch.mensagem_sistema(conn_ch, chamado, f"NF {_rotulo(envio)} cancelada {quem} pela aba Envios. Não será enviada à Stokki.")
            return True
        finally:
            conn_ch.close()
    except Exception as e:
        logger.warning(f"bloqueio: nao registrou cancelamento no chamado {chamado_id}: {e}")
        return False


def abrir_bloqueio(conn, cliente: dict, criados: list[dict], config: dict) -> int | None:
    """`conn` e a conexao de envio_pedidos (portal_envios). `cliente` =
    {cnpj, sender_id, nome}. Devolve o id do chamado ou None."""
    bloqueados = [c for c in criados if c.get("status") == STATUS_BLOQUEADO]
    if not bloqueados:
        return None
    ch = _chamados()
    nfs = ", ".join(_rotulo(c) for c in bloqueados)
    texto = texto_bloqueio(bloqueados)
    try:
        # conexao propria: chamados.conectar() garante as tabelas portal_chamados*
        conn_ch = ch.conectar()
        try:
            chamado = ch.criar_chamado(conn_ch, cliente, ch.ORIGEM_SISTEMA, ch.STATUS_AGUARDANDO_FL,
                                       assunto="Envio para região não atendida", area=AREA_CHAMADO, pedido_ref=f"NF {nfs}")
            ch.mensagem_sistema(conn_ch, chamado, texto)
            chamado_id = chamado["id"]
            emails_cliente = ch.emails_do_cliente(conn_ch, cliente.get("cnpj"))
        finally:
            conn_ch.close()
    except Exception as e:
        # Sem chamado nao ha como liberar (o painel libera pelo chamado), entao
        # fail-open: volta pra NA_FILA e avisa a equipe (revisao 23/09).
        logger.exception(f"bloqueio: nao abriu chamado ({e}); {len(bloqueados)} NF(s) voltam pra NA_FILA")
        ids = [c["id"] for c in bloqueados]
        conn.execute(f"UPDATE portal_envios SET status = 'NA_FILA' WHERE id IN ({','.join('?' * len(ids))}) AND status = ?",
                     (*ids, STATUS_BLOQUEADO))
        conn.commit()
        email_cfg = (config or {}).get("email", {}) or {}
        forcar = forcar_destino(config)
        interno = email_cfg.get("email_atendimento") or email_cfg.get("email_responsavel")
        if interno or forcar:
            enviar_email([forcar] if forcar else [interno], f"[Portal] Bloqueio de área SEM CHAMADO · NF {nfs} · {cliente.get('nome')}",
                         envelope_html(f"<p><b>{html.escape(cliente.get('nome') or '')}</b> enviou pedido(s) para região não atendida, "
                                       f"mas o chat não abriu ({html.escape(str(e))}). As NFs {html.escape(nfs)} <b>seguiram pra Stokki</b> "
                                       f"sem liberação -- conferir manualmente.</p>",
                                       rodape="Fresh Log · Portal do cliente · bloqueio", cor_acento="#EF4444"), email_cfg)
        return None
    conn.execute(f"UPDATE portal_envios SET bloqueio_chamado_id = ? WHERE id IN ({','.join('?' * len(bloqueados))})",
                 (chamado_id, *[c["id"] for c in bloqueados]))
    conn.commit()

    email_cfg = (config or {}).get("email", {}) or {}
    forcar = forcar_destino(config)
    nome = html.escape(cliente.get("nome") or "")
    corpo_html = "<p>" + html.escape(texto).replace("\n", "</p><p>") + "</p>"
    aviso = (f"<p style='background:#FFF4D6;padding:8px;border-radius:6px'>Redirecionado (piloto). Destino real: "
             f"{html.escape(', '.join(emails_cliente) or '(cliente sem e-mail)')}</p>" if forcar else "")
    destinos_cliente = [forcar] if forcar else emails_cliente
    if destinos_cliente:
        enviar_email(destinos_cliente, f"[Fresh Log] Envio aguardando liberação · NF {nfs}",
                     envelope_html(f"<p>Olá, <b>{nome}</b>.</p>{corpo_html}<p><a href='{URL_PORTAL}'>Abrir o portal</a></p>{aviso}",
                                   rodape="Fresh Log · Portal do cliente", cor_acento="#EF4444"), email_cfg)
    interno = email_cfg.get("email_atendimento") or email_cfg.get("email_responsavel")
    if interno:
        enviar_email([forcar] if forcar else [interno], f"[Portal] Bloqueio de área · NF {nfs} · {cliente.get('nome')}",
                     envelope_html(f"<p><b>{nome}</b> enviou pedido(s) para região não atendida. "
                                   f"Chamado #{chamado_id} no atendimento.</p>{corpo_html}",
                                   rodape="Fresh Log · Portal do cliente · bloqueio", cor_acento="#EF4444"), email_cfg)
    return chamado_id
