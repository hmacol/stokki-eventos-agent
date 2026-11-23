# ADR-0009: Roteirizador próprio; a Vuupt só executa a rota

## Status
Aceito

## Data
2026-08 (roteirizador próprio); calibração em 2026-09-20 e 2026-09-25

## Contexto
A Vuupt oferece `/route-optimization`, mas é cobrado por chamada e devolve só a sequência, sem respeitar as regras da Fresh Log: tipo de carga, nível de complexidade do destinatário (1-4), porte do veículo, rodízio de SP, dia fixo por região, dedicados, janelas de horário e orçamento de horas por motorista. O agrupamento por CEP antigo (`roteirizacao/roteirizar.py`, legado) não dava rotas boas.

Regras de negócio dadas pelo Hugo, que o código não deixa óbvias (20/09 e 25/09/2026):
- seco e refrigerado sempre juntos (toda a frota tem baú térmico);
- objetivo compacto: o vencedor diário é o menor km; sequência livre (regra "mais longe primeiro" de 03/08 revogada);
- `DISTANCIA_MAXIMA_ROTA_KM = 15` (calibrado pelo replay);
- nível 4 agrupa só mesmo endereço + mesmo embarcador; nunca por CEP ou coordenada;
- porte exato: quem não é Fiorino só pega rota do porte exato do carro (acabou "maior cobre menor");
- início das rotas às 06:00 BRT (`roteirizacao_dados.HORA_INICIO_ROTA` é a fonte única).

## Decisão
- A roteirização é **nossa** (`roteirizacao/`): pool → dia fixo / dedicados / área não atendida → partição por carga → agrupamento → seleção de modelo (Sweep Polar, Clarke-Wright, 2-Opt, CEP, K-means; `selecao_modelo.py` escolhe o menor km) → polimento → alocação de motorista → rascunho.
- A Vuupt recebe a rota pronta (`rotas_client.criar_rota`) e só executa no app dela. `otimizacao_client.py` (`/route-optimization`) existe só para comparação, nunca no fluxo automático.
- As regras acima são constantes e funções em `roteirizacao_dados.py`, `alocacao_motoristas.py`, `regras/tipo_veiculo.py`; mudar exige decisão do Hugo e ADR novo.

## Alternativas consideradas

### Otimizador da Vuupt no fluxo diário
- Rejeitada: cobrado, sem as regras de negócio, e desaparece com a saída da Vuupt (ADR-0001).

### Agrupamento por CEP (legado)
- Rejeitada: rotas ruins; mantido como um dos 5 modelos só para comparação.

### Entrelaçamento de rotas / "maior cobre menor" no porte
- Entrelaçamento adiado para fase seguinte. "Maior cobre menor" revogado em 25/09 para reservar VANs à carga grande.

## Consequências
- `roteirizacao/replay_rotas.py` e `benchmark_modelos.py` são o jeito de calibrar: toda mudança de constante passa por replay de dias passados.
- 3/4 e Truck sem motorista cadastrado saem com `[ALERTA_ALOCACAO]` em vez de cair em veículo menor.
- Travas na edição manual do planejamento (paradas, caixas, distância, nível 3-4) só avisam, nunca bloqueiam: o operador vence o algoritmo.
