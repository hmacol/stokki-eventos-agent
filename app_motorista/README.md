# FreshLog Motorista (app)

App Expo / React Native dos motoristas — Fase B do
`../DOC_EXECUCAO_CLAUDE_APP_MOTORISTAS.md`. Fala com a API
`../nucleo/api_motorista.py`.

## Rodar em desenvolvimento

1. API local (numa porta livre — nunca a 8070/8071 do painel):
   ```
   py -3.11 nucleo/api_motorista.py --porta 8073
   ```
   Precisa de `api_motorista.secret_key` no `config.yaml` (qualquer string
   longa aleatória); sem ela os tokens morrem a cada restart.
2. Usuário de teste + rota replicada (ver `nucleo/motoristas_cli.py`):
   ```
   py -3.11 nucleo/motoristas_cli.py criar --cpf 00000000000 --nome "Hugo (teste)" --pin 123456 --perfil TESTE
   py -3.11 nucleo/motoristas_cli.py replicar-rota --vuupt-route-id <id> --para-cpf 00000000000
   ```
3. App apontando pra API local: edite `app.json` → `extra.apiUrl` para
   `http://<IP-da-sua-máquina>:8073/api` (celular na mesma rede Wi-Fi;
   emulador Android usa `http://10.0.2.2:8073/api`).
4. `npm install` (uma vez) e `npx expo start`. Abra no **Expo Go** (câmera
   e localização funcionam; push só em build EAS).

`npm run typecheck` roda o `tsc`.

## Estrutura

- `app/` — telas (expo-router): `login`, `(tabs)/{rotas,ofertas,financeiro,disponibilidade,perfil}`, `rota/[id]`, `parada/[id]`.
- `src/api.ts` — cliente HTTP (token + refresh automático, erro de rede tipado).
- `src/fila.ts` — **fila offline**: evento de parada, foto, GPS, aceite/início/fim de rota entram na fila com `uuid` e sobem quando há sinal; a API ignora repetido.
- `src/local.ts` — estado otimista (parada entregue / rota iniciada aparecem na hora, mesmo sem rede).
- `src/gps.ts` — rastreamento em primeiro plano durante a rota → km real no financeiro.
- `src/assinatura.tsx` — assinatura na tela (WebView).

## Publicar (EAS)

```
npm i -g eas-cli
eas login                # conta Expo da FreshLog
eas init                 # grava extra.eas.projectId no app.json (necessário pro push)
eas build --profile preview --platform android   # APK de teste
eas build --platform ios                          # precisa da conta Apple Developer (D-U-N-S)
eas submit                                         # lojas
```
Distribuição: Apple **Unlisted** + Google Play **teste fechado** (app
privado, sem busca pública). Piloto via TestFlight / Internal Testing.
