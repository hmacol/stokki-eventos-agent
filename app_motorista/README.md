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

## Distribuir (Android, APK direto — decisão de 26/08: só Android por enquanto)

Sem Play Store, sem conta Google, sem D-U-N-S. Só uma conta gratuita em
[expo.dev](https://expo.dev).

```
npx eas-cli login                                   # conta Expo (uma vez nesta máquina)
npx eas-cli init                                    # grava extra.eas.projectId no app.json (uma vez)
npx eas-cli build -p android --profile preview      # gera o APK na nuvem (~10-15 min) e devolve um link
```
Manda o link do APK por WhatsApp; o motorista instala ("permitir fonte
desconhecida"). Mudança só de tela/regra vai sem reinstalar:
```
npx eas-cli update --channel preview --message "o que mudou"
```
Mudança nativa (nova permissão/módulo) → novo `eas build`, subir
`android.versionCode` no `app.json`.

**Push no Android** precisa de credenciais FCM (projeto Firebase gratuito
→ `google-services.json` → `npx eas-cli credentials`). Sem isso o app
funciona normalmente, só não recebe aviso de rota.

**Play Store / iPhone** ficam pra depois: o perfil `production` do
`eas.json` gera o AAB da loja; iOS exige conta Apple Developer.
