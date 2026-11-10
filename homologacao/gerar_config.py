"""Gera o config da homologacao a partir do de producao, sem segredos.

Roda na VPS (criar.sh), como root:
    venv/bin/python -m homologacao.gerar_config --origem <prod> --destino <homolog> --caminho-gcs <arq>

Remove toda chave com nome de segredo, em qualquer nivel; reinsere so os liberados (lista fixa);
gera secret_key novos para painel e portal; troca url_base de app. por homolog.; acrescenta a
secao homologacao. Grava o destino com 0600 e imprime SO nomes de chaves, nunca valores.
"""
import argparse
import copy
import os
import re
import secrets
import sys

import yaml

# Palavras longas: basta aparecer em qualquer parte do nome.
PADRAO_SEGREDO = re.compile(
    r"senha|token|secret|api_key|api_secret|password|passwd|passphrase|credenciais|cookie|dsn"
    r"|private", re.I)
# Palavras curtas: so como pedaco inteiro do nome (separado por _, -, digito ou corcova do
# camelCase), com plural. Dentro de outra palavra ("Pinheiros", "mapping", "passo") nao e
# segredo, e sumir com a chave mudaria o comportamento.
PEDACO_SEGREDO = re.compile(r"^(?:pin|cpf|pass)(?:e?s)?$")
# corcovas: sigla com plural ("PINs"), sigla, palavra ("user", "Pin")
_CORCOVAS = re.compile(r"[A-Z]+s(?![a-z])|[A-Z]+(?![a-z])|[A-Z]?[a-z]+")
_SEPARADORES = re.compile(r"[^A-Za-z]+")
# rede extra: nome que TERMINA em pin/cpf (com plural) logo depois de separador ou corcova
# (sem re.I de proposito: com ele a corcova [a-z]->[A-Z] casaria qualquer par de letras)
_FINAL_SEGREDO = re.compile(r"(?:^|(?<=[^A-Za-z])|(?<=[a-z])(?=[A-Z]))(?:[Pp][Ii][Nn]|[Cc][Pp][Ff])[sS]?$")
# nomes que casam com o padrao mas nao sao segredo
NAO_SEGREDOS = {"max_tokens"}
# valor com credencial embutida na URL (esquema://usuario[:senha]@...)
URL_COM_CREDENCIAL = re.compile(r"^[a-z][a-z0-9+.-]*://[^/?#\s@]+@", re.I)
VALOR_REDIGIDO = "HOMOLOG-REMOVIDO"
SEGREDOS_LIBERADOS = ["vuupt_api.token", "google_maps.api_key", "anthropic.api_key",
                      "freshhub_api.token", "gcs.credenciais_json"]
URL_PRODUCAO = "https://app.freshhub.com.br/"
URL_HOMOLOG = "https://homolog.freshhub.com.br/"
CHAVE_FALSA_OPENWA = "HOMOLOG-SEM-CHAVE"
CNPJS_PERMITIDOS = ["00000000000191"]


def _pedacos(nome: str) -> set:
    # duas leituras, para falhar fechado: so pelos separadores ("CPFs" -> cpfs) e
    # tambem pelas corcovas do camelCase ("userPin" -> user, pin)
    pedacos = set()
    for parte in _SEPARADORES.split(nome):
        if parte:
            pedacos.add(parte.lower())
            pedacos.update(p.lower() for p in _CORCOVAS.findall(parte))
    return pedacos


def _e_segredo(chave) -> bool:
    nome = str(chave)
    if nome.lower() in NAO_SEGREDOS:
        return False
    return (bool(PADRAO_SEGREDO.search(nome))
            or any(PEDACO_SEGREDO.match(p) for p in _pedacos(nome))
            or bool(_FINAL_SEGREDO.search(nome)))


def _url_com_credencial(valor) -> bool:
    return isinstance(valor, str) and bool(URL_COM_CREDENCIAL.match(valor.strip()))


def _remover_segredos(no, caminho, removidas):
    if isinstance(no, dict):
        for chave in list(no):
            nome = f"{caminho}.{chave}" if caminho else str(chave)
            if _e_segredo(chave):
                del no[chave]
                removidas.append(nome)
            elif _url_com_credencial(no[chave]):
                no[chave] = VALOR_REDIGIDO
                removidas.append(nome)
            else:
                _remover_segredos(no[chave], nome, removidas)
    elif isinstance(no, list):
        for i, item in enumerate(no):
            if _url_com_credencial(item):
                no[i] = VALOR_REDIGIDO
                removidas.append(f"{caminho}[{i}]")
            else:
                _remover_segredos(item, f"{caminho}[{i}]", removidas)


def _trocar_url_base(no):
    if isinstance(no, dict):
        for chave, valor in no.items():
            if chave == "url_base" and isinstance(valor, str) and valor.startswith(URL_PRODUCAO):
                no[chave] = URL_HOMOLOG + valor[len(URL_PRODUCAO):]
            else:
                _trocar_url_base(valor)
    elif isinstance(no, list):
        for item in no:
            _trocar_url_base(item)


def _ler(cfg, pontuado):
    no = cfg
    for parte in pontuado.split("."):
        if not isinstance(no, dict) or parte not in no:
            return False, None
        no = no[parte]
    return True, no


def _gravar(cfg, pontuado, valor):
    *pais, ultima = pontuado.split(".")
    no = cfg
    for parte in pais:
        no = no.setdefault(parte, {})
        if not isinstance(no, dict):
            return False
    no[ultima] = valor
    return True


def _url_base_fixa(cfg, secao, url):
    sec = cfg.get(secao)
    if isinstance(sec, dict) and isinstance(sec.get("url_base"), str):
        sec["url_base"] = url.rstrip("/") + ("/" if sec["url_base"].endswith("/") else "")


def gerar(prod: dict, *, url_base_painel: str, url_base_portal: str,
          caminho_gcs: str) -> tuple:
    """Devolve (config_homolog, chaves_removidas, chaves_mantidas). Nao altera `prod`."""
    cfg = copy.deepcopy(prod)
    removidas, mantidas = [], []
    _remover_segredos(cfg, "", removidas)

    for pontuado in SEGREDOS_LIBERADOS:
        existe, valor = _ler(prod, pontuado)
        if not existe:
            continue
        if pontuado == "gcs.credenciais_json":
            valor = caminho_gcs
        if _gravar(cfg, pontuado, valor):
            mantidas.append(pontuado)
            if pontuado in removidas:
                removidas.remove(pontuado)

    for secao in ("painel_agentes", "portal_cliente"):
        _gravar(cfg, f"{secao}.secret_key", secrets.token_hex(32))

    if isinstance(cfg.get("whatsapp_notificacoes"), dict):
        cfg["whatsapp_notificacoes"]["api_key"] = CHAVE_FALSA_OPENWA

    _trocar_url_base(cfg)
    _url_base_fixa(cfg, "painel_agentes", url_base_painel)
    _url_base_fixa(cfg, "portal_cliente", url_base_portal)

    cfg["homologacao"] = {"portal_cnpjs_permitidos": list(CNPJS_PERMITIDOS)}
    return cfg, removidas, mantidas


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Config da homologacao sem segredos (imprime so nomes).")
    ap.add_argument("--origem", required=True)
    ap.add_argument("--destino", required=True)
    ap.add_argument("--caminho-gcs", required=True)
    ap.add_argument("--url-base-painel", default=URL_HOMOLOG + "painel")
    ap.add_argument("--url-base-portal", default=URL_HOMOLOG + "cliente")
    args = ap.parse_args(argv)

    try:
        with open(args.origem, encoding="utf-8") as f:
            prod = yaml.safe_load(f)
    except yaml.YAMLError:
        # a mensagem do YAML cita o trecho do arquivo: nunca repassar
        print("erro: origem nao e um YAML valido", file=sys.stderr)
        return 1
    if not isinstance(prod, dict):
        print("erro: origem nao e um mapa YAML", file=sys.stderr)
        return 1

    cfg, removidas, mantidas = gerar(prod, url_base_painel=args.url_base_painel,
                                     url_base_portal=args.url_base_portal,
                                     caminho_gcs=args.caminho_gcs)
    texto = yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False, default_flow_style=False)
    fd = os.open(args.destino, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(texto)
    os.chmod(args.destino, 0o600)

    print("removidas: " + ", ".join(removidas))
    print("mantidas: " + ", ".join(mantidas))
    return 0


if __name__ == "__main__":
    sys.exit(main())
