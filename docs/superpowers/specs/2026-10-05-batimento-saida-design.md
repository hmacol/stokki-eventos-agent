# Batimento de pedidos: saída (aba Fechamento, Torre, e-mail)

Data: 05/10/2026. Complementa `DOC_EXECUCAO_CLAUDE_BATIMENTO_PEDIDOS.md`
(seção "Peças → A implementar → Saída"). O job `batimento/bater.py` já roda
em produção às 07h25 e grava `batimento_pedidos` e `batimento_rodadas`.

## Objetivo

Fazer o resultado do batimento chegar a quem age:

- a aba **Fechamento** no `/vigia` mostra a conta do dia e a lista de
  divergências, e deixa o time marcar uma divergência como tratada;
- a **Torre** puxa para a Fila de ação só o que pede ação e está parado;
- um **e-mail interno** avisa quando surge divergência nova ou a conta não
  fecha.

Sucesso: Hugo abre a aba e entende em segundos se a conta fechou e o que
falta resolver; nenhuma divergência real fica mais de 1 dia útil sem
aparecer na Torre; dia sem novidade não gera e-mail.

## Decisões do Hugo (05/10)

| Pergunta | Decisão |
|---|---|
| O que vai pra Torre | Só divergências **reais**, sem tratativa, abertas há mais de **1 dia útil**. As "sem comprovante" ficam só na aba e no e-mail. |
| E-mail | Diário, logo depois da rodada das 07h25, **só se houver novidade** (divergência nova ou conta que não fecha). |
| Tratar na aba | Sim: botão "Tratar" com observação, grava quem e quando. Níveis total e operador; leitura só vê. |
| Organização | Tudo dentro de `batimento/` (padrão do vigia); sem tela nem timer novos. |

Dia útil = segunda a sexta. Feriados continuam em aberto (decisão única
para batimento e agente analista); quando houver, entram na mesma função.

## Grupos de divergência

`batimento/regras.py` ganha duas tuplas (fonte única, usadas pela aba,
Torre e e-mail):

- `DIVERGENCIAS_REAIS`: `EXPEDIDO_SEM_ENTREGA`, `EXPEDIDO_SEM_DOCUMENTO`,
  `EXPEDIDO_COM_INSUCESSO_ABERTO`, `ENTREGUE_NAO_EXPEDIDO`,
  `CANCELADO_STOKKI_SERVICO_VIVO`, `CANCELADO_VUUPT_STOKKI_ABERTO`,
  `STATUS_STOKKI_DESCONHECIDO`.
- `DIVERGENCIAS_COMPROVANTE`: `REDESPACHO_SEM_COMPROVANTE`,
  `RETIRADA_SEM_COMPROVANTE`, `LALAMOVE_SEM_COMPROVANTE`.

Mais um rótulo curto em português por motivo (`ROTULOS_DIVERGENCIA`) para
tela e e-mail.

## 1. Dados (`batimento/banco.py`)

- Coluna nova `tratado_obs TEXT` (ALTER no `garantir_esquema`, como o
  vigia faz com `ausencias`).
- Em `gravar_rodada`, quando o pedido troca de (caixa, rótulo), limpa
  `tratado_em`, `tratado_por` e `tratado_obs`: a tratativa valia para o
  motivo anterior.
- `marcar_tratado(conn, codigo, por, obs)` grava os três campos. Só aceita
  pedido em `DIVERGENCIA`; observação obrigatória (até 300 caracteres).

## 2. Consulta (`batimento/consulta.py`, novo)

Só leitura, exceto `tratar`. Funções:

- `ultima_rodada(conn)` → dict da última linha de `batimento_rodadas`
  (com `resumo_json` decodificado) ou `None`.
- `fechamento(motivo="", so_reais=False, incluir_tratadas=False,
  busca="", db_path=None)` → `{linhas, por_motivo, total_abertas, ultima,
  rodadas}` (últimas 14 rodadas). Linhas ordenadas por `desde` (mais
  antiga primeiro). `por_motivo` conta as não tratadas, reais primeiro.
- `novidades(conn, rodada_em)` → divergências que entraram nesta rodada.
- `tratar(codigo, por, obs)` → grava a tratativa via `banco.marcar_tratado`.
- Funções que abrem o banco recebem `db_path` (padrão do `vigia/consulta.py`).
- `excecoes_torre(data_iso, agora=None)` → lista no formato do vigia:
  `{"id": f"batimento:{codigo}:{rotulo}", "severidade": "atencao",
  "tipo": "Batimento", "descricao": "<PS> <embarcador>: <motivo> desde
  <dd/mm>", "quando": None, "acao": {"tipo": "link", "url":
  "/vigia?aba=fechamento&busca=<PS>", "rotulo": "Ver no Fechamento"}}`
  para cada divergência real não tratada com `vencida(desde, agora)`.
  Se a última rodada tem `fecha = 0`, acrescenta um item `critico`
  (`id = f"batimento:nao-fecha:{rodada_em}"`). Sem rodada nenhuma: lista
  vazia. URLs relativas ao painel, sem `url_for` (roda fora de request).
- `vencida(desde, agora)` → `True` quando passou 1 dia útil desde
  `desde` (mesmo horário no próximo dia de segunda a sexta).

O id do item da Torre é estável por pedido e motivo: "Tratar" na Torre
esconde até o motivo mudar.

## 3. Aba Fechamento (`/vigia?aba=fechamento`)

Rota `vigia_pedidos` existente passa a ler `aba` (padrão do `/consulta`:
`aba = "fechamento" if ... else "abertos"`). A aba "Pedidos abertos" não
muda. Template `vigia_pedidos.html` ganha a faixa de abas e um bloco
`{% if aba == 'fechamento' %}`:

- **Topo:** a conta da última rodada (lançados = destino + em andamento +
  divergência), selo "fecha"/"NÃO fecha", hora da rodada, e a cobertura
  da faixa (listados, Importação, inexistentes).
- **Cartões** por motivo de divergência (não tratadas), reais com a classe
  `.critico`; clique filtra.
- **Filtros (GET):** motivo, "só reais", "incluir tratadas", busca;
  `<input type="hidden" name="aba" value="fechamento">`.
- **Tabela:** PS, embarcador, transportadora, motivo, desde (com marca de
  vencida), evidências, tratativa (quem/quando/obs) e botão **Tratar**.
  O botão só aparece para os níveis `total` e `operador`
  (`g.nivel_acesso`); abre um `prompt` para a observação e faz POST.
- **Histórico:** últimas 14 rodadas (data, lançados, destino, andamento,
  divergência, fecha).

Rota nova `POST /api/batimento/tratar`
(`@requer_auth(niveis=("total", "operador"))`, `@exige_mesma_origem`),
body `{codigo, obs}`, grava com `session.get("usuario") or
g.nivel_acesso`. Erro de validação → 400 com `{"erro": ...}`.

Falha ao ler o batimento na aba: mesma regra da aba atual (renderiza vazio
com `erro`), sem derrubar a página.

## 4. Torre

Em `painel_agentes/torre_controle.py::_montar_excecoes`, logo depois do
bloco do vigia, o mesmo padrão:

```python
try:
    from batimento.consulta import excecoes_torre as batimento_excecoes
    for x in batimento_excecoes(data_iso):
        excecoes.append({**x, "_epoch": 0.0})
except Exception:
    logger.exception("[torre] Batimento indisponível")
```

## 5. E-mail (`batimento/bater.py`)

Depois de gravar a rodada, se não for `--resumo`:

- "novidade" = pedidos que entraram em `DIVERGENCIA` nesta rodada
  (`desde == rodada_em`) ou rodada com `fecha = 0`;
- sem novidade: só log, nada enviado;
- com novidade: `email_utils.enviar_email([destino], assunto,
  envelope_html(corpo, cor_acento=...), config["email"])`, destino =
  `notificacao_execucao.destinatario` (fallback `hugo@freshlogbr.com`,
  igual ao `nucleo/comparar_pool.py`);
- assunto: `[Freshlog] Batimento: N divergência(s) nova(s)` ou
  `[Freshlog] Batimento NÃO FECHA: ...`;
- corpo: a conta do dia, a tabela das divergências novas (PS, embarcador,
  motivo, evidências) e o total em aberto por motivo, reais primeiro, e
  link para a aba (`https://app.freshhub.com.br/painel/vigia?aba=fechamento`).

Falha no envio não muda o código de saída do job (o batimento já gravou);
só loga. Conta que não fecha continua saindo com 1 (alerta de falha).

Na primeira rodada depois do deploy, todas as divergências já existentes
têm `desde` antigo, então não geram e-mail; só as que surgirem dali em
diante.

## 6. Testes

- `batimento/test_consulta.py`: `vencida` (sexta → segunda, sábado),
  filtro de reais, `excecoes_torre` (vencida/não vencida, tratada some,
  rodada que não fecha gera crítico), `listar_divergencias` com filtros.
- `batimento/test_banco.py`: troca de motivo limpa tratativa;
  `marcar_tratado` recusa pedido fora de DIVERGENCIA e obs vazia.
- `batimento/test_bater.py`: decisão de "novidade" e montagem do e-mail
  (envio mockado).
- `painel_agentes/test_batimento_tela.py` (rodar de dentro de
  `painel_agentes/`): `/vigia?aba=fechamento` 200 com banco sintético;
  POST `/api/batimento/tratar` grava e respeita nível.
- Verificação visual com Playwright local (porta 8099+), desktop e
  celular.

## Fora do escopo

- Captura de comprovante de redespacho/retirada e guarda do `pod_image`
  da Lalamove.
- Absorver `verificar_entregues_nao_expedidos.py`.
- Feriados.
- Sugestões do Agente Analista.
