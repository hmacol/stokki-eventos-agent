# Plano de Execução: App de Motoristas + Saída da VUUPT

**Status (26/08, 06h): FASES A e B DEPLOYADAS NA VPS (commit `1181053`) — API pública no ar, histórico de 30 dias carregado, usuário de teste do Hugo criado. Falta o teste no celular e as contas das lojas.** Este
documento é o roteiro combinado com o Hugo pra (1) criar um aplicativo
mobile próprio pros motoristas (App Store + Play Store) e (2) deixar de usar
a VUUPT, que hoje é ao mesmo tempo o banco oficial de pedidos/rotas, o app
do motorista (prova de entrega) e a fonte de status ao vivo da operação.

Mesmo formato do `DOC_EXECUCAO_CLAUDE_MIGRACAO_VPS.md`: decisões, situação
atual, arquitetura, fases, e um log de status no final que cada sessão
atualiza. Ler este arquivo ANTES de mexer em qualquer fase.

---

## 1. Decisões já tomadas com o Hugo (25/08)

| Pergunta | Decisão |
|---|---|
| Objetivo do app | Informar rotas, financeiro por dia, checklists de entrega, metrificar as rotas. |
| Continua na VUUPT? | **Não.** "Eu quero deixar de usar o Vuupt." O app substitui o app da VUUPT e o núcleo próprio substitui o banco dela. |
| Prazo | **Sem prazo** a princípio — sem contrato puxando cronograma. Qualidade e segurança da operação mandam. |
| Remuneração do motorista | **Fiorino: R$ 340,00 até 65 km + R$ 1,00/km adicional. HR e Van: R$ 550,00 até 100 km + R$ 1,00/km adicional.** (VUC, 3/4 e Truck: sem tarifa definida ainda — ver pendências.) |
| Time usa a web da VUUPT (`app.vuupt.com/manager`)? | **Sim, mas pouco.** O painel próprio (Torre/Planejamento/Expedição) precisa cobrir o pouco que falta antes do corte. |
| Modelo de migração | **Aprovado rota a rota:** piloto convive com a VUUPT; o Hugo escolhe no Planejamento qual rota vai pro app e qual vai pra VUUPT; a Torre lê as duas fontes. Nunca big bang. |
| Piloto | **Primeiro o próprio Hugo**, com um usuário dele no app e uma rota real de algum motorista REPLICADA (cópia, sem interferir na rota de verdade). Só depois 1-2 motoristas reais. |
| Stack do app | Expo (React Native) + EAS Build — uma base pra iOS e Android, push via Expo, atualização OTA. |
| Backend | Serviço Flask novo na MESMA VPS, sob `app.freshhub.com.br/<serviço>` (padrão do DOC_EXECUCAO_CLAUDE_MIGRACAO_VPS.md). Lê/grava direto em `dados/dados.db` (fonte de verdade desde o corte de 17/08). |
| Distribuição nas lojas | Apple **Unlisted App** (só com link) + Google Play **teste fechado** — app privado, não aparece em busca. Piloto via TestFlight / Internal Testing. |
| **Plataforma (26/08)** | **Só Android por enquanto** (decisão do Hugo). Distribuição por **APK direto** (EAS build `preview`, link por WhatsApp), sem Play Store e sem D-U-N-S. iPhone fica pra depois — exige conta Apple Developer (individual no nome do Hugo é o caminho rápido, transferível pra empresa). |

---

## 2. Situação atual (levantamento de 25/08)

### 2.1. O que a VUUPT é pra operação hoje — três papéis

| Papel | Evidência | O que substitui |
|---|---|---|
| **Banco oficial de pedidos e rotas** | `services`/`routes` são o registro. As tabelas locais `rotas`, `rotas_paradas`, `motoristas` existem em `dados.db` mas estão **vazias**; a Torre consulta tudo ao vivo (`torre_controle.py:41-43`). | Núcleo próprio (seção 3.2). **Maior buraco.** |
| **App do motorista / prova de entrega** | Aceitar rota, navegar, marcar entregue/insucesso + motivo (`failed_reason_id`), foto do canhoto (`GET /checklists/{id}/print`), timestamps `started_at/arrived_at/completed_at`. | App Expo (seção 3.4). |
| **Status ao vivo** | Torre, expedição no Stokki (`expedir_pedidos.py`), validação de canhoto por IA (`validar_checklists.py`), reentregas — tudo lê a VUUPT. | Vem de graça quando os dois de cima existirem. |

### 2.2. Acoplamento

88 arquivos `.py` tocam a VUUPT (1.302 ocorrências; ~30-40% são chamadas
reais, o resto doc/comentário). Três clientes concentram a comunicação
(`vuupt_client.py`, `roteirizacao/rotas_client.py`,
`roteirizacao/otimizacao_client.py`), mas **~25 módulos furam a abstração**
e montam URL/headers na mão. Dois domínios com o mesmo token:
`app.vuupt.com/api/v1` (services, customers, skills) e `api.vuupt.com/api/v1`
(routes, checklists, agents, vehicles).

Top-10 mais acoplados: `planejamento_rotas.py` (90), `rascunhos_rota.py`
(77), `vuupt_client.py` (65), `expedir_pedidos.py` (54), `torre_controle.py`
(53), `insucesso_entrega/expedir_pedidos.py` (51, gêmeo do anterior),
`pedidos_parados_triagem.py` (50), `pipeline.py` (45),
`validar_checklists.py` (40), `expedicao.py` (38). Os 39 arquivos de
`debug/` são descartáveis.

### 2.3. O que JÁ é nosso (não se perde ao sair)

- **Roteirização** (agrupamento + sequência): 100% em `roteirizacao/`. O
  solver `POST /route-optimization` da VUUPT está fora do fluxo desde 03/08.
  A VUUPT recebe a rota pronta.
- **Geocodificação**: Google Geocoding + cache `geocache` (3.750 endereços).
  A VUUPT só geocodifica como rede de segurança quando o Google falha
  (`ZERO_RESULTS`) — medir quantos são antes do corte.
- **Cadastro de clientes** (`clientes`, 5.378) e **de motoristas**
  (`dados/BD_MOTORISTAS.xlsx`, VUUPT é só leitura via `GET /agents`).
- **Rascunhos de rota** (`rascunhos_rota`/`rascunhos_parada`): a rota já
  nasce e é editada localmente; só o resultado aprovado vai pra VUUPT.
- **Confirmação e marketplace de rotas** (`confirmacao_motoristas/`, em
  produção em `confirmacao.freshhub.com.br`) — viram telas do app.
- **Catálogo de motivos de ocorrência** (`motivos_ocorrencia`, 22 linhas,
  com de-para pro `failed_reason_id` da VUUPT).

### 2.4. O que só a VUUPT tem hoje (e que o app passa a gerar melhor)

1. Foto do canhoto + assinatura (só como PDF baixado dela).
2. `started_at/arrived_at/completed_at` por parada — **ruidosos**: o
   motorista confirma em lote e o tempo no local vira zero
   (`revisar_complexidade_entrega.py:19-26`).
3. Motivo do insucesso escolhido pelo motorista.
4. Histórico de rotas passadas (nenhum, local).
5. `stat_average_time_on_site` por cliente.

Não consumimos GPS/posição do motorista em lugar nenhum — o app passa a
ter isso.

### 2.5. Desenho anterior reaproveitável (junho/2026, projeto `C:\agente_relatorio`)

Já existiu um desenho de app de motoristas ("Fase 1 do módulo de
roteirização", `agente_relatorio/agentes/roteirizacao/adicionar_roteirizacao.py`),
cujas tabelas foram criadas em `dados.db` e nunca usadas. **Reaproveitamos:**

- `motoristas` (cpf PK, `pin_hash`, `pin_salt`, telefone, ativo,
  `ultimo_login_em`) — vazia, esquema exato pro login CPF + PIN.
- `checklist_modelo` (16 linhas) — fluxos ENTREGUE / PARCIAL / NAO_ENTREGUE
  com nome do recebedor, vínculo (Próprio cliente, Filho, Porteiro, Zelador,
  Conferente, Gerente, Outro), documento, foto do canhoto; PARCIAL exige
  motivo + foto da NF de devolução + foto do produto devolvido.
- `motivos_ocorrencia` (22 linhas, categoria + `acao_padrao` + de-para VUUPT).
- `roteirizacao_config` — **já tinha a tarifa: `valor_base=340`,
  `km_franquia=65`, `valor_km_adicional=1.00`** (bate com a regra Fiorino
  confirmada em 25/08) e CD geocodificado (`-23.497039, -46.6605211`, Rua
  Zilda 288, Casa Verde Alta).
- Tabelas `rotas`/`rotas_paradas` do desenho antigo (`motorista_cpf`,
  `km_ida`/`km_volta`, `conta_volta_*`, `valor_inicial`/`valor_final`,
  `pedagio_informado`) — **não reaproveitadas**: chave por CPF em vez de
  `agent_id`, sem `service_id`, sem eventos. Ficam intocadas (0 linhas) até
  o Hugo autorizar o DROP. O núcleo novo usa prefixo `nucleo_`.

Regra de km do desenho antigo (a confirmar com o Hugo, seção 4): a volta
ao CD só conta quando houve insucesso ou parada fora da Grande SP
(`conta_volta_final`), e o motorista informa o pedágio no fechamento.

### 2.6. Gargalos que não são técnicos

- **Cadastro de motoristas incompleto**: CPF/telefone preenchidos pra
  minoria em `BD_MOTORISTAS.xlsx`. Sem CPF não há login. `sincronizar_cpf_motoristas.py`
  puxa da VUUPT o que ela tem.
- **Contas nas lojas**: Apple Developer (US$ 99/ano) e Google Play (US$ 25)
  como empresa exigem **número D-U-N-S** da FRESHLOG LOGISTICA LTDA —
  maior lead time do projeto. Iniciar já.
- `dados.db` roda em `journal_mode=delete`. Com o app escrevendo
  concorrentemente (20 motoristas × ~15 paradas/dia) precisa virar **WAL**
  na VPS antes do piloto (`PRAGMA journal_mode=WAL`, uma vez, com os
  serviços parados).

---

## 3. Arquitetura alvo

```
Stokki (WMS) ──pipeline.py──▶ NÚCLEO PRÓPRIO (dados.db na VPS)  ◀──API JSON──▶  App Expo (iOS/Android)
                               nucleo_pedidos · nucleo_rotas ·                     │ fotos / assinatura
                               nucleo_paradas · nucleo_eventos ·                   ▼
                               nucleo_comprovantes · motoristas (PIN)          GCS (bucket existente)
                                        ▲
                     Painel: Torre · Planejamento · Expedição · Financeiro
                     validação IA · expedição Stokki · reentregas · relatórios
```

### 3.1. Princípios

1. **Nada que roda hoje muda de comportamento até a Fase C.** Fase A só
   acrescenta gravação paralela (dual-write) e um job de leitura.
2. **Uma fonte de verdade por rota** — cada rota tem um `provedor`
   (`VUUPT` ou `APP`). Nunca as duas ao mesmo tempo pra mesma rota.
3. **Guardar o payload bruto** da VUUPT (`dados_json`) em tudo que é
   sincronizado — migração sem perder campo que ainda não sabíamos que
   importava.
4. **Best-effort nos ganchos**: falha do núcleo nunca derruba o envio à
   VUUPT nem o pipeline (log de warning e segue).

### 3.2. Núcleo de dados (`nucleo/`)

Tabelas novas em `dados/dados.db`, `CREATE TABLE IF NOT EXISTS` +
migração aditiva por `PRAGMA table_info`, mesmo padrão de
`rascunhos_rota.py`:

| Tabela | Chave | Conteúdo |
|---|---|---|
| `nucleo_pedidos` | `codigo` (PS-XXXXX) | Espelho do pedido: título, destinatário, endereço, lat/lng, remetente (`sender_id`), caixas, agendamento, `vuupt_service_id`, status próprio, `dados_json`. Gravado pelo `pipeline.py` (dual-write) e pelo sync. |
| `nucleo_rotas` | `id` | Rota materializada: `data_rota`, nome, `provedor` (VUUPT/APP), `vuupt_route_id`, `rascunho_id`, `agent_id`, `vehicle_id`, motorista, `tipo_veiculo`, `start_at`, `km_estimado`, `km_real`, status (PLANEJADA→ACEITA→EM_ROTA→CONCLUIDA/CANCELADA), timestamps. |
| `nucleo_paradas` | `id` (único por `rota_id+service_id`) | Parada: ordem, `service_id`, código, título, endereço, lat/lng, nível, caixas, janela; resultado (`situacao` PENDENTE/ENTREGUE/PARCIAL/INSUCESSO/CANCELADA, `motivo_id`, `completed_at`, `arrived_at`, `started_at`), `dados_json`. |
| `nucleo_eventos` | `id` | Log imutável: `rota_id`, `parada_id`, tipo (ROTA_ACEITA, INICIO_ROTA, CHEGADA, ENTREGUE, INSUCESSO, FOTO, GPS, ...), `ocorrido_em` (relógio do aparelho), `recebido_em` (servidor), lat/lng, `origem` (APP/VUUPT_SYNC/PAINEL), `dados_json`. É a base do financeiro e das métricas. |
| `nucleo_comprovantes` | `id` | Fotos/assinatura por parada: tipo (CANHOTO, NF_DEVOLUCAO, PRODUTO, ASSINATURA, OCORRENCIA), caminho GCS, hash, `validado_em`, `validado_por` (IA/humano). |
| `motoristas` (existente) | `cpf` | Ganha `agent_id`, `vehicle_id`, `tipo_veiculo`, `push_token`, `tentativas_pin`, `bloqueado_ate` — aditivo. |
| `tarifas_motorista` | `tipo_veiculo` | Tabela editável: valor base, km franquia, R$/km adicional, vigência. Semeada com Fiorino/VAN_HR da seção 4. |

`nucleo_eventos` é o que a VUUPT nunca deu: cada toque do motorista com
horário real e coordenada. As métricas ("tempo no local", "km real",
"eficácia por parada") são consultas sobre ele.

### 3.3. API do motorista (Fase B)

Serviço Flask `nucleo/api_motorista.py`, `app.freshhub.com.br/motorista/api`,
porta interna 8073, systemd `motorista-api.service`, Caddy `handle_path`.

- `POST /login` (CPF + PIN → JWT curto + refresh; 5 tentativas → bloqueio 15 min, igual `confirmacao_motoristas`).
- `GET /rota/hoje`, `GET /rota/amanha` (rota atribuída, com paradas na ordem; endereço completo só depois de ACEITA — regra atual do resumo).
- `POST /rota/{id}/aceitar|recusar`, `GET /ofertas`, `POST /ofertas/{id}/escolher` (marketplace — reaproveita `regras/ofertas_rota.py`).
- `POST /parada/{id}/evento` (CHEGADA/ENTREGUE/PARCIAL/INSUCESSO com checklist do `checklist_modelo`; idempotente por `uuid` gerado no aparelho — fila offline reenvia sem duplicar).
- `POST /parada/{id}/comprovante` (multipart → GCS `pedidos/{codigo}/Canhoto/...`, mesmo padrão de `documentos_pedido/storage_gcs.py`).
- `POST /gps` (lote de pontos), `POST /push-token`.
- `GET /financeiro?mes=` (extrato por dia/rota, seção 4).
- `GET /disponibilidade`, `PUT /disponibilidade` (reaproveita `regras/disponibilidade_motoristas.py`).

### 3.4. App Expo (Fase B)

Telas: Login (CPF + PIN) · Hoje (rota, progresso, botão iniciar) · Parada
(endereço, telefone, caixas, navegar no Google Maps/Waze por deep-link,
Entregue/Parcial/Insucesso → checklist com câmera e assinatura na tela) ·
Amanhã (aceitar/recusar) · Ofertas (marketplace) · Disponibilidade ·
Financeiro (dia/semana/mês) · Perfil. Fila offline obrigatória no v1
(SQLite local, reenvio com backoff). GPS em rota (foreground; background
só se o Hugo pedir — consumo de bateria e review da Apple).

### 3.5. Provedor de rota (o que permite migrar rota a rota)

`rascunhos_rota` ganha coluna `provedor` (`VUUPT` padrão / `APP`). O botão
"Confirmar e Enviar" do Planejamento passa a respeitar o provedor: VUUPT →
`rotas_client.criar_rota` como hoje; APP → só `nucleo.rotas.materializar`.
A Torre agrega `_coletar_rotas_dia` (VUUPT) + `nucleo.rotas.listar_dia`
(APP) na mesma lista. Expedição no Stokki (`expedir_pedidos.py`) passa a
ler entregues do núcleo (`nucleo_paradas.situacao=ENTREGUE` com comprovante)
além da VUUPT.

---

## 4. Regra financeira (confirmada 25/08)

| Veículo do motorista (`TIPO_VEICULO`) | Base | Franquia | Adicional |
|---|---|---|---|
| Fiorino / utilitário pequeno (`FIORINO`, ou coluna vazia) | R$ 340,00 | 65 km | R$ 1,00/km |
| HR / Van (`VAN_HR`) | R$ 550,00 | 100 km | R$ 1,00/km |
| VUC, 3/4, Truck | **sem tarifa definida** — `valor=None`, extrato mostra "a definir" | — | — |

Implementação: `regras/tarifa_motorista.py` (`calcular_valor_rota(tipo_veiculo, km)`),
valores em `tarifas_motorista` (editável) com os defaults acima no código.

**Km considerado:** `km_real` do app quando existir (GPS, Fase B); até lá,
`km_estimado` do rascunho (`rascunhos_rota.km_estimado`, Google Directions
ida + paradas). Coluna `km_fonte` guarda qual foi usado.

**A confirmar com o Hugo** (não bloqueia a Fase A):
1. O km é ida + volta ao CD sempre, ou a volta só conta com insucesso /
   parada fora da Grande SP (regra do desenho de junho)?
2. Pedágio: reembolsado à parte, informado pelo motorista no fechamento?
3. Rota com insucesso paga integral? Reentrega (`-R1`) conta como rota nova?
4. Tarifas de VUC / 3/4 / Truck.
5. Coluna `TIPO_VEICULO` da planilha hoje só tem os códigos de veículo
   grande; "FIORINO" passa a ser aceito como valor explícito (vazio = Fiorino).

---

## 5. Fases

### Fase A — Núcleo próprio + histórico (sem tocar no que roda) — CÓDIGO PRONTO, FALTA DEPLOY
Entregáveis:
- [x] `nucleo/banco.py` — esquema da seção 3.2, idempotente, com migração aditiva de `motoristas` (+ `agent_id`, `tipo_veiculo`, `push_token`, `tentativas_pin`, `bloqueado_ate`, `perfil`).
- [x] `regras/tarifa_motorista.py` (Fiorino 340/65, VAN_HR 550/100, R$1/km; VUC/3-4/Truck → `None`) + `tarifas_motorista` semeada por `semear_tarifas_padrao` + testes. `preferencias_motoristas` aceita "FIORINO"/"UTILITARIO" na planilha sem aviso.
- [x] `nucleo/rotas.py` — `registrar_rota_enviada(rascunho_id, vuupt_route_id)` chamado (best-effort) em `rascunhos_rota.marcar_enviado`; `materializar_rascunho(provedor=APP)` já pronto pra Fase C; consultas `listar_rotas_dia`, `buscar_rota`, `listar_rotas_motorista`.
- [x] `nucleo/sincronizar_vuupt.py` — CLI `--dias N` / `--data` / `--modo-teste`; timer `infra/stokki-nucleo-sincronizar-vuupt.{service,timer}` (a cada 30 min, :15/:45). Gera `nucleo_eventos` com `origem=VUUPT_SYNC`.
- [x] `nucleo/financeiro.py` — `extrato_motorista(agent_id, ini, fim, tipo_veiculo_motorista)` e `fechamento_periodo(ini, fim, tipo_por_agent)`.
- [x] Dual-write de pedidos em `pipeline.py` → `nucleo/pedidos.py::registrar_importacao` (best-effort).
- [x] 16 testes sem rede em `nucleo/test_nucleo_fase_a.py` (`python -m unittest nucleo.test_nucleo_fase_a`); 32+6 testes existentes continuam verdes.
- [x] **Validado contra a VUUPT real (26/08, numa CÓPIA do banco local):** 4 dias = 51 rotas / 487 paradas; 2ª rodada = 0 eventos novos (idempotente).
- [ ] **Deploy na VPS** (ação de produção, não tomada sozinha — ver 5.A.1).
- [ ] Backfill `--dias 30` na VPS e conferência com a Torre (critério de aceite).

**Achados da validação com dados reais (26/08):**
- Status brutos da VUUPT de rota: `not_started`, `assigned`, `started`, `finished`, `canceled` (a rota traz `started_at`/`finished_at`/`canceled_at` próprios — usados em `iniciada_em`/`concluida_em`/`cancelada_em`). De serviço: `assigned`, `accepted`, `not_assigned`, `on_route`, `done`+`status_done` (`success`/`failed`), `canceled`. O serviço traz `accepted_at`, `arrived_at`, `started_at`, `completed_at` e `stat_time_until_*` — tudo preservado em `dados_json`.
- **Rota CANCELADA lista os serviços com o status GLOBAL deles** — que já foram entregues em OUTRA rota (re-roteirização: 9/9, 12/12, 13/13… casos, todos 100% presentes numa rota ativa). Contar isso duplicava 65 entregas em 4 dias. Regra adotada: parada de rota cancelada = `CANCELADA` nesta rota, sem contador nem evento; o pedido não é alterado. Vale pro financeiro: rota cancelada nunca paga.
- `rascunho_id`/`km_estimado` só vinculam onde `rascunhos_rota` existe — na VPS (o `dados.db` local está congelado desde 17/08). Localmente todo extrato sai com `km_desconhecido` (só a base). Conferir na VPS após o backfill.

#### 5.A.1. Deploy da Fase A na VPS (checklist)
1. `git pull` em `/opt/stokki-eventos` (nada novo em `requirements.txt`).
2. `cp infra/stokki-nucleo-sincronizar-vuupt.* /etc/systemd/system/ && systemctl daemon-reload && systemctl enable --now stokki-nucleo-sincronizar-vuupt.timer`.
3. Backfill: `sudo -u www-data /opt/stokki-eventos/venv/bin/python nucleo/sincronizar_vuupt.py --dias 30` (cria as tabelas na 1ª execução).
4. Semear tarifas: `python -c "from nucleo import banco; from regras import tarifa_motorista as t; c=banco.conectar(); t.semear_tarifas_padrao(c)"`.
5. Conferir um dia contra a Torre (`nucleo.rotas.listar_rotas_dia(hoje)` × `/torre`).
6. **Antes do piloto (Fase B/C), não agora:** `PRAGMA journal_mode=WAL` com os serviços parados.
O painel e o pipeline passam a chamar o núcleo automaticamente após o `git pull`
(ganchos best-effort) — sem restart obrigatório dos timers; `painel-agentes.service`
precisa de restart pra carregar o `rascunhos_rota.py` novo.

### Fase B — App MVP + API — CÓDIGO PRONTO (26/08), FALTA RODAR NO CELULAR DO HUGO
Entregáveis:
- [x] **API** `nucleo/api_motorista.py` (Flask, factory `criar_app`) + `nucleo/auth_motorista.py` (CPF+PIN, PBKDF2, 5 erros → 15 min, tokens itsdangerous acesso 12h / refresh 30d, trocar PIN invalida sessões) + `nucleo/operacao.py` (regras: aceitar/recusar/iniciar/finalizar, eventos de parada idempotentes por uuid, comprovantes, GPS → km real, ofertas com claim atômico, disponibilidade, checklist). Endpoints da seção 3.3 todos implementados.
- [x] **CLI do piloto** `nucleo/motoristas_cli.py`: `criar` (usuário TESTE do Hugo, agent_id 999001), `replicar-rota` (copia rota real como rota APP, original intacta), `importar-planilha` (motoristas reais com CPF, PIN aleatório impresso 1x), `resetar-pin`, `listar`.
- [x] **App Expo** `app_motorista/` (SDK 57, expo-router, TypeScript estrito, `tsc` limpo): login, Rotas (hoje/amanhã/ontem, cache offline), detalhe da rota (aceitar/recusar/iniciar/finalizar, navegar Google Maps/Waze, ligar), Parada (Entregue/Parcial/Não entregue com checklist do `checklist_modelo`, fotos pela câmera, assinatura na tela, motivo do `motivos_ocorrencia`), Ofertas (marketplace), Financeiro (semana/mês/anterior + tarifa), Agenda (disponibilidade 28 dias), Perfil (fila pendente, sair). **Fila offline** com uuid + estado otimista + GPS em primeiro plano. Ver `app_motorista/README.md`.
- [x] Infra: `infra/motorista-api.service` (waitress 8073) + `infra/Caddyfile-motorista` (`handle_path /motorista/*`).
- [x] Testes: `nucleo/test_api_motorista.py` (7 cenários: bloqueio de PIN, refresh, fluxo completo com GPS/foto/insucesso/conclusão automática, rota VUUPT somente leitura + confirmação, ofertas, disponibilidade, checklist). Smoke ponta a ponta com rota REAL replicada numa cópia do banco: login → aceitar → iniciar → entrega → extrato R$340.
- [ ] `api_motorista.secret_key` no `config.yaml` (local e VPS) — arquivo de segredos do Hugo, não mexi.
- [ ] Deploy da API na VPS (checklist 5.B.1) + rodar no celular via Expo Go.
- [ ] ~~Contas Apple/Google (D-U-N-S)~~ → **Android só, APK direto** (26/08). Pré-requisitos: conta gratuita em expo.dev (Hugo) + `npx eas-cli login` nesta máquina; depois `npx eas-cli init` (grava `extra.eas.projectId`), `npx eas-cli build -p android --profile preview` → link do APK. `eas.json` já tem os perfis `preview` (APK) e `production` (AAB, só se a Play Store entrar). Push no Android exige credenciais FCM (projeto Firebase gratuito + `google-services.json` + `eas credentials`) — o app funciona sem, só não recebe push.
- [x] **APK v1.0.0 gerado (26/08, build `a41b9050`)**: https://expo.dev/artifacts/eas/0VgatpBWFYnCwoD6Adeg2ZEQrCyJK0fkMSyRJyL_yTE.apk — conta Expo `freshlogbr`, projeto `89779e04-…`, keystore na nuvem. Builds 1-2 falharam por `package-lock.json` dessincronizado (`npm ci` da nuvem) — corrigido com `.npmrc` (`legacy-peer-deps=true`) + lock regenerado; **sempre rodar `npm ci --include=dev` local antes de `eas build`**.
- [ ] Atualizações OTA: `npx eas-cli update --channel preview --message "..."` publica mudança de JS sem novo APK (o `preview` do `eas.json` já aponta pro canal; o APK acima já embute `expo-updates`).
- [ ] Login do EAS nesta máquina não persiste: usar `EXPO_TOKEN` (token criado em expo.dev → Account settings → Access tokens) por sessão; revogar o token usado em 26/08.
- [ ] Decisões pendentes da seção 4 (km ida/volta, pedágio, VUC/3-4/Truck).

**Ajustes de design (26/08, pedidos do Hugo):** paleta da marca (mesma
do painel) + logo no login + ícones/splash da folha; na tela da rota as
ações principais são por **arrastar pra direita** (`src/deslizar.tsx`:
aceitar, iniciar rota, finalizar) e cada parada tem os passos **Iniciar
deslocamento → Cheguei no local → Resultado** (eventos `DESLOCAMENTO`
→ `started_at`, `CHEGADA` → `arrived_at`, situação `EM_DESLOCAMENTO`
nova em `nucleo_paradas`). Publicado por OTA no canal `preview`; ícone/
splash exigem novo APK (pendente, aguardando o fim do lote de design).

**Limitações conhecidas do v1 (decisões, não bugs):** GPS só com o app aberto (background exige justificativa na review da Apple e gasta bateria — avaliar na Fase C); telefone/janela do destinatário na parada só aparecem quando o pedido passou pelo `pipeline.py` com dual-write (rotas só do backfill vêm sem); push só funciona depois do `eas init` (projectId); a recusa de rota exige sinal (é rara e a operação precisa saber na hora).

#### 5.B.1. Deploy da API na VPS (checklist)
1. `git pull`; acrescentar `api_motorista: {secret_key: "<64 hex aleatórios>", porta: 8073}` no `/opt/stokki-eventos/config.yaml`.
2. `cp infra/motorista-api.service /etc/systemd/system/ && systemctl daemon-reload && systemctl enable --now motorista-api`.
3. Bloco do `infra/Caddyfile-motorista` dentro do site `app.freshhub.com.br` no `/etc/caddy/Caddyfile` + `systemctl reload caddy`. Testar `https://app.freshhub.com.br/motorista/api/saude`.
4. `mkdir -p dados/comprovantes && chown www-data`. (`gcs` do config já sobe as fotos pro bucket `freshlog-documentos-pedidos`, caminho `pedidos/{codigo}/Canhoto/...`, o mesmo dos documentos.)
5. Usuário do Hugo: `python nucleo/motoristas_cli.py criar --cpf <cpf real ou 00000000000> --nome "Hugo" --pin <6 dígitos> --perfil TESTE`; replicar uma rota do dia: `python nucleo/motoristas_cli.py replicar-rota --vuupt-route-id <id> --para-cpf <cpf>`.
6. No celular: Expo Go + `npx expo start` na máquina do Hugo (apiUrl já aponta pra produção no `app.json`). Build de verdade só com as contas das lojas.
7. **WAL antes do piloto com motoristas reais** (seção 2.6).

### Fase C — Piloto real rota a rota (2-4 semanas)
- Coluna `provedor` em `rascunhos_rota` + escolha no Planejamento; Torre com as duas fontes; `validar_checklists.py` lendo `nucleo_comprovantes`; `expedir_pedidos.py` expedindo no Stokki a partir do núcleo.
- 1-2 motoristas escolhidos pelo Hugo. Aceite: uma semana sem intervenção manual.

### Fase D — Migrar o resto (3-4 semanas)
- Insucesso/reentregas (`aplicar_resposta_insucesso.py`, cadeia `-R1`), reagendamento e edição de endereço no Planejamento, relatórios (`relatorio_operacional.py`, `dashboard_embarcadores`), notificação de transportadoras, pedidos parados, romaneios, coleta Quatro Estrelas.
- Consolidar os ~25 módulos que montam URL na mão atrás de uma interface `Provedor`.

### Fase E — Corte
- 100% das rotas com `provedor=APP`; timers da VUUPT desligados; token revogado; `debug/` e clientes VUUPT arquivados. Financeiro e métricas já nascem no núcleo.

---

## 6. Riscos

- **Adoção**: o app tem que ser mais simples que o da VUUPT. Piloto com quem colabora.
- **Campo sem sinal**: fila offline no v1.
- **Concorrência no SQLite**: WAL antes do piloto; se um dia doer, Postgres na mesma VPS (o esquema já nasce compatível).
- **Review da Apple**: app "só pra funcionários" às vezes é questionado — Unlisted resolve; ter conta demo pro revisor.
- **Trabalho paralelo**: Hugo commita ao vivo na mesma pasta — `git status` antes de editar, sempre.

---

## 7. Log de status

- **25/08** — Levantamento completo (2 sub-agentes). Decisões da seção 1
  fechadas com o Hugo. Desenho de junho encontrado e reaproveitado (seção
  2.5). Fase A iniciada.
- **26/08 (madrugada)** — Fase A codificada e testada (16 testes novos +
  regressão verde), validada contra a VUUPT real numa cópia do banco
  (51 rotas/4 dias, idempotente). Achado das rotas canceladas com status
  global corrigido. **Nada commitado nem deployado** — Hugo revisa o diff
  e decide o deploy (checklist 5.A.1). Próximo passo técnico: Fase B
  (API do motorista + app Expo) em paralelo com D-U-N-S/contas das lojas.
- **26/08 (manhã)** — Hugo: "vamos seguir pra próxima fase". Fase B
  codificada: API + auth + operação + CLI do piloto + app Expo completo
  (typecheck limpo) + 7 testes de API + smoke com rota real replicada.
  Nada commitado/deployado; `secret_key` da API precisa entrar no
  `config.yaml` (Hugo). Próximo: deploy 5.A.1 + 5.B.1, Hugo testa no
  celular via Expo Go, depois contas das lojas → EAS build.
- **26/08 (06h)** — Hugo: "me ajuda a fazer minha parte". **DEPLOYADO NA
  VPS** (commit `1181053`): `secret_key` gravada nos dois `config.yaml`
  (backup `config.yaml.bak-26-08` na VPS), `motorista-api.service` ativo
  na 8073, bloco `/motorista/*` no Caddy (backup `Caddyfile.bak-26-08`),
  `https://app.freshhub.com.br/motorista/api/saude` OK, timer
  `stokki-nucleo-sincronizar-vuupt` a cada 30 min (:15/:45), backfill de
  30 dias = 406 rotas / 3.620 paradas / 3.307 eventos (145 rotas com
  rascunho + km vinculados), tarifas semeadas, `painel-agentes` reiniciado
  com o gancho. Usuário TESTE do Hugo (CPF `00000000000`, agent_id 999001,
  PIN passado no chat) + rota real de hoje replicada como APP #407
  (17 paradas). Login público testado. Falta: Hugo testar no celular
  (Expo Go), D-U-N-S/contas, decisões de km/pedágio, WAL antes do piloto real.