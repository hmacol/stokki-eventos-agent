# -*- coding: utf-8 -*-
"""
notificacao_entregas/notificar_entrega_concluida.py

E-mail por PEDIDO pro embarcador assim que a entrega é concluída na
Vuupt -- sucesso (com o canhoto em PDF anexo, se houver) ou falha (com o
motivo). Pedido do Hugo, 17/09; substitui o e-mail de "finalizado" da
Vuupt. Especificação: DOC_EXECUCAO_CLAUDE_NOTIFICACAO_ENTREGAS.md.

Roda a cada 5 min (infra/stokki-notificar-entregas.timer). O estado por
serviço (fingerprint_notificacao_entrega.py) garante 1 e-mail por pedido.

Travas (config.yaml -> notificacao_entregas, defaults seguros):
  - ativo: false        -> só registra IGNORADO (ligar vale dali pra frente)
  - forcar_destino: hugo@ -> todo e-mail vai só pra ele, com faixa "iria para X"
Envio real ao embarcador = ativo: true E forcar_destino: "".

--modo-teste ignora a flag `ativo`, manda no máximo --limite e-mails pra
hugo@ e NÃO grava nada no banco.

COMO USAR:
    py -3.11 notificacao_entregas/notificar_entrega_concluida.py --modo-teste
    py -3.11 notificacao_entregas/notificar_entrega_concluida.py --modo-teste --pedido PS-36327
    py -3.11 notificacao_entregas/notificar_entrega_concluida.py
"""
import argparse
import logging
import re
import sqlite3
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

_RAIZ_LOCAL = Path(__file__).parent
_RAIZ_PROJETO = _RAIZ_LOCAL.parent
if str(_RAIZ_PROJETO) not in sys.path:
    sys.path.insert(0, str(_RAIZ_PROJETO))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

from notificacao_entregas import fingerprint_notificacao_entrega as fp  # noqa: E402
from notificacao_entregas import regras_entrega as regras  # noqa: E402
from notificacao_entregas.montar_email_entrega import dados_do_servico, montar_assunto, montar_html  # noqa: E402

logger = logging.getLogger("notificar_entregas")

EMAIL_TESTE = "hugo@freshlogbr.com"
REPLY_TO = "entregas@freshlogbr.com"
PORTAL_URL_PADRAO = "https://app.freshhub.com.br/cliente"
# Gmail recusa mensagem acima de 25 MB (e o base64 infla ~33%).
MAX_BYTES_ANEXO = 15 * 1024 * 1024
# Teto por rodada: se o timer ficou parado, a fila escoa em algumas
# rodadas em vez de uma rajada só (o que sobra entra na próxima).
MAX_ENVIOS_POR_RODADA = 60
LIMITE_MODO_TESTE = 3
PAUSA_ENTRE_ENVIOS_SEG = 1.0


def _configurar_log() -> None:
    (_RAIZ_LOCAL / "dados").mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(_RAIZ_LOCAL / "dados" / "notificar_entregas.log", encoding="utf-8"),
        ],
    )


def _carregar_config() -> dict:
    import yaml
    with open(_RAIZ_PROJETO / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


# ── Banco ──────────────────────────────────────────────────────────────────────

def carregar_embarcadores(conn: sqlite3.Connection) -> dict[int, dict]:
    """sender_id -> {"nome","emails","notificar_email","fator_ponderado"} a
    partir de `interno`. Um embarcador pode ter mais de uma linha (filiais
    com o mesmo sender_id): os e-mails são unidos, sem repetir; basta uma
    linha com notificar_email=0 pra ninguém daquele sender receber."""
    embs: dict[int, dict] = {}
    for row in conn.execute(
            "SELECT sender_id, nome_remetente, apelido, email, notificar_email, fator_ponderado "
            "FROM interno WHERE sender_id IS NOT NULL ORDER BY cnpj_embarcador"):
        emails = [e.strip() for e in re.split(r"[,;\t]+", row["email"] or "") if e.strip() and "@" in e]
        notificar = 1 if row["notificar_email"] is None else int(row["notificar_email"])
        atual = embs.setdefault(int(row["sender_id"]), {
            "nome": "", "emails": [], "notificar_email": 1, "fator_ponderado": row["fator_ponderado"]})
        atual["nome"] = atual["nome"] or row["apelido"] or row["nome_remetente"] or ""
        atual["emails"] += [e for e in emails if e not in atual["emails"]]
        atual["notificar_email"] = min(atual["notificar_email"], notificar)
    return embs


def aplicar_preferencias(embs: dict[int, dict], db_path: Path) -> dict[int, dict]:
    """Sobrepoe as preferencias do portal do cliente (botao Notificacoes,
    preferencias_notificacao.py, tipo "entrega_concluida"): e-mail proprio
    de notificacoes e a chave liga/desliga do cliente. Tolerante de
    proposito: sem o modulo (ainda nao deployado) ou com erro nele, vale o
    cadastro da `interno` -- a rotina de 5 min nao pode cair por isso.

    interno.notificar_email = 0 (Hugo, 17/09): nao e veto pra este e-mail, o
    embarcador so NASCE com a chave desmarcada e liga sozinho no portal."""
    try:
        import preferencias_notificacao
        prefs = preferencias_notificacao.carregar_embarcadores("entrega_concluida", db_path=db_path)
    except Exception as e:
        logger.warning(f"Preferencias de notificacao indisponiveis ({e}) -- usando so o cadastro interno.")
        return embs
    for sender_id, emb in embs.items():
        pref = prefs.get(sender_id)
        if pref:
            emb["emails"] = pref.get("emails") or emb["emails"]
            emb["desligado"] = bool(pref.get("desligado"))
            # Com as preferencias no ar, interno.notificar_email = 0 nao e veto:
            # so faz a chave nascer desmarcada (ja embutido em "desligado"). Sem
            # o modulo, a coluna segue bloqueando -- nao ha como o cliente optar.
            emb["notificar_email"] = 1
    return embs


def buscar_nf_no_banco(conn: sqlite3.Connection, codigo: str) -> str:
    """Número(s) da NF do pedido -- mesma fonte da coluna NF do portal
    (documentos_processados). Vazio se não achar; nunca trava o envio."""
    try:
        rows = conn.execute(
            "SELECT DISTINCT numero_nf FROM documentos_processados "
            "WHERE status='ENVIADO' AND tipo='Nota Fiscal' AND numero_nf IS NOT NULL AND codigo_pedido = ?",
            (regras.codigo_base(codigo),)).fetchall()
        return ", ".join(sorted(str(r[0]) for r in rows if r[0]))
    except sqlite3.Error as e:
        logger.warning(f"NF de {codigo} indisponivel: {e}")
        return ""


# ── Núcleo ─────────────────────────────────────────────────────────────────────

def _ordem_modo_teste(servicos: list[dict]) -> list[dict]:
    """Amostra do modo teste: mais recentes primeiro, com 1 falha na
    frente (se houver) pra sempre dar pra ver os dois layouts."""
    recentes = sorted(servicos, key=lambda s: s.get("completed_at") or "", reverse=True)
    falhas = [s for s in recentes if s.get("status_done") == "failed"]
    return falhas[:1] + [s for s in recentes if s not in falhas[:1]]


def processar(servicos: list[dict], *, embarcadores: dict[int, dict], cfg: regras.ConfigEntregas,
              agora: datetime, conn: sqlite3.Connection, enviar, baixar_canhoto, buscar_nf,
              portal_url: str, promete_email_reenvio: bool, modo_teste: bool = False,
              max_envios: int = MAX_ENVIOS_POR_RODADA, forcar_pedido: bool = False,
              dormir=time.sleep) -> dict:
    """
    Decide e envia. Dependências injetadas (testável sem rede):
      enviar(destinatarios, assunto, corpo_html, cc=, anexos=, cabecalhos_extra=) -> bool
      baixar_canhoto(checklist_id, codigo) -> Path | None
      buscar_nf(codigo) -> str
    forcar_pedido (só com modo_teste, --pedido): ignora a idade da entrega
    e a espera do canhoto.
    """
    c = {"vistos": len(servicos), "enviados": 0, "com_canhoto": 0, "sem_canhoto": 0, "falhas_de_entrega": 0,
         "retiradas": 0,
         "aguardando_canhoto": 0, "ignorados": 0, "sem_destinatario": 0, "falhas_envio": 0, "adiados": 0,
         "erros_definitivos": []}

    if modo_teste:
        cfg = replace(cfg, ativo=True, forcar_destino=EMAIL_TESTE, embarcadores_piloto=[])
        if forcar_pedido:
            cfg = replace(cfg, max_atraso_horas=24 * 3650, espera_canhoto_min=0)
        estados_atuais: dict[int, str] = {}
        fila = _ordem_modo_teste(servicos)
    else:
        estados_atuais = fp.estados(conn, [s.get("id") for s in servicos])
        fila = sorted(servicos, key=lambda s: s.get("completed_at") or "")

    def _registrar(servico, estado, **kw):
        if not modo_teste:
            fp.registrar(conn, servico, estado, **kw)

    for servico in fila:
        sid = servico.get("id")
        codigo = regras.codigo_limpo(servico.get("code"))
        estado_atual = estados_atuais.get(sid)
        embarcador = embarcadores.get(servico.get("sender_id"))
        d = regras.decidir(servico, embarcador, cfg, agora, estado_atual)

        if d.acao == regras.NADA:
            continue
        if d.acao == regras.IGNORAR:
            c["ignorados"] += 1
            _registrar(servico, regras.ESTADO_IGNORADO, motivo=d.motivo)
            continue
        if d.acao == regras.SEM_DESTINATARIO:
            c["sem_destinatario"] += 1
            logger.warning(f"  {codigo}: {d.motivo} (sender_id {servico.get('sender_id')}) -- nao notificado.")
            _registrar(servico, regras.ESTADO_SEM_DESTINATARIO, motivo=d.motivo)
            continue

        if d.acao == regras.ENVIAR and c["enviados"] + c["falhas_envio"] >= max_envios:
            c["adiados"] += 1
            continue

        anexos: list[tuple[Path, str]] = []
        if d.acao == regras.ENVIAR and d.com_canhoto:
            pdf = baixar_canhoto(regras.checklist_id_com_foto(servico), codigo)
            if pdf is None:
                # Download falhou: vale como "sem canhoto" -- espera o prazo.
                d = regras.decidir({**servico, "checklistAnswers": None}, embarcador, cfg, agora, estado_atual)
            elif pdf.stat().st_size > MAX_BYTES_ANEXO:
                logger.warning(f"  {codigo}: canhoto com {pdf.stat().st_size} bytes, grande demais pra anexar.")
                d = regras.Decisao(regras.ENVIAR, "canhoto grande demais", com_canhoto=False)
            else:
                anexos = [(pdf, f"Canhoto_{codigo}.pdf")]

        if d.acao == regras.ESPERAR:
            c["aguardando_canhoto"] += 1
            if estado_atual != regras.ESTADO_AGUARDANDO_CANHOTO:
                logger.info(f"  {codigo}: entregue sem canhoto ainda -- aguardando ate {cfg.espera_canhoto_min} min.")
                _registrar(servico, regras.ESTADO_AGUARDANDO_CANHOTO, motivo=d.motivo)
            continue

        com_canhoto = bool(anexos)
        dados = dados_do_servico(servico, embarcador, buscar_nf(codigo), com_canhoto)
        originais = list(embarcador.get("emails") or [])
        destinos, cc = regras.destinos_finais(embarcador, cfg)
        assunto = montar_assunto(dados)
        aviso = ""
        if modo_teste:
            assunto = f"[TESTE] {assunto}"
            aviso = f"MODO TESTE: este e-mail iria para {', '.join(originais)}."
        elif cfg.forcar_destino:
            aviso = f"PILOTO (forcar_destino): este e-mail iria para {', '.join(originais)}."
        corpo = montar_html(dados, portal_url, promete_email_reenvio=promete_email_reenvio, aviso_topo=aviso)

        if c["enviados"] + c["falhas_envio"] > 0:
            dormir(PAUSA_ENTRE_ENVIOS_SEG)
        ok = enviar(destinos, assunto, corpo, cc=cc, anexos=anexos, cabecalhos_extra={"Reply-To": REPLY_TO})
        if ok:
            c["enviados"] += 1
            if dados["tipo"] == "retirada":
                c["retiradas"] += 1
            elif not dados["sucesso"]:
                c["falhas_de_entrega"] += 1
            elif com_canhoto:
                c["com_canhoto"] += 1
            else:
                c["sem_canhoto"] += 1
            rotulo = "RETIRADO" if dados["tipo"] == "retirada" else "ENTREGUE" if dados["sucesso"] else "NAO ENTREGUE"
            logger.info(f"  {codigo}: {rotulo} -> {destinos}"
                        f"{' (com canhoto)' if com_canhoto else ''}")
            _registrar(servico, regras.ESTADO_ENVIADO, motivo=d.motivo, com_canhoto=com_canhoto,
                       destinatarios=", ".join(destinos))
        else:
            c["falhas_envio"] += 1
            if not modo_teste and fp.registrar_falha_envio(conn, servico) == regras.ESTADO_ERRO_ENVIO:
                c["erros_definitivos"].append(codigo)

    return c


def resumo_texto(c: dict) -> str:
    return (f"{c['vistos']} concluido(s) na janela; {c['enviados']} e-mail(s) enviado(s) "
            f"({c['com_canhoto']} com canhoto, {c['sem_canhoto']} sem canhoto, {c['falhas_de_entrega']} de falha, "
            f"{c['retiradas']} de retirada); "
            f"{c['aguardando_canhoto']} aguardando canhoto; {c['ignorados']} ignorado(s); "
            f"{c['sem_destinatario']} sem destinatario; {c['falhas_envio']} falha(s) de envio; "
            f"{c['adiados']} adiado(s) pra proxima rodada.")


# ── CLI ────────────────────────────────────────────────────────────────────────

def main(modo_teste: bool, limite: int, pedido: str | None) -> int:
    from email_utils import enviar_email, notificacoes_automaticas_ativas
    from notificacao_entregas import vuupt_entregas

    inicio = time.time()
    config = _carregar_config()
    cfg = regras.carregar_cfg(config)
    token = (config.get("vuupt_api") or {}).get("token", "")
    config_email = config.get("email") or {}
    portal_url = ((config.get("portal_cliente") or {}).get("url_base") or PORTAL_URL_PADRAO).rstrip("/")
    agora = datetime.now(timezone.utc)

    logger.info(f"{'[MODO TESTE] ' if modo_teste else ''}Notificacao de entregas: ativo={cfg.ativo}, "
                f"forcar_destino={'sim' if cfg.forcar_destino else 'NAO (envio real)'}, "
                f"piloto={cfg.embarcadores_piloto or 'todos'}.")

    if pedido:
        servicos = vuupt_entregas.buscar_por_codigo(token, pedido)
        if not servicos:
            logger.error(f"Nenhum servico concluido com o codigo {pedido} na Vuupt.")
            return 1
    else:
        servicos = vuupt_entregas.buscar_concluidos(token, agora, horas=cfg.max_atraso_horas)

    conn = fp.conectar()
    try:
        c = processar(
            servicos, embarcadores=aplicar_preferencias(carregar_embarcadores(conn), fp.DB_PATH), cfg=cfg, agora=agora, conn=conn,
            enviar=lambda to, assunto, corpo, **kw: enviar_email(to, assunto, corpo, config_email, **kw),
            baixar_canhoto=lambda checklist_id, codigo: vuupt_entregas.baixar_canhoto_pdf(token, checklist_id, codigo),
            buscar_nf=lambda codigo: buscar_nf_no_banco(conn, codigo),
            portal_url=portal_url,
            # O e-mail de insucesso com botoes obedece a chave-mestra; so
            # prometemos "voce vai receber" quando ele esta ligado.
            promete_email_reenvio=notificacoes_automaticas_ativas(config),
            modo_teste=modo_teste, forcar_pedido=bool(pedido),
            max_envios=limite if modo_teste else MAX_ENVIOS_POR_RODADA,
        )
    finally:
        conn.close()

    logger.info(f"Concluido em {time.time() - inicio:.1f}s: {resumo_texto(c)}")

    # A cada 5 min nao cabe e-mail de execucao por rodada: so quando algum
    # pedido foi abandonado depois de MAX_TENTATIVAS_ENVIO falhas de SMTP.
    if c["erros_definitivos"] and not modo_teste:
        try:
            from notificar_execucao_agente import notificar_execucao
            notificar_execucao({"Notificacao de entregas": {
                "status": "erro",
                "detalhe": f"E-mail de entrega NAO enviado depois de {regras.MAX_TENTATIVAS_ENVIO} tentativas: "
                           f"{', '.join(c['erros_definitivos'])}. {resumo_texto(c)}"}},
                time.time() - inicio, modo_teste, config)
        except Exception as e:
            logger.warning(f"Falha ao notificar execucao (nao afeta o resultado): {e}")

    return 1 if c["erros_definitivos"] else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="E-mail por pedido pro embarcador quando a entrega e concluida")
    parser.add_argument("--modo-teste", action="store_true",
                        help="Manda so pra hugo@, ignora a flag ativo e nao grava nada no banco")
    parser.add_argument("--limite", type=int, default=LIMITE_MODO_TESTE,
                        help=f"Maximo de e-mails no modo teste (padrao {LIMITE_MODO_TESTE})")
    parser.add_argument("--pedido", help="Codigo de um pedido concluido (ex.: PS-36327); exige --modo-teste")
    args = parser.parse_args()
    if args.pedido and not args.modo_teste:
        parser.error("--pedido so funciona com --modo-teste")
    _configurar_log()
    sys.exit(main(modo_teste=args.modo_teste, limite=args.limite, pedido=args.pedido))
