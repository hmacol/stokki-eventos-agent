# Documento de Especificação e Execução: Alocação Automática e Equitativa de Motoristas por Rota

Este documento contém todas as instruções, regras de negócio, estrutura de código e comandos de validação para que o **Claude Code** (ou qualquer agente/desenvolvedor) implemente de forma autônoma e completa a **alocação automática de motoristas** no módulo de roteirização (`roteirizacao/`) do projeto `agente_stokki_eventos`.

---

## 1. Contexto e Objetivos

* **Alocação Automática de Agentes/Veículos:** Atribuir automaticamente um motorista (`agent_id` / `vehicle_id`) a cada rota criada ou incrementada no VUUPT.
* **Leitura da Base de Preferências:** Consultar a base cadastral de motoristas (`dados/BD_MOTORISTAS.xlsx` ou fallback `dados/motoristas_preferencias.json`).
* **Regra de Viagens (Fora da Grande SP):**
  * **Definição de Viagem:** Qualquer entrega com destino **fora do raio da Grande São Paulo** (distância > 70 km de SP centro ou pertencente a regiões externas como Vale do Paraíba, Baixada Santista, Sorocaba, Campinas e Piracicaba).
  * **Restrição Rigorosa:** Se a rota for uma "Viagem", ela **SÓ pode ser alocada para motoristas que tenham a opção "Viagens" habilitada em suas preferências**. Motoristas sem essa opção **não devem ser considerados em hipótese alguma para viagens**.
* **Distribuição Equitativa ("Sem Preferência"):**
  * Não dar prioridade/preferência a nenhum motorista.
  * Utilizar um algoritmo de **Round-Robin com Carga Mínima (Least-Allocated Load Balancing)** para garantir que o número de rotas atribuídas seja distribuído igualmente entre os motoristas elegíveis do dia.

---

## 2. Regras de Negócio e Algoritmo

### 2.1. Estrutura da Base de Preferências dos Motoristas

A base de preferências deve ser lida a partir de `dados/BD_MOTORISTAS.xlsx` (ou `dados/motoristas_preferencias.json`), com a seguinte estrutura de colunas:

| Coluna | Tipo | Descrição | Exemplo |
|---|---|---|---|
| `AGENT_ID_VUUPT` | Inteiro | ID do Agente/Motorista na API VUUPT | `1234` |
| `VEHICLE_ID_VUUPT` | Inteiro (opcional) | ID do Veículo associado na API VUUPT | `5678` |
| `NOME_MOTORISTA` | Texto | Nome completo do motorista | `"João Silva"` |
| `ACEITA_VIAGENS` | Booleano / Texto | Indica se realiza entregas fora da Grande SP (`SIM` / `NAO` ou `TRUE` / `FALSE`) | `"SIM"` |
| `DIAS_DISPONIVEIS` | Texto | Dias da semana em que atua (separados por vírgula) | `"SEGUNDA,TERCA,QUARTA,QUINTA,SEXTA"` |
| `MAX_ROTAS_DIA` | Inteiro | Limite máximo de rotas por dia (padrão: 1) | `1` |
| `ATIVO` | Booleano / Texto | Status do motorista (`SIM` / `NAO`) | `"SIM"` |

---

### 2.2. Algoritmo de Classificação de Rota (`classificar_rota_viagem`)

Dado um sublote/grupo de pedidos de uma rota:
1. Obter as coordenadas (latitude/longitude) ou CEP de cada serviço do sublote.
2. Calcular a distância em relação à base de referência de São Paulo (`RAIO_GRANDE_SP_KM = 70.0`).
3. Verificar a cidade do serviço contra a lista de cidades de regiões externas (`Vale do Paraíba`, `Baixada Santista`, `Sorocaba`, `Campinas`, `Piracicaba`).
4. **Classificação:** Se **ao menos 1 entrega** do sublote estiver a `> 70.0 km` ou em cidade externa, a rota inteira é classificada como **`É_VIAGEM = True`**. Caso contrário, é **`É_VIAGEM = False`** (Grande SP).

---

### 2.3. Algoritmo de Alocação Equitativa (`selecionar_motorista_equitativo`)

Para uma determinada rota a ser alocada na data $D$:

1. **Filtragem de Elegibilidade:**
   - `ATIVO == True`
   - O dia da semana de $D$ está contido em `DIAS_DISPONIVEIS`.
   - `rotas_alocadas_hoje < MAX_ROTAS_DIA`.
   - **Regra de Viagem:**
     - Se `É_VIAGEM == True` $\rightarrow$ exige `ACEITA_VIAGENS == True`.
     - Se `É_VIAGEM == False` $\rightarrow$ motoristas que não aceitam viagem **podem** ser alocados normalmente (e motoristas que aceitam viagem também, caso não haja motoristas exclusivos de Grande SP livres).

2. **Desempate e Distribuição Equitativa ("Sem Preferência"):**
   - Ordenar os motoristas elegíveis pelo **menor número de rotas já alocadas hoje**.
   - Em caso de empate na contagem de rotas, aplicar **Round-Robin circular** baseado no índice do motorista para garantir alternância contínua.

3. **Fallback para Falta de Motoristas:**
   - Se nenhum motorista elegível for encontrado (ex: todas as rotas de viagem preenchidas e nenhum motorista com `ACEITA_VIAGENS` livre), a rota é criada no VUUPT **sem motorista** (`agent_id = None`), e o evento é registrado no log e no e-mail de notificação com alerta visual (`[ALERTA_ALOCACAO]`).

---

## 3. Especificação dos Arquivos a Criar e Alterar

### 3.1. [NOVO] `regras/preferencias_motoristas.py`

Cria o módulo de gerenciamento e carga do catálogo de preferências.

```python
# -*- coding: utf-8 -*-
"""
regras/preferencias_motoristas.py

Leitura e gerenciamento do cadastro de preferências dos motoristas.
Suporta planilha Excel (dados/BD_MOTORISTAS.xlsx) ou JSON de fallback.
"""
from dataclasses import dataclass
from pathlib import Path
import logging
import pandas as pd

logger = logging.getLogger(__name__)

@dataclass
class MotoristaPreferencias:
    agent_id: int
    vehicle_id: int | None
    nome: str
    aceita_viagens: bool
    dias_disponiveis: list[int]  # 0=Segunda, ..., 6=Domingo
    max_rotas_dia: int
    ativo: bool

class CatalogoMotoristas:
    def __init__(self, motoristas: list[MotoristaPreferencias]):
        self.motoristas = motoristas

    @classmethod
    def carregar(cls, caminho_excel: Path) -> "CatalogoMotoristas":
        if not caminho_excel.exists():
            logger.warning(f"Arquivo de preferências {caminho_excel} não encontrado. Nenhum motorista carregado.")
            return cls([])
        
        df = pd.read_excel(caminho_excel)
        # Parse das colunas e conversão para objetos MotoristaPreferencias
        # ...
        return cls(motoristas)
```

---

### 3.2. [NOVO] `roteirizacao/alocacao_motoristas.py`

Contém a lógica core de classificação de rotas e seleção equitativa de motoristas.

```python
# -*- coding: utf-8 -*-
"""
roteirizacao/alocacao_motoristas.py

Módulo de classificação de viagens e alocação equitativa de motoristas por rota.
"""
import logging
from datetime import date
from geocodificacao import obter_coordenadas, distancia_km
from regioes_dia_fixo import RAIO_GRANDE_SP_KM, ENDERECO_REFERENCIA_SP, extrair_cidade
from regras.preferencias_motoristas import MotoristaPreferencias

logger = logging.getLogger(__name__)

COORD_BASE_SP = (-23.550520, -46.633309)

def classificar_rota_viagem(sublote: list[dict], api_key: str | None = None) -> bool:
    """
    Retorna True se ao menos uma entrega do sublote for fora da Grande SP.
    """
    for servico in sublote:
        coords = obter_coordenadas(servico, api_key)
        if coords:
            d = distancia_km(COORD_BASE_SP[0], COORD_BASE_SP[1], coords[0], coords[1])
            if d > RAIO_GRANDE_SP_KM:
                return True
    return False


def selecionar_motorista_equitativo(
    sublote: list[dict],
    data_rota: date,
    motoristas: list[MotoristaPreferencias],
    contagem_alocacoes_dia: dict[int, int],  # agent_id -> total rotas no dia
    api_key: str | None = None
) -> MotoristaPreferencias | None:
    """
    Seleciona o motorista mais indicado para o sublote de forma equitativa.
    Respeita estritamente a preferência de viagem quando a rota é viagem.
    """
    eh_viagem = classificar_rota_viagem(sublote, api_key)
    dia_semana = data_rota.weekday()

    elegiveis = []
    for m in motoristas:
        if not m.ativo:
            continue
        if dia_semana not in m.dias_disponiveis:
            continue
        if contagem_alocacoes_dia.get(m.agent_id, 0) >= m.max_rotas_dia:
            continue
        
        # Trava estrita de Viagem
        if eh_viagem and not m.aceita_viagens:
            continue
            
        elegiveis.append(m)

    if not elegiveis:
        tipo_str = "VIAGEM" if eh_viagem else "Grande SP"
        logger.warning(f"Nenhum motorista elegível disponível para rota tipo [{tipo_str}] em {data_rota}.")
        return None

    # Ordenação equitativa: menor número de alocações no dia
    elegiveis.sort(key=lambda m: (contagem_alocacoes_dia.get(m.agent_id, 0), m.agent_id))
    
    escolhido = elegiveis[0]
    return escolhido
```

---

### 3.3. [ALTERAR] `roteirizacao/rotas_client.py`

Garantir que a função `criar_rota` passe corretamente os argumentos `agent_id` e `vehicle_id` no payload JSON enviado para `POST /routes`.

```python
# Em rotas_client.py
if agent_id is not None:
    payload["agent_id"] = agent_id
if vehicle_id is not None:
    payload["vehicle_id"] = vehicle_id
```

---

### 3.4. [ALTERAR] `roteirizacao/criar_rotas_diarias.py`

1. Importar `CatalogoMotoristas` e as funções de `alocacao_motoristas.py`.
2. Carregar o catálogo de preferências no início da função `main()`.
3. Manter um dicionário `contagem_alocacoes_dia = {}` em memória.
4. Para cada sublote gerado:
   - Invocar `selecionar_motorista_equitativo(...)`.
   - Se um motorista for retornado, extrair `agent_id` e `vehicle_id` e repassar para `_criar_rota_removendo_conflitos(...)`.
   - Atualizar a contagem `contagem_alocacoes_dia[motorista.agent_id] += 1`.
   - Registrar no log o nome do motorista atribuído e o tipo da rota (`Grande SP` ou `VIAGEM`).
5. Incluir a matriz de alocação de motoristas no e-mail de notificação de execução (`notificar_execucao`).

---

### 3.5. [ALTERAR] `roteirizacao/incrementar_rotas.py`

Garantir que o processo de incremento de rotas existentes preserve os motoristas previamente alocados e verifique se a adição de um novo pedido não transforma uma rota urbana em viagem sem a devida permissão do motorista.

---

## 4. Plano de Validação e Testes

### 4.1. Validação em Modo Teste (Dry Run)

Executar o script de criação diária de rotas com a flag `--modo-teste`:

```bash
py -3.11 roteirizacao/criar_rotas_diarias.py --modo-teste
```

**Resultados Esperados nos Logs:**
- Identificação correta de rotas de `Grande SP` vs `VIAGEM`.
- Atribuição de rotas de viagem EXCLUSIVAMENTE a motoristas com `ACEITA_VIAGENS = SIM`.
- Distribuição equilibrada da quantidade de rotas entre os motoristas ativos.

### 4.2. Testes de Casos Limite (Edge Cases)

1. **Sem motorista de viagem disponível:** Criar cenário onde há 3 rotas de viagem e apenas 1 motorista de viagem. As rotas excedentes devem ser criadas sem motorista (`agent_id = None`) e com aviso de alerta no log.
2. **Motorista de folga:** Garantir que motoristas sem o dia da semana cadastrado em `DIAS_DISPONIVEIS` sejam ignorados.
3. **Distribuição Equitativa:** Verificar se 4 rotas da Grande SP são distribuídas alternadamente entre 2 motoristas com capacidade equivalente.

---

## 5. Critérios de Aceitação

- [ ] Arquivo `dados/BD_MOTORISTAS.xlsx` ou módulo de leitura cadastral implementado.
- [ ] Sublotes corretamente identificados como `VIAGEM` (entrega fora da Grande SP ou > 70km) ou `Grande SP`.
- [ ] Motoristas sem preferência de Viagem NUNCA são alocados em rotas de Viagem.
- [ ] Alocação realizada sem favorecimento, equilibrando o volume de rotas entre os motoristas disponíveis.
- [ ] Chamada da API `rotas_client.py` envia `agent_id` e `vehicle_id` atribuídos.
- [ ] Relatório de execução via e-mail e logs informam claramente os motoristas alocados para cada rota.
