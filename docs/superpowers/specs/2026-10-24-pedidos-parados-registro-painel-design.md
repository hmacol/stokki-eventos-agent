# Pedidos parados: registro direto no painel (sai o Lovable)

Data: 24/10/2026. Pedido do Hugo: "vamos passar ao lançamento direto na nossa
tela. Quero uma tela similar a que temos hoje no lovable e aí encerramos a
consulta ao lovable." Mockups aprovados no artifact
https://claude.ai/artifact/6VrGWWvpXbCaCpEKtiZ1K9 com quatro respostas:
(1) sem importar histórico do Lovable; (2) encaminhamento às Demandas
PAUSADO; (3) níveis de registro aprovados; (4) duplicata no dia permitida
com aviso e apagar em 15 min.

## Intenção

Hoje o galpão registra pedido parado em freshhub.com.br/pedidos-parados
(Lovable/Supabase, tabela `stalled_orders`) e o painel interno lê de lá via
`lovable/pedidos_parados.py` pra montar a triagem em `/pedidos-parados`.
Passa a existir uma aba **Registrar** na própria tela do painel, gravando
numa tabela nossa; a aba **Triagem** (tela atual) passa a ler dessa tabela.
O painel deixa de consultar o Lovable para pedidos parados.

## Objetivos

- Registrar pelo celular do galpão com o mesmo conteúdo do Lovable: pedido,
  volumes, foto opcional; quem registrou vem do login do painel.
- Triagem continua idêntica (classificações, Consultar Stokki/Vuupt,
  prioridade 2 dias, "Nx registrado", revisão urgente, Duplicar, Notificar
  embarcador), só trocando a fonte das linhas.
- Lista "Registrados hoje" e "Ontem e ainda não registrados hoje" com botão
  **Ainda parado** (re-registra com os mesmos volumes, sem foto). O
  re-registro diário é o que alimenta "dias parado"; a regra fica.

## Não objetivos / anti-objetivos

- Não importar `stalled_orders` (decisão 1). "Dias parado" começa a contar do
  primeiro registro na tabela nova.
- Não mexer nas regras de classificação nem nas consultas Stokki/Vuupt.
- Não remover `lovable/` nem `lovable/tasks.py`: o encaminhamento às Demandas
  fica PAUSADO por flag, não apagado (decisão 2). Religar é trocar a flag.
- Botão "Colunas" do Lovable não entra. "Exportar" vira CSV simples no
  navegador, a partir das linhas filtradas.
- Nada de e-mail/WhatsApp novo.

## Decisões de implementação

**Tabela** `pedidos_parados_registros` em `dados/dados.db`, criada em
`_conectar()` de `painel_agentes/pedidos_parados_triagem.py` ao lado das
outras `pedidos_parados_*`:

```
id                 INTEGER PRIMARY KEY AUTOINCREMENT
order_number       TEXT NOT NULL   -- normalizado (normalizar_order_number)
order_number_bruto TEXT            -- como foi digitado
volumes            INTEGER NOT NULL
foto               TEXT            -- nome do arquivo em dados/pedidos_parados/<id>/, ou NULL
registrado_por     TEXT NOT NULL   -- session["usuario"]
criado_em          TEXT NOT NULL   -- hora LOCAL "YYYY-MM-DD HH:MM:SS"
índice (order_number, criado_em)
```

**Fonte da triagem**: `_montar_base_externa()` deixa de chamar
`listar_pedidos_parados` e lê `pedidos_parados_registros` inteira
(agrupa por `order_number`: primeira vez, última vez, contagem). O resto
(filtro de entregues na Vuupt, `_resolver_ids_stokki`, cache de 60 s com
single-flight) fica igual. Registrar/apagar zera `_cache_base["quando"]`
pra linha nova aparecer na hora. `created_at` devolvido à tela passa a ser
hora local sem fuso ("YYYY-MM-DDTHH:MM:SS"); `_data_local` e o cálculo de
`revisao_urgente` tratam data sem fuso como local (antes era UTC do
Supabase). O campo `freshhub_id` da API/tabela de classificação continua
existindo e recebe `str(registro.id)` (evita migração e mudança nas
funções `verificar_na_*`).

**Foto**: `dados/pedidos_parados/<registro_id>/foto.<ext>`; validação
reaproveita `retornos._extensao_imagem` e `retornos.MAX_FOTO_BYTES` (jpg/png/
webp, 10 MB). Servida por `GET /pedidos-parados/foto/<id>` com a mesma
proteção de caminho de `retornos.caminho_arquivo`. Sem GCS (foto de
pedido parado é apoio da triagem, não documento).

**Rotas** (em `painel_agentes.py`, padrão CSRF `@exige_mesma_origem`):
- `GET /pedidos-parados`: níveis atuais + `galpao`, `expedicao`. Template
  recebe `pode_registrar`, `pode_editar`, `aba_inicial`
  (galpao/expedicao abrem em Registrar; demais em Triagem; `#registrar` /
  `#triagem` na URL vencem), `encaminhamento_ativo`.
- `GET /api/pedidos-parados/dados`: idem + `galpao`, `expedicao`.
- `GET /api/pedidos-parados/registros-hoje` → `{hoje: [...], ontem_sem_hoje: [...]}`;
  cada item: id, order_number, volumes, foto (bool), registrado_por,
  criado_em, classificacao, acao_status, dias_parado, pode_apagar.
- `POST /api/pedidos-parados/registrar` (multipart: `order_number`,
  `volumes`, `foto` opcional) → `{id, order_number, ja_hoje: n}`. Níveis:
  total, operador, logistica, galpao, expedicao. Erros 400: pedido vazio,
  volumes < 1, foto inválida/grande.
- `POST /api/pedidos-parados/registro/apagar` `{id}` → 200 só se
  `registrado_por == usuário logado` e `criado_em` há ≤ 15 min; apaga a
  linha e a pasta da foto. Senão 403 com mensagem.
- "Ainda parado" usa `/registrar` com `order_number` + `volumes` do
  registro de ontem (sem foto).
- `POST /api/pedidos-parados/encaminhar`: com a flag desligada responde
  409 `{"erro": "Encaminhamento às Demandas pausado (Hugo, 24/10)."}`.

**Pausa do encaminhamento**: constante
`ENCAMINHAMENTO_DEMANDAS_ATIVO = False` em `pedidos_parados_triagem.py`,
com comentário da decisão. `encaminhar_operacao` levanta `ValueError`
quando False. Na tela, enquanto pausado: sem botão Encaminhar; a linha
classificada como Cancelados/Devolução Parcial/Descartar/Verificar com
Cliente mostra "✓ classificado · encaminhamento pausado" e **conta como
tratada** no filtro "só não tratados" (senão ficaria pendente pra
sempre). Minha escolha; religar devolve o comportamento anterior.

**Template** `pedidos_parados_triagem.html`: abas `.seg` (mesmo
componente de `retornos.html`) "Registrar | Triagem". Registrar é
mobile-first: campo Pedido (ajuda sobre PS/NF), Volumes com − / + (mín 1),
Foto via `<input type=file accept=image/* capture=environment>` com
miniatura, aviso amarelo "Já registrado hoje às HH:MM por X" calculado no
cliente a partir de `registros-hoje`, botão grande "Registrar pedido
parado", cartão "Registrados hoje" (status da triagem em pílula) e
"Ontem e ainda não registrados hoje" (botão Ainda parado). Triagem ganha
colunas "Registrado por" e "Foto" (miniatura abre em nova aba) e botão
"Exportar CSV". Subtítulo passa a dizer que a origem é o próprio painel.
`PODE_EDITAR` e as consultas Stokki/Vuupt mantêm os níveis atuais
(galpão/expedição veem a triagem só leitura).

**Acessos**: `acesso_telas.TELAS` já cobre `^/api/pedidos-parados/`;
`contadores_menu.NIVEIS["pedidos_parados"]` ganha galpao/expedicao.

**Limpeza**: `pedidos_parados_triagem.py` deixa de importar
`FreshHubSession`/`listar_pedidos_parados`; `_sessao_freshhub` e
`criar_demanda_para_tratativa` ficam (usados quando religar). Constante
`LIMITE_HISTORICO_PEDIDOS_PARADOS` sai. `normalizar_order_number` segue
em `lovable/pedidos_parados.py` (função pura; mover fica pra Fase 1).

**Docs no mesmo commit**: `MAPA_DO_SISTEMA.txt` (tela, rotas, tabela,
pasta de fotos, pausa das Demandas, `lovable/` só pra Demandas),
`TRATATIVAS_PEDIDOS_PARADOS.md` (nota de 24/10 no topo: origem agora é o
painel; Demandas pausadas), `_tour_pedidos_parados.html` se existir.

## Testes

`painel_agentes/test_pedidos_parados_registro.py` (unittest, rodar de
dentro de `painel_agentes/`), com `DB_PATH` apontado pra arquivo
temporário:
- registrar normaliza "PS-40525" → "40525" e guarda o bruto; volumes 0
  recusado; foto inválida recusada.
- dois registros no mesmo dia → `vezes_registrado` 2 e um só na lista.
- `listar_com_classificacao` com Vuupt mockado: só o dia mais recente,
  `dias_parado` desde o primeiro registro, `revisao_urgente` com hora
  local.
- `registros_hoje`: ontem sem hoje aparece em `ontem_sem_hoje` e some
  depois de "Ainda parado".
- apagar: só o autor, só até 15 min; pasta da foto removida.
- `encaminhar_operacao` com flag False levanta ValueError sem tocar na
  Vuupt nem no Lovable.
- `test_pedidos_parados_reenvio.py` continua verde.

Prova na tela: Playwright local em porta ≠ 8070 (ver memória
`feedback_testar_painel_playwright_local`): registrar com foto pelo
viewport de celular, ver na Triagem, apagar, "Ainda parado".

## Fica a critério de quem implementa

Textos exatos de erro, ordem dos cartões no celular, formato do CSV
(cabeçalho + colunas visíveis), tamanho da miniatura.
