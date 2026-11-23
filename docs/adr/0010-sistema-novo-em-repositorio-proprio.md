# ADR-0010: O sistema novo (freshhub) vive em repositório próprio

## Status
Aceito

## Data
2026-10-17

## Contexto
Em 17/10/2026 o Hugo aprovou o plano mestre de um sistema 100% próprio, sem Stokki, Vuupt e Lovable. A primeira versão da spec previa o pacote novo dentro deste repositório. A execução da Fase 0.1 aqui mostrou que as guardas deste repo (hook `pre_comandos`, regras `deny`, ambiente Python global, lint e CI precisando de filtros pra ignorar o legado) travavam o pacote novo a cada passo. O Hugo pediu a separação e escolheu repositório próprio.

## Decisão
- O sistema novo vive em `C:\freshhub` (repo `freshhub`), com spec, planos, ADRs, CI e deploy próprios. **Nada do sistema novo entra neste repositório.**
- Aqui fica só o necessário pra convivência:
  - a pasta do Lovable renomeada de `freshhub/` pra `lovable/` (pra o nome `freshhub` ser o pacote instalado por `pip install -e C:\freshhub`, sem sombra do diretório local);
  - este ADR, como ponteiro.
- O ADR-0005 (SQLite único) continua valendo aqui; o sistema novo tem o banco dele (ADR-0002 e 0003 lá).
- Este repositório vai sendo esvaziado fase a fase, até ser desligado na Fase 4 do plano mestre.

## Alternativas consideradas
- **Mesmo repo (versão da manhã da spec)**: revertida pelo atrito medido.
- **Mesmo repo sob uma pasta**: não resolve hooks, deny e ambiente compartilhados.

## Consequências
- O MAPA deste repo aponta pro repo novo em vez de descrever o pacote.
- Deploy: ao subir a renomeação, mover na VPS `freshhub/sessao_freshhub.json` pra `lovable/sessao_freshhub.json` como `www-data` antes de reiniciar o `painel-agentes` (MAPA, seção 17).
