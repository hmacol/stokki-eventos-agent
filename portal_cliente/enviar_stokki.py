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

Pedidos de PLANILHA (origem='planilha', pedido do Hugo em 09/09/2026) vão
por OUTRO caminho da Stokki, o wizard de importação por Excel
(inventory/outbound/create/excel, descoberto por sondagem read-only em
09/09): um xlsx SKU/Quantidade/Valor Unitário por pedido + os campos do
formulário (client_id, origin_id, po, destination_id, type_transport,
packaging, delivery, carrier_id, expedition_date) num POST multipart em
inventory/outbound/create/store. O destinatário precisa existir no
cadastro de endereços do cliente na Stokki: procuramos em
/address/search/{client_id}/Destination e, se não achar, cadastramos em
client/transport/address/store. Tudo pelo contexto do navegador logado
(page.context.request), sem clicar no wizard. NUNCA rodou contra a
Stokki real -- formatos de resposta são best-effort (ver executar_wizard_excel).

COMO RODAR:
    py -3 portal_cliente/enviar_stokki.py --loop            # serviço (VPS)
    py -3 portal_cliente/enviar_stokki.py --uma-vez         # um ciclo
    py -3 portal_cliente/enviar_stokki.py --uma-vez --simular   # sem tocar a Stokki
    py -3 portal_cliente/enviar_stokki.py --uma-vez --visivel   # navegador na tela
"""
import argparse
import json
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
import pedidos_dedicados
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


# ── Wizard Excel da Stokki (pedidos de planilha) ───────────────────────────────

URL_BASE_STOKKI = "https://freshlog.stokki.com.br"
URL_STORE_EXCEL = f"{URL_BASE_STOKKI}/pt-br/administrator/inventory/outbound/create/store"
URL_BUSCA_DESTINO = f"{URL_BASE_STOKKI}/pt-br/address/search/{{client_id}}/Destination"
URL_CADASTRO_ENDERECO = f"{URL_BASE_STOKKI}/pt-br/administrator/client/transport/address/store"
_HEADERS_AJAX = {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json, text/javascript, */*; q=0.01"}


def _json_ou_none(resp):
    try:
        return resp.json()
    except Exception:
        return None


def _erros_da_resposta(resp) -> str:
    """Laravel devolve 422 {"message":..., "errors": {campo: [msgs]}}."""
    dados = _json_ou_none(resp)
    partes = []
    if isinstance(dados, dict):
        errs = dados.get("errors")
        if isinstance(errs, dict):
            for v in errs.values():
                partes.extend(v if isinstance(v, list) else [str(v)])
        elif isinstance(errs, list):
            partes.extend(str(v) for v in errs)
        if not partes and dados.get("message"):
            partes.append(str(dados["message"]))
    if not partes:
        texto = re.sub(r"<[^>]+>", " ", resp.text() or "")
        partes.append(f"HTTP {resp.status}: {' '.join(texto.split())[:200]}")
    return "; ".join(str(p) for p in partes)[:900]


def _achar_id(dados) -> str:
    """id num JSON de formato desconhecido: {id}, {data:{id}}, [{id}, ...]."""
    if isinstance(dados, dict):
        for k in ("id", "address_id", "destination_id"):
            if dados.get(k) not in (None, ""):
                return str(dados[k])
        for k in ("data", "address", "destination", "result"):
            if k in dados:
                achado = _achar_id(dados[k])
                if achado:
                    return achado
    elif isinstance(dados, list) and dados:
        return _achar_id(dados[0])
    return ""


def _resolver_destinatario(page, client_id: str, envio: dict, token_csrf: str) -> tuple[str, str]:
    """(destination_id, erro). Procura pelo CNPJ/CPF (com e sem pontuação);
    se não existir, cadastra o endereço do pedido no cliente da Stokki."""
    doc = ep._so_digitos(envio.get("destinatario_doc"))
    for busca in (ep.formatar_documento(doc), doc, (envio.get("destinatario_nome") or "")[:40]):
        if not busca:
            continue
        try:
            r = page.context.request.get(URL_BUSCA_DESTINO.format(client_id=client_id), params={"search": busca},
                                         headers=_HEADERS_AJAX, timeout=30000)
        except Exception as e:
            return "", f"busca de destinatário falhou: {e}"
        if r.ok:
            dest_id = _achar_id(_json_ou_none(r))
            if dest_id:
                logger.info(f"   destinatário {doc} encontrado na Stokki (id {dest_id}, busca '{busca}')")
                return dest_id, ""
    extra = {}
    try:
        extra = json.loads(envio.get("itens_json") or "{}").get("destinatario") or {}
    except Exception:
        pass
    nome = (envio.get("destinatario_nome") or "")[:120]
    partes_nome = nome.split(" ", 1)
    pj = len(doc) == 14
    cep = ep._so_digitos(envio.get("destinatario_cep"))
    uf = (envio.get("destinatario_uf") or "").upper()
    campos = {
        "_token": token_csrf, "client_id": client_id, "page": "create", "type_address": "Destination",
        "type_account_address": "company" if pj else "personal",
        "brand_name_address": nome, "company_name_address": nome if pj else "",
        "name_address": partes_nome[0] if not pj else "", "last_name_address": (partes_nome[1] if len(partes_nome) > 1 else "") if not pj else "",
        "cnpj_address": ep.formatar_documento(doc) if pj else "", "cpf_address": ep.formatar_documento(doc) if not pj else "",
        "state_registration_address": "", "telephone_address": envio.get("destinatario_telefone") or "",
        "email_address": extra.get("destinatario_email") or "",
        "zip_address": f"{cep[:5]}-{cep[5:]}" if len(cep) == 8 else cep,
        "street_address": extra.get("destinatario_logradouro") or (envio.get("destinatario_endereco") or "").split(",")[0],
        "number_address": extra.get("destinatario_numero") or "0",
        "complement_address": extra.get("destinatario_complemento") or "",
        "district_address": envio.get("destinatario_bairro") or "", "city_address": envio.get("destinatario_municipio") or "",
        "state_address": ep.NOMES_UF.get(uf, uf), "country_address": "Brasil", "latitude": "0", "longitude": "0",
    }
    try:
        r = page.context.request.post(URL_CADASTRO_ENDERECO, multipart=campos, headers=_HEADERS_AJAX, timeout=60000)
    except Exception as e:
        return "", f"cadastro do destinatário falhou: {e}"
    if not r.ok:
        return "", f"a Stokki recusou o cadastro do destinatário {nome}: {_erros_da_resposta(r)}"
    dest_id = _achar_id(_json_ou_none(r))
    if not dest_id:
        # cadastrou mas não devolveu id reconhecível: procura de novo
        try:
            r2 = page.context.request.get(URL_BUSCA_DESTINO.format(client_id=client_id), params={"search": ep.formatar_documento(doc)},
                                          headers=_HEADERS_AJAX, timeout=30000)
            dest_id = _achar_id(_json_ou_none(r2)) if r2.ok else ""
        except Exception:
            dest_id = ""
    if not dest_id:
        return "", f"destinatário {nome} cadastrado na Stokki mas sem id na resposta ({(r.text() or '')[:160]})"
    logger.info(f"   destinatário {doc} cadastrado na Stokki (id {dest_id})")
    return dest_id, ""


def _escolher_transportadora(page, cfg: dict) -> str:
    """carrier_id é obrigatório no wizard Excel e o select é carregado por
    cliente: usa o configurado, senão a opção cujo texto casa com
    carrier_nome (config), senão a primeira."""
    if cfg.get("carrier_id"):
        return str(cfg["carrier_id"])
    opcoes = page.evaluate("() => Array.from(document.querySelectorAll('#carrier_id option')).map(o => [o.value, o.textContent.trim()])")
    opcoes = [(v, t) for v, t in opcoes if v]
    if not opcoes:
        return ""
    alvo = (cfg.get("carrier_nome") or "").strip().lower()
    if alvo:
        for v, t in opcoes:
            if alvo in t.lower():
                return v
    return opcoes[0][0]


def executar_wizard_excel(cfg: dict, usuario: str, senha: str, envios: list[dict], arquivos: dict[str, Path], pasta_logs: Path, wiz,
                          headless: bool = True) -> tuple[list[dict], dict, dict[str, str]]:
    """Cria na Stokki os pedidos de planilha. Devolve (resultados por chave
    [{arquivo, criado, erro}], respostas por chave {status, body}, códigos
    por chave) -- mesmo contrato de executar_wizard, com a chave sintética
    no lugar do nome do XML."""
    from playwright.sync_api import sync_playwright

    pasta_logs.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    resultados: list[dict] = []
    respostas: dict = {}
    codigos: dict[str, str] = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        try:
            context = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                                     "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
            context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            page = context.new_page()
            wiz.fazer_login(page, usuario, senha, modo_automatico=True)
            page.goto(cfg["url_importacao_excel"], wait_until="networkidle")
            page.wait_for_timeout(1500)
            token_csrf = page.evaluate("() => (document.querySelector('meta[name=csrf-token]') || {}).content || ''")
            if not token_csrf:
                token_csrf = page.evaluate("() => (document.querySelector('input[name=_token]') || {}).value || ''")
            wiz.selecionar_valor_select(page, "#client_id", cfg["client_id"])
            wiz.selecionar_valor_select(page, "#warehouse_id", cfg["warehouse_id"], aguardar_opcoes=True)
            wiz.selecionar_valor_select(page, "#type_transport", cfg["tipo_transporte"])
            page.wait_for_timeout(500)
            try:
                wiz.selecionar_valor_select(page, "#packaging", cfg["embalagem"])
            except Exception as e:
                logger.info(f"   (embalagem '{cfg['embalagem']}' não selecionável no wizard Excel: {e})")
            page.wait_for_timeout(500)
            carrier = _escolher_transportadora(page, cfg)
            page.screenshot(path=str(pasta_logs / f"{ts}_excel_form.png"), full_page=True)
            if not carrier:
                raise RuntimeError("O wizard Excel da Stokki exige transportadora (carrier_id) e o cliente não tem nenhuma cadastrada -- "
                                   "cadastre na Stokki ou informe portal_cliente.stokki_padrao.carrier_id.")
            for e in envios:
                chave = e["chave_nfe"]
                arquivo = arquivos[chave]
                dest_id, erro = _resolver_destinatario(page, cfg["client_id"], e, token_csrf)
                if not dest_id:
                    resultados.append({"arquivo": arquivo.name, "criado": False, "erro": erro})
                    respostas[chave] = {"status": 0, "body": erro}
                    continue
                data_exp = e.get("data_expedicao") or datetime.now().strftime("%Y-%m-%d")
                if data_exp < datetime.now().strftime("%Y-%m-%d"):
                    data_exp = datetime.now().strftime("%Y-%m-%d")
                campos = {
                    "_token": token_csrf, "position": "0", "motion": "sale", "client_id": cfg["client_id"],
                    "origin_id": cfg["warehouse_id"], "po": (e.get("referencia") or "")[:60], "destination_id": dest_id,
                    "type_transport": cfg["tipo_transporte"], "packaging": cfg["embalagem"], "delivery": cfg.get("prioridade") or "",
                    "carrier_id": carrier, "expedition_date": "/".join(reversed(data_exp.split("-"))), "check_declaration": "0",
                    "file_excel[]": {"name": arquivo.name,
                                     "mimeType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                     "buffer": arquivo.read_bytes()},
                }
                try:
                    r = page.context.request.post(URL_STORE_EXCEL, multipart=campos, headers=_HEADERS_AJAX, timeout=120000)
                except Exception as ex:
                    resultados.append({"arquivo": arquivo.name, "criado": False, "erro": f"falha ao enviar à Stokki: {ex}"})
                    respostas[chave] = {"status": 0, "body": str(ex)}
                    continue
                corpo = r.text() or ""
                respostas[chave] = {"status": r.status, "body": corpo[:4000]}
                if r.ok:
                    resultados.append({"arquivo": arquivo.name, "criado": True, "erro": ""})
                    codigo = _codigo_da_resposta(respostas[chave])
                    if codigo:
                        codigos[chave] = codigo
                    logger.info(f"   pedido {e.get('referencia')} criado (HTTP {r.status})")
                else:
                    resultados.append({"arquivo": arquivo.name, "criado": False, "erro": _erros_da_resposta(r)})
                    logger.info(f"   pedido {e.get('referencia')} recusado (HTTP {r.status}): {resultados[-1]['erro'][:200]}")
            criados = [e for e in envios if codigos.get(e["chave_nfe"]) is None and respostas.get(e["chave_nfe"], {}).get("status", 0) in range(200, 300)]
            if criados:
                try:
                    achados = _buscar_codigos_na_listagem(page, [x["referencia"] for x in criados if x.get("referencia")])
                    for x in criados:
                        if achados.get(x.get("referencia")):
                            codigos[x["chave_nfe"]] = achados[x["referencia"]]
                except Exception as ex:
                    logger.info(f"   (não consegui consultar a listagem pra achar os códigos: {ex})")
            page.screenshot(path=str(pasta_logs / f"{ts}_excel_final.png"), full_page=True)
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


def _gravar_codigo(conn, envio_id: int, codigo: str | None, **campos) -> None:
    """Grava o PS do envio e, se ele foi liberado como dedicado antes de ter
    codigo (Hugo, 23/09), vincula a marca em pedidos_dedicados -- vale tanto
    pro codigo que a Stokki devolve na criacao quanto pro achado depois na
    reconciliacao. Nao faz commit."""
    _marcar(conn, envio_id, codigo_pedido=codigo, **campos)
    if not codigo:
        return
    try:
        pedidos_dedicados.vincular_codigo(conn, envio_id, codigo)
    except Exception as ex:
        logger.warning(f"nao vinculou dedicado do envio {envio_id}: {ex}")


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
    arquivos: list[Path] = []              # XMLs prontos pro wizard de NF-e
    arquivos_plan: dict[str, Path] = {}    # chave -> xlsx da Stokki (pedidos de planilha)
    prontos: list[dict] = []
    prontos_xml: list[dict] = []
    prontos_plan: list[dict] = []
    try:
        wiz = px = None
        if not simular:
            wiz, px = carregar_importador(config)
        for e in envios:
            planilha = (e.get("origem") or ep.ORIGEM_XML) == ep.ORIGEM_PLANILHA
            try:
                if planilha:
                    itens = (json.loads(e.get("itens_json") or "{}") or {}).get("itens") or []
                    if not itens:
                        raise ValueError("pedido de planilha sem itens")
                    destino = pasta_lote / f"{e['chave_nfe']}.xlsx"
                    destino.write_bytes(ep.xlsx_pedido_stokki(itens))
                    arquivos_plan[e["chave_nfe"]] = destino
                    prontos_plan.append(e)
                else:
                    destino = pasta_lote / f"{e['chave_nfe']}.xml"
                    if simular:
                        shutil.copyfile(ep.caminho_xml(e), destino)
                    else:
                        transformar_xml(cfg["regra_xml"], ep.caminho_xml(e), destino, px)
                    arquivos.append(destino)
                    prontos_xml.append(e)
                prontos.append(e)
            except Exception as ex:
                o_que = "o arquivo do pedido" if planilha else f"o XML ({cfg['regra_xml']})"
                logger.warning(f"[{cfg['nome']}] {ep.rotulo_envio(e)}: falha ao preparar {o_que} ({ex})")
                _marcar(conn, e["id"], status=ep.STATUS_ERRO, erro=f"Falha ao preparar {o_que}: {ex}"[:900])
                conn.commit()
                _avisar_erros(config, cfg, [{**e, "erro": str(ex)}], "O pedido não pôde ser preparado pra Stokki.")
                resumo["erros"] += 1
        if not prontos:
            return resumo

        if simular:
            logger.info(f"[{cfg['nome']}] SIMULAÇÃO: {len(prontos_xml)} XML(s) e {len(prontos_plan)} pedido(s) de planilha "
                        f"marcados como criados sem tocar a Stokki.")
            resultados = [{"arquivo": p.name, "criado": True, "erro": ""} for p in arquivos]
            resultados += [{"arquivo": p.name, "criado": True, "erro": ""} for p in arquivos_plan.values()]
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
                resultados, respostas, codigos = [], {}, {}
                if arquivos:
                    logger.info(f"[{cfg['nome']}] enviando {len(arquivos)} XML(s) à Stokki (client_id={cfg['client_id']}, regra={cfg['regra_xml']})...")
                    r1, resp1, cod1 = executar_wizard(cfg, usuario, senha, arquivos, pasta_lote, wiz, headless=headless)
                    resultados += r1
                    respostas.update(resp1)
                    codigos.update(cod1)
                if prontos_plan:
                    logger.info(f"[{cfg['nome']}] enviando {len(prontos_plan)} pedido(s) de planilha à Stokki (wizard Excel, client_id={cfg['client_id']})...")
                    r2, resp2, cod2 = executar_wizard_excel(cfg, usuario, senha, prontos_plan, arquivos_plan, pasta_lote, wiz, headless=headless)
                    resultados += r2
                    respostas.update(resp2)
                    codigos.update(cod2)
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
                codigo = (codigos.get(e["numero_nf"]) if e.get("numero_nf") else None) or codigos.get(e["chave_nfe"]) \
                    or _codigo_da_resposta(resposta) or None
                _gravar_codigo(conn, e["id"], codigo, status=ep.STATUS_CRIADO, erro=None, resposta_stokki=resposta_txt,
                               criado_stokki_em=ep._agora())
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
        f"<tr><td style='padding:6px 10px;border-bottom:1px solid #E5E7EB'><b>{ep.rotulo_envio(e)}</b></td>"
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

def _codigo_em_documentos(conn, envio: dict) -> str | None:
    """Pedido do envio olhando as DANFEs já processadas.

    O número da NF NÃO é único: ele se repete entre embarcadores (no banco
    de 08/26, 7 de 1209 NFs apontam pra mais de um pedido -- a 245699 pra
    11). documentos_processados não guarda o emitente, só
    cnpj_contraparte, que é o DESTINATÁRIO da NF (ver
    documentos_pedido/fingerprint_documentos.py) -- serve de desempate,
    mas não dá pra exigir: 23% das linhas de NF têm contraparte vazia.
    Então: desempata pelo destinatário quando ele resolve, aceita a NF
    sozinha quando ela aponta pra um pedido só, e desiste quando fica
    ambígua (aí a busca em pedidos_historico, que filtra por embarcador,
    ainda pode responder; senão o próximo ciclo tenta de novo)."""
    linhas = conn.execute(
        "SELECT DISTINCT codigo_pedido, "
        "REPLACE(REPLACE(REPLACE(COALESCE(cnpj_contraparte,''),'.',''),'/',''),'-','') AS dest "
        "FROM documentos_processados WHERE tipo = 'Nota Fiscal' AND numero_nf = ? "
        "AND COALESCE(codigo_pedido,'') != ''", (envio["numero_nf"],)).fetchall()
    if not linhas:
        return None
    dest = re.sub(r"\D", "", envio.get("destinatario_doc") or "")
    candidatos = {r["codigo_pedido"] for r in linhas if dest and r["dest"] == dest}
    if not candidatos:
        candidatos = {r["codigo_pedido"] for r in linhas}
    if len(candidatos) == 1:
        return candidatos.pop()
    logger.warning(f"envio {envio['id']}: NF {envio['numero_nf']} aponta pra {len(candidatos)} pedidos "
                   f"({', '.join(sorted(candidatos))}) e o destinatário não desempata -- deixando sem código")
    return None


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
            codigo = _codigo_em_documentos(conn, e)
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
            _gravar_codigo(conn, e["id"], codigo)
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
