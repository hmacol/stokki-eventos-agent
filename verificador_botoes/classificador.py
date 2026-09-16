# -*- coding: utf-8 -*-
"""
Miolo puro do verificador de botoes: decide o que o navegador deixa passar,
confere se uma URL chamada existe no mapa de rotas do Flask e classifica o
que aconteceu depois de um clique. Nada aqui toca rede, banco ou navegador,
pra ser testavel com unittest.
"""
from urllib.parse import urlsplit

from werkzeug.exceptions import MethodNotAllowed, NotFound
from werkzeug.routing import Map, RequestRedirect

# Recursos que o navegador pode buscar de verdade no servidor local: sao
# arquivos, nao acoes. Qualquer outra coisa (fetch, xhr, POST de form) vira
# resposta falsa.
_RECURSOS_ESTATICOS = {"stylesheet", "script", "image", "font", "media", "manifest"}

STATUS_FALHA = ("ERRO_JS", "ROTA_INEXISTENTE", "LINK_QUEBRADO", "SEM_EFEITO")


def conferir_rota(mapa: Map, caminho: str, metodo: str) -> str:
    """Devolve "OK", "INEXISTENTE" ou "METODO" para um caminho (query string
    e ignorada) e metodo HTTP, contra o url_map do Flask."""
    caminho = urlsplit(caminho).path or "/"
    adaptador = mapa.bind("localhost")
    try:
        adaptador.match(caminho, method=metodo.upper())
    except RequestRedirect:
        return "OK"
    except MethodNotAllowed:
        return "METODO"
    except NotFound:
        return "INEXISTENTE"
    return "OK"


def decidir_intercepcao(caminho: str, metodo: str, tipo_recurso: str, navegacao: bool,
                        api_leitura: bool = False, proibidas: set[str] | None = None) -> str:
    """"PASSAR" deixa a requisicao chegar ao servidor; "FALSIFICAR" responde
    no proprio navegador e registra. Sempre passa a carga da pagina (GET de
    navegacao) e arquivos estaticos; com `api_leitura`, tambem GET em /api/
    (leituras do banco local, pra tela ter dados reais), menos as `proibidas`
    (as que tocam a Stokki). Nada que nao seja GET passa nunca."""
    if metodo.upper() != "GET":
        return "FALSIFICAR"
    if navegacao and tipo_recurso == "document":
        return "PASSAR"
    if tipo_recurso in _RECURSOS_ESTATICOS:
        return "PASSAR"
    so_caminho = urlsplit(caminho).path
    if api_leitura and so_caminho.startswith("/api/") and so_caminho not in (proibidas or set()):
        return "PASSAR"
    return "FALSIFICAR"


def telas_do_mapa(mapa: Map) -> list[tuple[str, str]]:
    """Paginas candidatas a verificacao: rotas GET sem parametro na URL,
    fora /api/, static, login e logout. Ordenadas pelo caminho."""
    telas = []
    for regra in mapa.iter_rules():
        if regra.arguments or "GET" not in (regra.methods or ()):
            continue
        if regra.endpoint in ("static", "login", "logout"):
            continue
        if regra.rule.startswith("/api/"):
            continue
        telas.append((regra.rule, regra.endpoint))
    return sorted(telas)


def separar_erros(erros: list[dict], chamadas: list[dict]) -> tuple[list[str], list[str]]:
    """Divide os erros de JS (cada um {t, msg}) em (do_botao, pos_resposta):
    erro depois da primeira chamada de rede (cada uma {t, ...}) e quase
    sempre o codigo engasgando na resposta falsa da trava, nao bug do botao."""
    if not chamadas:
        return [e["msg"] for e in erros], []
    primeira = min(c["t"] for c in chamadas)
    do_botao = [e["msg"] for e in erros if e["t"] < primeira]
    pos_resposta = [e["msg"] for e in erros if e["t"] >= primeira]
    return do_botao, pos_resposta


def _e_link_puro(evento: dict) -> bool:
    href = evento.get("href")
    if not href or evento.get("onclick"):
        return False
    if href.startswith("#") or href.startswith("javascript:"):
        return False
    return True


def classificar(mapa: Map, evento: dict) -> dict:
    """Classifica o que um botao fez. `evento` traz: desabilitado, href,
    onclick, chamadas [{metodo, caminho}], erros_js [str], navegou (bool),
    mutacoes (int, alteracoes no DOM depois do clique)."""
    if evento.get("desabilitado"):
        return {"status": "DESABILITADO", "detalhe": ""}

    if _e_link_puro(evento):
        href = evento["href"]
        if href.startswith(("http://", "https://", "mailto:", "tel:")):
            return {"status": "LINK_OK", "detalhe": "externo, nao conferido"}
        if conferir_rota(mapa, href, "GET") == "OK":
            return {"status": "LINK_OK", "detalhe": href}
        return {"status": "LINK_QUEBRADO", "detalhe": href}

    erros = evento.get("erros_js") or []
    if erros:
        return {"status": "ERRO_JS", "detalhe": "; ".join(erros)[:300]}

    quebradas = []
    for chamada in evento.get("chamadas") or []:
        if chamada.get("externo"):
            continue
        resultado = conferir_rota(mapa, chamada["caminho"], chamada["metodo"])
        if resultado != "OK":
            quebradas.append(f"{chamada['metodo']} {chamada['caminho']} ({resultado.lower()})")
    if quebradas:
        return {"status": "ROTA_INEXISTENTE", "detalhe": "; ".join(quebradas)}

    if evento.get("chamadas"):
        chamadas = ", ".join(
            f"{c['metodo']} {c['caminho'] if c.get('externo') else urlsplit(c['caminho']).path}"
            for c in evento["chamadas"])
        if evento.get("navegou"):
            chamadas += f"; navegou para {evento.get('url_depois') or '?'}"
        return {"status": "OK", "detalhe": chamadas}
    if evento.get("navegou"):
        return {"status": "NAVEGOU", "detalhe": evento.get("url_depois") or ""}
    if evento.get("dialogos"):
        return {"status": "OK", "detalhe": "dialogo: " + "; ".join(evento["dialogos"])[:200]}
    if (evento.get("mutacoes") or 0) > 0:
        return {"status": "OK", "detalhe": f"mexeu na tela ({evento['mutacoes']} alteracoes)"}
    if evento.get("form_invalido"):
        return {"status": "PRECISA_DADOS", "detalhe": "form com campos obrigatorios vazios; o navegador segurou o envio"}
    if evento.get("campo_vazio_perto"):
        return {"status": "PRECISA_DADOS", "detalhe": f"campo de texto vazio ao lado ({evento['campo_vazio_perto']})"}
    return {"status": "SEM_EFEITO", "detalhe": ""}
