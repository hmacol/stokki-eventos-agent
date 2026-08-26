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
| 1 | **Manual, antes de tudo**: desregistrar o número da WhatsApp Cloud API no Meta Business Manager | Hugo | ☐ |
| 2 | Subir o stack Evolution API na VPS (`atendimento/infra/instalar_vps.sh`) | Claude + Hugo | ☐ |
| 3 | Criar a instância e parear o número (QR ou código por telefone, tela `/admin/whatsapp`) | Hugo | ☐ |
| 4 | Testar persistência: reiniciar o container Evolution e confirmar que não pede pareamento de novo | Claude | ☐ |
| 5 | Construir `atendimento/` (banco, app, templates) + `integracao_evolution.py` | Claude | ☑ (código pronto, falta validar contra a instância real) |
| 6 | Deploy (`git pull` + `infra/atendimento-central.service` + atualizar `Caddyfile-atendimento`) | Claude + Hugo | ☐ |
| 7 | Criar o primeiro usuário admin via CLI (`python -m atendimento.gerenciar_usuarios --criar ...`) | Hugo | ☐ |
| 8 | Migrar `avisar_motoristas_rotas.py` e testar o aviso de rota ponta a ponta | Claude | ☑ (código pronto, falta testar contra instância real) |
| 9 | Desligar/remover o stack Chatwoot (containers + volumes), só depois de tudo acima validado | Hugo | ☐ |
| 10 | Teste de aceite (abaixo) | Todos | ☐ |

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

## Teste de aceite

- [ ] WhatsApp de um celular pessoal → aparece na Inbox nova → resposta chega no celular.
- [ ] Reiniciar o container da Evolution API → instância continua conectada, sem pedir pareamento de novo.
- [ ] Dois atendentes logados ao mesmo tempo, cada um assumindo e respondendo conversas diferentes do mesmo número, sem conflito.
- [ ] Aviso automático de oferta de rota chega via Evolution API (`avisar_motoristas_rotas.py`).
- [ ] `https://atendimento.freshhub.com.br` com cadeado (HTTPS) válido, servindo o app novo (não mais o Chatwoot).
