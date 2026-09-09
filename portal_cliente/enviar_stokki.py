# -*- coding: utf-8 -*-
"""
portal_cliente/enviar_stokki.py

Worker da fila da máscara de envio de pedidos (pedido do Hugo, 08/09/2026):
pega os XMLs NA_FILA em portal_envios, agrupa por embarcador, aplica a
regra de transformação do XML do cliente e cria os pedidos na Stokki pelo
MESMO wizard de importação que o importador por e-mail usa
(agente_importacao_stokki/etapa3_importacao_stokki/importar_stokki.py --
importado daqui, não copiado, pra login/seletores viverem num lugar só).

Antes de abrir o navegador na Stokki, respeita a trava cooperativa
stokki/sessao_uso.py: se um agente do painel ou o importador estiver
usando a Stokki, ESPERA a vez (item 5 das decisões do Hugo). O cliente
vê o pedido como "Na fila" enquanto isso.

Resultado por XML:
  CRIADO     -- barra verde/resposta OK; tenta descobrir o PS-xxxxx
  DUPLICADO  -- "Chave da NFe já utilizada" (o pedido já existia)
  ERRO       -- recusa da Stokki (vai pra tela + e-mail na hora) ou falha
                técnica repetida 3x (antes disso volta pra fila)

COMO RODAR:
    py -3 portal_cliente/enviar_stokki.py --loop            # serviço (VPS)
    py -3 portal_cliente/enviar_stokki.py --uma-vez         # um ciclo
    py -3 portal_cliente/enviar_stokki.py --uma-vez --simular   # sem tocar a Stokki
    py -3 portal_cliente/enviar_stokki.py --uma-vez --visivel   # navegador na tela
"""
import argparse
import logging
import re
import shutil
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
_AQUI = Path(__file__).parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import yaml

import envio_pedidos as ep
from email_utils import enviar_email, envelope_html
from stokki import sessao_uso

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("portal_envios")

DONO_TRAVA = "portal-envios"
INTERVALO_LOOP_SEGUNDOS = 20
MAX_TENTATIVAS_TECNICAS = 3
PASTA_LOTES = ep.PASTA_XMLS / "_lotes"


def carregar_config() -> dict:
    with open(_RAIZ / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _cfg_portal(config: dict) -> dict:
    return config.get("portal_cliente", {}) or {}


# ── Importador (repo separado) ─────────────────────────────────────────────────

def _caminho_importador(config: dict) -> Path:
    cfg = _cfg_portal(config).get("caminho_importador")
    if cfg:
        return Path(cfg)
    for candidato in (_RAIZ.parent / "agente_importacao_stokki", Path("/opt/agente-importacao-stokki")):
        if candidato.is_dir():
            return candidato
    return _RAIZ.parent / "agente_importacao_stokki"


def carregar_importador(config: dict):
    """(wizard, processar_xml) do agente_importacao_stokki."""
    base = _caminho_importador(config)
    if not (base / "etapa3_importacao_stokki" / "importar_stokki.py").is_file():
        raise RuntimeError(f"Importador não encontrado em {base} -- ajuste portal_cliente.caminho_importador no config.yaml.")
    for sub in ("etapa2_processamento_xml", "etapa3_importacao_stokki"):
        p = str(base / sub)
        if p not in sys.path:
            sys.path.insert(0, p)
    import importar_stokki as wiz  # noqa: E402
    import processar_xml as px  # noqa: E402
    return wiz, px


def transformar_xml(regra: str, origem: Path, destino: Path, px) -> None:
    if regra in (None, "", "nenhuma"):
        shutil.copyfile(origem, destino)
        return
    fn = px.TRANSFORMACOES.get(regra)
    if fn is None:
        raise ValueError(f"Regra de XML '{regra}' não existe no importador (TRANSFORMACOES).")
    fn(origem, destino)


# ── Wizard da Stokki ───────────────────────────────────────────────────────────

def _credenciais(config: dict) -> tuple[str, str]:
    cfg = _cfg_portal(config).get("stokki") or {}
    usuario = cfg.get("usuario") or config.get("stokki", {}).get("usuario", "")
    senha = cfg.get("senha") or config.get("stokki", {}).get("senha", "")
    if not usuario or not senha:
        raise RuntimeError("Credenciais da Stokki ausentes (portal_cliente.stokki ou stokki no config.yaml).")
    return usuario, senha


def _codigo_da_resposta(resp: dict | None) -> str:
    """Tenta achar o id do pedido no corpo da resposta do /sale/xml/store
    (best-effort: /show/123 ou "id":123)."""
    corpo = (resp or {}).get("body") or ""
    m = re.search(r"/show/(\d+)", corpo) or re.search(r'"(?:id|order_id|sale_id)"\s*:\s*"?(\d+)', corpo)
    return f"PS-{m.group(1)}" if m else ""


def _buscar_codigos_na_listagem(page, numeros_nf: list[str]) -> dict[str, str]:
    """Depois de criar, pergunta à listagem de pedidos (outbound/table)
    pelo número da NF pra descobrir o PS-xxxxx -- usa a sessão do próprio
    navegador (sem novo login). Best-effort."""
    from stokki import pedidos as stokki_pedidos

    class _Sessao:
        def get(self, url, params=None, headers=None):
            r = page.context.request.get(url, params=params or {}, headers=headers or {})

            class _R:
                status_code = r.status
                text = r.text()

                def raise_for_status(self):
                    if not r.ok:
                        raise RuntimeError(f"HTTP {r.status}")

                def json(self):
                    return r.json()
            return _R()

    achados: dict[str, str] = {}
    sessao = _Sessao()
    for nf in numeros_nf:
        try:
            dados = stokki_pedidos.listar_pedidos(sessao, status="", busca=nf, por_pagina=20, ordenar_coluna="1", ordenar_dir="desc")
        except Exception as e:
            logger.info(f"   (listagem por NF {nf} falhou: {e})")
            continue
        for linha in dados.get("aaData", []) or []:
            texto = " ".join(str(v) for v in (linha.values() if isinstance(linha, dict) else linha))
            if re.search(rf"(?<!\d){re.escape(nf)}(?!\d)", re.sub(r"<[^>]+>", " ", texto)):
                codigo = stokki_pedidos.extrair_codigo_ps_da_linha(linha)
                if codigo:
                    achados[nf] = codigo
                    break
    return achados


def executar_wizard(cfg: dict, usuario: str, senha: str, arquivos: list[Path], pasta_logs: Path, wiz,
                    headless: bool = True) -> tuple[list[dict], dict, dict[str, str]]:
    """Mesma sequência de importar_stokki.importar_pedidos, mas com os
    parâmetros vindos do banco (por embarcador) em vez do config.yaml do
    importador. Devolve (resultados por XML, respostas HTTP por chave,
    códigos PS por número de NF)."""
    from playwright.sync_api import sync_playwright, Error as PlaywrightError

    pasta_logs.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    caminhos = [str(p) for p in arquivos]
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        try:
            context = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                                     "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
            context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            wiz.instalar_captura_respostas(context)
            page = context.new_page()
            wiz.fazer_login(page, usuario, senha, modo_automatico=True)
            page.goto(cfg["url_importacao"], wait_until="networkidle")
            page.wait_for_timeout(1500)
            wiz.selecionar_valor_select(page, "#client_id", cfg["client_id"])
            wiz.selecionar_valor_select(page, "#warehouse_id", cfg["warehouse_id"], aguardar_opcoes=True)
            wiz.selecionar_valor_select(page, "#type_transport", cfg["tipo_transporte"])
            page.wait_for_timeout(500)
            wiz.selecionar_valor_select(page, "#packaging", cfg["embalagem"])
            page.wait_for_timeout(500)
            page.set_input_files("#input_drop_file", caminhos)
            page.wait_for_timeout(2000)
            page.get_by_role("link", name="Próximo").click()
            page.wait_for_selector("text=Arquivos anexados", timeout=30000)
            page.wait_for_timeout(1500)
            page.screenshot(path=str(pasta_logs / f"{ts}_arquivos.png"), full_page=True)
            try:
                page.get_by_role("button", name="Criar todos pedidos").click()
            except PlaywrightError:
                page.get_by_role("button", name="Criar pedido").first.click()
            wiz.aguardar_processamento_completo(page, esperado=len(caminhos))
            page.screenshot(path=str(pasta_logs / f"{ts}_final.png"), full_page=True)
            respostas = wiz.ler_respostas_capturadas(page)
            resultados = wiz.coletar_resultados(page, caminhos, respostas)
            criados_nf = [Path(r["arquivo"]).stem for r in resultados if r["criado"]]
            codigos: dict[str, str] = {}
            if criados_nf:
                try:
                    codigos = _buscar_codigos_na_listagem(page, criados_nf)
                except Exception as e:
                    logger.info(f"   (não consegui consultar a listagem pra achar os códigos: {e})")
        finally:
            browser.close()
    return resultados, respostas, codigos


# ── Processamento de um lote (um embarcador) ───────────────────────────────────

def _e_duplicado(erro: str) -> bool:
    e = (erro or "").lower()
    return "já utilizada" in e or "ja utilizada" in e


def _marcar(conn, envio_id: int, **campos) -> None:
    campos["atualizado_em"] = ep._agora()
    sets = ", ".join(f"{k} = ?" for k in campos)
    conn.execute(f"UPDATE portal_envios SET {sets} WHERE id = ?", (*campos.values(), envio_id))


def _falha_tecnica(conn, envios: list[dict], erro: str) -> list[dict]:
    """Volta pra fila (até MAX_TENTATIVAS_TECNICAS), depois vira ERRO.
    Devolve os que viraram ERRO (pra e-mail)."""
    definitivos = []
    for e in envios:
        tentativas = int(e.get("tentativas") or 0) + 1
        if tentativas >= MAX_TENTATIVAS_TECNICAS:
            _marcar(conn, e["id"], status=ep.STATUS_ERRO, tentativas=tentativas,
                    erro=f"Falha ao enviar à Stokki ({tentativas}x): {erro}"[:900])
            definitivos.append({**e, "erro": erro})
        else:
            _marcar(conn, e["id"], status=ep.STATUS_NA_FILA, tentativas=tentativas,
                    erro=f"Tentativa {tentativas} falhou, vai tentar de novo: {erro}"[:900])
    conn.commit()
    return definitivos


def processar_lote(conn, cnpj: str, envios: list[dict], config: dict, simular: bool = False, headless: bool = True) -> dict:
    cfg = ep.config_stokki_cliente(conn, cnpj, config)
    resumo = {"cliente": cfg["nome"], "criados": 0, "duplicados": 0, "erros": 0, "adiados": 0}
    if not cfg["envio_ativo"]:
        logger.info(f"[{cfg['nome']}] envio desativado em portal_clientes_envio -- {len(envios)} XML(s) ficam na fila.")
        resumo["adiados"] = len(envios)
        return resumo
    if not cfg["client_id"]:
        for e in envios:
            _marcar(conn, e["id"], status=ep.STATUS_ERRO, erro="Embarcador sem client_id da Stokki (interno.stkkc_id) -- cadastre e reenvie.")
        conn.commit()
        _avisar_erros(config, cfg, envios, "Embarcador sem client_id da Stokki (interno.stkkc_id).")
        resumo["erros"] = len(envios)
        return resumo

    ids = [e["id"] for e in envios]
    conn.execute(f"UPDATE portal_envios SET status = ?, enviado_stokki_em = ?, atualizado_em = ? WHERE id IN ({','.join('?' * len(ids))})",
                 (ep.STATUS_ENVIANDO, ep._agora(), ep._agora(), *ids))
    conn.commit()

    pasta_lote = PASTA_LOTES / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{cfg['cnpj']}"
    pasta_lote.mkdir(parents=True, exist_ok=True)
    arquivos: list[Path] = []
    prontos: list[dict] = []
    try:
        wiz = px = None
        if not simular:
            wiz, px = carregar_importador(config)
        for e in envios:
            destino = pasta_lote / f"{e['chave_nfe']}.xml"
            try:
                if simular:
                    shutil.copyfile(ep.caminho_xml(e), destino)
                else:
                    transformar_xml(cfg["regra_xml"], ep.caminho_xml(e), destino, px)
                arquivos.append(destino)
                prontos.append(e)
            except Exception as ex:
                logger.warning(f"[{cfg['nome']}] NF {e['numero_nf']}: falha ao preparar o XML ({ex})")
                _marcar(conn, e["id"], status=ep.STATUS_ERRO, erro=f"Falha ao preparar o XML ({cfg['regra_xml']}): {ex}"[:900])
                conn.commit()
                _avisar_erros(config, cfg, [{**e, "erro": str(ex)}], "O XML não pôde ser preparado pra Stokki.")
                resumo["erros"] += 1
        if not prontos:
            return resumo

        if simular:
            logger.info(f"[{cfg['nome']}] SIMULAÇÃO: {len(prontos)} XML(s) marcados como criados sem tocar a Stokki.")
            resultados = [{"arquivo": p.name, "criado": True, "erro": ""} for p in arquivos]
            respostas, codigos = {}, {}
        else:
            espera = int(_cfg_portal(config).get("espera_stokki_minutos") or 45) * 60
            if not sessao_uso.adquirir(DONO_TRAVA, ttl_segundos=30 * 60, esperar_segundos=espera):
                ocupante = sessao_uso.em_uso()
                logger.warning(f"[{cfg['nome']}] Stokki ocupada por '{ocupante}' há mais de {espera // 60} min -- lote volta pra fila.")
                conn.execute(f"UPDATE portal_envios SET status = ?, atualizado_em = ? WHERE id IN ({','.join('?' * len(prontos))})",
                             (ep.STATUS_NA_FILA, ep._agora(), *[e["id"] for e in prontos]))
                conn.commit()
                resumo["adiados"] = len(prontos)
                return resumo
            try:
                usuario, senha = _credenciais(config)
                logger.info(f"[{cfg['nome']}] enviando {len(arquivos)} XML(s) à Stokki (client_id={cfg['client_id']}, regra={cfg['regra_xml']})...")
                resultados, respostas, codigos = executar_wizard(cfg, usuario, senha, arquivos, pasta_lote, wiz, headless=headless)
            finally:
                sessao_uso.liberar(DONO_TRAVA)

        por_chave = {Path(r["arquivo"]).stem: r for r in resultados}
        erros_definitivos = []
        for e in prontos:
            r = por_chave.get(e["chave_nfe"])
            resposta = respostas.get(e["chave_nfe"])
            resposta_txt = (resposta or {}).get("body") if resposta else None
            if r is None:
                _falha_tecnica(conn, [e], "a Stokki não devolveu resultado pra esse arquivo")
                resumo["erros"] += 1
                continue
            if r["criado"]:
                codigo = codigos.get(e["numero_nf"]) or _codigo_da_resposta(resposta) or None
                _marcar(conn, e["id"], status=ep.STATUS_CRIADO, erro=None, resposta_stokki=resposta_txt,
                        criado_stokki_em=ep._agora(), codigo_pedido=codigo)
                resumo["criados"] += 1
                if codigo and e.get("agendamento_data") and not e.get("agendamento_pendente"):
                    ep.registrar_agendamento_pedido(conn, {**e, "codigo_pedido": codigo})
            elif _e_duplicado(r["erro"]):
                _marcar(conn, e["id"], status=ep.STATUS_DUPLICADO, erro=r["erro"][:900], resposta_stokki=resposta_txt,
                        criado_stokki_em=ep._agora())
                resumo["duplicados"] += 1
            else:
                _marcar(conn, e["id"], status=ep.STATUS_ERRO, erro=(r["erro"] or "recusado pela Stokki")[:900],
                        resposta_stokki=resposta_txt, tentativas=int(e.get("tentativas") or 0) + 1)
                erros_definitivos.append({**e, "erro": r["erro"]})
                resumo["erros"] += 1
        conn.commit()
        if erros_definitivos:
            _avisar_erros(config, cfg, erros_definitivos, "A Stokki recusou o(s) pedido(s) abaixo.")
        logger.info(f"[{cfg['nome']}] lote concluído: {resumo}")
        return resumo
    except Exception as ex:
        logger.error(f"[{cfg['nome']}] falha técnica no lote: {ex}\n{traceback.format_exc()}")
        pendentes = [e for e in prontos] or envios
        definitivos = _falha_tecnica(conn, pendentes, f"{type(ex).__name__}: {ex}")
        if definitivos:
            _avisar_erros(config, cfg, definitivos, "Não conseguimos enviar o(s) pedido(s) abaixo à Stokki depois de 3 tentativas.")
        resumo["erros"] += len(definitivos)
        resumo["adiados"] += len(pendentes) - len(definitivos)
        return resumo


# ── E-mail de erro (item 5: erro na tela E por e-mail) ─────────────────────────

def _avisar_erros(config: dict, cfg: dict, envios: list[dict], cabecalho: str) -> None:
    if not envios:
        return
    email_cfg = config.get("email", {}) or {}
    destinos = list(cfg.get("emails") or [])
    atendimento = email_cfg.get("email_atendimento") or email_cfg.get("email_responsavel")
    cc = [atendimento] if atendimento and atendimento not in destinos else []
    if not destinos and not cc:
        logger.warning(f"[{cfg['nome']}] sem e-mail cadastrado -- erro só na tela.")
        return
    url = (_cfg_portal(config).get("url_base") or "https://app.freshhub.com.br/cliente").rstrip("/")
    linhas = "".join(
        f"<tr><td style='padding:6px 10px;border-bottom:1px solid #E5E7EB'><b>NF {e.get('numero_nf') or '?'}</b></td>"
        f"<td style='padding:6px 10px;border-bottom:1px solid #E5E7EB'>{e.get('destinatario_nome') or ''}</td>"
        f"<td style='padding:6px 10px;border-bottom:1px solid #E5E7EB;color:#B91C1C'>{(e.get('erro') or '')[:300]}</td></tr>"
        for e in envios)
    corpo = envelope_html(
        f"<p>Olá, <strong>{cfg['nome']}</strong>.</p><p>{cabecalho}</p>"
        f"<table style='border-collapse:collapse;font-size:13px;width:100%'>"
        f"<tr><th align='left' style='padding:6px 10px'>Nota</th><th align='left' style='padding:6px 10px'>Destinatário</th>"
        f"<th align='left' style='padding:6px 10px'>Motivo</th></tr>{linhas}</table>"
        f"<p style='margin-top:20px'>Você pode corrigir e reenviar pelo portal: <a href='{url}/?aba=envios'>{url}</a>. "
        f"Se precisar de ajuda, responda este e-mail.</p>",
        rodape="Fresh Log · Portal do cliente · envio de pedidos", cor_acento="#EF4444")
    ok = enviar_email(destinos or cc, f"Fresh Log · Pedido(s) não criado(s) na Stokki ({len(envios)})", corpo, email_cfg,
                      cc=cc if destinos else None)
    logger.info(f"[{cfg['nome']}] e-mail de erro {'enviado' if ok else 'FALHOU'} pra {destinos or cc}")


# ── Conciliação: descobrir o PS-xxxxx depois ───────────────────────────────────

def reconciliar_codigos(conn) -> int:
    """Pedidos criados sem código: procura pelo número da NF nas tabelas
    que o pipeline já alimenta (documentos_processados, pedidos_historico).
    Com o código em mãos, o agendamento informado no portal entra em
    agendamentos_pedido."""
    rows = conn.execute("SELECT * FROM portal_envios WHERE status IN ('CRIADO','DUPLICADO') AND codigo_pedido IS NULL "
                        "AND numero_nf IS NOT NULL AND numero_nf != ''").fetchall()
    achados = 0
    for r in rows:
        e = dict(r)
        codigo = None
        try:
            d = conn.execute("SELECT codigo_pedido FROM documentos_processados WHERE tipo = 'Nota Fiscal' AND numero_nf = ? "
                             "ORDER BY rowid DESC LIMIT 1", (e["numero_nf"],)).fetchone()
            if d and d["codigo_pedido"]:
                codigo = d["codigo_pedido"]
        except Exception:
            pass
        if not codigo:
            try:
                emb = re.sub(r"\D", "", e["cnpj_embarcador"])
                h = conn.execute("SELECT id_pedido FROM pedidos_historico WHERE numero_nfe = ? "
                                 "AND REPLACE(REPLACE(REPLACE(COALESCE(cliente_cnpj,''),'.',''),'/',''),'-','') = ? "
                                 "ORDER BY atualizado_em DESC LIMIT 1", (e["numero_nf"], emb)).fetchone()
                if h and h["id_pedido"]:
                    codigo = h["id_pedido"] if str(h["id_pedido"]).upper().startswith("PS-") else f"PS-{h['id_pedido']}"
            except Exception:
                pass
        if codigo:
            _marcar(conn, e["id"], codigo_pedido=codigo)
            conn.commit()
            achados += 1
            if e.get("agendamento_data") and not e.get("agendamento_pendente") and not e.get("agendamento_aplicado_em"):
                ep.registrar_agendamento_pedido(conn, {**e, "codigo_pedido": codigo})
    return achados


# ── Ciclo ──────────────────────────────────────────────────────────────────────

def ciclo(config: dict, simular: bool = False, headless: bool = True) -> dict:
    ep.limpar_temporarios()
    conn = ep.conectar()
    try:
        lote_max = int(_cfg_portal(config).get("lote_maximo") or 30)
        # ENVIANDO órfão (worker caiu no meio) volta pra fila depois de 30 min
        conn.execute("UPDATE portal_envios SET status = 'NA_FILA', atualizado_em = datetime('now','localtime') "
                     "WHERE status = 'ENVIANDO' AND atualizado_em < datetime('now','localtime','-30 minutes')")
        conn.commit()
        rows = conn.execute("SELECT * FROM portal_envios WHERE status = 'NA_FILA' ORDER BY cnpj_embarcador, criado_em, id").fetchall()
        por_cliente: dict[str, list[dict]] = {}
        for r in rows:
            por_cliente.setdefault(r["cnpj_embarcador"], []).append(dict(r))
        total = {"lotes": 0, "criados": 0, "duplicados": 0, "erros": 0, "adiados": 0}
        for cnpj, envios in por_cliente.items():
            for i in range(0, len(envios), lote_max):
                resumo = processar_lote(conn, cnpj, envios[i:i + lote_max], config, simular=simular, headless=headless)
                total["lotes"] += 1
                for k in ("criados", "duplicados", "erros", "adiados"):
                    total[k] += resumo.get(k, 0)
        try:
            total["conciliados"] = reconciliar_codigos(conn)
        except Exception as e:
            logger.warning(f"conciliação de códigos falhou: {e}")
        return total
    finally:
        conn.close()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Worker da fila de envio de pedidos do portal do cliente")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--loop", action="store_true", help="roda pra sempre, um ciclo a cada 20 s (serviço)")
    g.add_argument("--uma-vez", action="store_true", help="um ciclo só")
    p.add_argument("--simular", action="store_true", help="não toca a Stokki: marca como criado (teste local)")
    p.add_argument("--visivel", action="store_true", help="abre o navegador na tela (debug)")
    args = p.parse_args(argv)
    if not args.loop and not args.uma_vez:
        args.uma_vez = True
    config = carregar_config()
    if args.loop:
        logger.info(f"worker iniciado (ciclo a cada {INTERVALO_LOOP_SEGUNDOS}s{' · SIMULAÇÃO' if args.simular else ''})")
        while True:
            try:
                r = ciclo(config, simular=args.simular, headless=not args.visivel)
                if r["lotes"]:
                    logger.info(f"ciclo: {r}")
            except Exception as e:
                logger.error(f"ciclo falhou: {e}\n{traceback.format_exc()}")
            time.sleep(INTERVALO_LOOP_SEGUNDOS)
    r = ciclo(config, simular=args.simular, headless=not args.visivel)
    logger.info(f"ciclo: {r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
