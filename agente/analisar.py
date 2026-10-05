# -*- coding: utf-8 -*-
"""
agente/analisar.py

Job do agente analista (timer stokki-agente-analisar, a cada 5 min). SO LE
o banco, como o vigia: nunca chama Stokki nem Vuupt.

A cada rodada:
  1. monta os FATOS: cada linha de vigia_pedidos (pedido aberto) e, quando
     a tabela existir, cada divergencia de batimento_pedidos;
  2. agente/regras.decidir diz as acoes;
  3. acao que ja existe em agente_acoes (mesmo pedido + fato + template) e
     ignorada -- idempotencia;
  4. AVISAR: manda ao embarcador (WhatsApp pelo OpenWA + e-mail), grava
     ENVIADA/FALHOU; com agente.ativo desligado grava DESLIGADA com o texto
     que teria saido (pra ver o que ele faria antes de ligar);
  5. PROPOR: grava PROPOSTA pra Torre -- ninguem executa nada aqui.

Config (config.yaml, secao `agente`):
  ativo: false                 # liga os avisos ao embarcador
  forcar_destino_email: ""     # piloto: todo e-mail vai so pra este endereco
  link_atendimento: https://app.freshhub.com.br/cliente
O WhatsApp tambem obedece whatsapp_notificacoes.clientes (ativo, teto,
forcar_destino) -- e o mesmo canal do aviso de chamado sem resposta.

Uso:
    venv/bin/python -m agente.analisar               # rodada normal
    venv/bin/python -m agente.analisar --modo-teste  # nao grava, nao envia; mostra o que faria
"""
import argparse
import logging
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
if str(_RAIZ) not in sys.path:
    sys.path.insert(0, str(_RAIZ))

import yaml  # noqa: E402

from agente import banco, regras, templates  # noqa: E402

logger = logging.getLogger("agente")


# --- Config -------------------------------------------------------------------

def carregar_config() -> dict:
    with open(_RAIZ / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _cfg(config: dict) -> dict:
    return (config or {}).get("agente") or {}


def ativo(config: dict) -> bool:
    return bool(_cfg(config).get("ativo", False))


# --- Fatos --------------------------------------------------------------------

def _tem_tabela(conn: sqlite3.Connection, nome: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (nome,)).fetchone() is not None


def fatos_do_vigia(conn: sqlite3.Connection) -> list[dict]:
    if not _tem_tabela(conn, "vigia_pedidos"):
        return []
    return [{"fonte": "vigia", **dict(r)} for r in conn.execute(
        "SELECT codigo, estado, motivo, desde, vence_em, vencido, service_id, detalhe FROM vigia_pedidos")]


def fatos_do_batimento(conn: sqlite3.Connection) -> list[dict]:
    """Divergencias abertas do batimento. A tabela e da spec do batimento
    (DOC_EXECUCAO_CLAUDE_BATIMENTO_PEDIDOS.md); enquanto nao existir, nada."""
    if not _tem_tabela(conn, "batimento_pedidos"):
        return []
    import json
    fatos = []
    for r in conn.execute("SELECT codigo, motivo, evidencias_json FROM batimento_pedidos "
                          "WHERE destino = 'DIVERGENCIA' AND tratado_em IS NULL"):
        try:
            ev = json.loads(r["evidencias_json"] or "{}")
        except ValueError:
            ev = {}
        fatos.append({"fonte": "batimento", "codigo": r["codigo"], "motivo": r["motivo"], "evidencias": ev})
    return fatos


# --- Contato do embarcador ----------------------------------------------------

def _pedido(conn: sqlite3.Connection, codigo: str) -> dict:
    if not _tem_tabela(conn, "nucleo_pedidos"):
        return {}
    r = conn.execute("SELECT remetente_nome, sender_id, destinatario_nome FROM nucleo_pedidos WHERE codigo = ?",
                     (codigo,)).fetchone()
    return dict(r) if r else {}


def _embarcadores(db_path: Path | None) -> dict:
    """{sender_id: {"nome","emails","cnpj","desligado"}} pelas preferencias do
    portal (tipo `insucesso`: e o aviso mais proximo do que o agente manda)."""
    try:
        import preferencias_notificacao as pn
        return pn.carregar_embarcadores("insucesso", chave="sender_id", db_path=db_path)
    except Exception as e:
        logger.warning(f"Sem cadastro de embarcadores para o agente: {e}")
        return {}


def _whatsapp(conn: sqlite3.Connection, cnpj) -> str | None:
    """Numero que o embarcador cadastrou no portal (preferencias_notificacao.whatsapp)."""
    if not cnpj or not _tem_tabela(conn, "preferencias_notificacao"):
        return None
    colunas = {r[1] for r in conn.execute("PRAGMA table_info(preferencias_notificacao)")}
    if "whatsapp" not in colunas:
        return None
    digitos = "".join(ch for ch in str(cnpj) if ch.isdigit())
    r = conn.execute("SELECT whatsapp FROM preferencias_notificacao WHERE cnpj_embarcador = ?", (digitos,)).fetchone()
    if not r or not r[0]:
        return None
    try:
        import preferencias_notificacao as pn
        return pn.normalizar_whatsapp(r[0]) or None
    except Exception:
        return None


def contato(conn: sqlite3.Connection, codigo: str, embarcadores: dict) -> dict:
    p = _pedido(conn, codigo)
    emb = embarcadores.get(p.get("sender_id")) or {}
    return {
        "remetente": emb.get("nome") or p.get("remetente_nome") or "",
        "destinatario": p.get("destinatario_nome") or "",
        "emails": list(emb.get("emails") or []),
        "desligado": bool(emb.get("desligado")),
        "whatsapp": _whatsapp(conn, emb.get("cnpj")),
    }


# --- Envio --------------------------------------------------------------------

def _enviar_whatsapp_padrao(telefone, texto, assinatura, config, modo_teste, **kw):
    import notificar_whatsapp as nw
    return nw.avisar_embarcador(telefone, texto, assinatura, config, tipo="agente", modo_teste=modo_teste, **kw)


def _enviar_email_padrao(destinatarios, assunto, corpo_html, config, modo_teste):
    import email_utils
    if modo_teste:
        logger.info(f"[MODO TESTE] E-mail nao enviado para {destinatarios}: {assunto}")
        return True
    html = email_utils.envelope_html(corpo_html)
    return email_utils.enviar_email(destinatarios, assunto, html, config.get("email", {}))


def _avisar(acao, ct: dict, config: dict, conn, modo_teste: bool, enviar_whatsapp, enviar_email) -> tuple[str, str, str]:
    """Devolve (status, resultado, texto_whatsapp)."""
    link = templates.link_central(config)
    dados = {**acao.dados, "remetente": ct["remetente"], "destinatario": ct["destinatario"]}
    texto = templates.whatsapp(acao.template, dados, link)
    assunto, corpo = templates.email(acao.template, dados, link)
    if not ativo(config):
        return banco.DESLIGADA, "agente.ativo desligado", texto

    partes = []
    if ct["whatsapp"]:
        sit = enviar_whatsapp(ct["whatsapp"], texto, f"agente:{acao.fato_origem}:{acao.template}",
                              config, modo_teste, conn=conn)
        partes.append(f"whatsapp={sit}")
    else:
        partes.append("whatsapp=sem_numero")

    forcar = str(_cfg(config).get("forcar_destino_email") or "").strip()
    destinos = [forcar] if forcar else ct["emails"]
    if ct["desligado"]:
        partes.append("email=embarcador_desligou")
    elif not destinos:
        partes.append("email=sem_endereco")
    else:
        if forcar:
            corpo = f'<p style="color:#b45309;"><b>PILOTO:</b> este e-mail iria para {", ".join(ct["emails"]) or "(sem e-mail)"}.</p>' + corpo
        ok = enviar_email(destinos, assunto, corpo, config, modo_teste)
        partes.append("email=" + ("enviado" if ok else "falhou"))

    resultado = " ".join(partes)
    chegou = any(p.endswith(("=enviado", "=modo_teste")) for p in partes)
    return (banco.ENVIADA if chegou else banco.FALHOU), resultado, texto


# --- Rodada -------------------------------------------------------------------

def executar(config: dict, conn: sqlite3.Connection | None = None, agora: datetime | None = None,
             modo_teste: bool = False, db_path: Path | None = None,
             enviar_whatsapp=_enviar_whatsapp_padrao, enviar_email=_enviar_email_padrao) -> dict:
    agora = agora or datetime.now()
    fechar = conn is None
    if fechar:
        conn = banco.conectar(db_path)
    else:
        banco.garantir_esquema(conn)
    resumo = {"fatos": 0, "acoes_novas": 0, "enviadas": 0, "desligadas": 0, "propostas": 0, "falhas": 0, "itens": []}
    rodada_id = None if modo_teste else banco.abrir_rodada(conn, modo_teste, agora)
    try:
        fatos = fatos_do_vigia(conn) + fatos_do_batimento(conn)
        resumo["fatos"] = len(fatos)
        embarcadores = _embarcadores(db_path or banco.DB_PATH)
        for fato in fatos:
            for acao in regras.decidir(fato):
                if banco.ja_existe(conn, acao.codigo, acao.fato_origem, acao.template):
                    continue
                resumo["acoes_novas"] += 1
                if acao.tipo == regras.PROPOR:
                    texto = templates.proposta(acao.template, acao.dados)
                    status, resultado, destinatario = banco.PROPOSTA, None, "torre"
                    resumo["propostas"] += 1
                else:
                    ct = contato(conn, acao.codigo, embarcadores)
                    status, resultado, texto = _avisar(acao, ct, config, conn, modo_teste, enviar_whatsapp, enviar_email)
                    destinatario = ct["whatsapp"] or (ct["emails"][0] if ct["emails"] else None)
                    chave = {banco.ENVIADA: "enviadas", banco.DESLIGADA: "desligadas"}.get(status, "falhas")
                    resumo[chave] += 1
                resumo["itens"].append({"codigo": acao.codigo, "tipo": acao.tipo, "template": acao.template,
                                        "status": status, "resultado": resultado, "texto": texto})
                if modo_teste:
                    logger.info(f"[MODO TESTE] {acao.tipo} {acao.template} {acao.codigo} -> {status} ({resultado})")
                    continue
                banco.registrar(conn, acao, status, destinatario=destinatario, texto=texto,
                                resultado=resultado, agora=agora)
                _registrar_tratativa(acao, status, texto)
    finally:
        if rodada_id is not None:
            banco.fechar_rodada(conn, rodada_id, resumo, agora)
        if fechar:
            conn.close()
    return resumo


def _registrar_tratativa(acao, status: str, texto: str | None) -> None:
    """Deixa rastro em /historico-tratativas. Best-effort: nunca derruba a rodada."""
    try:
        import tratativas
        tratativas.registrar_evento(acao.codigo, "agente", f"{acao.template}:{status}", texto=(texto or "")[:500])
    except Exception as e:
        logger.debug(f"tratativa nao registrada ({acao.codigo}): {e}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Agente analista: le vigia/batimento e decide avisos e propostas.")
    parser.add_argument("--modo-teste", action="store_true", help="nao grava nem envia; mostra o que faria")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s - %(message)s")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    config = carregar_config()
    resumo = executar(config, modo_teste=args.modo_teste)
    logger.info(f"Rodada: {resumo['fatos']} fatos, {resumo['acoes_novas']} acoes novas "
                f"({resumo['enviadas']} enviadas, {resumo['desligadas']} desligadas, "
                f"{resumo['propostas']} propostas, {resumo['falhas']} falhas). agente.ativo={ativo(config)}")
    for it in resumo["itens"][:50]:
        logger.info(f"  {it['codigo']:<14} {it['tipo']:<7} {it['template']:<38} {it['status']} {it['resultado'] or ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
