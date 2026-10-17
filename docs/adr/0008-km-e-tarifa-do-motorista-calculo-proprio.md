# ADR-0008: Km e valor do motorista: cálculo próprio é a base, o da Vuupt é ignorado

## Status
Aceito

## Data
2026-09-15

## Contexto
A Vuupt registra `done_trip_distance` (km pelo GPS do app dela) e um custo por rota configurado lá. Medido em 12-16/09: o km real da Vuupt é ruidoso (31 de 537 rotas concluídas com 0 km), não pode ser enviado pela API, e o custo configurado é R$ 25,00 fixo por rota (`done_cost_per_agent_per_route`), que nunca refletiu a tarifa real. O financeiro só conta rotas no relatório da Vuupt e faz a conta fora.

Decisão do Hugo em 15/09/2026 (`DOC_EXECUCAO_CLAUDE_SAIDA_VUUPT.md`, seção 1).

## Decisão
- **Km cobrado** vem de `regras/km_cobrado.py` (qual km entra na franquia) e **remuneração** de `regras/tarifa_motorista.py` (por rota/veículo). Esses dois são a base.
- `done_trip_distance` e o custo configurado na Vuupt **não entram em conta nenhuma**: nem extrato do motorista, nem relatório, nem KPI.
- O relatório fiel do financeiro (`nucleo/relatorios_financeiro.py`) reproduz as colunas da Vuupt para conferência de período, mas a cobrança usa o cálculo próprio.

## Alternativas consideradas

### Usar o km da Vuupt como fonte
- Rejeitada: ruidoso, zero em rotas concluídas, e desaparece quando a Vuupt sair.

### Configurar a tarifa real na Vuupt
- Rejeitada: a Vuupt só aceita um custo fixo por rota por agente; a tarifa real varia por veículo e km.

## Consequências
- `regras/km_cobrado.py` e `regras/tarifa_motorista.py` são puros e testados; mudança de tarifa é mudança neles, com teste.
- Quando o app próprio produzir GPS, o km real entra pelo núcleo (`nucleo/recalcular_km_rotas.py`), não pela Vuupt.
- Relatórios do financeiro (quais, para quê, periodicidade) ficaram para etapa posterior por decisão do Hugo.
