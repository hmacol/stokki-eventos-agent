# Review de segurança: feriados em portal_cliente/chamados.py

Data: 07/10/2026. Commit e822848 (ramo feriados → master). Base em produção: 868be85.

## Escopo

`git diff 868be85 e822848 -- portal_cliente/` toca só `portal_cliente/chamados.py`,
função `situacao_horario` (horário de atendimento dos chamados):

- importa `regras.feriados` (módulo puro, sem I/O, sem rede, sem banco);
- `proximo_dia_util` interno passa a pular feriado (laço de 8 para 10 iterações,
  teto fixo, sem entrada do usuário);
- "fora do horário" também quando o dia é feriado.

## Análise

- Entrada externa: nenhuma. `agora` vem do relógio do servidor; `config` do config.yaml.
- Superfície HTTP: inalterada (nenhuma rota nova, nenhum parâmetro novo).
- Falha possível: `feriados.eh_feriado` só faz aritmética de datas; não levanta
  para datas válidas. O laço tem teto de 10 iterações.
- Blast radius: `situacao_horario` é chamada pelo portal e pela API do motorista
  para mostrar "volta em ..."; mudança de valor, não de forma.
- Cobertura: `regras/test_feriados.py` (11 testes) cobre o módulo; não há teste
  específico de `situacao_horario` em feriado (comportamento trivial).

## Achados

Nenhum ALTO ou MÉDIO. Nenhum BAIXO.
