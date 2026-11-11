# Editar rota já enviada: motorista, carro e pedidos (design, 10/11/2026)

## Por quê

Depois do "Confirmar e enviar" o card da rota na tela Planejamento fica travado.
Hoje, para trocar o motorista ou o carro de uma rota enviada, a única saída é
cancelar a rota e reenviar, e isso só funciona antes de o motorista sair. Pedido
que entra ou sai no meio do dia (cliente cancelou, pedido liberado tarde, não
coube no carro) não tem caminho na tela Planejamento. A Torre tira pedido de rota
em andamento, mas sem trava nenhuma para pedido já entregue.

Além disso, o motorista nem sempre leva o carro dele para a Vuupt: das 279 rotas
enviadas nos últimos 30 dias, 75 saíram com motorista e sem veículo (19 da
Lalamove, que é virtual; ~56 de motoristas reais: Caio (Erick 2) 19, Iago 11,
André 7...). 7 dos 20 motoristas ativos estão sem `vehicle_id` no cadastro.

O Hugo quer (10/11):
1. Trocar **só** o motorista e/ou o carro de uma rota enviada, sem cancelar e
   reenviar.
2. Adicionar e tirar pedidos de rota enviada, **mesmo depois de o motorista ter
   iniciado a rota**.
3. O motorista estar **sempre** ligado ao carro dele no lançamento da rota.

## Regras decididas pelo Hugo

- **Tirar pedido da rota: só pedido que o motorista ainda não começou.** Pedido a
  caminho, no local, entregue ou com insucesso não sai da rota. Vale para todas as
  telas (Planejamento e Torre).
- **Trocar motorista em rota já iniciada não é permitido.**
- **Trocar o carro de uma rota vale só para aquela rota.** O cadastro do
  motorista (o carro "dele") não muda.
- O motorista sempre vai com um carro no lançamento.

## O que a Vuupt aceita (provado em 10/11 com o agente TESTE 28438)

Teste com rotas 5449893 e 5449907, cliente "usuario teste", veículos genéricos
`.Seco` (74274) e `..Carro_Grande` (103399). Scripts em
`C:\Users\HUGOMA~1\AppData\Local\Temp\claude\0e2c71a7243c86ad\670d1b9f-19f6-479c-9820-8a81cb4de924\scratchpad\sonda1_rota_iniciada.py`
e `sonda2_rota_iniciada.py`, que ficam só no scratchpad, fora do repositório.

| Chamada | Rota não iniciada | Rota com pedido entregue / a caminho |
|---|---|---|
| `PUT /routes/{id}` `{"vehicle_id": X}` | 200, troca | 200, troca |
| `PUT /routes/{id}` `{"agent_id": X, "vehicle_id": Y}` | 200, troca, serviços vão pro novo agente | 200 com o mesmo agente; `agent_id: null` também passa e desatribui os pendentes. Trocar por outro agente não foi testado (fica proibido por regra) |
| `POST /routes/{id}/activities` | 200 | 200; com agente na rota o serviço já entra `assigned` |
| `DELETE /routes/{id}/services/{sid}` (**endpoint novo, o projeto ainda não usa**) | 200, serviço volta `not_assigned` sem rota | 200 para pendente **e também para `on_route` e `done`**. Um `done` removido continua `done`, mas perde o `route_id` (estraga romaneio, km e financeiro) |
| `PUT /routes/{id}` com `activities` (método atual da Torre) | 200 | 200 quando a lista inclui o entregue ainda ligado à rota; 400 "Serviço não pode ser atribuído no status atual" quando a lista tem um serviço que não está mais na rota |

Conclusão: **quem trava é o nosso sistema**, porque a Vuupt deixa tirar até
pedido entregue. A doc oficial do DELETE (coleção Postman em developers.vuupt.com)
diz: "não é possível remover serviços de rotas finalizadas ou canceladas, nem de
rotas com vale-pedágio ativo".

Status de serviço na Vuupt: `not_assigned`, `assigned`, `accepted`, `on_route`,
`arrived`, `done`, `canceled`.

## Como está hoje (origin/master em 10/11)

- Card da rota enviada: `painel_agentes/templates/planejamento_rotas.html`,
  `criarCardRota` (`bloqueada = enviada || ofertada` desliga o seletor de
  motorista, o drag das paradas e as ações). O seletor manda
  `/api/planejamento/trocar-motorista` com o `vehicle_id` do motorista (lista
  `MOTORISTAS`, montada em `planejamento_rotas.py` a partir de
  `regras/preferencias_motoristas.CatalogoMotoristas`, coluna `VEHICLE_ID_VUUPT`
  / `motoristas.vehicle_id`).
- `painel_agentes/rascunhos_rota.py`: `trocar_motorista`, `remover_parada` e
  `adicionar_parada` só mexem no banco local. `preparar_cancelamento_de_parada`
  (botão "Cancelar pedido" em rota enviada) usa PUT com a lista e só aceita rota
  não iniciada. `enviar_rascunho` → `rotas_client.criar_rota_removendo_conflitos`
  já manda `agent_id`/`vehicle_id`; se o `vehicle_id` está vazio, a rota sai sem
  carro.
- Torre, "Excluir da Rota": `/api/torre/excluir-pedido` →
  `painel_agentes/expedicao.excluir_pedido_da_rota(permitir_rota_em_andamento=True)`.
  Ele recusa só rota `finished`/`canceled`, **não confere o status do pedido** e
  usa PUT com a lista inteira. Também **não atualiza o rascunho local**, então o
  card do Planejamento continua mostrando o pedido.
- `roteirizacao/rotas_client.py`: `atualizar_rota` (PUT lista), `adicionar_atividades`
  (POST), `cancelar_rota`. Não existe função para o DELETE de um serviço nem para
  PUT só de agente/veículo.
- Rotina que envia rascunho sozinha: sequências 16h/18h/22h (`infra/sequencia_*.sh`)
  chamam `enviar_rascunho`.

## O que construir

### 1. Cliente Vuupt

Em `roteirizacao/rotas_client.py`:
- `remover_servico_da_rota(token, route_id, service_id)` → `DELETE /routes/{id}/services/{sid}`.
- `atualizar_motorista_veiculo(token, route_id, agent_id, vehicle_id)` → `PUT /routes/{id}`
  com `agent_id`/`vehicle_id` e **sem** `activities` (sem `activities` a Vuupt não
  mexe nas paradas).

### 2. Uma regra só para "pedido não começado"

Uma função (sugestão: em `rascunhos_rota.py` ou num módulo pequeno ao lado) que lê
a rota **ao vivo** (`buscar_rota(..., include=["services"])`) e diz, por pedido, se
ele pode sair:
- **Pode sair:** `assigned`, `accepted` (e `not_assigned` ainda pendurado na rota).
- **Não pode:** `on_route`, `arrived`, `done` (sucesso ou insucesso), `canceled`.

Todas as telas que tiram pedido de rota usam essa função: Planejamento, Torre e
o "Cancelar pedido" do Planejamento em rota enviada.

**Rota "iniciada"** (para a trava de troca de motorista): algum pedido da rota em
`on_route`, `arrived` ou `done`, ou status da rota `started`/`finished`.

### 3. Trocar motorista e/ou carro (card da rota enviada)

- O seletor de motorista volta a funcionar no card ENVIADO (só OFERTADA continua
  travado). Ao lado dele, um **seletor de carro**.
- Ao escolher o motorista, o carro do cadastro dele entra sozinho. O seletor de
  carro deixa trocar só para essa rota (`rascunhos_rota.vehicle_id`), sem tocar
  no cadastro.
- Em rota enviada, a troca chama a Vuupt (`atualizar_motorista_veiculo`) e só grava
  no banco local depois do 200. Na Vuupt, os pedidos pendentes passam para o
  motorista novo.
- **Motorista:** só troca se a rota não foi iniciada (regra acima). Rota iniciada →
  o seletor de motorista fica travado com o aviso "rota já começou: só o carro pode
  mudar".
- **Carro:** troca mesmo em rota iniciada.
- Também vale para o card em RASCUNHO (o seletor de carro aparece lá; hoje o carro
  é sempre o do cadastro).
- Fora do escopo: rota da Lalamove (`agent_id` = LALAMOVE). Lá o carro é o da
  corrida.

### 4. Motorista sempre com carro

- Rota com motorista real (não Lalamove, não Retirada) e sem carro **não sai** pelo
  "Confirmar e enviar": o botão avisa "motorista X sem carro cadastrado: escolha o
  carro no card ou cadastre o veículo dele".
- O card mostra um selo "sem carro" quando o motorista escolhido não tem
  `vehicle_id`, para isso aparecer antes da hora do envio.
- **Veículo genérico não conta como carro** (Hugo, 10/11): placa que começa com "."
  (`.Seco`, `..Carro_Grande`, `.REFRI & CONG-*`, `..Rota-Centro`) é tratada como
  sem carro e não aparece no seletor.
- **Antes de ligar, acertar o cadastro** (Hugo, 10/11): lista de 10/11 com 21
  motoristas ativos na `bd_motoristas` sem `VEHICLE_ID_VUUPT` (é dela que o
  Planejamento lê, via `CatalogoMotoristas._carregar_banco`; a tabela `motoristas`
  do app é outra coisa). A lista está no scratchpad
  (`lista_motoristas_carro.json`) e no resumo da sessão. A trava só entra no ar
  depois que o Hugo acertar a lista.
- Envio automático (sequências 16h/18h/22h), call do Claude: a rota **sai mesmo
  assim** e posta um aviso no grupo Alertas do /atendimento ("rota X saiu sem carro:
  motorista Y sem carro cadastrado"). Barrar às 22h faria os pedidos perderem o dia.
  Com o cadastro acertado, isso deve ser raro. A trava fica no endpoint do
  "Confirmar e enviar", não em `enviar_rascunho`.

### 5. Adicionar pedido em rota enviada (inclusive iniciada)

- Arrastar um pedido do pool para o card ENVIADO (o mesmo gesto do rascunho).
- Mesmas travas de quando o pedido entra no envio: `nucleo/prontidao_stokki.py`
  (pedido "Em espera" não entra), pedido já em outra rota não entra. Capacidade
  (15 pedidos, teto de caixas) segue igual ao rascunho manual de hoje: aparece no
  indicador de capacidade do card e não bloqueia.
- Chamada: `adicionar_atividades`. A posição é decisão do builder; ela não pode
  ficar antes de um pedido já começado ou entregue.
- Depois do 200: grava a parada em `rascunhos_parada`, dispara os mesmos efeitos do
  envio para esse pedido (espelho no núcleo, evento `roteirizado`/`rota_enviada`
  no freshhub via `nucleo/eventos_freshhub.py`, romaneio refeito por
  `documentacao_rota.agendar`).
- Rota da Lalamove com corrida lançada: não aceita.

### 6. Tirar pedido de rota enviada (inclusive iniciada)

- No card ENVIADO, o X da parada (hoje escondido) volta para pedido não começado.
  Pedido começado/entregue aparece sem o X, com o status dele.
- Pede o motivo, com a mesma lista da Torre (`expedicao.MOTIVOS_EXCLUSAO`) e
  observação, e grava a auditoria com `_registrar_exclusao`.
- Confere o status **ao vivo** antes de chamar a Vuupt (o card pode estar velho).
- Chamada: `remover_servico_da_rota` (DELETE). Na Vuupt o pedido fica
  `not_assigned`, **nunca é cancelado**.
- **Rota não iniciada:** o pedido volta para o pool normalmente (o motorista ainda
  não saiu com ele).
- **Rota iniciada:** o pedido está no carro. Ele fica **"Retornando" até chegar ao
  galpão** (Hugo, 10/11), ver seção 6a.
- Se era o último pedido e nenhum outro começou: cancela a rota inteira
  (`cancelar_rota(services_action="unassign")`) e o rascunho volta como hoje. Se
  ainda há pedido começado ou entregue, a rota fica com eles e o motorista termina
  normalmente.
- Depois do 200: tira a parada de `rascunhos_parada`, refaz o romaneio, recalcula
  o km.

### 6a. Pedido "Retornando" (tirado de rota iniciada)

- A remoção de rota iniciada, por qualquer tela, grava o pedido como **retornando**,
  com rota, motorista, motivo, quem tirou e quando. A sugestão é aproveitar a
  auditoria de `_registrar_exclusao` com uma marca de "rota iniciada".
- Enquanto estiver retornando:
  - no pool do Planejamento, aparece com o selo "Retornando — no carro de
    <motorista>" e **não pode** entrar em rascunho nem em rota (arrastar, Incrementar,
    roteirizar selecionados, rascunho automático das 16h e incremento das 18h/22h
    pulam o pedido);
  - aparece na tela **/retornos** como um tipo novo, "Retirado da rota", junto com
    os insucessos do mesmo motorista. O recebedor confere do mesmo jeito (caixas,
    avaria, foto da carga, assinatura).
- Ao receber em /retornos, o pedido deixa de estar retornando e volta ao pool
  normal.
- A cobrança das 07h do próximo dia útil (`alertar_retornos_atrasados.py`) vale
  também para esse tipo.
- Hoje a /retornos lê só insucesso e parcial de `nucleo_paradas`, com a parada como
  chave. O tipo novo precisa de outra fonte (o registro de retornando), porque o
  pedido sai da rota e não vira insucesso. A forma de juntar as duas fontes na tela
  fica com o builder.
- Freshhub, call do Claude: fica fora desta entrega. Não existe evento "saiu da rota"
  em `nucleo/eventos_freshhub.py`, e criar um mexe no outro repositório.

### 7. Torre usa a mesma regra

`expedicao.excluir_pedido_da_rota` passa a:
- recusar pedido começado ou entregue (regra 2);
- usar o DELETE em vez do PUT com a lista;
- tirar a parada do rascunho local quando a rota tem rascunho
  (`rascunhos_rota.vuupt_route_id`).

A Expedição (`permitir_rota_em_andamento=False`) mantém a trava atual de rota não
iniciada.

O "Cancelar pedido" do Planejamento em rota enviada
(`rascunhos_rota.preparar_cancelamento_de_parada`) também troca a trava de "rota
não iniciada" pela regra 2: pedido não começado sai, mesmo com a rota iniciada.
Também troca o PUT com a lista pelo DELETE, antes do `PUT /services/{id}/cancel`.

### 8. Permissões e travas que continuam

- Níveis `total`, `operador`, `logistica` (os mesmos endpoints de hoje), com
  `exige_mesma_origem`.
- `@bloqueia_planejamento_passado`: rota de dia anterior continua só consulta.
- Endpoint novo entra em `painel_agentes/acesso_telas.TELAS` se for rota nova.

## Não é objetivo

- Trocar motorista de rota já iniciada, ou dividir a rota entre dois motoristas.
- Mudar o carro do cadastro do motorista pela tela de rota.
- Cadastrar veículo novo na Vuupt.
- Editar rota da Lalamove com corrida lançada.
- Avisar o motorista por WhatsApp da mudança (o app da Vuupt já mostra a rota
  atualizada).

## Seria um fracasso se

- Um pedido entregue, a caminho ou no local saísse da rota por qualquer tela.
- A rota saísse sem carro sem ninguém perceber.
- O card do Planejamento mostrasse uma rota diferente da que está na Vuupt
  depois de uma edição (por qualquer tela).
- Tirar um pedido o cancelasse na Vuupt (tem que voltar ao pool; DELETE de
  serviço recria, ver `project_cancelar_pedido_delete_recria`).

## Decisões deixadas ao builder

- Onde fica a função de status ao vivo e como a tela recebe o status por parada
  (no carregamento ou só na hora da ação).
- Posição em que o pedido adicionado entra.
- De onde vem a lista do seletor de carro (sugestão: carros dos motoristas
  cadastrados, com placa e dono, primeiro; depois os veículos ativos da Vuupt com
  busca por placa).
- Se a troca de motorista/carro ganha linha de auditoria própria ou só log.

## Calls do Claude (o Hugo pode corrigir)

- `accepted` conta como "não começou" (o motorista aceitou a rota no app, mas não
  saiu para aquele pedido).
- A troca de motorista fica liberada até o primeiro pedido `on_route`/`arrived`/`done`,
  mesmo que o motorista antigo já tenha aceitado.
- Onde fica: no card da rota enviada na tela Planejamento, mais a correção da Torre.
- O "Cancelar pedido" em rota enviada segue a mesma regra e usa o DELETE (seção 7).
  Pedido cancelado não vira "Retornando": ele sai da operação.
- Envio automático sem carro sai e avisa no grupo Alertas (seção 4).
- "Retornando" só quando a rota já foi iniciada (definição da seção 2). Tirado de rota
  não iniciada volta direto para o pool.
- Freshhub fora desta entrega (seção 6a).
- Rodízio de SP na troca de carro: se a placa escolhida está no rodízio do dia e a
  rota tem parada no Centro Expandido (`roteirizacao/rodizio_sp.py`), a tela **avisa
  e pede confirmação**. Ela não bloqueia, porque a troca à mão é decisão de quem
  está operando.

## Como provar

- `py -3.11 -m unittest` dos testes novos (fakes da Vuupt): regra de status, trava
  de motorista em rota iniciada, rota sem carro barrada, remoção do último pedido.
  Testes do painel rodam de dentro de `painel_agentes/`.
- Prova real com o agente TESTE 28438, como as sondas de 10/11: rota enviada pela
  tela, trocar carro, trocar motorista (TESTE ↔ sem motorista), adicionar,
  tirar pendente, tentar tirar entregue (tem que recusar na tela).
- Na produção, depois do deploy: uma rota real de hoje com a troca de carro feita
  pela tela, conferida no GET da rota.
