# -*- coding: utf-8 -*-
"""
boleto_faltando.py

Peças do aviso de boleto faltando ao embarcador (06/10/2026, spec
docs/superpowers/specs/2026-10-06-clientes-boleto-aviso-design.md):
quais pedidos de uma data estão sem boleto (só clientes marcados
TEM_BOLETO em regras/clientes_boleto.py), quem já foi avisado, o e-mail
e o chamado. Usado por aviso_boleto_faltando.py (18h30, pedidos de
amanhã) e pelo resumo das 20h (entregues hoje).
"""
import html
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

_RAIZ = Path(__file__).parent
sys.path.insert(0, str(_RAIZ))

from email_utils import envelope_html  # noqa: E402
from regras.clientes_boleto import TEM_BOLETO, codigo_base, so_digitos  # noqa: E402

DB_PATH = _RAIZ / "dados" / "dados.db"
EMAIL_RESPOSTA = "entregas@freshlogbr.com"
URL_PORTAL = "https://app.freshhub.com.br/cliente"
AREA_CHAMADO = "boleto"


def whatsapp_segurado(config: dict, forcar_email: str) -> bool:
    """True quando o e-mail do aviso está em piloto (forcar_email preenchido) mas o
    WhatsApp de clientes está com envio real (whatsapp_notificacoes.clientes.forcar_destino
    vazio): o WhatsApp não pode sair antes de o Hugo liberar o aviso."""
    clientes = ((config or {}).get("whatsapp_notificacoes") or {}).get("clientes") or {}
    return bool(forcar_email) and not str(clientes.get("forcar_destino") or "").strip()


def pedidos_da_data(conn: sqlite3.Connection, data_iso: str) -> list[dict]:
    """Pedidos de entrega da data: os que já estão em rota no núcleo e os de
    rascunho vivo. O ramo do rascunho copia o critério de vigia/vigiar.py
    (coletar_fatos): status RASCUNHO/OFERTADA/ERRO_ENVIO, só o lote mais
    recente da data (o que a tela mostra) e pedido ainda sem rota (quem já
    tem rota é coberto pelo ramo da rota). Cancelado nunca entra."""
    rows = conn.execute("""
        SELECT p.codigo, p.sender_id, p.destinatario_codigo, p.destinatario_nome, p.remetente_nome
        FROM nucleo_rotas r JOIN nucleo_pedidos p ON p.vuupt_route_id = r.vuupt_route_id
        WHERE r.data_rota = ? AND r.cancelada_em IS NULL AND COALESCE(r.status, '') != 'CANCELADA'
          AND p.excluido_em IS NULL AND p.tipo = 'delivery' AND COALESCE(p.status, '') != 'CANCELADO'
        UNION ALL
        SELECT p.codigo, p.sender_id, p.destinatario_codigo, p.destinatario_nome, p.remetente_nome
        FROM rascunhos_rota r JOIN rascunhos_parada rp ON rp.rascunho_id = r.id
             JOIN nucleo_pedidos p ON p.vuupt_service_id = rp.service_id
        WHERE r.data_alvo = ? AND r.status IN ('RASCUNHO', 'OFERTADA', 'ERRO_ENVIO')
          AND r.lote_id = (SELECT lote_id FROM rascunhos_rota WHERE data_alvo = ? AND status != 'DESCARTADO'
                           ORDER BY criado_em DESC, id DESC LIMIT 1)
          AND p.vuupt_route_id IS NULL
          AND p.excluido_em IS NULL AND p.tipo = 'delivery' AND COALESCE(p.status, '') != 'CANCELADO'
    """, (data_iso, data_iso, data_iso)).fetchall()
    vistos, saida = set(), []
    for codigo, sid, dest, nome, emb in rows:
        base = codigo_base(codigo)
        if base in vistos:
            continue
        vistos.add(base)
        saida.append({"codigo": base, "codigo_servico": str(codigo or "").lstrip("#"), "sender_id": sid,
                      "documento": so_digitos(dest), "cliente": nome or "",
                      "embarcador": (emb or "").split(" - ")[-1].strip()})
    return saida


def faltando(conn: sqlite3.Connection, pedidos: list[dict], situacoes: dict[str, str]) -> list[dict]:
    """Pedidos de clientes TEM_BOLETO sem boleto ENVIADO. Também fica de fora o
    pedido cuja(s) NF(s) ENVIADA(s) têm todas cobranca = 0 (quadro de fatura
    sem duplicata = à vista): decisão de 08/10, falso aviso ao embarcador é o
    pior erro. cobranca NULL (não lida) ou -1 (indefinida) não dispensa."""
    alvo = [p for p in pedidos if situacoes.get(p["codigo"]) == TEM_BOLETO]
    if not alvo:
        return []
    marc = ",".join("?" * len(alvo))
    codigos = [p["codigo"] for p in alvo]
    com_boleto = {r[0] for r in conn.execute(
        f"SELECT DISTINCT codigo_pedido FROM documentos_processados WHERE status = 'ENVIADO' "
        f"AND tipo = 'Boleto' AND codigo_pedido IN ({marc})", codigos)}
    nfs = {r[0]: r[1] for r in conn.execute(
        f"SELECT codigo_pedido, MAX(numero_nf) FROM documentos_processados WHERE status = 'ENVIADO' "
        f"AND tipo = 'Nota Fiscal' AND codigo_pedido IN ({marc}) GROUP BY codigo_pedido", codigos)}
    a_vista = {r[0] for r in conn.execute(
        f"SELECT codigo_pedido FROM documentos_processados WHERE status = 'ENVIADO' "
        f"AND tipo = 'Nota Fiscal' AND codigo_pedido IN ({marc}) GROUP BY codigo_pedido "
        f"HAVING SUM(CASE WHEN cobranca = 0 THEN 0 ELSE 1 END) = 0", codigos)}
    return [{**p, "nf": nfs.get(p["codigo"]) or ""} for p in alvo
            if p["codigo"] not in com_boleto and p["codigo"] not in a_vista]


def _garantir_tabela(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS boleto_faltando_avisos (codigo TEXT, data_entrega TEXT, "
                 "sender_id INTEGER, avisado_em TEXT, canais TEXT, PRIMARY KEY (codigo, data_entrega))")


def ja_avisados(conn: sqlite3.Connection, data_iso: str) -> set[str]:
    _garantir_tabela(conn)
    return {r[0] for r in conn.execute("SELECT codigo FROM boleto_faltando_avisos WHERE data_entrega = ?",
                                       (data_iso,))}


def registrar_avisados(conn: sqlite3.Connection, itens: list[dict], data_iso: str, canais: list[str]) -> None:
    _garantir_tabela(conn)
    agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.executemany("INSERT OR IGNORE INTO boleto_faltando_avisos VALUES (?, ?, ?, ?, ?)",
                     [(i["codigo"], data_iso, i.get("sender_id"), agora, ",".join(canais)) for i in itens])
    conn.commit()


def agrupar_por_embarcador(itens: list[dict]) -> dict[int, list[dict]]:
    grupos: dict[int, list[dict]] = {}
    for i in itens:
        grupos.setdefault(i["sender_id"], []).append(i)
    return grupos


def email_aviso(nome_emb: str, itens: list[dict], data_entrega: str,
                destino_original: list[str] | None) -> tuple[str, str]:
    n = len(itens)
    assunto = f"[Freshlog] {n} pedido{'s' if n != 1 else ''} sem boleto para entrega em {data_entrega[:5]}"
    linhas = "".join(
        f"<tr><td style='padding:8px 10px;border-top:1px solid #E5E7EB;white-space:nowrap;'>"
        f"<strong>{html.escape(i.get('nf') or '—')}</strong><br><span style='font-size:12px;color:#6B7280;'>"
        f"{html.escape(i['codigo'])}</span></td>"
        f"<td style='padding:8px 10px;border-top:1px solid #E5E7EB;'>{html.escape(i.get('cliente') or '')}</td></tr>"
        for i in sorted(itens, key=lambda x: (x.get("nf") or "~", x["codigo"])))
    aviso = (f"<p style='margin:0 0 16px;padding:10px 14px;font-size:12px;background:#FEF3C7;border-radius:6px;'>"
             f"<strong>Redirecionado (piloto).</strong> Destino real: "
             f"{html.escape(', '.join(destino_original) or 'sem e-mail cadastrado')}</p>"
             if destino_original is not None else "")
    conteudo = f"""
    {aviso}
    <h2 style="margin:0 0 6px;font-size:19px;">Pedidos sem boleto · entrega em {html.escape(data_entrega)}</h2>
    <p style="margin:0 0 16px;font-size:14px;line-height:1.6;">Olá, <strong>{html.escape(nome_emb)}</strong>!
      Estes pedidos são de clientes que pagam com boleto e ainda não recebemos o boleto. Para ele seguir junto
      com a mercadoria, <strong>responda este e-mail com o boleto</strong> ou anexe no pedido da Stokki.</p>
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
           style="border:1px solid #E5E7EB;border-radius:8px;margin-bottom:20px;font-size:13px;">
      <tr style="background:#F3F4F6;"><td style="padding:8px 10px;font-weight:600;">NF / pedido</td>
      <td style="padding:8px 10px;font-weight:600;">Cliente</td></tr>
      {linhas}
    </table>
    <p style="margin:0 0 8px;font-size:13px;line-height:1.6;">Se algum desses clientes não paga com boleto,
      responda avisando que ajustamos aqui. Também abrimos um chamado no
      <a href="{URL_PORTAL}">portal do cliente</a> para acompanhar.</p>
    <p style="margin:0;font-size:13px;">Dúvidas: <a href="mailto:{EMAIL_RESPOSTA}">{EMAIL_RESPOSTA}</a></p>"""
    return assunto, envelope_html(conteudo, "Freshlog Logística - aviso automático de boleto faltando.")


def registrar_no_chamado(emb: dict, sender_id: int, texto: str, pedido_ref: str, config: dict,
                         modulo_chamados=None) -> int | None:
    """Mensagem no chamado de boleto aberto do embarcador, ou abre um
    (área 'boleto'). Devolve o id. Exceções propagam para quem chama (que decide se derruba o aviso)."""
    ch = modulo_chamados
    if ch is None:
        sys.path.insert(0, str(_RAIZ / "portal_cliente"))
        import chamados as ch
    conn = ch.conectar()
    try:
        r = conn.execute(f"SELECT id FROM portal_chamados WHERE cnpj_embarcador = ? AND area = '{AREA_CHAMADO}' "
                         f"AND status != 'RESOLVIDO' ORDER BY id DESC LIMIT 1", (emb.get("cnpj") or "",)).fetchone()
        if r:
            chamado = ch.buscar_chamado(conn, r[0])
        else:
            chamado = ch.criar_chamado(conn, {"cnpj": emb.get("cnpj") or "", "sender_id": sender_id,
                                              "nome": emb.get("nome") or ""},
                                       ch.ORIGEM_SISTEMA, ch.STATUS_AGUARDANDO_FL, assunto="Boleto faltando",
                                       area=AREA_CHAMADO, pedido_ref=pedido_ref)
            ch.whatsapp_para_atendimento(chamado, config)
        ch.mensagem_sistema(conn, chamado, texto)
        return chamado["id"]
    finally:
        conn.close()
