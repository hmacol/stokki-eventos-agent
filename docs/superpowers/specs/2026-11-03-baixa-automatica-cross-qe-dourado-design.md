# Baixa automática dos pedidos Cross (Quatro Estrelas e Dourado)

03/11/2026 · brainstorming com o Hugo · status: aguardando aprovação

## Por que

Quatro Estrelas (stkkc-98) e Laticínios Dourado (stkkc-18) são **Cross**: a carga chega ao galpão já
separada pelo embarcador (item único `XD...`). Mesmo assim, na Stokki o pedido nasce em **Aberto** e só
chega a **Aguardando Transportador** depois de passar por separação e conferência, coisa que ninguém faz
fisicamente para esses dois.

Enquanto o pedido fica em Aberto, a regra de prontidão (`nucleo/prontidao_stokki.py`) o trata como "não
pronto": ele aparece no bloco "Em separação na Stokki" do pool, sai do rascunho no envio
(`rascunhos_rota.enviar_rascunho`) e não entra no incremento das 22h (`roteirizacao/incrementar_rotas.py`).
Na foto de 02/11 22h13, a maioria dos 107 pedidos "Open" era desses dois embarcadores.

Este projeto vem **antes** de deixar a foto da Stokki mais frequente (próximo projeto: `prontidao_stokki`
lendo `galpao_pedidos` do coletor do Galpão 3D). Com a foto mais nova e esses pedidos ainda em Aberto, eles
seriam barrados mais depressa ainda.

## O que é

Uma rotina agendada que pega **todo** pedido em Aberto da Quatro Estrelas e da Dourado e leva até
Aguardando Transportador, pelos mesmos passos que um operador faria na área de operador da Stokki (conta
provider, wms@, usuário "ClaudeIA").

**Regra de negócio (Hugo, 03/11): esses dois embarcadores podem SEMPRE passar para Aguardando
Transportador.** Não há conferência física, nem checagem de chegada da carga, de data de expedição ou de
qualquer outra coisa. Se está Aberto e é de um dos dois, avança.

## Objetivos

1. Nenhum pedido da Quatro Estrelas ou da Dourado fica em Aberto mais de ~15 min em dia útil, das 6h às 22h.
2. Antes de cada rodada que decide rota (pipeline horário às :05, sequências 16h, 18h e 22h), a rotina já
   rodou.
3. O número de volumes que fica na Stokki é o volume real do pedido, nunca 1 fixo.
4. Falha de um pedido não trava os outros e não passa em silêncio.

## Não-objetivos

- Os outros embarcadores Cross (Facilita 10, Jatobá 15, Jersey 79, Dansken 85, CIAO 87, Morota 77, Fruta
  Fina 94, Burin 12). Ficam de fora; incluir é decisão futura do Hugo.
- Imprimir DANFE ou etiqueta. O assistente de impressão que a Stokki abre depois da conferência é pulado,
  como no piloto.
- Mudar a foto da Stokki ou a regra de prontidão (próximo projeto).
- Rodar em fim de semana, em feriado ou depois das 22h.

## Antiobjetivos (falharia mesmo "funcionando")

- **Pedido grande registrado como 1 volume.** O volume informado na conferência ganha do volume da NF quando
  o pipeline monta o serviço na Vuupt (`stokki/pedidos.py`, "Quantidade de volumes informado na conferência"
  → `quantidade_volumes` → `dimension_3` em `pipeline.py`). Errar aqui subdimensiona a rota.
- **Derrubar a sessão de outro processo na Stokki.** Login concorrente na mesma conta derruba a outra sessão.
- **Mexer em pedido de outro embarcador.** Um id digitado errado na Seleção por Lista separa o pedido errado.
- **Spam no grupo Alertas** (o mesmo erro a cada 15 min).

## Como funciona (decidido)

### Os 3 passos (provados no PS-42990 em 02/11; scripts do piloto em `/tmp/piloto_*.py` na VPS)

Tudo por Playwright, com o login provider de `stokki/estacao_impressao.py` (`_credenciais_provider`,
`_novo_contexto_disfarcado`, `_login_provider`) e dentro de `@sessao_uso.na_vez_da_provider` (espera a vez,
não derruba ninguém). Antes de cada passo, `stokki/avancar_cross.situacao_do_pedido` confere a situação; a
rotina **retoma do passo em que o pedido está**:

| Situação atual | Passo | Como (piloto) |
|---|---|---|
| Aberto | 1. Iniciar separação | `/provider/operation/picking/list` → `#btn_option_order` → `#btn_order` → digitar o id em `#input_order_id` + Enter → exigir que a lista tenha **exatamente** esse id em `.btn_select_order[data-id]` → clicar → `#select_operator` = `10` (Recebimentofrio01) → conferir `#input_provider_outbound_id` == id → `#btn_next_operator` (POST picking/list/finish). Fica "Em separação". |
| Em separação | 2. Concluir a separação | `/provider/operation/order/packaging` → `$('#search_station').val('1'); $('#search').val(id); $('#form_search').submit()` → a resposta de `/packaging/search` tem que trazer `picking_list_finish: true` e `provider_outbound_id` == id → `#btn_picking_list` (GET picking/list/conclude/{task_id}). Fica "Em Conferência". |
| Em Conferência | 3. Finalizar sem conferência | mesma busca (resposta com `picking_list: true` e `provider_outbound_id` == id) → `/provider/operation/order/packaging/create/{id}/1/1/0` → ler `var count_product = 'N'` do HTML → preencher `#packaging_no_conference=1`, `#weight_no_conference=''`, `#packaging_id_no_conference=1` (exigir a opção "1=Padrao"), `#volume=N` → `$('#form_packaging').submit()` (POST packaging/store). Fica "Aguardando Transportador". |
| Aguardando Transportador ou outra | nada | conta como feito / fora do escopo |

Diferença para o piloto: no passo 2, o `task_id` não é conhecido de antemão. Aceita-se o `task_id` que a
busca devolver, desde que o `provider_outbound_id` seja o do pedido.

Depois do passo 3, a rotina relê a situação. Só conta como sucesso se estiver "Aguardando Transportador" (o
POST packaging/store redireciona, não dá pra ler o corpo).

### Volumes

`#volume` = `count_product` da tela de conferência. Para pedido Cross, `count_product` é a quantidade do
item XD, que a importação já grava como a soma dos `<vol><qVol>` da NF (regra do Cross de 24/10; sem qVol,
1). Exemplos reais da Dourado em 03/11: 3, 39, 1, 4, 2. Sem `count_product` legível ou com 0, o pedido
**falha** nesse passo. A rotina nunca inventa 1.

Conferido no HTML da tela de conferência salvo na sonda de 02/11 (`/tmp/sonda_picking/conferencia_create.html`
na VPS), no clique de "Finalizar" do modal "Finalizar sem Conferência":
- `#packaging_no_conference` (name `no_conference`) é só a **marca** "sem conferência" = 1;
- `#volume` recebe o campo "Quantidade de volumes" do modal (`#input_volume_no_conference`);
- a Stokki recusa volume maior que `count_product` ("Quantidade de volumes maior que a quantidade de
  produtos").

Logo, o N vai em `#volume`. A prova real (Virada, passo 2) tem que usar pedido com **mais de 1 volume** e
conferir, na página do pedido, "Quantidade de volumes informado na conferência" = N.

**Só pedido de item único XD (minha escolha, 03/11).** Antes do passo 1, a rotina exige que o pedido tenha
um único produto e que o código dele comece com `XD`. Fora disso (pedido feito à mão, de armazenagem ou
importado antes da regra do Cross), `count_product` contaria unidades e a separação seria dada sem ninguém
separar estoque de verdade. Esse pedido não avança e vai direto para o aviso, sem esperar 3 tentativas.

### Quem entra

Pedidos em **Aberto** (e os que pararam no meio: Em separação ou Em Conferência) com stkkc 98 ou 18.
Embarcadores fixos numa constante no módulo (como `EMBARCADORES_IMPORTAR_ABERTOS` do pipeline), não em
config.

### Quando roda

- Timer systemd `stokki-baixa-cross.timer`: dia útil, a cada 15 min, das **06h05 às 21h50**
  (`OnCalendar=Mon..Fri 06..21:05/15`). As rodadas das :50 caem antes do pipeline horário (:05) e das
  sequências de 16h, 18h e 22h.
- Feriado: o script confere `regras/feriados.eh_dia_util` e sai sem fazer nada (mesmo padrão de
  `alertar_retornos_atrasados.py`).
- `Type=oneshot`: se uma rodada ainda estiver rodando quando a próxima chegar, o systemd não dispara outra.
- **Teto de tempo (minha escolha, 03/11).** A conta provider tem uma fila só, dividida com a Estação de
  Impressão e os documentos. A rotina segura essa conta no máximo **10 min** por rodada. Entre um pedido e
  outro, ela confere a fila (`sessao_uso.fila` da provider) e, se tiver alguém esperando, para e devolve a
  vez (mesmo padrão do `motivo_para_ceder` de `galpao3d/coletar.py`). O que sobrar fica para a próxima
  rodada.
- As sequências de 16h, 18h e 22h rodam o pipeline (que grava a foto) antes de `criar_rotas_diarias` /
  `incrementar_rotas`. Então a baixa das :50 já aparece na decisão.

### Falhas e avisos

- Tabela nova `cross_baixas` no `dados.db`, uma linha por pedido: `codigo`, `id_stokki`, `embarcador`,
  `etapa_atual`, `tentativas`, `ultimo_erro`, `primeira_vez_em`, `concluido_em`, `alertado_em`.
- Um pedido que falha volta a ser tentado na próxima rodada. Na **3ª falha seguida**, a rotina para de
  tentar e manda **uma** mensagem no grupo "Alertas" do /atendimento (`painel_agentes/retornos.postar_alerta`
  ou equivalente via `grupos_equipe`), listando os pedidos travados e o erro. Mudar de situação na Stokki
  (alguém resolveu à mão) zera a contagem.
- Erro de login ou da tela (seletor sumiu, Stokki fora do ar) para a rodada inteira, sem marcar falha em
  cada pedido. Uma mensagem no grupo, no máximo uma por dia para o mesmo erro.

### Execução e testes

- Script: `cross/baixa_automatica.py` ou `stokki/baixa_cross.py` (o builder escolhe o lugar). Roda na VPS
  como www-data, com `venv/bin/python`.
- `--modo-teste`: lista os pedidos e o passo que cada um faria, com os volumes lidos; não clica em nada que
  grave e não posta no grupo.
- `--pedido PS-NNNNN`: só aquele pedido (para a primeira prova real).
- `--limite N`: no máximo N pedidos na rodada.
- Testes unitários da decisão (situação → passo, volumes, contagem de falhas, alerta uma vez só) com a
  Stokki falsa. O Playwright fica atrás de funções pequenas que dá pra trocar no teste.

### Virada

1. `--modo-teste` na VPS.
2. Prova real com `--pedido` num pedido Aberto da Dourado e outro da Quatro Estrelas.
3. `--limite 5`.
4. Ligar o timer.

Antes do passo 2, garantir que nenhuma sessão está rodando o lote manual dos scripts do piloto. Em 03/11
00h30 havia um em andamento (separação no nome do Recebimentofrio01). Atualizar `MAPA_DO_SISTEMA.txt` no
mesmo commit.

## Fica com o builder

- Nome e lugar do módulo, estrutura das funções, como isolar o Playwright para teste.
- De onde vem a lista de candidatos. Sugestão: `galpao_pedidos` (situação Aberto, Em separação ou Em
  Conferência e embarcador terminando em `#stkkc-98`/`#stkkc-18`), que o Galpão 3D atualiza a cada minuto
  sem custo de sessão. Ou a Lista de Pedidos da própria área provider. Como a situação é sempre reconferida
  na página do pedido antes de cada passo, a lista é só ponto de partida.
- Esperas e timeouts do Playwright (o piloto usa até 2 min no passo 1).
- Quantos pedidos por login (um navegador por rodada, não um por pedido).
