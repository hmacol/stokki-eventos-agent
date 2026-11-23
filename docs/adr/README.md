# Registros de decisão (ADR)

Cada arquivo registra UMA decisão que seria cara de reverter e que não se deduz do código: o contexto, o que foi decidido, o que foi descartado e as consequências.

Regras:

- Decisão nova ou revertida = ADR novo. Nunca apagar nem reescrever um ADR antigo: ele vira `Substituído por ADR-XXXX`.
- Numeração sequencial `NNNN-titulo-curto.md`. Seções: Status, Data, Contexto, Decisão, Alternativas consideradas, Consequências.
- A data é a da decisão (quem decidiu está no Contexto), não a do arquivo.
- Decisão de negócio ambígua (horário de corte, quem recebe e-mail, regra de roteirização) é do Hugo. Perguntar antes de registrar como Aceito.
- Antes de propor refatoração ou mudança de arquitetura, ler os ADRs da área: eles existem para a mesma discussão não ser reaberta.

| ADR | Título | Status |
|---|---|---|
| [0001](0001-saida-da-vuupt-por-etapas-com-replica.md) | Sair da Vuupt por etapas reversíveis, replicando tudo nela até a virada | Aceito |
| [0002](0002-stokki-sem-api-sessao-por-conta-com-fila.md) | Stokki sem API pública: Playwright + endpoints internos, uma sessão por conta com fila | Aceito |
| [0003](0003-rascunho-de-rota-exige-confirmacao-humana.md) | Rascunho de rota só vira rota com confirmação humana | Aceito |
| [0004](0004-envio-real-desligado-por-padrao.md) | Envio real a clientes e motoristas nasce desligado | Aceito |
| [0005](0005-sqlite-unico-sem-orm-tabelas-por-modulo.md) | Um SQLite (`dados/dados.db`), sem ORM, cada módulo dono das próprias tabelas | Aceito |
| [0006](0006-telas-web-sob-um-dominio-por-prefixo.md) | Telas web sob `app.freshhub.com.br/<serviço>` via Caddy + ProxyFix + `url_for` | Aceito |
| [0007](0007-agendamento-informado-vence-dia-fixo.md) | Agendamento informado vence o dia fixo da região | Aceito |
| [0008](0008-km-e-tarifa-do-motorista-calculo-proprio.md) | Km e valor do motorista: cálculo próprio é a base, o da Vuupt é ignorado | Aceito |
| [0009](0009-roteirizador-proprio-vuupt-so-executa.md) | Roteirizador próprio; a Vuupt só executa a rota | Aceito |
| [0010](0010-sistema-novo-em-repositorio-proprio.md) | O sistema novo (freshhub) vive em repositório próprio (`C:\freshhub`); aqui fica só a convivência | Aceito |
