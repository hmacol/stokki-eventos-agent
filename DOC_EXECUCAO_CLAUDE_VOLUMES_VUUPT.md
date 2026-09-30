# Documento de Especificação e Execução: Envio de Volumes Normalizados para o VUUPT

Este documento especifica a implementação necessária para que a quantidade de volumes dos pedidos seja calculada (utilizando o **multiplicador/normalizador de volume** cadastrado no banco de dados) e enviada para a API do VUUPT (`dimension_3`).

---

## 1. Contexto do Problema

Atualmente, o pipeline de importação ([pipeline.py](file:///c:/agente_stokki_eventos/pipeline.py)) obtém os detalhes dos pedidos no Stokki e gera o payload do VUUPT via `montar_payload_vuupt()`. No entanto, o campo de volume do serviço (`dimension_3` no VUUPT) **não está sendo preenchido**, impedindo a realização dos cálculos de ocupação e capacidade por veículo (roteirização e alocação de frota).

Cada embarcador ou tipo de produto possui um **multiplicador de normalização** (ex: volume unitário em m³ por caixa/unidade ou fator de conversão de volume por carro) armazenado no banco de dados (`dados.db` na tabela `interno` ou planilha `BD_CLIENTES.xlsx`).

---

## 2. Requisitos Técnicos e Regras de Negócio

### 2.1. Cálculo do Volume Normalizado

$$\text{Volume Final (VUUPT dimension\_3)} = \text{round}(\text{Quantidade Bruta de Itens/Caixas} \times \text{Multiplicador de Volume do Embarcador})$$

1. **Obtenção da Quantidade Bruta:**
   - Extrair a quantidade total de itens/caixas/pacotes do pedido a partir dos detalhes obtidos do Stokki ([stokki/pedidos.py](file:///c:/agente_stokki_eventos/stokki/pedidos.py)).
   - Se a quantidade não estiver informada ou for 0, utilizar como fallback o valor padrão $1$.

2. **Obtenção do Multiplicador/Normalizador:**
   - Consultar o multiplicador de volume do embarcador na tabela `interno` do SQLite (`dados/dados.db`) através do `stkkc_id` do embarcador.
   - Se o embarcador não possuir multiplicador cadastrado (ou se for `None`/`0`), utilizar o multiplicador padrão $1.0$.

3. **Inclusão no Payload do VUUPT:**
   - No arquivo `pipeline.py`, na função `montar_payload_vuupt()`, atribuir o resultado calculado ao campo `"dimension_3"` do payload.

---

## 3. Modificações Necessárias no Código

### 3.1. [ALTERAR] `pipeline.py` — Busca do Multiplicador do Embarcador

Atualizar a função `_buscar_dados_embarcador_banco(stkkc_id)` para incluir a coluna de multiplicador de volume (ex: `multiplicador_volume` ou `fator_volume`):

```python
def _buscar_dados_embarcador_banco(stkkc_id: int) -> dict:
    padrao = {
        "sender_id": None, "apelido": "", "habilidade": "",
        "cnpj_embarcador": "", "email": "", "notificar_email": True,
        "multiplicador_volume": 1.0,  # Fator normalizador de volume por carro
    }
    if not DB_PATH.exists():
        return padrao
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT sender_id, apelido, nome_remetente, habilidade, "
            "cnpj_embarcador, email, notificar_email, multiplicador_volume "
            "FROM interno WHERE stkkc_id = ?",
            (stkkc_id,)
        ).fetchone()
        conn.close()
        if row:
            mult = row["multiplicador_volume"] if "multiplicador_volume" in row.keys() and row["multiplicador_volume"] else 1.0
            return {
                "sender_id":       row["sender_id"],
                "apelido":         row["nome_remetente"] or row["apelido"] or "",
                "habilidade":      (row["habilidade"] or "").strip(),
                "cnpj_embarcador": row["cnpj_embarcador"] or "",
                "email":           row["email"] or "",
                "notificar_email": bool(row["notificar_email"]) if row["notificar_email"] is not None else True,
                "multiplicador_volume": float(mult),
            }
    except Exception as e:
        logger.debug(f"Erro ao buscar embarcador stkkc_id={stkkc_id}: {e}")
    return padrao
```

---

### 3.2. [ALTERAR] `pipeline.py` — Função `montar_payload_vuupt()`

Adicionar os parâmetros de quantidade bruta e multiplicador de volume para calcular e incluir `"dimension_3"` no payload:

```python
def montar_payload_vuupt(
    id_stokki: int,
    codigo_ps: str,
    referencia: str,
    detalhe: dict,
    endereco_resolvido: dict,
    skill_ids: list[int],
    horario_entrega: dict | None = None,
    sender_id: int | None = None,
    apelido_embarcador: str = "",
    data_saida: str = "",
    indice_xmls: dict | None = None,
    scheduled_start_final: str | None = None,
    scheduled_end_final: str | None = None,
    multiplicador_volume: float = 1.0,
) -> dict:
    ...
    # Extrai quantidade bruta de itens do detalhe do pedido Stokki
    qtd_bruta = detalhe.get("quantidade_total") or detalhe.get("total_itens") or 1
    
    # Calcula o volume normalizado (dimension_3)
    volume_calculado = int(round(float(qtd_bruta) * float(multiplicador_volume)))
    volume_final = max(1, volume_calculado)

    payload = {
        "title": titulo,
        "code":  codigo_ps,
        "type":  "delivery",
        "dimension_3": volume_final,  # <--- Volume normalizado enviado ao VUUPT
        "customer": {
            "name":    nome_dest,
            "code":    cnpj_dest,
            "address": endereco_str,
        },
    }
    ...
    return payload
```

---

### 3.3. [ALTERAR] `stokki/pedidos.py` — Extração de Quantidade Total no Detalhe

Garantir que `_parsear_pagina_detalhe` em [stokki/pedidos.py](file:///c:/agente_stokki_eventos/stokki/pedidos.py) extraia o total de itens/volumes da tabela de produtos do HTML do pedido:

```python
# Na função _parsear_pagina_detalhe(html, id_pedido):
# Extrai o somatório de quantidades da tabela de itens/produtos
qtd_total = 0
for row in soup.find_all("tr", class_="item-row"):
    col_qtd = row.find("td", class_="col-qtd")
    if col_qtd:
        try:
            qtd_total += int(float(col_qtd.get_text(strip=True)))
        except ValueError:
            pass

# Retornar "quantidade_total": qtd_total no dict do detalhe
```

---

## 4. Plano de Validação e Testes

1. **Teste Unitário do Pipeline (Modo Teste):**
   ```bash
   py -3.11 pipeline.py --modo-teste --pedido PS-34345
   ```
   *Verificar se o log exibe o payload gerado contendo o campo `"dimension_3"` com o valor correto pós-multiplicação.*

2. **Validação via Painel de Agentes:**
   - Rodar *"Somente Importação"* em modo de teste pelo Painel Web (`http://localhost:8070`).
   - Conferir se os serviços simulados apresentam a propriedade `dimension_3` preenchida nos logs de auditoria.

---

## 5. Critérios de Aceitação

- [x] A quantidade de volumes é extraída do Stokki ou XML.
- [x] O multiplicador do embarcador cadastrado no banco de dados (`interno.multiplicador_volume`) é aplicado no cálculo.
- [x] O payload enviado à API do VUUPT inclui o campo `"dimension_3"`.
- [x] Execuções em `--modo-teste` validam o cálculo nos logs sem quebrar o pipeline.
