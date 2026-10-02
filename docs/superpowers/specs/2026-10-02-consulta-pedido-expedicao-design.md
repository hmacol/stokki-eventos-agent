# Consulta de pedido na Expedição — design

Data: 02/10/2026 · Aprovado em conversa pelo Hugo (regras e arquitetura)

## Objetivo

Na separação da rota, o operador às vezes não encontra um pedido e hoje
chama o Hugo para saber se ele ainda deveria estar no galpão. A tela
responde isso sozinha: o operador digita o código e recebe um veredito
claro, cruzando Vuupt e Stokki **ao vivo**.

Sucesso = o operador decide "procura que está aqui" ou "não está aqui,
por causa de X" sem chamar ninguém, no celular ou no computador.

## Fora do escopo

- Consulta de vários pedidos de uma vez, escolha pela rota, histórico de
  consultas.
- Qualquer escrita (Stokki, Vuupt, `pedidos_parados_*`, tratativas). A
  tela é só leitura.

## Regras do veredito

A Vuupt manda (é o registro físico do motorista); a Stokki complementa e
desempata. Sempre se avalia o serviço **mais recente da cadeia de
reentrega** (`_servico_mais_recente_da_cadeia`).

| Vuupt (serviço mais recente) | Stokki | Veredito |
|---|---|---|
| `not_assigned` (pool / agendado) | Aguardando Transportador / Em espera | 🟢 Deveria estar no galpão — "sem rota" ou "agendado pra dd/mm" |
| `assigned`/`accepted` em rota não iniciada | idem | 🟢 Deveria estar no galpão — "rota X, motorista Y" |
| `on_route` (ou rota já iniciada) | qualquer | 🟡 Saiu em outra rota — motorista, placa, hora de saída |
| `done` + sucesso | qualquer | 🔵 Já foi entregue — data/hora, motorista |
| `done` + insucesso, sem reentrega concluída | qualquer | 🟢 Deveria estar no galpão — "voltou de insucesso em dd/mm (motivo)" |
| retirada no galpão fechada / transportadora de retirada + Enviado | Enviado | ⚪ Retirado — quem e quando |
| qualquer | Cancelado | ⚪ Cancelado na Stokki — não procurar |
| não achado | Aguardando / Em espera | 🟢 Deveria estar no galpão — "ainda não foi pra roteirização" |
| não achado | não existe | ❓ Pedido não encontrado — conferir o código |

Ordem de avaliação: Stokki Cancelado vence tudo; depois as linhas da
Vuupt de cima para baixo.

**Divergência:** quando a Stokki contradiz o veredito da Vuupt (ex.:
Vuupt `not_assigned` e Stokki "Enviado"; Vuupt entregue e Stokki ainda
"Aguardando Transportador" há mais de 24h), mostra o veredito da Vuupt
com faixa "⚠ divergência: Stokki diz <status>". O operador chama o Hugo.

**Stokki indisponível** (agente rodando no painel, outra consulta Stokki
em curso, erro de rede): veredito só pela Vuupt + aviso "Stokki não
conferida — tente em 1 min".

**Vuupt indisponível:** erro "Vuupt indisponível — tente de novo". Sem
veredito chutado.

Abaixo do veredito, sempre duas linhas pequenas com o status bruto:
`Stokki: <situação> · <transportadora>` e `Vuupt: <status> · rota <id>`.

## Tela

- Rota `/expedicao/consulta`, um template responsivo (celular e
  computador), seguindo o visual do painel e `url_for()` em tudo.
- Campo grande, `inputmode="numeric"`, aceita `PS-38123`, `#PS-38123`,
  `38123`, `PS-38123-R1`; normaliza com `normalizar_order_number`.
- Enter ou botão "Consultar"; durante a busca: "Consultando Vuupt e
  Stokki…".
- Resposta: cartão com cor do veredito, frase grande, uma linha de
  contexto, as duas linhas brutas e, se houver, a faixa de divergência
  ou o aviso de Stokki não conferida.
- Campo volta focado e selecionado para o próximo código.
- Menu lateral: item "Consultar pedido" no grupo da Expedição, visível
  para os níveis total, operador, expedicao e galpao.

## Arquitetura

**`painel_agentes/consulta_expedicao.py` (novo)**

- `decidir_veredito(servico, rota, stokki) -> dict` — função pura com a
  tabela acima. Entrada: o serviço Vuupt (ou `None`), um resumo da rota
  (`iniciada`, nome, motorista, placa, ou `None`) e o resultado Stokki
  (`{"status", "transportadora"}`, `None` se não existe, ou marcador de
  "não conferida"). Saída: `{"veredito", "cor", "titulo", "contexto",
  "divergencia", "stokki_bruto", "vuupt_bruto", "aviso"}`.
- `consultar(codigo) -> dict` — normaliza o código, busca Vuupt
  (`buscar_servico_por_code` + cadeia de reentrega + rota do serviço),
  busca Stokki (`_status_e_transportadora_stokki`, com o mesmo
  `_lock_consulta_stokki` e `_painel_tem_execucao_rodando` de
  `pedidos_parados_triagem`), chama `decidir_veredito`. Não grava nada.
- Identificação de retirada: mesma `_tipo_retira` de
  `pedidos_parados_triagem`.

**`painel_agentes/painel_agentes.py`**

- `GET /expedicao/consulta` — página.
- `GET /api/expedicao/consulta?codigo=` — JSON de `consultar`.
- Ambas com `requer_auth(niveis=("total", "operador", "expedicao", "galpao"))`.

**Não reaproveitar** `verificar_na_stokki` / `verificar_na_vuupt`: elas
gravam classificação em Pedidos Parados.

## Riscos

- Abrir `StokkiSession` pode coincidir com o login da importação (outro
  projeto) e derrubar a sessão dela com 401. Mesmo risco que Pedidos
  Parados já corre; aceito porque é um pedido por clique.
- Custo na Vuupt: 1 a 3 chamadas por consulta (código, reentregas, rota).

## Testes

- `painel_agentes/test_consulta_expedicao.py` (unittest, roda de dentro
  de `painel_agentes/`): um caso por linha da tabela, divergência
  (Stokki Enviado × Vuupt pool), Stokki não conferida, Vuupt fora,
  normalização dos formatos de código.
- Teste real na porta 8099 com três códigos verdadeiros (entregue, no
  pool, em rota) e verificação visual em viewport de celular via
  Playwright.
- `MAPA_DO_SISTEMA.txt` atualizado com a tela e o módulo novo.
