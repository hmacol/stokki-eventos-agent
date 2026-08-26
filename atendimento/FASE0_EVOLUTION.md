# Central de Atendimento — Evolution API (substitui o Chatwoot)

Decisão do Hugo (26/08): abandonar o Chatwoot + WhatsApp Cloud API oficial.
Motivo: verificação de CNPJ na Meta travada há dias e custo por conversa da
Cloud API incompatíveis com a viabilidade financeira atual. Troca pra
**Evolution API** (gateway open-source, protocolo WhatsApp Web/Baileys) --
risco de banimento aceito conscientemente. Mesmo número/chip reaproveitado
(desregistrado da Cloud API antes do pareamento). Chatwoot é abandonado por
completo, não só o conector de WhatsApp -- substituído por um backend+UI
próprios (`atendimento/app.py`), nos mesmos padrões do resto do projeto.

Este documento substitui o `FASE0.md` (Chatwoot) -- ver ali o histórico da
fase anterior, que nunca chegou a sair do ar de verdade (CNPJ nunca foi
aprovado).

**Pronto quando:** uma mensagem de WhatsApp chega na Inbox nova; dois
atendentes logados ao mesmo tempo assumem e respondem conversas diferentes
do mesmo número sem conflito; o aviso automático de oferta de rota
(`roteirizacao/avisar_motoristas_rotas.py`) chega via Evolution API.

## Checklist

| # | Tarefa | Quem | Status |
|---|--------|------|--------|
| 1 | **Manual, antes de tudo**: desregistrar o número da WhatsApp Cloud API no Meta Business Manager | Hugo | ☑ (26/08) |
| 2 | Subir o stack Evolution API na VPS (`atendimento/infra/instalar_vps.sh`) | Claude + Hugo | ☑ (26/08, pasta isolada `atendimento/infra-evolution/` na VPS -- ver nota abaixo) |
| 3 | Criar a instância e parear o número (QR ou código por telefone, tela `/admin/whatsapp`) | Hugo | ☑ (26/08, número final `11991919762` -- trocado do número original da Cloud API a pedido do Hugo; pareado por linha de comando, a tela `/admin/whatsapp` ainda não foi usada de verdade) |
| 4 | Testar persistência: reiniciar o container Evolution e confirmar que não pede pareamento de novo | Claude | ☐ |
| 5 | Construir `atendimento/` (banco, app, templates) + `integracao_evolution.py` | Claude | ☑ |
| 6 | Deploy (`git pull` + `infra/atendimento-central.service` + atualizar `Caddyfile-atendimento`) | Claude + Hugo | ☑ (26/08) |
| 7 | Criar o primeiro usuário admin via CLI (`python -m atendimento.gerenciar_usuarios --criar ...`) | Hugo | ☑ (26/08, hugo@freshlogbr.com) |
| 8 | Migrar `avisar_motoristas_rotas.py` e testar o aviso de rota ponta a ponta | Claude | ☑ código migrado; ☐ teste ponta a ponta ainda não feito |
| 9 | Desligar/remover o stack Chatwoot (containers + volumes), só depois de tudo acima validado | Hugo | ☐ |
| 10 | Teste de aceite (abaixo) | Todos | ☐ (recebimento OK; envio de resposta bloqueado agora por rate-limit da Meta, ver risco novo abaixo) |

**Nota sobre o passo 2:** a Evolution API foi instalada numa pasta separada
(`atendimento/infra-evolution/`), não em `atendimento/infra/` -- essa
última ainda tem o `docker-compose.yml`/`.env` do Chatwoot **rodando de
verdade** na VPS (projeto Docker `infra`). Subir o compose novo na mesma
pasta recriaria o container `postgres` do projeto `infra` com uma imagem
diferente sobre o volume do Chatwoot, corrompendo os dois. Resolver isso
(mover pra `infra/` de vez) só no passo 9, depois do Chatwoot desligado.

## Riscos aceitos / a lembrar

- **Sem canal alternativo se o número for banido**: o aviso de motoristas
  cai automaticamente no fallback de e-mail/copiar-colar (já existe, sem
  mudança). A central de atendimento em si **não tem fallback** -- se
  banir, fica muda até parear outro número (e-mail está fora do escopo
  desta fase).
- Mensagens `fromMe:true` (mandadas direto do celular vinculado, fora da
  UI) aparecem na thread como OUT sem atendente -- webhook já trata isso.
- Formato exato de request/webhook da Evolution API precisa ser conferido
  contra a instância real instalada antes de considerar `integracao_evolution.py`
  definitivo -- primeiro lugar a olhar se o envio/recebimento falhar.
- Pareamento consome um dos ~4 slots de "aparelho conectado" do WhatsApp
  nesse número -- se já tiver muitos vinculados, pode ser preciso
  desvincular algum antes.
- `backup_dados_gcs.py` já cobre `dados/atendimento.db` (feito nesta
  entrega) -- confirmar que aparece no primeiro backup depois que a
  central entrar no ar.
- **Observado de verdade em 26/08, não só teórico:** depois de vários
  ciclos seguidos de pareamento/despareamento (trocamos o número, várias
  tentativas de código expiradas, apagamos e recriamos a instância), o
  WhatsApp passou a **bloquear só o envio** de mensagens pro número que
  tinha acabado de mandar mensagem de teste (erro `463` /
  `NackCallerReachoutTimelocked` -- Baileys), enquanto o **recebimento
  seguiu funcionando normalmente**. É um rate-limit/"esquenta de conta"
  que a Meta aplica a aparelho recém-pareado que troca de vínculo demais
  em pouco tempo -- não é o banimento definitivo, mas é o mesmo tipo de
  penalidade que o risco aceito previa, só que aparecendo bem mais cedo
  do que o esperado. Lição prática: **depois de parear, evitar
  reconectar/trocar de número repetidas vezes** -- deixar a sessão
  quieta por um tempo (o time-lock costuma passar sozinho) antes de
  testar de novo, e considerar usar o número normalmente pelo próprio
  celular (fora da API) por um tempo pra "esquentar" a conta antes de
  depender dela pra produção.

## Teste de aceite

- [ ] WhatsApp de um celular pessoal → aparece na Inbox nova → resposta chega no celular.
- [ ] Reiniciar o container da Evolution API → instância continua conectada, sem pedir pareamento de novo.
- [ ] Dois atendentes logados ao mesmo tempo, cada um assumindo e respondendo conversas diferentes do mesmo número, sem conflito.
- [ ] Aviso automático de oferta de rota chega via Evolution API (`avisar_motoristas_rotas.py`).
- [ ] `https://atendimento.freshhub.com.br` com cadeado (HTTPS) válido, servindo o app novo (não mais o Chatwoot).
