# -*- coding: utf-8 -*-
"""
aviso_boleto_faltando.py

Aviso de boleto faltando ao embarcador (06/10/2026, spec
docs/superpowers/specs/2026-10-06-clientes-boleto-aviso-design.md).
Último passo da sequência das 18h (infra/sequencia_incremento.sh), com
as rotas de amanhã já montadas:

  1. pedidos de amanhã (rota ou rascunho) de clientes TEM_BOLETO sem boleto;
  2. busca documentos SÓ desses (e-mail dos embarcadores + aba da Stokki),
     pra não avisar de boleto que chegou depois da rodada das 16h;
  3. quem continua sem boleto: e-mail + WhatsApp ao embarcador e chamado
     (área Boleto) no atendimento. Cada pedido é avisado uma vez por data
     de entrega, e só conta como avisado se algum canal funcionou.

Envio real desligado por padrão: aviso_boleto_faltando.forcar_destino
(sem a chave = hugo@) e whatsapp_notificacoes.clientes.forcar_destino.
O chamado abre de verdade (decisão do Hugo).

COMO USAR:
    py -3.11 aviso_boleto_faltando.py --modo-teste
    py -3.11 aviso_boleto_faltando.py --data 2026-10-07 --sem-atualizar
"""
import argparse
import logging
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

_RAIZ = Path(__file__).parent
sys.path.insert(0, str(_RAIZ))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

import yaml

import boleto_faltando as bf
import notificar_whatsapp
import preferencias_notificacao
from email_utils import enviar_email
from notificar_execucao_agente import notificar_execucao
from regras import clientes_boleto

logger = logging.getLogger("aviso_boleto_faltando")
TITULO = "Aviso de boleto faltando"
EMAIL_TESTE = "hugo@freshlogbr.com"
DONO_TRAVA = "aviso-boleto-faltando"


def _carregar_config() -> dict:
    with open(_RAIZ / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _secao(config: dict) -> dict:
    return (config or {}).get("aviso_boleto_faltando") or {}


def forcar_destino(config: dict) -> str:
    secao = _secao(config)
    if "forcar_destino" not in secao:
        return EMAIL_TESTE
    return str(secao.get("forcar_destino") or "").strip()


def _telefone(cnpj: str) -> str | None:
    conn = sqlite3.connect(bf.DB_PATH, timeout=30)
    try:
        return preferencias_notificacao.whatsapp_do_embarcador(conn, cnpj, tipo="boleto_faltando")
    finally:
        conn.close()


def _rodar_documentos(codigos: list[str], modo_teste: bool) -> None:
    """Atualiza os documentos dos pedidos. Levanta RuntimeError se a Stokki não liberou em 20 min
    ou se alguma etapa da atualização deu erro (o executar segue com os dados do banco e marca falha)."""
    sys.path.insert(0, str(_RAIZ / "documentos_pedido"))
    import processar_documentos
    from stokki import sessao_uso
    # Espera limitada: a fila da provider pode passar de 2h e o aviso não pode virar 20h30.
    if not sessao_uso.adquirir(DONO_TRAVA, ttl_segundos=30 * 60, esperar_segundos=20 * 60, conta="provider"):
        raise RuntimeError("Stokki ocupada há mais de 20 min -- aviso segue com os dados do banco")
    try:
        resumo = {}
        processar_documentos.main(modo_teste=modo_teste, pedidos_stokki=codigos, notificar=False,
                                  com_email_embarcadores=True, resumo_saida=resumo)
    finally:
        sessao_uso.liberar(DONO_TRAVA)
    erros = [f"{etapa}: {(info or {}).get('detalhe', '')}" for etapa, info in resumo.items()
             if (info or {}).get("status") != "ok"]
    if erros:
        raise RuntimeError("; ".join(erros))


def executar(config: dict, data_iso: str, modo_teste: bool = False, atualizar: bool = True, conn=None,
             enviar=None, situacoes=None, embarcadores=None, rodar_documentos=None) -> dict:
    r = {"candidatos": 0, "faltando": 0, "embarcadores": 0, "avisados": 0, "falhas": []}
    enviar = enviar or enviar_email
    rodar_documentos = rodar_documentos or _rodar_documentos
    proprio = conn is None
    conn = conn or sqlite3.connect(bf.DB_PATH, timeout=30)
    try:
        pedidos = bf.pedidos_da_data(conn, data_iso)
        if situacoes is None:
            situacoes = clientes_boleto.situacao_por_pedido([p["codigo"] for p in pedidos])
        avisados = bf.ja_avisados(conn, data_iso)
        candidatos = [p for p in bf.faltando(conn, pedidos, situacoes) if p["codigo"] not in avisados]
        r["candidatos"] = len(candidatos)
        if not candidatos:
            return r

        if atualizar:
            try:
                rodar_documentos([p["codigo"] for p in candidatos], modo_teste)
            except Exception as e:
                logger.exception(f"Atualização dos documentos falhou ({e}) -- avisando com o que há no banco.")
                r["falhas"].append(f"atualização dos documentos: {e}")
        ainda = {p["codigo"] for p in candidatos}
        itens = [p for p in bf.faltando(conn, pedidos, situacoes) if p["codigo"] in ainda]
        r["faltando"] = len(itens)

        if embarcadores is None:
            embarcadores = preferencias_notificacao.carregar_embarcadores("boleto_faltando")
        forcar = forcar_destino(config)
        data_br = datetime.strptime(data_iso, "%Y-%m-%d").strftime("%d/%m/%Y")
        quando = "amanhã" if data_iso == (date.today() + timedelta(days=1)).isoformat() else f"{data_br[:5]}"

        for sid, lista in bf.agrupar_por_embarcador(itens).items():
            r["embarcadores"] += 1
            emb = embarcadores.get(sid) or {"nome": lista[0]["embarcador"], "emails": [], "cnpj": "", "desligado": True}
            nome = emb["nome"] or lista[0]["embarcador"] or f"Remetente {sid}"
            codigos = [i["codigo"] for i in lista]
            canais = []

            if not emb["desligado"] and (emb["emails"] or forcar or modo_teste):
                destinos = [EMAIL_TESTE] if modo_teste else ([forcar] if forcar else emb["emails"])
                original = emb["emails"] if (modo_teste or forcar) else None
                assunto, corpo = bf.email_aviso(nome, lista, data_br, original)
                if enviar(destinos, assunto, corpo, config.get("email", {}),
                          cabecalhos_extra={"Reply-To": bf.EMAIL_RESPOSTA}):
                    canais.append("email")
                else:
                    r["falhas"].append(f"{nome}: e-mail não saiu")

            if not emb["desligado"] and emb.get("cnpj") and not modo_teste and bf.whatsapp_segurado(config, forcar):
                logger.info("  WhatsApp de boleto segurado: aviso em piloto e WhatsApp de clientes com envio real")
            elif not emb["desligado"] and emb.get("cnpj"):
                tel = _telefone(emb["cnpj"])
                if tel:
                    st = notificar_whatsapp.avisar_cliente_boleto_faltando(
                        f"boleto:{sid}:{data_iso}", tel, notificar_whatsapp.texto_boleto_faltando(codigos, quando),
                        config, modo_teste=modo_teste)
                    if st in ("enviado", "modo_teste"):
                        canais.append("whatsapp")
                    elif st not in ("desligado", "nao_enviado", "numero_sem_whatsapp"):
                        r["falhas"].append(f"{nome}: WhatsApp {st}")

            if not modo_teste and not emb.get("cnpj"):
                # sem CNPJ todos os desconhecidos cairiam no mesmo chamado
                r["falhas"].append(f"{nome}: sem CNPJ do embarcador, chamado não aberto")
            elif not modo_teste:
                texto = (f"Pedidos com entrega em {data_br} de clientes que pagam com boleto, ainda sem boleto: "
                         + ", ".join(f"{i['codigo']}{' (NF ' + i['nf'] + ')' if i.get('nf') else ''} - {i['cliente']}"
                                     for i in lista)
                         + ". Envie o boleto respondendo aqui, por e-mail ou anexando no pedido da Stokki.")
                try:
                    if bf.registrar_no_chamado(emb, sid, texto, codigos[0], config):
                        canais.append("chamado")
                except Exception as e:
                    logger.exception(f"  {nome}: chamado não abriu: {e}")
                    r["falhas"].append(f"{nome}: chamado {e}")

            if canais and not modo_teste:
                bf.registrar_avisados(conn, lista, data_iso, canais)
            if canais:
                r["avisados"] += len(lista)
                logger.info(f"  {nome}: {len(lista)} pedido(s) avisado(s) por {', '.join(canais)}")
            else:
                r["falhas"].append(f"{nome}: nenhum canal funcionou ({', '.join(codigos)})")
        return r
    finally:
        if proprio:
            conn.close()


def main(data: str | None = None, modo_teste: bool = False, atualizar: bool = True) -> dict:
    inicio = time.time()
    config = _carregar_config()
    data_iso = data or (date.today() + timedelta(days=1)).isoformat()
    if not modo_teste and not _secao(config).get("ativo", True):
        logger.info("aviso_boleto_faltando.ativo=false -- nada a fazer.")
        return {}
    resumo = {}
    try:
        r = executar(config, data_iso, modo_teste=modo_teste, atualizar=atualizar)
        resumo[TITULO] = {"status": "erro" if r["falhas"] else "ok",
                          "detalhe": f"{data_iso}: {r['candidatos']} candidato(s), {r['faltando']} ainda sem boleto, "
                                     f"{r['avisados']} avisado(s) em {r['embarcadores']} embarcador(es)"
                                     + (" | " + "; ".join(r["falhas"][:5]) if r["falhas"] else "")}
    except Exception as e:
        logger.exception(f"Erro no aviso de boleto faltando: {e}")
        r, resumo[TITULO] = {}, {"status": "erro", "detalhe": str(e)}
    try:
        notificar_execucao(resumo, time.time() - inicio, modo_teste, config)
    except Exception as e:
        logger.warning(f"Falha ao notificar execução (não afeta o resultado): {e}")
    return r


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
                        handlers=[logging.StreamHandler(),
                                  logging.FileHandler(_RAIZ / "dados" / "aviso_boleto_faltando.log", encoding="utf-8")])
    p = argparse.ArgumentParser(description="Avisa o embarcador dos pedidos de amanhã sem boleto")
    p.add_argument("--data", default=None, help="Data de entrega AAAA-MM-DD (padrão: amanhã)")
    p.add_argument("--modo-teste", action="store_true", help="Não grava, não abre chamado; e-mail/WhatsApp só pro teste")
    p.add_argument("--sem-atualizar", action="store_true", help="Não busca documentos antes de avisar")
    a = p.parse_args()
    main(a.data, modo_teste=a.modo_teste, atualizar=not a.sem_atualizar)
