# Mapa da rota no app dos motoristas — plano de implementação

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Tela "Mapa da rota" no app dos motoristas (mapa nativo Google, pinos numerados, trajeto pelas ruas, cartão com os passos e a baixa), mais o aviso "baixe a versão nova" pros aparelhos no APK 1.1.1.

**Architecture:** O servidor calcula o trajeto pela Routes API (chave `google_maps.api_key` que já existe), guarda por rota numa tabela nova e entrega em `GET /api/rotas/<id>/trajeto`. O app desenha com `react-native-maps` (APK 1.2.0) e reaproveita a lógica de passos da tela da rota, movida pra um módulo compartilhado. O aviso de versão sai antes, por OTA, pro 1.1.1.

**Tech Stack:** Python 3.11 + Flask + SQLite (`nucleo/`), `requests` na Routes API v2; Expo SDK 57 + expo-router + TypeScript + react-native-maps; EAS Build/Update.

**Spec:** `docs/superpowers/specs/2026-11-18-mapa-da-rota-app-motorista-design.md`

## Global Constraints

- Ramo próprio `mapa-rota-app` num worktree (o Hugo e outras sessões mexem no master ao vivo). `git add` só dos arquivos da tarefa; nunca `-A`.
- Commit/push/PR/deploy **só com o OK do Hugo** em cada ponto marcado "PARA: OK do Hugo".
- Python sempre `py -3.11`; testes `py -3.11 -m unittest <modulo>` a partir da raiz; nunca misturar `nucleo` com `painel_agentes`.
- `config.yaml` nunca é lido no chat nem alterado; chave do Google do app **nunca** no git (vai como variável de ambiente do EAS `GOOGLE_MAPS_ANDROID_KEY`).
- APK 1.2.0 **não sai antes de sexta 20/11** (fim do piloto do Clayson e do Erick no 1.1.1).
- A OTA do aviso pro 1.1.1 sai do **master com `version` 1.1.1**, sem o código do mapa.
- Base/CD: `regras.km_cobrado.COORD_CD = (-23.497039, -46.6605211)`.
- Trajeto = CD → paradas `situacao != 'CANCELADA'` com coordenada, por `ordem` → CD (mesma régua de `nucleo/recalcular_km_rotas.recalcular_rota`).
- Fontes do trajeto: `"GOOGLE_ROUTES"` (guardado) | `"LINHA_RETA"` (não guardado).
- Cartão: ações sempre `Deslizar` (regra do Hugo de 26/08), troca de cartão por setas ‹ › e toque no pino; Entregue / Não entregue só depois do "Cheguei no local".
- Link fixo do APK: `https://app.freshhub.com.br/motorista/app.apk`.
- Comentários/strings no padrão de cada arquivo; textos de tela em português com acento.
- Mudou arquivo/rotina/tela → atualizar `MAPA_DO_SISTEMA.txt` (seção 13 e a da API) no mesmo commit.

## Review Focus

- **Parada sem coordenada** (`latitude`/`longitude` nulos): fica fora do trajeto e sem pino, mas aparece no cartão com "sem localização no mapa"; nada quebra. → teste na Tarefa 2 + roteiro manual da Tarefa 7.
- **Rota com mais de 25 paradas** (a maior do último mês teve 47): trajeto partido em chamadas encadeadas, sem ponto repetido na emenda. → teste na Tarefa 1.
- **Rota editada depois do trajeto guardado** (ordem/paradas mudam): a assinatura muda e o trajeto é recalculado; reagendar (só muda situação) **não** recalcula. → teste na Tarefa 2.
- **Google fora / sem chave**: resposta em `LINHA_RETA` na hora, sem gravar, e a próxima abertura tenta o Google de novo. → teste na Tarefa 2.
- **Sem internet ao abrir o mapa**: usa o último trajeto guardado no aparelho; sem nenhum, liga os pinos em linha reta; nunca tela vermelha. → roteiro manual da Tarefa 7.

---

### Tarefa 1: Routes API devolve o desenho do trajeto

**Files:**
- Modify: `roteirizacao/km_rodoviario.py` (constantes no topo; funções novas depois de `calcular_trajeto`)
- Test: `roteirizacao/test_km_rodoviario_polyline.py` (novo)

**Interfaces:**
- Produces:
  - `FIELD_MASK_POLYLINE = "routes.polyline.encodedPolyline"`
  - `decodificar_polyline(codificada: str) -> list[tuple[float, float]]` — algoritmo "Encoded Polyline" do Google, precisão 5.
  - `tracar_google(pontos: list[tuple[float, float]], api_key: str, timeout: float = TIMEOUT_S) -> list[tuple[float, float]]` — `pontos` já inclui origem e destino (o chamador põe o CD nas pontas). Usa `_post_com_mascara(corpo, api_key, timeout, FIELD_MASK_POLYLINE)` com o mesmo corpo de `calcular_trajeto_google` sem `extraComputations`. Particiona igual a `calcular_km_google` (até `MAX_INTERMEDIARIAS_POR_CHAMADA` intermediárias por chamada; origem da próxima = destino da anterior) e concatena descartando o 1º ponto de cada trecho depois do primeiro. Levanta em falha (sem chave, sem `routes`, sem polyline, menos de 2 pontos).

- [ ] **Step 1: Testes que falham** (`unittest`, `mock.patch.object(km_rodoviario, "_post_com_mascara")`, mesmo `sys.path` de `nucleo/recalcular_km_rotas.py`):
  - `test_decodifica_exemplo_do_google`: ``decodificar_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@")`` == `[(38.5, -120.2), (40.7, -120.95), (43.252, -126.453)]` (comparar com `assertAlmostEqual(..., places=5)`).
  - `test_uma_chamada_ate_25_intermediarias`: 27 pontos (CD + 25 + CD) → `_post_com_mascara` chamado 1 vez, com `len(corpo["intermediates"]) == 25` e 4º argumento `FIELD_MASK_POLYLINE`.
  - `test_47_paradas_partem_e_nao_repetem_emenda`: 49 pontos → 2 chamadas; o mock devolve polylines cujo último ponto de um trecho = 1º do seguinte; resultado não tem dois pontos consecutivos iguais e começa/termina nos pontos devolvidos.
  - `test_sem_chave_levanta` e `test_resposta_sem_routes_levanta`: `ValueError`.
- [ ] **Step 2:** `py -3.11 -m unittest roteirizacao.test_km_rodoviario_polyline -v` → FALHA (nomes não existem).
- [ ] **Step 3:** Implementar `FIELD_MASK_POLYLINE`, `decodificar_polyline`, `tracar_google` em `roteirizacao/km_rodoviario.py`. Não mexer em `_post`/`calcular_km*`.
- [ ] **Step 4:** Mesmo comando → PASSA. E `py -3.11 -m unittest roteirizacao.test_pedido_cabe_no_trajeto nucleo.test_nucleo_fase_a` continua passando.
- [ ] **Step 5:** Commit no ramo: `Roteirizacao: Routes API devolve o desenho do trajeto (polyline) pro mapa do app`.

### Tarefa 2: Trajeto guardado por rota + rotas na API do motorista

**Files:**
- Create: `nucleo/trajeto_rota.py`
- Modify: `nucleo/banco.py` (`_DDL`: tabela nova), `nucleo/api_motorista.py` (2 rotas novas + constantes), `MAPA_DO_SISTEMA.txt`
- Test: `nucleo/test_trajeto_rota.py` (novo; setUp copiado do `TestApiMotorista` de `nucleo/test_api_motorista.py`, incluindo `_rascunho`/`_rota_app`)

**Interfaces:**
- Consumes: `km_rodoviario.tracar_google`, `regras.km_cobrado.COORD_CD`, `operacao.rota_do_motorista`.
- Produces:
  - Tabela `nucleo_rotas_trajeto (rota_id INTEGER PRIMARY KEY, assinatura TEXT NOT NULL, pontos_json TEXT NOT NULL, fonte TEXT NOT NULL, criado_em TEXT NOT NULL)` em `banco._DDL`.
  - `trajeto_rota.FONTE_GOOGLE = "GOOGLE_ROUTES"`, `FONTE_LINHA_RETA = "LINHA_RETA"`.
  - `pontos_da_rota(conn, rota_id: int, base=COORD_CD) -> list[tuple[float, float]]` — CD + paradas válidas por `ordem` + CD; `[]` se nenhuma parada tem coordenada.
  - `assinatura(pontos) -> str` — sha1 do JSON dos pontos arredondados a 6 casas.
  - `trajeto(conn, rota_id: int, api_key: str | None) -> dict` → `{"pontos": [[lat, lng], ...], "fonte": str, "assinatura": str, "base": [lat, lng]}`. Cache: linha da tabela com a mesma assinatura → devolve sem chamar o Google. Senão `tracar_google`; sucesso → grava (upsert) com `GOOGLE_ROUTES`; falha/sem chave → devolve os próprios `pontos_da_rota` com `LINHA_RETA`, **sem gravar**, log `warning`. Sem pontos → `{"pontos": [], "fonte": "LINHA_RETA", ...}` sem chamar nada.
  - `GET /api/rotas/<int:rota_id>/trajeto` (`@requer_motorista`): `operacao.rota_do_motorista(conn(), rota_id, agent_id())` (404 de outro motorista) → `jsonify(trajeto_rota.trajeto(conn(), rota_id, chave))`, chave = `(app.config["CONFIG_PROJETO"].get("google_maps") or {}).get("api_key")`.
  - `GET /api/app-versao` (sem login): `{"versao_atual": APK_VERSAO_ATUAL, "link": APK_LINK}` com constantes de módulo `APK_VERSAO_ATUAL: str | None = None` e `APK_LINK = "https://app.freshhub.com.br/motorista/app.apk"`. `None` = sem aviso. (Liga-se na Tarefa 9 trocando a constante por commit.)

- [ ] **Step 1: Testes que falham** (`mock.patch.object(km_rodoviario, "tracar_google")`; por isso `trajeto_rota` chama `km_rodoviario.tracar_google(...)` pelo módulo, sem `from ... import`):
  - `test_trajeto_google_e_guardado`: rota com 2 paradas → 1ª chamada `fonte == "GOOGLE_ROUTES"`, `tracar_google` chamado 1 vez com 4 pontos (CD, A, B, CD); 2ª chamada não chama de novo.
  - `test_parada_sem_coordenada_e_cancelada_ficam_fora`: zerar lat/lng de uma parada e marcar outra `CANCELADA` → pontos passados ao Google só com as válidas.
  - `test_reordenar_recalcula_reagendar_nao`: trocar `ordem` das paradas → chama o Google de novo; só mudar `situacao` de uma pra `EM_DESLOCAMENTO` → não chama.
  - `test_google_falha_devolve_linha_reta_sem_gravar`: mock levanta → `fonte == "LINHA_RETA"`, `pontos` == pontos da rota, tabela vazia; próxima chamada tenta de novo.
  - `test_api_trajeto_404_de_outro_motorista` e `test_api_trajeto_200` (resposta tem `pontos`, `fonte`, `assinatura`, `base == [-23.497039, -46.6605211]`).
  - `test_app_versao_sem_login`: 200 com `versao_atual is None` e `link` do APK.
- [ ] **Step 2:** `py -3.11 -m unittest nucleo.test_trajeto_rota -v` → FALHA.
- [ ] **Step 3:** Implementar DDL, `nucleo/trajeto_rota.py` e as duas rotas (sys.path do `roteirizacao/` como em `recalcular_km_rotas.py`).
- [ ] **Step 4:** `py -3.11 -m unittest nucleo.test_trajeto_rota nucleo.test_api_motorista -v` → PASSA.
- [ ] **Step 5:** Atualizar `MAPA_DO_SISTEMA.txt` (tabela nova, rotas novas). Commit: `App motorista: API entrega o trajeto da rota (Google, guardado por rota) e a versao atual do APK`.
- [ ] **Step 6: PARA: OK do Hugo** → push, PR, deploy pela skill `deploy-vps` (restart só do `motorista-api`). Prova: `curl -s http://127.0.0.1:8073/api/app-versao` → `{"versao_atual": null, ...}`; e na VPS, `sudo -u www-data venv/bin/python -c` chamando `trajeto_rota.trajeto` numa rota de hoje com chave do config → `fonte GOOGLE_ROUTES` e algumas centenas de pontos; segunda chamada sem rede ao Google (mesma assinatura).

### Tarefa 3: Aviso "tem versão nova do app" (vai por OTA pro 1.1.1)

Ramo próprio `aviso-versao-app` a partir do master (não do ramo do mapa): é o código que sai por OTA pro 1.1.1.

**Files:**
- Create: `app_motorista/src/avisoVersao.tsx`
- Modify: `app_motorista/src/api.ts` (1 função), `app_motorista/app/_layout.tsx` (renderizar o aviso dentro de `Guarda`, acima do `Stack`), `MAPA_DO_SISTEMA.txt`

**Interfaces:**
- Consumes: `GET /api/app-versao` (Tarefa 2).
- Produces:
  - `api.appVersao = () => chamar<{ versao_atual: string | null; link: string }>('/app-versao')`
  - `export function versaoMenor(a: string, b: string): boolean` em `src/avisoVersao.tsx` (compara "1.1.1" < "1.2.0" por partes numéricas).
  - `export function AvisoVersao(): JSX.Element | null` — busca 1x ao montar e quando o app volta ao primeiro plano (`AppState`); compara `Updates.runtimeVersion` (de `expo-updates`; `null` no Expo Go → não mostra) com `versao_atual`; se menor, faixa fixa no topo, cor `cores.alerta`, texto **"Tem versão nova do app. Toque pra baixar."**, toque → `Linking.openURL(link)`. Erro de rede → não mostra nada.

- [ ] **Step 1:** Implementar. `cd app_motorista && npm run typecheck` → sem erros.
- [ ] **Step 2:** Expo Go apontando pra uma API local de teste com `APK_VERSAO_ATUAL = "9.9.9"` (só no teste, não commitar): faixa aparece e abre o link; com `None` some. Sem rede: nada aparece, app normal.
- [ ] **Step 3:** Commit: `App motorista: aviso de versao nova do APK (sai por OTA pro 1.1.1)`.
- [ ] **Step 4: PARA: OK do Hugo** → push + PR + merge. **A OTA em si só na Tarefa 9.**

### Tarefa 4: Lógica de passos da parada num módulo compartilhado (sem mudar comportamento)

**Files:**
- Create: `app_motorista/src/paradaPasso.ts`
- Modify: `app_motorista/app/rota/[id].tsx` (remover as definições locais e importar)

**Interfaces:**
- Produces (exportados de `src/paradaPasso.ts`, mesmo comportamento das versões atuais de `app/rota/[id].tsx`):
  - `ehPendente(p: Parada): boolean`, `emAndamento(p: Parada): boolean`
  - `abrirNavegacao(p: Parada): void`
  - `ordenarParadas(paradas: Parada[]): Parada[]` (o sort de `paradasOrdenadas`)
  - `rotaOperavel(rota: Rota): boolean` (`editavel && status === 'EM_ROTA'`)
  - `paradaDaVez(rota: Rota): Parada | null` (`atual` de hoje)
  - `registrarPasso(p: Parada, tipo: 'DESLOCAMENTO' | 'CHEGADA'): Promise<void>` (corpo de `passoParada` sem o `carregar()`; a tela chama `carregar()` depois)

- [ ] **Step 1:** Mover o código, importar na tela da rota. `npm run typecheck` → sem erros.
- [ ] **Step 2:** Expo Go numa rota de teste do app (provedor APP, `motoristas_cli.py replicar-rota` ou rota de teste existente): aceitar → iniciar → iniciar deslocamento → cheguei → finalizar entrega; tudo igual a antes, inclusive "Finalize a parada X pra liberar esta".
- [ ] **Step 3:** Commit: `App motorista: passos da parada num modulo compartilhado (preparo do mapa)`.

### Tarefa 5: Tela da parada abre com o resultado já escolhido

**Files:**
- Modify: `app_motorista/app/parada/[id].tsx`

**Interfaces:**
- Produces: rota `/parada/<id>?rota=<rotaId>&fluxo=ENTREGUE|NAO_ENTREGUE`. Ao carregar, sem rascunho salvo da mesma parada, chama `escolherComConfirmacao(fluxo)` (a confirmação "Sim, finalizar" de hoje continua). Rascunho salvo vence o parâmetro. Valor fora desses dois é ignorado.

- [ ] **Step 1:** Implementar; `npm run typecheck`.
- [ ] **Step 2:** Expo Go: abrir `/parada/<id>?rota=<r>&fluxo=NAO_ENTREGUE` → pergunta de confirmação e já mostra o motivo; "trocar resultado" volta às 4 opções; sem `fluxo` → igual a hoje.
- [ ] **Step 3:** Commit: `App motorista: tela da parada aceita o resultado ja escolhido (vindo do mapa)`.

### Tarefa 6: react-native-maps, chave fora do git e APK 1.2.0

**Files:**
- Modify: `app_motorista/package.json` / `package-lock.json` (`npx expo install react-native-maps -- --legacy-peer-deps`), `app_motorista/app.json` (`"version": "1.2.0"`), `app_motorista/eas.json` (perfil `preview` e `production` com `"environment": "preview"` / `"production"`)
- Create: `app_motorista/app.config.js` — `({ config }) => ({ ...config, plugins: [...(config.plugins ?? []), ['react-native-maps', { androidGoogleMapsApiKey: process.env.GOOGLE_MAPS_ANDROID_KEY }]] })`

- [ ] **Step 1:** Instalar e criar os arquivos. `npx expo config --type public` mostra o plugin `react-native-maps` e `version` 1.2.0 (com a variável vazia localmente, tudo bem).
- [ ] **Step 2:** `npm run typecheck` → sem erros.
- [ ] **Step 3:** Commit: `App motorista: react-native-maps e versao 1.2.0 (chave do Maps por variavel do EAS)`.
- [ ] **Step 4: PARA: Hugo** cria a chave (passo a passo no fim deste plano) e roda `npx eas-cli env:create --environment preview --name GOOGLE_MAPS_ANDROID_KEY --visibility sensitive` (o valor ele cola no terminal dele, nunca no chat).

### Tarefa 7: Tela do mapa

**Files:**
- Create: `app_motorista/src/trajeto.ts`, `app_motorista/src/mapa/CartaoParada.tsx`, `app_motorista/app/mapa/[id].tsx`
- Modify: `app_motorista/src/api.ts` (1 função), `app_motorista/src/tipos.ts` (tipo `Trajeto`), `app_motorista/app/_layout.tsx` (`<Stack.Screen name="mapa/[id]" options={{ title: 'Mapa da rota' }} />`), `app_motorista/app/rota/[id].tsx` (botão "Mapa"), `MAPA_DO_SISTEMA.txt`

**Interfaces:**
- Consumes: `GET /api/rotas/<id>/trajeto` (Tarefa 2), `src/paradaPasso.ts` (Tarefa 4), `?fluxo=` (Tarefa 5), `carregarRotas()` de `src/rotasStore.ts`.
- Produces:
  - `type Trajeto = { pontos: [number, number][]; fonte: 'GOOGLE_ROUTES' | 'LINHA_RETA'; assinatura: string; base: [number, number] }`
  - `api.trajeto = (rotaId: number) => chamar<Trajeto>(\`/rotas/${rotaId}/trajeto\`)`
  - `carregarTrajeto(rotaId: number): Promise<Trajeto | null>` em `src/trajeto.ts` — API; sucesso com `GOOGLE_ROUTES` grava em AsyncStorage `motorista.trajeto.v1.<rotaId>`; `ErroRede` → devolve o guardado ou `null`.
  - `CartaoParada({ parada, rota, total, indice, onAnterior, onProxima, onPasso, onBaixa })`.
- Comportamento (do spec, seções 3–5):
  - Botão "Mapa" (ícone `map`) na tela da rota, ao lado do resumo, para rota com `liberada !== false` e pelo menos 1 parada; `router.push(\`/mapa/${rota.id}\`)`.
  - `MapView` com `provider={PROVIDER_GOOGLE}`, `showsUserLocation`; ao abrir, `fitToCoordinates` de todos os pinos + base. Cabeçalho: "N de M", botão "minha localização" e "ver rota inteira".
  - Pinos: `Marker` com view própria (círculo com o número `ordem`); cor `cores.primaria` pendente, destaque maior pra em andamento, verde ENTREGUE/PARCIAL, vermelho INSUCESSO; CANCELADA não aparece; reagendada com selo "volta depois"; base com ícone de galpão. Sem coordenada: sem pino.
  - Linha: `Polyline` com `trajeto.pontos`; enquanto carrega ou se `null`, linha reta entre base → pinos (ordem) → base.
  - Cartão: ordem de `ordenarParadas`; abre na `paradaDaVez` (ou 1ª); setas ‹ › e toque no pino trocam o cartão e centralizam o pino (`animateToRegion`). Conteúdo: número, endereço, destinatário, código, janela; "sem localização no mapa" quando faltar coordenada; "Navegar" (`abrirNavegacao`).
  - Ações no cartão, só se `rotaOperavel(rota)` e `ehPendente(p)`; se outra parada está em andamento → texto "Finalize a parada X pra liberar esta.": PENDENTE → `Deslizar` "Iniciar deslocamento"; EM_DESLOCAMENTO → `Deslizar` "Cheguei no local"; EM_ROTA → dois `Deslizar`: "Entregue" (`cores.acento`) e "Não entregue" (`cores.perigo`) → `router.push(\`/parada/${p.id}?rota=${rota.id}&fluxo=ENTREGUE|NAO_ENTREGUE\`)`. Depois de passo/baixa: `carregarRotas()` de novo (no `useFocusEffect`) e cartão vai pra nova `paradaDaVez`.

- [ ] **Step 1:** Implementar; `npm run typecheck` → sem erros.
- [ ] **Step 2: Roteiro no Expo Go** (react-native-maps funciona lá sem chave), contra a API de produção com uma rota de teste do app:
  1. Rota com ~15 paradas: pinos numerados, linha pelas ruas, enquadra tudo.
  2. Setas e toque no pino trocam o cartão; "N de M" acompanha.
  3. Iniciar deslocamento → Cheguei → Entregue abre a parada com Entregue escolhido; checklist/fotos; volta ao mapa e o pino fica verde, cartão na próxima.
  4. Não entregue → abre com motivo.
  5. Com outra parada em andamento, o cartão de uma pendente mostra a trava.
  6. Parada sem coordenada (zerar numa rota de teste): sem pino, aviso no cartão.
  7. Modo avião depois de abrir uma vez: linha guardada aparece; rota nunca aberta: linhas retas; sem tela de erro.
  8. Rota concluída / por aceitar: cartões só de leitura.
- [ ] **Step 3:** Atualizar `MAPA_DO_SISTEMA.txt` (seção 13: `app/mapa/[id].tsx`, `src/trajeto.ts`, `src/paradaPasso.ts`, `src/avisoVersao.tsx`, react-native-maps, chave no EAS). Commit: `App motorista: tela Mapa da rota (pinos, trajeto e baixa pelo cartao)`.

### Tarefa 8: APK de teste com a chave e teste do Hugo

- [ ] **Step 1: PARA: OK do Hugo** → push do ramo `mapa-rota-app`.
- [ ] **Step 2:** `npx eas-cli build --profile preview --platform android` (token Expo como na skill `publicar-ota`, sem vazar). Copiar o SHA-1 do keystore em expo.dev → Credentials e passar ao Hugo pra restrição da chave, se ainda não estiver.
- [ ] **Step 3:** Instalar o APK num Android real (o Hugo) e repetir o roteiro da Tarefa 7, Step 2, itens 1–3 e 7. Mapa com fundo cinza/sem ruas = chave errada ou SHA-1 faltando.
- [ ] **Step 4:** Correções voltam pras tarefas 4–7. Com o OK do Hugo: PR do ramo (não publicar o APK no link fixo ainda).

### Tarefa 9: Soltar depois de sexta 20/11

- [ ] **Step 1: PARA: Hugo confirma o fim do piloto.**
- [ ] **Step 2:** Do **master com o PR da Tarefa 3 e sem o do mapa**, `version` 1.1.1: publicar OTA pela skill `publicar-ota` (canal `preview`). Conferir num aparelho 1.1.1 que nada muda (API ainda com `APK_VERSAO_ATUAL = None`).
- [ ] **Step 3:** Merge do PR do mapa. Build preview final; APK em `dados/app_motorista/app.apk` na VPS (link fixo). Baixar pelo link e conferir a versão 1.2.0.
- [ ] **Step 4:** Commit `APK_VERSAO_ATUAL = "1.2.0"` em `nucleo/api_motorista.py`, deploy (`motorista-api`). Prova: `curl -s http://127.0.0.1:8073/api/app-versao` → `"versao_atual": "1.2.0"`; aparelho 1.1.1 mostra a faixa e baixa o APK; no 1.2.0 a faixa não aparece.
- [ ] **Step 5:** Atualizar a memória do projeto e avisar o Hugo pra mandar a mensagem aos motoristas.

---

## Passo a passo da chave do Google (pro Hugo, Tarefa 6 Step 4)

1. console.cloud.google.com → mesmo projeto da chave do servidor → APIs e serviços → Biblioteca → ativar **Maps SDK for Android**.
2. Credenciais → Criar credencial → Chave de API → editar: Restrição de aplicativo **Apps Android**, pacote `br.com.freshlog.motorista` + SHA-1 que eu passar (do keystore do EAS); Restrição de API: só **Maps SDK for Android**.
3. No terminal dele, em `app_motorista/`: `npx eas-cli env:create --environment preview --name GOOGLE_MAPS_ANDROID_KEY --visibility sensitive` e colar a chave quando pedir. Repetir com `--environment production` quando houver Play Store.
