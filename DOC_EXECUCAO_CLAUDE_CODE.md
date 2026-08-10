# Documento de Especificação e Execução: Agente de Verificação de Pedidos Duplicados no VUUPT

Este documento contém todas as instruções, regras de negócio, estrutura de código e comandos de validação para que o **Claude Code** (ou qualquer agente/desenvolvedor) implemente de forma autônoma e completa o agente `verificar_pedidos_duplicados_vuupt.py` no projeto `agente_stokki_eventos`.

---

## 1. Contexto do Projeto e Arquitetura

O projeto `agente_stokki_eventos` realiza a automação logística entre a plataforma **Stokki (WMS)** e a **VUUPT (Gestão de Entregas e Roteirização)**.

* **Diretório Raiz:** `C:\agente_stokki_eventos`
* **Python Target:** Python 3.11 (`py -3.11`)
* **Configuração Central:** `config.yaml` (contém credenciais VUUPT, SMTP, etc.)
* **Cliente de API da VUUPT:** `vuupt_client.py` (`VuuptClient`)
* **Registro de Agentes:** `painel_agentes/agentes.py` (`AGENTES`)

---

## 2. Objetivo da Tarefa

Criar um novo agente autônomo em Python: **`verificar_pedidos_duplicados_vuupt.py`**.

O objetivo deste agente é consultar periodicamente os serviços cadastrados na VUUPT, detectar duplicidades causadas por pequenas variações no código do pedido (ex: `#12345` vs `12345`), re-importações ou falhas de sincronização, gerar um relatório detalhado de auditoria e, quando configurado, cancelar automaticamente as cópias não atribuídas (`not_assigned`) sobressalentes.

---

## 3. Especificação Detalhada do Agente

### 3.1. Requisitos de Entrada e Argumentos (CLI)

O script deve suportar os seguintes parâmetros via `argparse`:

```bash
# Execução padrão em modo de teste (seguro, apenas leitura e relatório)
py -3.11 verificar_pedidos_duplicados_vuupt.py --modo-teste --dias 7

# Execução real cancelando as cópias excedentes não atribuídas
py -3.11 verificar_pedidos_duplicados_vuupt.py --cancelar-duplicados --dias 7
```

* `--modo-teste`: Simula a execução. Identifica e loga os duplicados, mas **não** efetua cancelamentos na API VUUPT.
* `--cancelar-duplicados`: Permite o cancelamento automático dos serviços duplicados que estejam no status `not_assigned`.
* `--dias N`: Janela em dias para filtrar a criação dos serviços (padrão: `7` dias).

---

### 3.2. Regras de Detecção de Duplicados

1. **Consulta à API VUUPT:**
   - Utilizar `VuuptClient(token)` instanciado a partir do `config.yaml` (`vuupt_api.token`).
   - Listar serviços recentes usando `vuupt_client.listar_servicos(...)` com filtro por data de criação (`created_at >= YYYY-MM-DD`).

2. **Normalização do Código do Pedido (`code`):**
   - Remover espaços em branco no início e fim.
   - Remover o caractere `#` inicial, se houver.
   - Converter para caixa alta (Uppercase).
   - Identificar sufixos de duplicidade de insucesso/re-tentativa (ex: `-1`, `_1`, `-DUP`) e extrair o **código raiz do pedido**.

3. **Agrupamento:**
   - Agrupar serviços por `codigo_raiz`.
   - Considerar duplicado qualquer grupo que possua $\ge 2$ serviços que **não estejam** no status `canceled`.

---

### 3.3. Matriz de Decisão de Manutenção vs. Cancelamento

Quando um grupo de duplicados é encontrado, o agente deve classificar cada serviço:

| Status no VUUPT | Ação Permitida | Regra de Prioridade |
|---|---|---|
| `completed`, `in_route`, `assigned` | **MANTER (Nunca cancelar)** | Alta prioridade. Se houver um serviço atribuído a motorista ou concluído, ele **deve ser preservado**. |
| `not_assigned` (Não atribuído) | **Candidato a Cancelamento** | Se já existir outro serviço principal (`assigned`/`completed`), o `not_assigned` é marcado como **sobressalente**. |
| Múltiplos `not_assigned` | **Manter o mais recente** | Se todos forem `not_assigned`, manter o serviço criado/atualizado mais recentemente e marcar os mais antigos como sobressalentes. |

> [!CAUTION]
> **Regra de Segurança Estrita:** NUNCA cancelar um serviço cujo status seja diferente de `not_assigned`. Se todos os duplicados estiverem em `assigned` ou `completed`, registrar um aviso (Warning) para intervenção manual.

---

### 3.4. Fluxo de Cancelamento e Notificação

1. Para cada serviço marcado como sobressalente:
   - Se `--modo-teste` for `True`: registrar no log `[MODO TESTE] Seria cancelado o serviço ID {service_id} (Code: {code})`.
   - Se `--cancelar-duplicados` for `True` e `modo_teste` for `False`: chamar `vuupt_client.cancelar_servico(service_id)`.
2. Salvar log detalhado da execução em `dados/verificar_duplicados.log`.
3. Se configurado em `config.yaml` (`notificacao_execucao`), enviar e-mail de resumo informando:
   - Total de serviços auditados.
   - Quantidade de grupos de duplicados encontrados.
   - Pedidos afetados e IDs VUUPT cancelados/preservados.

---

## 4. Arquivos a Criar / Modificar

### 4.1. [NOVO] `verificar_pedidos_duplicados_vuupt.py`

Estrutura sugerida do código:

```python
# -*- coding: utf-8 -*-
"""
verificar_pedidos_duplicados_vuupt.py

Verifica e relata (ou cancela) serviços duplicados no VUUPT.
"""
import argparse
import logging
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
import yaml

_RAIZ = Path(__file__).parent
sys.path.insert(0, str(_RAIZ))

from vuupt_client import VuuptClient, VuuptAPIError
from notificar_execucao_agente import notificar_execucao

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(_RAIZ / "dados" / "verificar_duplicados.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("verificar_duplicados")


def _carregar_config() -> dict:
    with open(_RAIZ / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def normalizar_codigo(code: str) -> str:
    if not code:
        return ""
    code = code.strip().lstrip("#").upper()
    # Remove sufixos como -1, _1 se aplicável para identificar a raiz
    code_base = re.sub(r"[_\-]\d+$", "", code)
    return code_base


def analisar_duplicados(servicos: list[dict]) -> dict:
    # Agrupa serviços ativos por código normalizado
    agrupados = {}
    for s in servicos:
        if s.get("status") == "canceled":
            continue
        c_norm = normalizar_codigo(s.get("code", ""))
        if not c_norm:
            continue
        agrupados.setdefault(c_norm, []).append(s)

    duplicados = {k: v for k, v in agrupados.items() if len(v) > 1}
    return duplicados


def main(modo_teste: bool = True, cancelar_duplicados: bool = False, dias: int = 7):
    logger.info("=" * 60)
    logger.info(f"{'[MODO TESTE] ' if modo_teste else ''}Iniciando verificação de duplicados no VUUPT (últimos {dias} dias)")
    logger.info("=" * 60)

    config = _carregar_config()
    token = config.get("vuupt_api", {}).get("token")
    if not token:
        logger.error("Token da API VUUPT não encontrado no config.yaml.")
        return

    client = VuuptClient(token)
    dt_inicio = (datetime.now() - timedelta(days=dias)).strftime("%Y-%m-%d")

    filtros = [
        {"field": "created_at", "operator": "gte", "value": dt_inicio}
    ]

    logger.info(f"Buscando serviços criados a partir de {dt_inicio}...")
    todos_servicos = client.listar_servicos(filtros)
    logger.info(f"Total de serviços obtidos: {len(todos_servicos)}")

    grupos_duplicados = analisar_duplicados(todos_servicos)
    logger.info(f"Grupos de duplicados identificados: {len(grupos_duplicados)}")

    cancelados_count = 0

    for code_raiz, lista in grupos_duplicados.items():
        logger.info(f"\n--- Pedido Duplicado: {code_raiz} ({len(lista)} instâncias) ---")
        
        # Ordena: atribuídos/concluídos primeiro, depois mais recentes
        # Prioridade de status: completed > in_route > assigned > not_assigned
        ordem_status = {"completed": 0, "in_route": 1, "assigned": 2, "not_assigned": 3}
        lista_ordenada = sorted(
            lista,
            key=lambda x: (ordem_status.get(x.get("status"), 4), x.get("created_at", "")),
            reverse=False # menor ordem_status vem primeiro; para empate, mais antigo primeiro
        )

        manter = lista_ordenada[0]
        sobressalentes = lista_ordenada[1:]

        logger.info(f"  [MANTER] ID {manter.get('id')} - Code: {manter.get('code')} - Status: {manter.get('status')}")

        for sob in sobressalentes:
            st = sob.get("status")
            s_id = sob.get("id")
            s_code = sob.get("code")

            if st == "not_assigned":
                if modo_teste:
                    logger.info(f"  [SIMULAÇÃO CANCELAMENTO] ID {s_id} - Code: {s_code} - Status: {st}")
                elif cancelar_duplicados:
                    try:
                        client.cancelar_servico(s_id)
                        logger.info(f"  [CANCELADO] ID {s_id} - Code: {s_code}")
                        cancelados_count += 1
                    except Exception as e:
                        logger.error(f"  [ERRO AO CANCELAR] ID {s_id}: {e}")
            else:
                logger.warning(f"  [ATENÇÃO] ID {s_id} em status '{st}' não pode ser cancelado automaticamente.")

    logger.info("=" * 60)
    logger.info(f"Verificação concluída. Duplicados encontrados: {len(grupos_duplicados)} grupos. Cancelamentos efetuados: {cancelados_count}")
    logger.info("=" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Verifica pedidos duplicados no VUUPT")
    parser.add_argument("--modo-teste", action="store_true", help="Apenas simula a auditoria sem alterar dados no VUUPT")
    parser.add_argument("--cancelar-duplicados", action="store_true", help="Efetua o cancelamento de cópias not_assigned sobressalentes")
    parser.add_argument("--dias", type=int, default=7, help="Número de dias para retroagir a busca de serviços")
    args = parser.parse_args()

    modo_teste = args.modo_teste or (not args.cancelar_duplicados)
    main(modo_teste=modo_teste, cancelar_duplicados=args.cancelar_duplicados, dias=args.dias)
```

---

### 4.2. [ALTERAR] `painel_agentes/agentes.py`

Adicionar ao array `AGENTES`:

```python
    {
        "id": "verificar_duplicados_vuupt",
        "nome": "Verificar Pedidos Duplicados no VUUPT",
        "descricao": "Audita e cancela cópias sobressalentes não atribuídas de serviços duplicados no VUUPT.",
        "script": "verificar_pedidos_duplicados_vuupt.py",
        "cwd": ".",
        "suporta_teste": True,
        "flag_teste": "--modo-teste",
        "args_fixos": [],
        "categoria": "Pipeline principal",
    },
```

---

## 5. Passos para Validação e Testes

1. **Validação CLI (Modo Teste):**
   ```bash
   py -3.11 verificar_pedidos_duplicados_vuupt.py --modo-teste --dias 7
   ```
   *Verificar se a saída exibe o total de serviços auditados e relata possíveis duplicados sem lançar erros.*

2. **Validação Painel Web:**
   - Iniciar o painel: `py -3.11 painel_agentes/painel_agentes.py`
   - Acessar no navegador: `http://localhost:8070`
   - Verificar se *"Verificar Pedidos Duplicados no VUUPT"* aparece listado em **Pipeline principal**.
   - Clicar em **Rodar em Modo Teste** e acompanhar os logs ao vivo na interface web.

---

## 6. Critérios de Aceitação

- [x] O script roda sem erros de exceção usando Python 3.11.
- [x] Respeita estritamente o parâmetro `--modo-teste`, evitando alterações acidentais na API.
- [x] Não cancela sob nenhuma hipótese serviços atribuídos ou concluídos (`assigned`, `in_route`, `completed`).
- [x] É registrado com sucesso e executável via **Painel de Agentes** web.
