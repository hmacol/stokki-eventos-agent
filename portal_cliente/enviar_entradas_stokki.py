# -*- coding: utf-8 -*-
"""
portal_cliente/enviar_entradas_stokki.py

Worker da fila de Pedidos de Entrada (25/09/2026): pega as entradas
ANUNCIADO / NA_FILA em portal_entradas e cria o recebimento (#PE) na
Stokki. Mesmo desenho de enviar_stokki.py (fila de saída): trava
cooperativa stokki/sessao_uso.py, Playwright no wizard, 3 tentativas
técnicas antes de ERRO, recusa da Stokki vira ERRO na hora com o texto
na aba do cliente.

Sondagem de 24-25/09 (spec, seção 8.1): a Stokki cria recebimento por
  - inventory/incoming/xml/multiple/create -> POST incoming/xml/multiple/store
    (XML da NF-e; gêmeo do sale/xml do outbound; o JS da página lê a
    NF-e e manda po = nº da NF, invoice = chave, sku[]/quantity[]);
  - inventory/incoming/create/excel/incoming -> POST incoming/excel/store
    (planilha SKU|Quantidade|Valor Unitário, o mesmo modelo do outbound).

Por entrada, o resultado é:
  CRIADO      -- 2xx no store; o #PE é descoberto em seguida pela chave
                 (procurar_pe) e gravado em stokki_id/stokki_codigo;
  CRIADO (já existia) -- antes de subir, procurar_pe achou um #PE com a
                 mesma chave (resposta perdida numa rodada anterior, ou a
                 equipe criou à mão): não cria de novo;
  ERRO        -- 4xx com errors, ou 3 falhas técnicas seguidas.

NUNCA sonda URL da Stokki por adivinhação (URL inexistente redireciona
pro /login e o auth.py refaz o login, derrubando as outras sessões).

COMO RODAR:
    py -3 portal_cliente/enviar_entradas_stokki.py --loop              # serviço (VPS)
    py -3 portal_cliente/enviar_entradas_stokki.py --uma-vez           # um ciclo
    py -3 portal_cliente/enviar_entradas_stokki.py --uma-vez --simular # sem tocar a Stokki
    py -3 portal_cliente/enviar_entradas_stokki.py --uma-vez --visivel # navegador na tela
"""
import argparse
import json
import logging
import re
import shutil
import sys
import time
import traceback
from datetime import date, datetime
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
_AQUI = Path(__file__).parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import entradas as en
import envio_pedidos as ep
import enviar_stokki as fila_saida   # reaproveita config, importador, credenciais e helpers do wizard Excel
from email_utils import enviar_email, envelope_html
from stokki import recebimentos as stokki_recebimentos
from stokki import sessao_uso

logger = logging.getLogger("portal_entradas")

DONO_TRAVA = "portal-entradas"
INTERVALO_LOOP_SEGUNDOS = 20
MAX_TENTATIVAS_TECNICAS = 3
URL_BASE = "https://freshlog.stokki.com.br"
URL_WIZARD_XML = f"{URL_BASE}/pt-br/administrator/inventory/incoming/xml/multiple/create"
URL_WIZARD_EXCEL = f"{URL_BASE}/pt-br/administrator/inventory/incoming/create/excel/incoming"
URL_STORE_EXCEL = f"{URL_BASE}/pt-br/administrator/inventory/incoming/excel/store"
URL_CLIENTE_EXCEL = f"{URL_BASE}/pt-br/administrator/inventory/incoming/create/excel/client/{{client_id}}"
_HEADERS_AJAX = fila_saida._HEADERS_AJAX

carregar_config = fila_saida.carregar_config
carregar_importador = fila_saida.carregar_importador
_credenciais = fila_saida._credenciais
_cfg_portal = fila_saida._cfg_portal


def _pasta_lotes() -> Path:
    return ep._RAIZ / "dados" / "portal_entradas" / "_lotes"


def _arrival_date(data_prevista: str) -> str:
    """dd/mm/aaaa pro wizard, nunca no passado (o datepicker da Stokki tem
    minDate = hoje): anunciou pra ontem e o worker rodou hoje -> hoje."""
    hoje = date.today()
    try:
        d = date.fromisoformat(str(data_prevista or ""))
    except ValueError:
        d = hoje
    return max(d, hoje).strftime("%d/%m/%Y")


# ── Hook no XMLHttpRequest (mesma técnica de importar_stokki.py) ──────────────

_JS_HOOK_XHR = """
(() => {
  window.__stokki_respostas_incoming = [];
  const send = XMLHttpRequest.prototype.send;
  const open = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function(m, u) { this.__url = String(u || ''); return open.apply(this, arguments); };
  XMLHttpRequest.prototype.send = function(body) {
    try {
      if (this.__url.indexOf('/incoming/xml/multiple/store') !== -1) {
        let invoice = null;
        if (body && typeof body.get === 'function') invoice = body.get('invoice');
        const xhr = this;
        this.addEventListener('loadend', function() {
          window.__stokki_respostas_incoming.push({ invoice: invoice, status: xhr.status, body: String(xhr.responseText || '').slice(0, 1000) });
        });
      }
    } catch (e) {}
    return send.apply(this, arguments);
  };
})();
"""

_JS_LINHAS = """
() => Array.from(document.querySelectorAll('form.form_incoming')).map(f => {
    const nome = (f.querySelector('.col-1') || {}).textContent || '';
    const barra = f.querySelector('.progress-bar');
    let erros = Array.from(f.querySelectorAll('[id^=validation_errors_order] li')).map(e => e.textContent.trim()).filter(Boolean);
    if (!erros.length) erros = Array.from(f.querySelectorAll('[id^=validation_errors_order] .alert')).map(e => e.textContent.trim()).filter(Boolean);
    const invoice = (f.querySelector('input[name=invoice]') || {}).value || '';
    return { nome: nome.trim(), invoice: invoice, barra_texto: barra ? barra.textContent.trim() : '',
             barra_classe: barra ? barra.className : '', erros: Array.from(new Set(erros)) };
})
"""


def _criado_da_resposta(resp: dict | None) -> tuple[bool | None, str]:
    """(criado, motivo) da resposta HTTP capturada; None sem resposta."""
    if not resp:
        return None, ""
    body = resp.get("body", "")
    try:
        dados = json.loads(body)
    except Exception:
        dados = None
    if isinstance(dados, dict):
        erros = dados.get("errors") or dados.get("error") or []
        if isinstance(erros, dict):
            erros = [m for v in erros.values() for m in (v if isinstance(v, list) else [v])]
        if isinstance(erros, str):
            erros = [erros]
        if erros:
            return False, "; ".join(str(e).strip() for e in erros)
    if 200 <= int(resp.get("status", 0)) < 300:
        return True, ""
    return False, f"HTTP {resp.get('status')}: {body[:200].strip() or 'sem corpo'}"


def resultados_do_lote(linhas: list[dict], chaves: list[str], respostas: dict) -> dict[str, dict]:
    """{chave: {criado, erro}} cruzando a tela (barra verde/vermelha) com a
    resposta HTTP do store. A resposta manda; a tela só confirma. Sem os
    dois: não criado (a Stokki recusa como duplicado se já existir)."""
    saida = {}
    for chave in chaves:
        linha = next((l for l in linhas if l.get("invoice") == chave or l.get("nome", "").startswith(chave)), None)
        criado_http, motivo_http = _criado_da_resposta(respostas.get(chave))
        if linha is None and criado_http is None:
            saida[chave] = {"criado": False, "erro": "linha do arquivo não encontrada na tela da Stokki"}
            continue
        criado_tela, motivo_tela = None, ""
        if linha is not None:
            texto, classe = linha["barra_texto"].lower(), linha["barra_classe"]
            criado_tela = ("pedido criado" in texto or "bg-success" in classe) and "bg-danger" not in classe
            motivo_tela = "; ".join(linha["erros"]) or linha["barra_texto"]
        if criado_http is None:
            criado, motivo = bool(criado_tela), motivo_tela or "sem mensagem na tela"
        else:
            criado = criado_http and (criado_tela is not False)
            motivo = motivo_http or motivo_tela or ("" if criado else "sem mensagem na tela")
        saida[chave] = {"criado": criado, "erro": "" if criado else motivo}
    return saida


# ── Descoberta do #PE pela listagem (sessão do próprio navegador) ─────────────

class _SessaoNavegador:
    """Adapta page.context.request ao contrato .get() de stokki.recebimentos
    (mesmo truque de enviar_stokki._buscar_codigos_na_listagem)."""

    def __init__(self, page):
        self._page = page

    def get(self, url, params=None, headers=None):
        r = self._page.context.request.get(url, params=params or {}, headers=headers or {})

        class _R:
            status_code = r.status
            text = r.text()

            def raise_for_status(self):
                if not r.ok:
                    raise RuntimeError(f"HTTP {r.status}")

            def json(self):
                return r.json()
        return _R()


def procurar_pe(sessao, client_id: str, busca: str, chave_nfe: str = "", referencia: str = "") -> tuple[int | None, str]:
    """(id_stokki, '#PE-n') do recebimento do cliente que bate com a chave
    NF-e (XML: abre o detalhe só das linhas cuja Ref. do Pedido = nº da NF)
    ou com a referência (planilha: Ref. do Pedido da própria linha). Sem
    par: (None, '')."""
    vistos = set()
    for tentativa in ({"busca": busca, "por_pagina": 10}, {"busca": "", "por_pagina": 20}):
        try:
            dados = stokki_recebimentos.listar_recebimentos(sessao, cliente=str(client_id), **tentativa)
        except Exception as e:  # noqa: BLE001
            logger.info(f"   (listagem de recebimentos falhou: {e})")
            return None, ""
        for linha in dados.get("aaData") or []:
            id_stokki = stokki_recebimentos.extrair_id_da_linha(linha)
            if not id_stokki or id_stokki in vistos:
                continue
            vistos.add(id_stokki)
            ref = stokki_recebimentos.extrair_ref_da_linha(linha).strip().upper()
            codigo = stokki_recebimentos.extrair_codigo_da_linha(linha)
            if referencia and not chave_nfe:
                if ref == str(referencia).strip().upper():
                    return id_stokki, codigo
                continue
            if busca and ref and ref != str(busca).strip().upper():
                continue   # outro número de NF: nem abre o detalhe
            try:
                det = stokki_recebimentos.ler_detalhe(sessao, id_stokki)
            except Exception as e:  # noqa: BLE001
                logger.info(f"   (detalhe do #PE {id_stokki} falhou: {e})")
                continue
            if chave_nfe and det["chave_nfe"] == chave_nfe:
                return id_stokki, codigo
        if not busca:
            break
    return None, ""


# ── Wizard XML múltiplo ────────────────────────────────────────────────────────

def _novo_navegador(p, wiz, usuario: str, senha: str, headless: bool):
    browser = p.chromium.launch(headless=headless)
    context = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                             "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
    context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    context.add_init_script(_JS_HOOK_XHR)
    page = context.new_page()
    wiz.fazer_login(page, usuario, senha, modo_automatico=True)
    return browser, page


def executar_wizard_xml(cfg: dict, usuario: str, senha: str, lote: list[dict], arquivos: dict[str, Path], pasta_logs: Path, wiz,
                        headless: bool = True) -> dict[str, dict]:
    """Um wizard por data prevista (o formulário tem UM arrival_date pro
    lote). Antes de subir, procura #PE já existente pela chave. Devolve
    {chave: {criado, ja_existia, erro, stokki_id, codigo, resposta}}."""
    from playwright.sync_api import sync_playwright, Error as PlaywrightError

    pasta_logs.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    saida: dict[str, dict] = {}
    with sync_playwright() as p:
        browser, page = _novo_navegador(p, wiz, usuario, senha, headless)
        try:
            sessao = _SessaoNavegador(page)
            a_subir: list[dict] = []
            for e in lote:
                id_pe, codigo = procurar_pe(sessao, cfg["client_id"], e.get("numero_nf") or "", chave_nfe=e["chave_nfe"])
                if id_pe:
                    logger.info(f"   {en.rotulo_entrada(e)} já existe na Stokki ({codigo}) -- não cria de novo")
                    saida[e["chave_nfe"]] = {"criado": False, "ja_existia": True, "erro": "", "stokki_id": id_pe, "codigo": codigo, "resposta": ""}
                else:
                    a_subir.append(e)
            por_data: dict[str, list[dict]] = {}
            for e in a_subir:
                por_data.setdefault(_arrival_date(e.get("data_prevista")), []).append(e)
            for data_br, grupo in por_data.items():
                caminhos = [str(arquivos[e["chave_nfe"]]) for e in grupo]
                page.goto(URL_WIZARD_XML, wait_until="networkidle")
                page.wait_for_timeout(1500)
                wiz.selecionar_valor_select(page, "#client_id", cfg["client_id"])
                wiz.selecionar_valor_select(page, "#warehouse_id", cfg["warehouse_id"], aguardar_opcoes=True)
                wiz.selecionar_valor_select(page, "#type_transport", cfg["tipo_transporte"])
                page.wait_for_timeout(500)
                wiz.selecionar_valor_select(page, "#packaging", cfg["embalagem"])
                page.wait_for_timeout(300)
                if data_br == date.today().strftime("%d/%m/%Y"):
                    page.evaluate("() => { const c = document.querySelector('#same_day_receipt'); if (c && !c.checked) { c.checked = true; c.dispatchEvent(new Event('change')); } }")
                else:
                    page.evaluate("(v) => { const el = document.querySelector('#arrival_date'); el.value = v; el.dispatchEvent(new Event('change')); }", data_br)
                page.wait_for_timeout(300)
                page.set_input_files("#input_drop_file", caminhos)
                page.wait_for_timeout(2000)
                page.get_by_role("link", name="Próximo").click()
                page.wait_for_selector("text=Arquivos anexados", timeout=30000)
                page.wait_for_timeout(1500)
                page.screenshot(path=str(pasta_logs / f"{ts}_{data_br.replace('/', '-')}_arquivos.png"), full_page=True)
                try:
                    page.get_by_role("button", name="Criar todos pedidos").click()
                except PlaywrightError:
                    page.get_by_role("button", name="Criar pedido").first.click()
                wiz.aguardar_processamento_completo(page, esperado=len(caminhos))
                page.screenshot(path=str(pasta_logs / f"{ts}_{data_br.replace('/', '-')}_final.png"), full_page=True)
                try:
                    capturadas = page.evaluate("() => window.__stokki_respostas_incoming || []")
                except PlaywrightError:
                    capturadas = []
                respostas = {i["invoice"]: {"status": i["status"], "body": i["body"]} for i in capturadas if i.get("invoice")}
                try:
                    linhas = page.evaluate(_JS_LINHAS)
                except PlaywrightError:
                    linhas = []
                res = resultados_do_lote(linhas, [e["chave_nfe"] for e in grupo], respostas)
                for e in grupo:
                    r = res[e["chave_nfe"]]
                    item = {"criado": r["criado"], "ja_existia": False, "erro": r["erro"], "stokki_id": None, "codigo": "",
                            "resposta": (respostas.get(e["chave_nfe"]) or {}).get("body", "")}
                    if r["criado"]:
                        item["stokki_id"], item["codigo"] = procurar_pe(sessao, cfg["client_id"], e.get("numero_nf") or "", chave_nfe=e["chave_nfe"])
                    saida[e["chave_nfe"]] = item
        finally:
            browser.close()
    return saida


# ── Wizard Excel (remessas de planilha) ───────────────────────────────────────

def _primeira_origem(dados) -> str:
    """origin_id do JSON de create/excel/client/<id>, formato desconhecido:
    procura uma lista com 'id' em chaves plausíveis. '' se não houver."""
    if not isinstance(dados, dict):
        return ""
    for chave in ("origins", "origin", "addresses", "address", "data"):
        v = dados.get(chave)
        if isinstance(v, list) and v and isinstance(v[0], dict) and v[0].get("id") not in (None, ""):
            return str(v[0]["id"])
        if isinstance(v, dict) and v.get("id") not in (None, ""):
            return str(v["id"])
    return ""


def executar_wizard_excel(cfg: dict, usuario: str, senha: str, lote: list[dict], arquivos: dict[str, Path], pasta_logs: Path, wiz,
                          headless: bool = True) -> dict[str, dict]:
    from playwright.sync_api import sync_playwright

    pasta_logs.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    saida: dict[str, dict] = {}
    with sync_playwright() as p:
        browser, page = _novo_navegador(p, wiz, usuario, senha, headless)
        try:
            sessao = _SessaoNavegador(page)
            page.goto(URL_WIZARD_EXCEL, wait_until="networkidle")
            page.wait_for_timeout(1500)
            token_csrf = page.evaluate("() => (document.querySelector('meta[name=csrf-token]') || {}).content || ''") \
                or page.evaluate("() => (document.querySelector('input[name=_token]') || {}).value || ''")
            wiz.selecionar_valor_select(page, "#client_id", cfg["client_id"])
            page.wait_for_timeout(1500)
            try:
                r = page.context.request.get(URL_CLIENTE_EXCEL.format(client_id=cfg["client_id"]), headers=_HEADERS_AJAX, timeout=30000)
                origem_id = _primeira_origem(fila_saida._json_ou_none(r)) if r.ok else ""
            except Exception as e:  # noqa: BLE001
                logger.info(f"   (origem do cliente no wizard Excel indisponível: {e})")
                origem_id = ""
            carrier = fila_saida._escolher_transportadora(page, cfg)
            page.screenshot(path=str(pasta_logs / f"{ts}_excel_form.png"), full_page=True)
            for e in lote:
                chave = e["chave_nfe"]
                id_pe, codigo = procurar_pe(sessao, cfg["client_id"], e.get("referencia") or "", referencia=e.get("referencia") or "")
                if id_pe:
                    saida[chave] = {"criado": False, "ja_existia": True, "erro": "", "stokki_id": id_pe, "codigo": codigo, "resposta": ""}
                    continue
                data_br = _arrival_date(e.get("data_prevista"))
                campos = {
                    "_token": token_csrf, "position": "0", "motion": "incoming", "client_id": cfg["client_id"],
                    "destination_id": cfg["warehouse_id"], "po": (e.get("referencia") or "")[:60], "origin_id": origem_id,
                    "same_day_receipt": "1" if data_br == date.today().strftime("%d/%m/%Y") else "0",
                    "arrival_date": data_br, "carrier_id": carrier,
                    "file_excel[]": {"name": arquivos[chave].name,
                                     "mimeType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                     "buffer": arquivos[chave].read_bytes()},
                }
                try:
                    r = page.context.request.post(URL_STORE_EXCEL, multipart=campos, headers=_HEADERS_AJAX, timeout=120000)
                except Exception as ex:  # noqa: BLE001
                    saida[chave] = {"criado": False, "ja_existia": False, "erro": f"falha ao enviar à Stokki: {ex}", "stokki_id": None, "codigo": "", "resposta": str(ex)}
                    continue
                corpo = (r.text() or "")[:4000]
                if r.ok:
                    id_pe, codigo = procurar_pe(sessao, cfg["client_id"], e.get("referencia") or "", referencia=e.get("referencia") or "")
                    saida[chave] = {"criado": True, "ja_existia": False, "erro": "", "stokki_id": id_pe, "codigo": codigo, "resposta": corpo}
                else:
                    erro = fila_saida._erros_da_resposta(r)
                    if not origem_id and "origin" in erro.lower():
                        erro = "A Stokki exige uma origem cadastrada pra remessa por planilha e o cliente não tem nenhuma -- " \
                               "anuncie pelo XML da NF-e ou peça à Fresh Log pra cadastrar a origem na Stokki. Detalhe: " + erro
                    saida[chave] = {"criado": False, "ja_existia": False, "erro": erro, "stokki_id": None, "codigo": "", "resposta": corpo}
        finally:
            browser.close()
    return saida


# ── Lote ───────────────────────────────────────────────────────────────────────

def _marcar(conn, entrada_id: int, **campos) -> None:
    campos["atualizado_em"] = ep._agora()
    sets = ", ".join(f"{k} = ?" for k in campos)
    conn.execute(f"UPDATE portal_entradas SET {sets} WHERE id = ?", (*campos.values(), entrada_id))


def _falha_tecnica(conn, lote: list[dict], erro: str) -> list[dict]:
    definitivos = []
    for e in lote:
        tentativas = int(e.get("stokki_tentativas") or 0) + 1
        if tentativas >= MAX_TENTATIVAS_TECNICAS:
            _marcar(conn, e["id"], stokki_status=en.STOKKI_ERRO, stokki_tentativas=tentativas,
                    stokki_erro=f"Falha ao criar na Stokki ({tentativas}x): {erro}"[:900])
            definitivos.append({**e, "erro": erro})
        else:
            _marcar(conn, e["id"], stokki_status=en.STOKKI_NA_FILA, stokki_tentativas=tentativas,
                    stokki_erro=f"Tentativa {tentativas} falhou, vai tentar de novo: {erro}"[:900])
    conn.commit()
    return definitivos


def processar_lote(conn, cnpj: str, lote: list[dict], config: dict, simular: bool = False, headless: bool = True) -> dict:
    cfg = ep.config_stokki_cliente(conn, cnpj, config)
    resumo = {"cliente": cfg["nome"], "criados": 0, "ja_existiam": 0, "erros": 0, "adiados": 0}
    if not en.config_entradas_cliente(conn, cnpj)["entradas_ativo"]:
        logger.info(f"[{cfg['nome']}] Pedidos de Entrada desativado -- {len(lote)} entrada(s) ficam na fila.")
        resumo["adiados"] = len(lote)
        return resumo
    if not cfg["client_id"]:
        for e in lote:
            _marcar(conn, e["id"], stokki_status=en.STOKKI_ERRO, stokki_erro="Embarcador sem client_id da Stokki (interno.stkkc_id).")
        conn.commit()
        resumo["erros"] = len(lote)
        return resumo

    ids = [e["id"] for e in lote]
    conn.execute(f"UPDATE portal_entradas SET stokki_status = ?, atualizado_em = ? WHERE id IN ({','.join('?' * len(ids))})",
                 (en.STOKKI_ENVIANDO, ep._agora(), *ids))
    conn.commit()

    pasta_lote = _pasta_lotes() / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{cfg['cnpj']}"
    pasta_lote.mkdir(parents=True, exist_ok=True)
    arquivos: dict[str, Path] = {}
    prontos_xml: list[dict] = []
    prontos_plan: list[dict] = []
    try:
        wiz = None
        if not simular:
            wiz, _px = carregar_importador(config)
        for e in lote:
            try:
                if e["origem"] == ep.ORIGEM_PLANILHA:
                    itens = [dict(r) for r in conn.execute("SELECT sku, quantidade FROM portal_entrada_itens WHERE entrada_id = ? ORDER BY linha", (e["id"],))]
                    if not itens:
                        raise ValueError("remessa de planilha sem itens")
                    destino = pasta_lote / f"{e['chave_nfe']}.xlsx"
                    destino.write_bytes(ep.xlsx_pedido_stokki([{"sku": i["sku"], "quantidade": i["quantidade"], "valor_unitario": None} for i in itens]))
                    prontos_plan.append(e)
                else:
                    destino = pasta_lote / f"{e['chave_nfe']}.xml"
                    shutil.copyfile(en.caminho_arquivo(e), destino)
                    prontos_xml.append(e)
                arquivos[e["chave_nfe"]] = destino
            except Exception as ex:  # noqa: BLE001
                _marcar(conn, e["id"], stokki_status=en.STOKKI_ERRO, stokki_erro=f"Falha ao preparar o arquivo: {ex}"[:900])
                conn.commit()
                resumo["erros"] += 1
        prontos = prontos_xml + prontos_plan
        if not prontos:
            return resumo

        if simular:
            logger.info(f"[{cfg['nome']}] SIMULAÇÃO: {len(prontos)} entrada(s) marcadas como criadas sem tocar a Stokki.")
            resultados = {e["chave_nfe"]: {"criado": True, "ja_existia": False, "erro": "", "stokki_id": None, "codigo": "", "resposta": ""} for e in prontos}
        else:
            espera = int(_cfg_portal(config).get("espera_stokki_minutos") or 45) * 60
            if not sessao_uso.adquirir(DONO_TRAVA, ttl_segundos=30 * 60, esperar_segundos=espera):
                logger.warning(f"[{cfg['nome']}] Stokki ocupada por '{sessao_uso.em_uso()}' -- lote volta pra fila.")
                conn.execute(f"UPDATE portal_entradas SET stokki_status = ?, atualizado_em = ? WHERE id IN ({','.join('?' * len(prontos))})",
                             (en.STOKKI_NA_FILA, ep._agora(), *[e["id"] for e in prontos]))
                conn.commit()
                resumo["adiados"] = len(prontos)
                return resumo
            try:
                usuario, senha = _credenciais(config)
                resultados = {}
                if prontos_xml:
                    logger.info(f"[{cfg['nome']}] criando {len(prontos_xml)} recebimento(s) por XML na Stokki (client_id={cfg['client_id']})...")
                    resultados.update(executar_wizard_xml(cfg, usuario, senha, prontos_xml, arquivos, pasta_lote, wiz, headless=headless))
                if prontos_plan:
                    logger.info(f"[{cfg['nome']}] criando {len(prontos_plan)} recebimento(s) por planilha na Stokki...")
                    resultados.update(executar_wizard_excel(cfg, usuario, senha, prontos_plan, arquivos, pasta_lote, wiz, headless=headless))
            finally:
                sessao_uso.liberar(DONO_TRAVA)

        erros_definitivos = []
        for e in prontos:
            r = resultados.get(e["chave_nfe"])
            if r is None:
                _falha_tecnica(conn, [e], "a Stokki não devolveu resultado pra esse arquivo")
                resumo["erros"] += 1
                continue
            if r["criado"] or r["ja_existia"]:
                _marcar(conn, e["id"], stokki_status=en.STOKKI_CRIADO, stokki_erro=None, stokki_id=r["stokki_id"],
                        stokki_codigo=r["codigo"] or None)
                resumo["ja_existiam" if r["ja_existia"] else "criados"] += 1
                if r["criado"] and not r["stokki_id"]:
                    logger.info(f"   {en.rotulo_entrada(e)} criado, mas o #PE ainda não foi achado -- o timer do WMS amarra pela chave.")
            else:
                _marcar(conn, e["id"], stokki_status=en.STOKKI_ERRO, stokki_erro=(r["erro"] or "recusado pela Stokki")[:900],
                        stokki_tentativas=int(e.get("stokki_tentativas") or 0) + 1)
                erros_definitivos.append({**e, "erro": r["erro"]})
                resumo["erros"] += 1
        conn.commit()
        if erros_definitivos:
            _avisar_erros(config, cfg, erros_definitivos, "A Stokki recusou a(s) entrada(s) abaixo.")
        logger.info(f"[{cfg['nome']}] lote concluído: {resumo}")
        return resumo
    except Exception as ex:  # noqa: BLE001
        logger.error(f"[{cfg['nome']}] falha técnica no lote: {ex}\n{traceback.format_exc()}")
        pendentes = (prontos_xml + prontos_plan) or lote
        definitivos = _falha_tecnica(conn, pendentes, f"{type(ex).__name__}: {ex}")
        if definitivos:
            _avisar_erros(config, cfg, definitivos, "Não conseguimos criar a(s) entrada(s) abaixo na Stokki depois de 3 tentativas.")
        resumo["erros"] += len(definitivos)
        resumo["adiados"] += len(pendentes) - len(definitivos)
        return resumo


def _avisar_erros(config: dict, cfg: dict, lote: list[dict], cabecalho: str) -> None:
    """Mesmo desenho de enviar_stokki._avisar_erros: e-mail pro cliente com
    cópia pro atendimento. Respeita portal_cliente.envios.forcar_destino
    (default hugo@) enquanto o Hugo não ligar o envio real."""
    if not lote:
        return
    import bloqueio_area
    email_cfg = config.get("email", {}) or {}
    forcar = bloqueio_area.forcar_destino(config)
    destinos = [forcar] if forcar else list(cfg.get("emails") or [])
    atendimento = email_cfg.get("email_atendimento") or email_cfg.get("email_responsavel")
    cc = [] if forcar else ([atendimento] if atendimento and atendimento not in destinos else [])
    if not destinos and not cc:
        return
    url = (_cfg_portal(config).get("url_base") or "https://app.freshhub.com.br/cliente").rstrip("/")
    linhas = "".join(f"<tr><td style='padding:6px 10px;border-bottom:1px solid #E5E7EB'><b>{en.rotulo_entrada(e)}</b></td>"
                     f"<td style='padding:6px 10px;border-bottom:1px solid #E5E7EB;color:#B91C1C'>{(e.get('erro') or '')[:300]}</td></tr>" for e in lote)
    corpo = envelope_html(f"<p>Olá, <strong>{cfg['nome']}</strong>.</p><p>{cabecalho}</p>"
                          f"<table style='border-collapse:collapse;font-size:13px;width:100%'><tr><th align='left' style='padding:6px 10px'>Entrada</th>"
                          f"<th align='left' style='padding:6px 10px'>Motivo</th></tr>{linhas}</table>"
                          f"<p style='margin-top:20px'>Você pode cancelar e anunciar de novo pelo portal: <a href='{url}/?aba=entradas'>{url}</a>.</p>",
                          rodape="Fresh Log · Portal do cliente · pedidos de entrada", cor_acento="#EF4444")
    enviar_email(destinos or cc, f"Fresh Log · Entrada(s) não criada(s) na Stokki ({len(lote)})", corpo, email_cfg, cc=cc if destinos else None)


# ── Ciclo ──────────────────────────────────────────────────────────────────────

def ciclo(config: dict, simular: bool = False, headless: bool = True) -> dict:
    ep.limpar_temporarios()
    conn = en.conectar()
    try:
        lote_max = int(_cfg_portal(config).get("lote_maximo") or 30)
        conn.execute("UPDATE portal_entradas SET stokki_status = 'NA_FILA', atualizado_em = datetime('now','localtime') "
                     "WHERE stokki_status = 'ENVIANDO' AND atualizado_em < datetime('now','localtime','-30 minutes')")
        conn.commit()
        rows = conn.execute("SELECT * FROM portal_entradas WHERE stokki_status = 'NA_FILA' AND status = 'ANUNCIADO' "
                            "ORDER BY cnpj_embarcador, data_prevista, id").fetchall()
        por_cliente: dict[str, list[dict]] = {}
        for r in rows:
            por_cliente.setdefault(r["cnpj_embarcador"], []).append(dict(r))
        total = {"lotes": 0, "criados": 0, "ja_existiam": 0, "erros": 0, "adiados": 0}
        for cnpj, lote in por_cliente.items():
            for i in range(0, len(lote), lote_max):
                resumo = processar_lote(conn, cnpj, lote[i:i + lote_max], config, simular=simular, headless=headless)
                total["lotes"] += 1
                for k in ("criados", "ja_existiam", "erros", "adiados"):
                    total[k] += resumo.get(k, 0)
        return total
    finally:
        conn.close()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Worker da fila de Pedidos de Entrada do portal (cria o #PE na Stokki)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--loop", action="store_true", help="roda pra sempre, um ciclo a cada 20 s (serviço)")
    g.add_argument("--uma-vez", action="store_true", help="um ciclo só")
    p.add_argument("--simular", action="store_true", help="não toca a Stokki: marca como criado (teste local)")
    p.add_argument("--visivel", action="store_true", help="abre o navegador na tela (debug)")
    args = p.parse_args(argv)
    config = carregar_config()
    if args.loop:
        logger.info(f"worker de entradas iniciado (ciclo a cada {INTERVALO_LOOP_SEGUNDOS}s{' · SIMULAÇÃO' if args.simular else ''})")
        while True:
            try:
                r = ciclo(config, simular=args.simular, headless=not args.visivel)
                if r["lotes"]:
                    logger.info(f"ciclo: {r}")
            except Exception as e:  # noqa: BLE001
                logger.error(f"ciclo falhou: {e}\n{traceback.format_exc()}")
            time.sleep(INTERVALO_LOOP_SEGUNDOS)
    r = ciclo(config, simular=args.simular, headless=not args.visivel)
    logger.info(f"ciclo: {r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
