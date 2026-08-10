# Documento de Especificação e Execução: Limite de Volumes e Entregas na Roteirização

Este documento especifica as alterações necessárias no módulo de roteirização ([roteirizacao/](file:///c:/agente_stokki_eventos/roteirizacao)) para que a criação e o incremento de rotas considerem a **quantidade de caixas/volumes (máximo 100 por rota)** e o **novo limite máximo de 18 entregas por rota**.

---

## 1. Contexto e Objetivos

* **Novo Limite de Entregas por Rota:** Aumentar `TAMANHO_MAXIMO_ROTA` de **15 para 18 entregas**.
* **Novo Limite de Volume/Caixas por Rota:** Definir `VOLUME_MAXIMO_ROTA = 100` caixas.
* **Objetivo de Otimização:** O algoritmo de subdivisão em sublotes deve agrupar os pedidos geograficamente buscando preencher as rotas o mais próximo possível do limite de 100 caixas ou 18 entregas (o que atingir primeiro), sem ultrapassar nenhum dos dois limites.

---

## 2. Regras de Negócio e Algoritmo

### 2.1. Extração de Volume/Caixas de um Serviço

A quantidade de caixas de um pedido no VUUPT é obtida a partir do campo `"dimension_3"` do serviço (com fallback seguro para 1 se estiver ausente/nulo):

```python
def extrair_volume_caixas(servico: dict) -> int:
    vol = servico.get("dimension_3")
    if vol is not None:
        try:
            return max(1, int(vol))
        except (ValueError, TypeError):
            pass
    return 1
```

### 2.2. Algoritmo de Subdivisão em Sublotes (`dividir_em_sublotes`)

No arquivo [roteirizacao/roteirizacao_dados.py](file:///c:/agente_stokki_eventos/roteirizacao/roteirizacao_dados.py), a divisão de uma região geográfica em sublotes/rotas deve seguir as duas travas:

1. **Prioridade Geográfica:** Ordenar os pedidos da região por proximidade espacial (coordenadas/CEP).
2. **Empacotamento Ganancioso (Greedy Bin Packing):**
   - Adicionar pedidos ao sublote atual enquanto:
     - `quantidade_de_entregas + 1 <= 18`
     - `soma_de_caixas_do_sublote + caixas_do_pedido <= 100`
   - Se o próximo pedido estourar 18 entregas ou 100 caixas, fechar o sublote atual e abrir um novo.
   - **Exceção de Pedido Gigante:** Se um único pedido possuir > 100 caixas, ele aloca uma rota exclusiva isolada.

---

## 3. Especificação dos Arquivos a Alterar

### 3.1. [ALTERAR] `roteirizacao/roteirizacao_dados.py`

1. Adicionar a função `extrair_volume_caixas(servico)`.
2. Atualizar os parâmetros e lógica de `dividir_em_sublotes`:

```python
def dividir_em_sublotes(
    servicos: list[dict],
    tamanho_minimo: int = 10,
    tamanho_maximo: int = 18,
    volume_maximo: int = 100,
    api_key: str | None = None
) -> list[list[dict]]:
    """
    Divide uma região em sublotes respeitando:
    - Máximo de 18 entregas por rota
    - Máximo de 100 caixas/volumes por rota (visando atingir o limite)
    - Proximidade geográfica dos pontos
    """
    def _chave_ordenacao(servico: dict):
        coords = obter_coordenadas(servico, api_key)
        if coords:
            return (0, coords[0], coords[1])
        cep = extrair_cep(servico)
        return (1, int(cep) if cep else float("inf"), 0.0)

    ordenados = sorted(servicos, key=_chave_ordenacao)
    
    sublotes = []
    sublote_atual = []
    caixas_atual = 0

    for servico in ordenados:
        cx_pedido = extrair_volume_caixas(servico)
        
        # Verifica se o pedido cabe no sublote atual
        cabe_entregas = len(sublote_atual) + 1 <= tamanho_maximo
        cabe_caixas = (caixas_atual + cx_pedido <= volume_maximo) or not sublote_atual

        if cabe_entregas and cabe_caixas:
            sublote_atual.append(servico)
            caixas_atual += cx_pedido
        else:
            sublotes.append(sublote_atual)
            sublote_atual = [servico]
            caixas_atual = cx_pedido

    if sublote_atual:
        sublotes.append(sublote_atual)

    return sublotes
```

---

### 3.2. [ALTERAR] `roteirizacao/criar_rotas_diarias.py`

Atualizar as constantes centrais:

```python
TAMANHO_MINIMO_ROTA = 10
TAMANHO_MAXIMO_ROTA = 18  # Aumentado de 15 para 18 entregas por rota
VOLUME_MAXIMO_ROTA  = 100 # Novo limite máximo de caixas por rota
```

E passar `volume_maximo=VOLUME_MAXIMO_ROTA` e `tamanho_maximo=TAMANHO_MAXIMO_ROTA` na chamada de `dividir_em_sublotes`.

---

### 3.3. [ALTERAR] `roteirizacao/incrementar_rotas.py`

1. Atualizar constantes para `TAMANHO_MAXIMO_ROTA = 18` e `VOLUME_MAXIMO_ROTA = 100`.
2. Ao selecionar uma rota existente para receber um novo pedido, verificar se:
   - `qtd_entregas_atuais < 18`
   - `qtd_caixas_atuais + caixas_novo_pedido <= 100`

---

### 3.4. [ALTERAR] `roteirizacao/roteirizar.py`

Atualizar as constantes:
```python
TAMANHO_MINIMO_ROTA = 10
TAMANHO_MAXIMO_ROTA = 18
VOLUME_MAXIMO_ROTA  = 100
```

---

## 4. Plano de Validação e Testes

1. **Teste em Modo Teste (Criação de Rotas):**
   ```bash
   py -3.11 roteirizacao/criar_rotas_diarias.py --modo-teste
   ```
   *Validar nos logs se nenhuma rota criada ultrapassa 18 entregas ou 100 caixas, e se o agrupamento tenta se aproximar do limite de 100 caixas.*

2. **Teste em Modo Teste (Incremento de Rotas):**
   ```bash
   py -3.11 roteirizacao/incrementar_rotas.py --modo-teste
   ```
   *Validar se o encaixe de novos pedidos respeita a capacidade remanescente de caixas até 100.*

---

## 5. Critérios de Aceitação

- [x] Limite de entregas alterado para **18 por rota**.
- [x] Limite máximo de **100 caixas/volumes por rota** implementado.
- [x] As rotas geradas buscam preencher o volume máximo (próximo de 100 caixas) sem estourar as travas.
- [x] Tanto a criação diária quanto o incremento de rotas respeitam as duas regras simultaneamente.
