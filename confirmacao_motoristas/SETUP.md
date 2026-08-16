# Confirmação de rota — checklist de deploy

Página pública onde o motorista confirma/recusa a rota do dia (ver `README` no
topo de `app.py` pro desenho completo).

## Status (16/08)

- ✅ VPS contratada (Hostinger, Ubuntu 24.04.4, IP `187.127.52.197`).
- ✅ Chave SSH autorizada, deploy feito (`instalar_vps.sh` rodado com sucesso
  — pacotes, ufw, Caddy, venv, systemd; serviço `confirmacao-motoristas`
  ativo e respondendo em `127.0.0.1:8090`).
- ⏳ **Domínio escolhido: `confirmacao.freshhub.com.br`** (trocado de
  `confirmacao.freshlogbr.com` — domínio não aceita cedilha/til sem punycode,
  então o nome do subdomínio ficou sem acento, igual ao resto do projeto).
  Caddyfile já atualizado local e na VPS (recarregado). **Falta o Hugo criar
  o registro DNS** (tipo A, nome `confirmacao`, valor `187.127.52.197`) em
  onde o `freshhub.com.br` estiver gerenciado — o certificado HTTPS só é
  emitido depois que isso propagar.
- ⏳ Depois do DNS: rodar `avisar_motoristas_rotas.py --gerar-confirmacoes`
  de verdade (fora do modo teste) pra validar o domínio real, e decidir com o
  Hugo se as 2 tarefas agendadas (aviso diário + sync a cada 30min) já entram
  automáticas ou se ele quer rodar manualmente uma vez antes pra conferir.

## Verificação já feita localmente (sem a VPS de verdade)

- Ciclo completo local↔servidor testado via HTTP real num servidor local
  (porta 8099): criação da confirmação → push → `GET /r/<token>` mostrando os
  dados da rota → `POST` com 4 dígitos errados (bloqueado com aviso) → `POST`
  com dígitos certos recusando com motivo → pull das respostas → aplicação no
  banco local (`regras/confirmacao_rotas.py`), status refletido corretamente.
- Token adulterado/inválido → link recusado (404). Chamada de sync sem o
  segredo correto → 401.
- `planejamento_rotas.py` importa e monta `confirmacao_motorista` por rota sem
  erro; o card em `/planejamento` só mostra o badge pra rota já **ENVIADO**
  (rascunho ainda não tem motorista "confirmável" — pode trocar até lá).

## Depois do deploy, testar de verdade

- Abrir um link real (`/r/<token>` gerado por uma rota de amanhã) no celular,
  confirmar e depois recusar, e ver o card em `/planejamento` mudar de badge
  após rodar `sincronizar_respostas_confirmacao.py`.
- Testar um link vencido (>72h) e conferir a mensagem de "link expirado".
