# -*- coding: utf-8 -*-
"""
sincronizar_cpf_motoristas.py

Preenche a coluna CPF_MOTORISTA da tabela bd_motoristas a partir do
VUUPT (pedido do Hugo, 22/08 -- identificação do motorista na página
compartilhada do marketplace de rotas, ver regras/preferencias_motoristas.py
e confirmacao_motoristas/app.py::escolher_rota_por_cpf).

O CPF do motorista já é cadastrado no VUUPT, só que não num campo
chamado "CPF" -- é o campo genérico "code" do agente (rótulo na tela do
VUUPT: "Matrícula, Doc. Identidade, Nº CNH, etc", achado do Hugo,
22/08). Confirmado em 22/08: 20/20 motoristas ativos têm esse campo
preenchido no VUUPT -- MAS nem todo valor é um CPF de verdade (ex.:
agent_id 46454 tem code='caioerick2', claramente um apelido de conta,
não documento). Por isso este script só aceita o valor quando, depois
de tirar tudo que não é dígito, sobram EXATAMENTE 11 dígitos -- mesmo
padrão de "dado não reconhecido não vira dado usado" já visto em
TIPO_VEICULO (regras/preferencias_motoristas.py). Não faz validação de
dígito verificador de CPF de verdade -- só o formato (11 dígitos),
suficiente pra descartar apelidos/matrículas curtas.

Só ATUALIZA a coluna CPF_MOTORISTA -- nunca sobrescreve um CPF já
preenchido manualmente (mesmo padrão protetivo de
sincronizar_placas_motoristas.py). Roda sob demanda (não é rotina
agendada) -- execute de novo sempre que motoristas novos entrarem.

COMO USAR:
    py -3.11 sincronizar_cpf_motoristas.py            # a seco: só mostra
    py -3.11 sincronizar_cpf_motoristas.py --aplicar  # grava na tabela
"""
import argparse
import logging
import re
import sys
from pathlib import Path

import requests
import yaml

RAIZ = Path(__file__).resolve().parent
sys.path.insert(0, str(RAIZ))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

from nucleo.banco import conectar  # noqa: E402
from regras.motoristas_banco import atualizar, escrever_espelho, listar  # noqa: E402

API_BASE = "https://api.vuupt.com/api/v1"
ORIGEM = Path(__file__).name


def _listar_tudo(endpoint: str, headers: dict) -> list[dict]:
    todos = []
    pagina = 1
    while True:
        resp = requests.get(f"{API_BASE}{endpoint}", headers=headers,
                            params={"per_page": 100, "page": pagina}, timeout=20)
        resp.raise_for_status()
        corpo = resp.json()
        dados = corpo.get("data", [])
        todos.extend(dados)
        paginacao = corpo.get("meta", {}).get("pagination", {})
        if pagina >= paginacao.get("total_pages", pagina):
            break
        pagina += 1
    return todos


def _cpf_valido(code) -> str | None:
    digitos = re.sub(r"\D", "", str(code or ""))
    return digitos if len(digitos) == 11 else None


def sincronizar(conn, agentes: list[dict], aplicar: bool) -> dict:
    """Preenche CPF_MOTORISTA vazio em bd_motoristas com o 'code' do agente no
    VUUPT (só se forem 11 dígitos; gravado só com dígitos, texto). Sem
    `aplicar` só calcula (nada é gravado)."""
    cpf_por_agent_id: dict[int, str] = {}
    code_nao_reconhecido: dict[int, str] = {}
    for agente in agentes:
        code = agente.get("code")
        if not code:
            continue
        cpf = _cpf_valido(code)
        if cpf:
            cpf_por_agent_id[agente["id"]] = cpf
        else:
            code_nao_reconhecido[agente["id"]] = str(code)

    atualizados, ja_tinha, sem_dado = [], 0, []
    for linha in listar(conn):
        agent_id = linha.get("AGENT_ID_VUUPT")
        if agent_id is None:
            continue

        cpf_atual = str(linha.get("CPF_MOTORISTA") or "").strip()
        if cpf_atual and cpf_atual.lower() != "nan":
            ja_tinha += 1
            continue

        cpf_vuupt = cpf_por_agent_id.get(agent_id)
        if cpf_vuupt:
            if aplicar:
                atualizar(conn, linha["id"], {"CPF_MOTORISTA": cpf_vuupt}, ORIGEM)
            atualizados.append((agent_id, linha.get("NOME_MOTORISTA"), cpf_vuupt))
        elif agent_id in code_nao_reconhecido:
            sem_dado.append((agent_id, linha.get("NOME_MOTORISTA"), code_nao_reconhecido[agent_id]))
        else:
            sem_dado.append((agent_id, linha.get("NOME_MOTORISTA"), None))

    if aplicar and atualizados:
        escrever_espelho(conn)
    return {"atualizados": atualizados, "ja_tinha": ja_tinha, "sem_dado": sem_dado}


def main():
    parser = argparse.ArgumentParser(description="Preenche CPF_MOTORISTA vazio em bd_motoristas a partir do VUUPT.")
    parser.add_argument("--aplicar", action="store_true",
                        help="grava na tabela (sem isso só mostra o que faria)")
    args = parser.parse_args()

    config = yaml.safe_load(open(RAIZ / "config.yaml", encoding="utf-8"))
    token = config.get("vuupt_api", {}).get("token", "")
    headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}

    agentes = _listar_tudo("/agents", headers)
    logger.info(f"{len(agentes)} agente(s) carregado(s) do VUUPT.")

    conn = conectar()
    try:
        r = sincronizar(conn, agentes, args.aplicar)
    finally:
        conn.close()

    rotulo = "" if args.aplicar else "[A SECO -- use --aplicar para gravar] "
    logger.info(f"{rotulo}{len(r['atualizados'])} CPF(s) preenchido(s) via VUUPT, {r['ja_tinha']} já tinham CPF cadastrado.")
    for agent_id, nome, cpf in r["atualizados"]:
        logger.info(f"  + {nome} (agent_id={agent_id}): ...{cpf[-4:]}")

    if r["sem_dado"]:
        logger.warning(
            f"{len(r['sem_dado'])} motorista(s) SEM CPF resolvido -- preencher manualmente na tela "
            f"/motoristas (campo 'code' do VUUPT vazio, ou preenchido com "
            f"algo que não são 11 dígitos -- ver valor bruto abaixo quando existir):"
        )
        for agent_id, nome, bruto in r["sem_dado"]:
            sufixo = f" -- campo 'code' do VUUPT tem {bruto!r} (não são 11 dígitos)" if bruto else ""
            logger.warning(f"  - {nome} (agent_id={agent_id}){sufixo}")


if __name__ == "__main__":
    main()
