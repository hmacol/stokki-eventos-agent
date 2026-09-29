# Gateway OpenWA (notificações internas por WhatsApp)

Gateway não-oficial de WhatsApp usado **só** por `notificar_whatsapp.py`.
Não tem relação com a central de atendimento (`atendimento/`, Evolution API).

- Projeto: https://github.com/rmyndharis/OpenWA (licença MIT)
- Na VPS: `/opt/openwa`, clonado em 28/09/2026 no commit `036dd70f` (v0.23.8)
- Serviço do compose: `openwa`. Container: `openwa-api`
- Porta: `127.0.0.1:2785` (nunca exposta pelo Caddy)
- Motor: `whatsapp-web.js` (Chromium headless). Em repouso, sem sessão: ~125 MB
- Dados (banco SQLite, sessão pareada, chave inicial): `/opt/openwa/data/`
- MCP: desligado

## Configuração

`infra/openwa/env-openwa.txt` é copiado para `/opt/openwa/.env`. Não tem segredo.
A chave de administrador nasce aleatória no primeiro boot e fica em
`/opt/openwa/data/.api-key` (só root lê). Nunca copiar para o repositório.

## Subir, parar, ver

```bash
cd /opt/openwa
docker compose -f docker-compose.dev.yml up -d --build   # primeira vez ou depois de git pull
docker compose -f docker-compose.dev.yml restart
docker compose -f docker-compose.dev.yml logs --tail 100
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:2785/api/health/ready   # 200
ss -ltnp | grep 2785                                       # tem que ser 127.0.0.1
```

## Painel (parear o número, criar chave)

Da máquina do Hugo, abrir um túnel e acessar http://localhost:2785 :

```bash
ssh -i ~/.ssh/atendimento_vps -L 2785:127.0.0.1:2785 root@187.127.52.197
```

Entrar com a chave de administrador (`cat /opt/openwa/data/.api-key` na VPS).

1. Criar a sessão `notificacoes` e iniciar.
2. Ler o QR no celular: WhatsApp > Aparelhos conectados > Conectar um aparelho.
3. Criar uma chave de API de papel `operator`, restrita à sessão `notificacoes`.
   É essa chave que vai no `config.yaml`, não a de administrador.

## Descobrir o id da sessão e do grupo

Na VPS, com a chave de operador lida sem eco:

```bash
read -s API_KEY; export API_KEY
curl -s http://127.0.0.1:2785/api/sessions -H "X-API-Key: $API_KEY"
export SESSION_ID=<id da sessao notificacoes>
curl -s "http://127.0.0.1:2785/api/sessions/$SESSION_ID/groups" -H "X-API-Key: $API_KEY"
```

O grupo tem o formato `<numero>@g.us`.

## Mensagem de prova

```bash
export GRUPO_ID=<id do grupo>
curl -s -X POST "http://127.0.0.1:2785/api/sessions/$SESSION_ID/messages/send-text" \
  -H "Content-Type: application/json" -H "X-API-Key: $API_KEY" \
  -d "{\"chatId\": \"$GRUPO_ID\", \"text\": \"Prova do gateway de notificacoes. Pode ignorar.\"}"
```

## config.yaml da VPS

```yaml
whatsapp_notificacoes:
  ativo: false                 # ligar e decisao do Hugo
  base_url: "http://127.0.0.1:2785/api"
  api_key: "<chave de operador>"
  sessao: "<id da sessao>"
  grupo_id: "<id do grupo>@g.us"
  sempre_avisar:
    - cancelar_rotas_sem_motorista
    - criar_rotas_diarias
    - executar_tudo
  teto_diario: 20
  intervalo_min_seg: 20
  janela_repeticao_min: 120
  falhas_para_alerta: 3
  pausa_canal_min: 60
```

## Provado em 29/09/2026

- Pareamento por QR com o numero final 5364: conectou com `WWEBJS_WEB_VERSION=off`.
  Com a versao fixada pelo padrao (alpha de terceiros) o WhatsApp desfez o
  vinculo (LOGOUT) segundos depois da leitura.
- Pareamento por CODIGO nao funcionou e a documentacao do gateway avisa que,
  neste motor, pode derrubar os outros aparelhos do numero. Usar sempre QR.
- Depois de recriar o container a sessao volta sozinha para `ready` em cerca
  de 70 s, sem novo QR (`AUTO_START_SESSIONS=true`). Nesse intervalo as rotas
  respondem 400/409.
- Sessao que NUNCA foi autenticada nao inicia sozinha: precisa de
  `POST /api/sessions/<id>/start` antes de pedir o QR.
- Memoria: ~130 MB sem sessao, ~1,0 a 1,3 GB com a sessao ativa (limite do
  container: 2 GB). O numero tem 113 grupos.

- Envio ao grupo ALERTAS FRESH: HTTP 201, resposta `{"messageId": ..., "timestamp": ...}`,
  cerca de 3,5 s (o gateway simula "digitando").
- `SEND_PACING_ENABLED=true` NAO serve para numero de uso pessoal: a conta do
  gateway inclui o que o dono digita no celular, e o limite do primeiro dia
  (20) ja vinha estourado (HTTP 429 `SEND_PACING_LIMITED`). Ficou desligado.
- Chave das rotinas: papel `operator`, restrita a sessao `notificacoes` E ao
  grupo ALERTAS FRESH (`allowedChats`). Envio a outro grupo com ela: HTTP 403.
- Secao `whatsapp_notificacoes` incluida no `config.yaml` da VPS em 29/09/2026
  com `ativo: false`. Backup: `config.yaml.bak-20260929-antes-whatsapp`.

## Pendente

- Merge e deploy do ramo `whatsapp-notificacoes`.
- Ligar (`ativo: true`) e prova real com `alertar_falha_job.py ... --forcar`.
