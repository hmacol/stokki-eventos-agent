# Mapa da rota no app dos motoristas — desenho

Data: 18/11/2026 · Pedido do Hugo · Referência: vídeo do app da Vuupt (18/11, "Mapa da Rota")

## Por que

O motorista hoje só vê as paradas em lista e abre UMA por vez no Google Maps/Waze
(`app/rota/[id].tsx`, `abrirNavegacao`). Ele não enxerga a rota como um todo. O Hugo
quer um "Mapa da rota" como o do app da Vuupt: todas as paradas numeradas no mapa,
a linha do trajeto pelas ruas e um cartão embaixo com a parada e os botões de baixa.
Isso pesa na saída da Vuupt: o app próprio precisa ter o que o motorista já usa lá.

## O que é sucesso

- Da tela da rota, o motorista abre o mapa e vê todas as paradas numeradas pela
  ordem da rota, coloridas pela situação, com a linha do trajeto pelas ruas.
- Arrastando o cartão de baixo (ou tocando num pino) ele passa de parada em parada.
- Pelo cartão ele avança a parada (iniciar deslocamento → cheguei → baixa) do
  mesmo jeito que pela lista, sem atalho que pule checklist, foto ou canhoto.
- Custo do Google continua dentro das faixas grátis no volume atual (~500 rotas/mês).

## Fora do escopo (não fazer)

- Navegação curva a curva dentro do app: o botão "Navegar" continua abrindo Google Maps/Waze.
- Reordenar paradas pelo mapa.
- Trânsito em tempo real, recalcular trajeto a partir da posição do motorista.
- iOS (o app é só Android hoje; o código não deve impedir iOS, mas não se testa).
- Mapa no painel/Torre.

## Anti-objetivos (seria falha mesmo funcionando)

- Baixa pelo mapa que pule o checklist/fotos/canhoto ou a regra de "uma parada em andamento por vez".
- Chave do Google embutida no app sem restrição (qualquer um copiaria e gastaria na nossa conta).
- Chave/segredo commitado no git.
- O mapa derrubar ou travar a tela da rota quando não houver internet ou faltar coordenada.

## Restrições do que já existe

- App: Expo SDK 57, expo-router, só Android. `react-native-webview` já instalado; nenhum mapa.
- Mapa nativo exige **APK novo** (módulo nativo): sobe `version` no `app.json` (é a
  runtimeVersion), assim OTA nova não chega em APK antigo sem o módulo.
- Paradas já vêm com `latitude`/`longitude` (`nucleo/operacao.montar_parada`); podem vir nulas.
- Base/CD: `regras/km_cobrado.COORD_CD = (-23.497039, -46.6605211)`.
- Servidor já usa a Google Routes API (`roteirizacao/km_rodoviario.py`, chave
  `google_maps.api_key` do config, 411 de 444 rotas do último mês com km `GOOGLE_ROUTES`).
  Já parte rota com mais de 25 paradas em várias chamadas. Hoje pede só distância
  (sem polyline, de propósito).
- Rotas Vuupt também aparecem no app (ponte); `rota.editavel` diz se o app opera a rota.
- Fluxo da parada no app: `PENDENTE` → "Iniciar deslocamento" (`EM_DESLOCAMENTO`) →
  "Cheguei no local" → "Finalizar entrega" abre `app/parada/[id].tsx`, onde se escolhe
  Entregue / Entregue parcial / Não entregue / Reagendar, com checklist e fotos.
  Os passos vão pela fila offline (`src/fila.ts`, `passoParada` em `app/rota/[id].tsx`).

## Caminhos considerados

| Caminho | Prós | Contras |
|---|---|---|
| **A. Mapa nativo Google (react-native-maps) + trajeto do Google calculado no servidor** — ESCOLHIDO | Igual ao do vídeo, fluido, ponto azul do motorista; mapa nativo é grátis e ilimitado | APK novo pra todos; precisa chave Android no Google Cloud |
| B. Google Maps JavaScript dentro de WebView | Sai por OTA | Mais pesado em celular barato; cada abertura gasta cota (10 mil grátis/mês, folga curta) |
| C. OpenStreetMap em WebView com linhas retas | Sem Google, OTA | Linha corta quarteirão, visual pobre, longe do pedido |

O Hugo escolheu A (18/11). Razão: é o que ele mostrou, custa R$ 0 no volume atual e o
custo do APK é o menor que vai ser (quase todas as rotas ainda são Vuupt, poucos
motoristas no app). `expo-maps` foi descartado: está em alfa no SDK 57.

## O que o Hugo decidiu

- O mapa mostra a rota inteira **e dá baixa** pelo cartão (18/11).
- Mapa nativo do Google (caminho A) (18/11).

## Desenho

### 1. Servidor: trajeto da rota

- Rota nova na API do motorista: `GET /api/rotas/<id>/trajeto` (`@requer_motorista`,
  só rota do próprio motorista, como `/api/rotas/<id>`).
- Resposta: `{"pontos": [[lat, lng], ...], "fonte": "GOOGLE_ROUTES" | "LINHA_RETA",
  "assinatura": "<hash>"}`. Pontos já decodificados (o app não precisa decodificar polyline).
- Trajeto = CD → paradas não canceladas com coordenada, na `ordem` → CD (mesma régua do km).
- Cálculo: função nova em `roteirizacao/km_rodoviario.py` que pede à Routes API o
  `routes.polyline.encodedPolyline` (mesmo particionamento de 25 intermediárias),
  decodifica e concatena. Falha/sem chave → linha reta entre os pontos, `fonte=LINHA_RETA`.
- Cache: tabela nova `nucleo_rotas_trajeto (rota_id PK, assinatura, pontos_json, fonte, criado_em)`.
  `assinatura` = hash da lista ordenada de coordenadas usada. Se a rota for editada
  (paradas/ordem mudam), a assinatura muda e recalcula. Linha reta não é guardada como
  definitiva (tenta o Google de novo no próximo pedido).
- Custo: uma chamada (ou duas, rota >25 paradas) por rota/edição, ~500/mês → dentro das
  5.000 grátis do nível Pro, somadas ao uso atual do km.

### 2. App: dependência e chave

- `npx expo install react-native-maps` (versão do SDK 57; doc recomenda 1.27.2).
- Chave Android **separada** da do servidor, criada pelo Hugo no mesmo projeto do Google
  Cloud: habilitar "Maps SDK for Android"; restrição "Apps Android" com pacote
  `br.com.freshlog.motorista` + SHA-1 do keystore do EAS (e, quando houver Play Store,
  o SHA-1 da assinatura da Play).
- A chave entra no build por variável de ambiente do EAS (`GOOGLE_MAPS_ANDROID_KEY`) lida
  num `app.config.js` que acrescenta o plugin `["react-native-maps", {"androidGoogleMapsApiKey": ...}]`.
  Nunca no `app.json`/git.
- `version` do `app.json` sobe (ex.: 1.1.1 → 1.2.0). APK novo no link fixo
  `app.freshhub.com.br/motorista/app.apk`.

### 3. App: entrada

- Na tela da rota, botão "Mapa" no cabeçalho (ícone `map`) e/ou ação visível perto do
  resumo — abre `app/rota/[id]/mapa.tsx` (ou `app/mapa/[id].tsx`, a critério de quem
  implementa, respeitando o expo-router). Visível para qualquer rota liberada com paradas.

### 4. App: tela do mapa

- Título "Mapa da rota", "N de M" (posição do cartão), botão "minha localização" e
  botão "ver rota inteira" (enquadra todas as paradas).
- Pinos numerados pela `ordem`; cor pela situação: pendente/em andamento = cor primária
  (a em andamento destacada), entregue/parcial = verde, insucesso = vermelho,
  cancelada = não aparece. Pino do CD diferente.
- Linha do trajeto (`Polyline`) com os pontos do servidor. Sem internet: usa o último
  trajeto guardado localmente pra rota; sem nada guardado, liga os pinos em linha reta.
- Ponto azul do motorista (`showsUserLocation`); a permissão de localização já existe.
- Ao abrir: enquadra a rota inteira e posiciona o cartão na "parada da vez" (mesma
  regra da tela da rota: a em andamento ou a primeira pendente na ordem).
- Parada sem coordenada: aparece no cartão (com aviso "sem localização no mapa"), sem pino.

### 5. App: cartão de baixo

- Um cartão por parada, na mesma ordem da tela da rota (incluindo as reagendadas no fim).
  Troca de cartão por **setas ‹ ›** no cartão e **toque no pino**, sem arrastar o cartão
  pro lado: as ações são o `Deslizar` (arrastar pra confirmar, regra do Hugo de 26/08
  contra toque acidental com o celular no suporte) e dois gestos horizontais brigariam.
  Trocar de cartão centraliza o pino.
- Conteúdo: número, endereço, destinatário, código do pedido / embarcador, janela de horário.
- Ações (decisão minha, ver "Chamadas minhas"): o cartão mostra **o mesmo controle da lista**,
  respeitando `operavel` e a trava "finalize a parada X pra liberar esta":
  - PENDENTE → "Iniciar deslocamento"
  - EM_DESLOCAMENTO → "Cheguei no local"
  - chegou → dois `Deslizar` (não botões de toque): **"Entregue"** (verde) e **"Não entregue"** (vermelho),
    que abrem `app/parada/[id].tsx` já com o fluxo escolhido (parâmetro novo `fluxo`).
    Entregue parcial e Reagendar continuam na tela da parada ("trocar resultado").
  - Também: "Navegar" (o `abrirNavegacao` de hoje).
  - Rota não operável (por aceitar, concluída, `editavel=false`): cartão só de leitura.
- Depois da baixa (volta da tela da parada), o mapa recarrega a rota e o cartão vai pra
  próxima parada da vez.
- A lógica de passo (`passoParada`, `ehPendente`, `emAndamento`, ordenação) sai de
  `app/rota/[id].tsx` para um módulo compartilhado em `src/` usado pelas duas telas —
  sem mudar o comportamento da lista.

### 6. Tela da parada

- Aceita `?fluxo=ENTREGUE|NAO_ENTREGUE` e já abre com esse resultado escolhido (a
  confirmação que hoje aparece ao deslizar continua). Rascunho salvo vence o parâmetro.

## Prova (antes de entregar)

- Servidor: teste unitário do trajeto (assinatura, partição >25, fallback linha reta,
  rota de outro motorista = 404) e chamada real na VPS a uma rota de hoje.
- App: `npm run typecheck`; desenvolvimento no Expo Go (react-native-maps funciona sem
  chave lá); APK de preview com a chave instalado num Android real: abrir uma rota com
  ~15 paradas, ver pinos/linha, arrastar cartões, fazer deslocamento → chegada →
  Entregue numa rota de teste e conferir no painel.

### 7. Aviso "baixe a versão nova" (pros aparelhos no APK 1.1.1)

- Servidor diz qual é o APK atual: campo novo na resposta de uma rota que o app já
  chama ao abrir (ex.: `/api/eu`) ou rota própria, com `apk_versao_atual` e o link fixo
  do APK, vindo do config (sem config = sem aviso).
- App compara com a runtime nativa (`Updates.runtimeVersion` do expo-updates, que é a
  `version` do APK — não usar `expoConfig.version`, que muda com a OTA). Se a do aparelho
  for menor, mostra uma faixa fixa "Tem versão nova do app: toque pra baixar" que abre o link.
- Esse código vai **por OTA pro canal do 1.1.1 antes** do APK novo sair, e também fica no 1.2.0.

## Ordem de entrega (decidido pelo Hugo em 18/11: soltar o APK só depois do piloto)

O piloto do app só (Clayson e Erick, prazo sexta 20/11) termina no APK 1.1.1, que segue
recebendo correções por OTA. O APK 1.2.0 não sai antes do fim do piloto.

1. Servidor (trajeto + cache + teste + campo da versão) → deploy (não quebra nada: app antigo não chama).
2. Hugo cria a chave Android restrita e cadastra no EAS.
3. App no Expo Go → APK preview → teste do Hugo (já pode ser antes de sexta, fora do piloto).
4. Depois de 20/11: OTA do aviso pro 1.1.1 → APK 1.2.0 no link fixo → ligar `apk_versao_atual`
   no config → os motoristas veem o aviso e reinstalam.

## Chamadas minhas (o Hugo confere)

- Cartão segue os mesmos passos da lista e só mostra Entregue / Não entregue depois do
  "Cheguei no local" (no vídeo os botões aparecem direto). Motivo: manter a sequência e a
  trava de uma parada por vez que já existem.
- Trajeto inclui a volta ao CD (mesma régua do km).
- Trajeto e números dos pinos seguem a `ordem` planejada, não a ordem com reagendadas no
  fim: o cache fica estável (reagendar não gera chamada nova ao Google) e bate com o km.
  Pino de parada reagendada ganha um selo "volta depois"; o cartão segue a ordem da lista.
- Cartão troca por setas e toque no pino (não por arrastar), e Entregue / Não entregue
  são `Deslizar`, mantendo a regra de 26/08 de arrastar pra confirmar.
- Chave do app separada da do servidor, restrita ao pacote.
- Trajeto calculado sob demanda na primeira abertura e guardado (não em lote).

## Fica pra quem implementar

- Desenho visual dos pinos e do cartão (seguir `src/tema.ts`).
- Nome/arquivo da rota no expo-router; componente do carrossel (FlatList horizontal com paginação basta).
- Guardar o trajeto no cache local junto do cache de rotas ou separado.
