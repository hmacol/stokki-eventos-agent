# Página inicial do painel (`/inicio`)

Data: 29/09/2026
Pedido do Hugo: criar uma página de início do painel, com conteúdo conforme
o tipo de usuário. Formato escolhido: pendências + resumo do dia.

## Objetivo

Quem entra no painel vê, numa tela só e sem esperar, o que está aguardando
ação dele e como está a operação de hoje. Hoje o login joga cada nível
direto numa tela de trabalho (Torre, Atendimento...) e o `/` é a tela de
Agentes, restrita ao nível total.

## Decisões já tomadas (Hugo, 29/09)

- Formato: pendências no topo, resumo do dia abaixo, rotinas só pro total.
- Níveis `expedicao` e `galpao` ficam de fora: cada um só tem uma tela, e o
  galpão é aparelho compartilhado. Login deles não muda.
- O bloco de rotinas mostra só as execuções disparadas pelo painel
  (`painel_execucoes`). Timers systemd ficam de fora desta versão.

## Fora do escopo

- Saúde dos timers systemd.
- Gráficos, tendência, comparação com períodos anteriores (isso é a Torre).
- Personalização por usuário (os logins são um par fixo por nível).
- Versão mobile separada: a tela usa `base.html`, que já vira gaveta no
  celular, e a grade de cartões é responsiva.
- Mudar o `/` (continua sendo Agentes, endpoint `index`).

## Rota e entrada

| Item | Valor |
|---|---|
| Tela | `GET /inicio`, endpoint `inicio` |
| Dados | `GET /api/inicio/dados` |
| Níveis (as duas rotas) | `total`, `operador`, `leitura`, `atendimento` |
| Menu lateral | item "Início" sozinho no topo, antes do grupo Acompanhar |

Login (`painel_agentes.py`, função `login`): a página padrão passa a ser
`url_for("inicio")` para total, operador, leitura e atendimento. Expedição
continua caindo em `/expedicao` e galpão em `/wms`. O parâmetro `proximo`
continua vencendo a página padrão, como hoje.

## Blocos da tela

### 1. Esperando você

Um cartão por fila que o nível pode abrir. Cada cartão mostra o número, o
nome da fila, uma linha de apoio e leva à tela no clique.

| Cartão | Rota | Linha de apoio | Níveis |
|---|---|---|---|
| Torre de Controle | `torre` | "N críticas" quando houver | total, operador, leitura, atendimento |
| Pedidos Parados | `pedidos_parados` | "N urgentes" quando houver | total, operador, leitura, atendimento |
| Atendimento | `atendimento` | chamados na fila | total, operador, atendimento |
| Pedágios | `pedagios` | aguardando aprovação | total, operador, leitura |
| Canhotos | `canhotos` | reprovados na conferência | total, operador, leitura |

Os níveis são os de `NIVEIS_POR_CONTADOR` em `contadores_menu.py`; a tela
não repete a regra, só desenha o que o endpoint devolver.

Estados de cada cartão:
- com pendência: número em destaque; borda/ponto vermelho se `criticas > 0`;
- zerado: "Em dia", visual discreto;
- sem leitura ainda (contador caro que não voltou): "Carregando", sem número.

Ordem: cartões com críticas primeiro, depois com pendência, depois zerados.
Para o nível atendimento, Atendimento vem primeiro em caso de empate.

### 2. Hoje na operação

Números do dia, vindos da última coleta da Torre:

- Rotas: concluídas, em andamento, não iniciadas, atrasadas.
- Pedidos: total do dia, entregues, insucessos, sem rota (atrasados).
- Rodapé do bloco: "Atualizado às HH:MM".

Sem leitura do dia: o bloco mostra "Os números de hoje ainda estão sendo
coletados" no lugar dos valores. Nunca mostra número de outro dia.

### 3. Rotinas (só nível total)

- Agentes rodando agora (nome + desde quando).
- Últimas execuções com status diferente de SUCESSO nas últimas 24 h
  (ERRO, TIMEOUT, INTERROMPIDO), no máximo 5, com link pro log
  (`url_for('execucao', execucao_id=...)`).
- Sem nada a mostrar: "Nenhuma falha nas últimas 24 horas".
- Link "Ver agentes" pro `/`.

Para os outros níveis o bloco não é desenhado e a chave `rotinas` não vem
no JSON.

## Dados

### Regra que manda

A mesma dos badges do menu: a página nunca espera fonte cara. O HTML sobe
sem números; o JS busca `/api/inicio/dados` depois do load e repete a cada
2 minutos, só com a aba visível.

### Formato de `/api/inicio/dados`

```json
{
  "pendencias": [
    {"chave": "torre", "rotulo": "Torre de Controle", "url": "/painel/torre",
     "qtd": 3, "criticas": 1}
  ],
  "operacao": {
    "gerado_em": "14:32",
    "rotas": {"concluidas": 4, "em_andamento": 6, "nao_iniciadas": 1, "atrasadas": 2},
    "pedidos": {"total": 118, "entregues": 71, "insucessos": 3, "sem_rota": 5}
  },
  "rotinas": {
    "rodando": [{"nome": "...", "iniciado_em": "..."}],
    "falhas": [{"nome": "...", "status": "ERRO", "quando": "...", "url": "..."}]
  }
}
```

- `pendencias`: cartão sem leitura vem com `qtd: null`.
- `operacao`: `null` quando não há leitura de hoje.
- `rotinas`: ausente fora do nível total.
- `url` sempre por `url_for()` (o painel roda atrás de `/painel`).

### Fontes

| Bloco | Fonte | Custo |
|---|---|---|
| Pendências | `contadores_menu.contadores(nivel)`, sem alteração | o de hoje |
| Operação | snapshot novo publicado por `torre_controle.buscar_dados_torre` | zero chamadas novas |
| Rotinas | `executor.listar_execucoes_recentes` + `AGENTES` | SQLite local |

### Snapshot do resumo do dia (`torre_controle.py`)

Novo dicionário `_snapshot_resumo_dia`, gravado no mesmo ponto e sob o mesmo
`_lock_caches` em que `buscar_dados_torre` já grava `_snapshot_fila_acao`.
Guarda `data_iso`, `gerado_em`, `rotas_resumo` e os quatro números de
pedidos (`pedidos["total"]`, `["sucesso"]`, `["falha"]`,
`["qtd_nao_atribuidos"]`).

Nova função `snapshot_resumo_dia(data_alvo) -> dict | None`, irmã de
`snapshot_fila_acao`: não dispara coleta, devolve `None` se não há leitura
ou se ela é de outro dia.

Quem mantém o snapshot fresco: a própria chamada a
`contadores_menu.contadores`, que a página inicial já faz e que renova a
coleta da Torre em segundo plano quando o valor passa de 5 minutos. A
página inicial não cria thread nem cache próprios.

## Arquivos

Novos:
- `painel_agentes/pagina_inicial.py`: `montar_dados(nivel_acesso) -> dict`.
  Junta as três fontes e aplica o filtro por nível. Sem Flask dentro, pra
  ser testável sozinho; as URLs entram por um parâmetro `url_de(rota)`.
- `painel_agentes/templates/inicio.html`: estende `base.html`, usa as
  variáveis de cor já existentes (`--superficie`, `--borda`,
  `--texto-suave`, `--acento`).
- `painel_agentes/test_pagina_inicial.py`.

Alterados:
- `painel_agentes/painel_agentes.py`: rotas `inicio` e `api_inicio_dados`;
  página padrão do login.
- `painel_agentes/torre_controle.py`: `_snapshot_resumo_dia` e
  `snapshot_resumo_dia`.
- `painel_agentes/templates/_menu_lateral_nav.html`: item "Início".
- `MAPA_DO_SISTEMA.txt`: tela nova e módulo novo.

## Falhas

- Fonte de pendência que falha: aquele cartão vem sem número (já é o
  comportamento de `contadores`), os outros seguem.
- Falha ao ler `painel_execucoes`: `rotinas` vem com listas vazias e o erro
  vai pro log; a página não quebra.
- Falha no fetch do JS: a tela mantém os últimos números e tenta de novo no
  ciclo seguinte. Sessão expirada (401) redireciona pro login.

## Limites conhecidos

- Logo depois de reiniciar o painel, o bloco "Hoje na operação" e os cartões
  de Torre e Pedidos Parados ficam sem número até a primeira coleta
  terminar (cerca de 1 minuto).
- Os números do dia podem ter até 5 minutos de idade; a hora da leitura fica
  visível na tela.
- Falha de timer systemd não aparece no bloco Rotinas.

## Testes

Unitários (`py -3.11 -m unittest painel_agentes.test_pagina_inicial`), com
as fontes substituídas por dublês:

1. Cada nível recebe só os cartões que pode abrir (total, operador, leitura,
   atendimento).
2. `rotinas` só existe para o nível total.
3. Snapshot ausente ou de outro dia resulta em `operacao: null`.
4. Contador sem leitura vira cartão com `qtd: null`.
5. Ordenação: críticas, depois pendências, depois zerados.
6. Rotinas: só status diferente de SUCESSO, só últimas 24 h, no máximo 5.
7. `snapshot_resumo_dia` devolve `None` para data diferente da gravada.

Rotas (test_client do Flask):

8. `/inicio` e `/api/inicio/dados` respondem 200 para os quatro níveis e 403
   para expedicao e galpao.
9. Login de cada nível redireciona para a página padrão certa.

Prova manual: painel local na porta 8099, login em cada nível, conferir os
cartões e o menu. Bloquear `/api/torre` no teste com Playwright (armadilha
já conhecida).

## Critérios de aceite

- Login de total, operador, leitura e atendimento cai em `/inicio`.
- A página abre sem esperar Vuupt, Stokki ou Fresh Hub.
- Nenhum nível vê cartão ou link de tela que devolveria 403.
- Os números de pendência batem com os badges do menu lateral.
- Os números do dia batem com a Torre aberta no mesmo momento.
