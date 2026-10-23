# ADR-0002: Stokki sem API pública: Playwright + endpoints internos, uma sessão por conta com fila

## Status
Aceito

## Data
2026-10-05 (fila por conta; a integração por Playwright vem da origem do projeto)

## Contexto
A Stokki (WMS dos embarcadores) não tem API pública. O sistema loga com Playwright, guarda os cookies e depois chama os endpoints internos da tela com `requests`. Dois fatos medidos mandam na arquitetura:

- **Login novo derruba a sessão anterior da mesma conta** (401 na outra ponta), inclusive a do segundo projeto `agente_importacao_stokki`, que compartilha a conta.
- Chamada manual ao DataTables da Stokki dá 500; URL chutada gera login em cascata.

Até 05/10 havia uma trava única para todas as contas: expedição (conta provider `wms@`) e importação/WMS (conta admin `hugo@`) esperavam uma pela outra sem motivo, e rotinas logavam por cima depois de 15 min. Testado ao vivo em 05/10: a conta provider não acessa `/administrator` (302), então não serve de reserva da conta admin.

Decisões do Hugo em 05/10/2026.

## Decisão
1. Todo acesso à Stokki passa por `stokki/` (`StokkiSession`, `contar_pedidos`, `listar_transportadoras`, parsers testados com HTML salvo). Ninguém fora da pasta monta URL ou mexe em Playwright.
2. **Trava por conta**, com **fila por ordem de chegada** (`stokki/sessao_uso.py`, tabela `stokki_fila` com heartbeat de 60 s):
   - rotinas recorrentes (WMS, expedição, importação horária, documentos incrementais) esperam sem limite;
   - rotinas importantes (pipeline das sequências e do painel, portal, importador do 2º projeto) esperam até 2 h e desistem com erro, sem logar por cima.
3. Terceira conta admin **opcional** como reserva (`stokki.reserva` no config): fallback só principal → reserva; a provider não tem alternativa.

## Alternativas consideradas

### Uma sessão por processo, cada script loga quando precisa
- Rejeitada: era o comportamento original e derrubava a sessão alheia (401) várias vezes por dia.

### Conta provider como reserva da admin
- Rejeitada: testado em 05/10, a provider não entra na área admin.

### Pedir API à Stokki
- Fora do nosso controle; fica como desejo, não como plano.

## Consequências
- Pipeline horário desiste se a Stokki estiver em uso (`stokki/sessao_uso.py`), por isso pode pular uma rodada.
- Deploy que toca `stokki/` vale para dois repos: o importador é copiado por `scp`.
- Painel em execução deixou de ocupar conta; só ocupa durante a chamada.
- A reserva só liga quando o Hugo criar o terceiro usuário admin e testá-lo em `/pt-br/administrator`.
