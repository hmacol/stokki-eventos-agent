# ADR-0004: Envio real a clientes e motoristas nasce desligado

## Status
Aceito

## Data
2026-08 (prática desde a fase local; escrita no CLAUDE.md em 2026-09)

## Contexto
O sistema manda e-mail e WhatsApp a embarcadores, destinatários, motoristas, transportadoras e financeiro em dezenas de pontos (resumo das 20h, NFs em rota às 07h, insucesso, entrega concluída, área não atendida, cobrança de XML, dedicados ao financeiro, alerta de chamado). Um erro de regra ou um `--modo-teste` mal implementado manda mensagem real para cliente. Já aconteceu: o portal mandou e-mail REAL ao cliente numa falha de envio de XML.

## Decisão
- Todo canal de envio novo nasce **desligado**: flag `ativo` na seção do config (`notificacoes_automaticas.ativo`, `whatsapp_notificacoes.ativo`) e `forcar_destino: hugo@...`, que desvia tudo para o Hugo.
- **Ligar é decisão do Hugo**, feita no `config.yaml` da VPS, nunca no código.
- Scripts de lote aceitam `--modo-teste` (não escreve na Stokki/Vuupt; e-mail vai para `forcar_destino`). Rodar em modo teste antes de qualquer execução real.
- Botões de resposta em e-mail apontam sempre para `entregas@`, nunca para o remetente técnico.

## Alternativas consideradas

### Ligado por padrão, desligar o que incomodar
- Rejeitada: o custo de um envio errado (cliente recebendo aviso falso) é maior que o de um envio atrasado.

### Ambiente de homologação separado
- Rejeitada por enquanto: um só banco e uma só VPS; o custo de manter dois ambientes não compensou até aqui.

## Consequências
- Há canais prontos no código que nunca foram ao ar ou estão presos em `hugo@` (entrega concluída, notas 07h, calculadora de frete, notificação de transportadoras). Isso é esperado, não bug.
- Quem lê o código precisa conferir o config da VPS para saber o que está ligado de verdade; a memória do projeto e o `MAPA_DO_SISTEMA.txt` seção 17 listam o estado.
- `--modo-teste` precisa ser testado também: já houve modo teste que mandava e-mail real.
