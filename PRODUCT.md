# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

- **Operador de logística da Fresh Log / Fresh Hub** (Hugo e a equipe de operação, níveis "total" e "operador"), no escritório, à tarde e à noite (a sequência das 22h roda sozinha; o humano revisa antes e depois), num monitor de 1366 a 1920 px, às vezes no celular no fim do dia. Job: fechar o plano de entregas do dia seguinte: transformar o pool de pedidos importados da Stokki em rascunhos de rota, alocar motorista e veículo, e enviar as rotas para a Vuupt (roteirizador) ou para a Lalamove.
- **Leitura** (nível "leitura"): consulta a mesma tela sem editar.
- [inferido do código] A tela também é aberta em dias passados só para consulta (modo somente leitura), o que faz dela a memória visual do que foi planejado.

## Product Purpose

Painel interno (Flask) que orquestra a operação logística da Fresh Log: integra o WMS Stokki (pedidos, NF, expedição), o roteirizador Vuupt (rotas, agentes, checklists), a Lalamove (corridas avulsas), o app dos motoristas e o portal do cliente. A tela de **Planejamento de Rotas** é o cockpit central desse fluxo: mostra o que ainda não tem rota (pool), o que já virou rascunho, quem vai dirigir, e o resultado no mapa. Sucesso = todas as entregas do dia seguinte saem com rota viável, motorista confirmado e documentação pronta antes das 04h, com o mínimo de cliques e sem pedido esquecido.

## Positioning

Único lugar onde Stokki, Vuupt, Lalamove, disponibilidade de motoristas, janelas de horário do cliente e regras próprias de roteirização (níveis 1-4, tipo de veículo, seco/frio, rodízio SP, área atendida) se encontram numa só mesa de trabalho editável por arrastar. Nem a Vuupt nem a Stokki oferecem essa visão combinada.

## Operating Context

- Ritual diário: importação (Stokki) → criar rascunhos (roteirizador próprio) → incrementar com pedidos tardios → alocar motoristas → confirmar e enviar (Vuupt) → documentos/romaneios/impressão (22h e 04h). Sete "agentes" em lote são disparados dessa tela ou por timers na VPS.
- Pool dividido em: pra rotear hoje, agendados pra depois, fora da área de atendimento, a retirar no galpão.
- Rotas têm estados: RASCUNHO, OFERTADA (marketplace de motoristas), ENVIADO (Vuupt), ERRO_ENVIO, Lalamove; e a confirmação do motorista (confirmou / não pode / aguardando).
- Pedidos carregam: código (#PS-n, com sufixo -R2 em reentrega), NF, remetente, destinatário, endereço, caixas, agendamento (dia / atrasado / futuro), janela de horário, nível de dificuldade 1-4, tipo de carga (seco / refrigerado / congelado), fora da área.
- Rotas carregam: nome, cor, motorista, veículo (VAN/HR, VUC, 3/4, Truck), km estimado e real, caixas e paradas com limite, status de envio, oferta, Lalamove.
- Mapa Google Maps com camadas (não alocados, alocados, futuros, enviadas) e seleção por área.
- Drag-and-drop (SortableJS) pool → rota e entre rotas; no celular, select "Adicionar à rota…".
- Vários operadores e várias sessões editam ao mesmo tempo; quase toda ação recarrega a página.

## Capabilities and Constraints

- Stack fixa: Jinja2 + CSS/JS inline num único template (`painel_agentes/templates/planejamento_rotas.html`), shell compartilhado em `base.html`, paleta em `_paleta_cores.html`. Sem build step, sem framework de front, sem biblioteca de ícones; SortableJS e Google Maps JS API são as únicas libs.
- Todos os IDs, endpoints e funções JS existentes precisam continuar funcionando (o modo celular reusa o mesmo DOM e o mesmo JS).
- Tema claro e escuro globais (classe `html.tema-escuro`), alternados pela sidebar; sidebar navy fixa em ambos.
- Telas seguem `app.freshhub.com.br/<serviço>` atrás do Caddy; `url_for()` sempre.
- Idioma: português do Brasil, sem acento em strings de log.
- [inferido] Operação é de baixa latência de leitura: o operador escaneia dezenas de cards por minuto; densidade e escaneabilidade valem mais do que expressão visual.

## Brand Commitments

- Nome do painel: "Painel de Agentes" (marca "Freshlog" / "Fresh Hub").
- Paleta existente: navy `#141428` (sidebar), verde `#0EA575` (acento, claro) / `#2FD9AE` (escuro), vermelho `#E1483D`, âmbar `#E8A33D`; neutros `#F3F4F6` / `#FFFFFF` / `#E5E7EB` / `#1F2937` / `#6B7280`. Tema escuro "Torre": `#0B0E14` / `#121722` / `#262F3F`.
- Fonte do sistema: Segoe UI / -apple-system / Roboto; mono Cascadia Code / Consolas.
- Sidebar agrupada por momento do dia (Acompanhar / Planejar / Operar / Motoristas / Sistema), com badges.

## Evidence on Hand

- Screenshots da tela atual (claro, escuro, data passada, data futura, celular) capturados em 15/09/2026 do painel local com o banco congelado de 14/08/2026.
- Dados reais de exemplo no banco local: rascunhos de 12/08 a 17/08 (até 83 rascunhos num dia), 1.287 paradas.
- Especificações por feature em `DOC_EXECUCAO_CLAUDE_*.md` na raiz.
- Não há pesquisa formal com usuários além do próprio Hugo (pedidos registrados nos comentários do template, datados).

## Product Principles

1. A tela existe pra fechar o dia seguinte: tudo que não ajuda a tirar pedido do pool e mandar rota pra rua é secundário.
2. Estado sempre legível: um card isolado precisa dizer em que estado está sem depender de opacidade ou cor de borda.
3. Densidade com hierarquia: muitos cards na tela, mas o próximo passo óbvio.
4. Mesma tela no desktop e no celular, mesmo DOM, mesmo JS.
5. Nada de dado inventado: contagens, km, caixas e nomes vêm do banco.
