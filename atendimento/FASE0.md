# Fase 0 — Fundação da Central de Atendimento

Objetivo: infraestrutura no ar e canais conectados.
**Pronto quando:** uma mensagem de WhatsApp e um e-mail aparecem na mesma tela do Chatwoot e dois atendentes respondem pelo mesmo número ao mesmo tempo.

Plano geral do projeto: https://claude.ai/code/artifact/e06e4d13-79b6-4aff-acd2-7f0c07f48b7e

## Checklist geral

| # | Tarefa | Quem | Status |
|---|--------|------|--------|
| 1 | Comprar chip/número dedicado do atendimento | Hugo | ☐ |
| 2 | Contratar VPS (Ubuntu 24.04, ≥4 GB RAM) | Hugo | ☐ |
| 3 | Criar DNS `atendimento.freshlogbr.com` → IP da VPS | Hugo | ☐ |
| 4 | Instalar Chatwoot na VPS (`infra/instalar_vps.sh`) | Claude + Hugo | ☐ |
| 5 | Criar `atendimento@freshlogbr.com` no Google Workspace + senha de app | Hugo | ☐ |
| 6 | Verificação da empresa no Meta Business (CNPJ) | Hugo | ☐ |
| 7 | App na Meta + número na WhatsApp Cloud API + token permanente | Hugo (com roteiro abaixo) | ☐ |
| 8 | Conectar canal WhatsApp no Chatwoot | Claude + Hugo | ☐ |
| 9 | Conectar canal E-mail no Chatwoot | Claude + Hugo | ☐ |
| 10 | Criar usuários, equipes e caixas de entrada | Claude + Hugo | ☐ |
| 11 | Teste de aceite (critério de pronto) | Todos | ☐ |

As tarefas 1, 2, 3, 5 e 6 podem andar **em paralelo desde já**. A 6 (verificação Meta) é a mais lenta — dias a semanas — por isso deve ser disparada primeiro.

---

## 1. Número dedicado

- Comprar um **chip novo** (pré-pago ou controle) exclusivo do atendimento. Número fixo também funciona (a verificação pode ser por ligação).
- O número **não pode estar registrado no aplicativo WhatsApp**. Se já foi usado, apagar a conta no app antes (Configurações → Conta → Apagar conta).
- Depois de conectado à Cloud API, o número **não funciona mais no app do celular** — ele passa a viver só na API/Chatwoot.
- Guardar o chip num aparelho qualquer só para receber o SMS/ligação de verificação.

## 2. VPS

Requisitos: Ubuntu 24.04, **4 GB RAM / 2 vCPU** (2 GB roda, mas 4 GB dá folga para o bot da Fase 2 no mesmo servidor), 40 GB+ de disco.

Opções (valores de referência, conferir na contratação):

| Provedor | Plano | Local | Faixa de preço |
|----------|-------|-------|----------------|
| Hostinger (recomendado) | KVM 2 (2 vCPU/8 GB) | São Paulo | ~R$ 30–60/mês |
| Vultr | Regular 4 GB | São Paulo | ~US$ 24/mês |
| Hetzner | CX32 (4 vCPU/8 GB) | Europa | ~€ 7/mês (latência maior, funciona bem) |

Ao contratar: escolher Ubuntu 24.04, adicionar chave SSH (posso gerar), anotar o **IP fixo**.

## 3. DNS

No gerenciador do domínio `freshlogbr.com` (Registro.br, Cloudflare ou onde estiver):

```
Tipo A | Nome: atendimento | Valor: IP_DA_VPS | TTL: 300
```

Fazer isso **antes** da instalação — o certificado HTTPS só é emitido com o DNS apontando.

## 4. Instalação do Chatwoot

Arquivos prontos em [infra/](infra/): `docker-compose.yml` (Chatwoot + Postgres + Redis + Caddy com HTTPS automático), `env.exemplo` e `instalar_vps.sh`.

```bash
# do Windows (PowerShell ou Git Bash), na pasta atendimento/:
scp -r infra/ root@IP_DA_VPS:/opt/chatwoot

ssh root@IP_DA_VPS
cd /opt/chatwoot
bash instalar_vps.sh        # 1ª execução: cria o .env com segredos e para
nano .env                   # revisar SMTP (senha de app do Google — item 5)
bash instalar_vps.sh        # 2ª execução: baixa imagens, prepara banco, sobe tudo
```

Primeiro acesso em `https://atendimento.freshlogbr.com`: criar a conta **FreshLog** (o primeiro usuário vira administrador — usar o e-mail do gerente ou do Hugo). Em seguida trocar `ENABLE_ACCOUNT_SIGNUP=false` no `.env` e rodar `docker compose up -d`.

## 5. E-mail de atendimento

1. No Google Workspace Admin, criar a conta `atendimento@freshlogbr.com` (ou um alias/grupo, mas conta própria é mais simples para IMAP).
2. Na conta nova: ativar verificação em 2 etapas → gerar **senha de app** (https://myaccount.google.com/apppasswords). Ela serve para o SMTP do `.env` e para o canal de e-mail do Chatwoot.
3. Ativar IMAP (Gmail → Configurações → Encaminhamento e POP/IMAP).

## 6. Meta Business — verificação da empresa

1. Acessar https://business.facebook.com com a conta que administra a empresa.
2. Criar (ou usar) o portfólio empresarial da FreshLog.
3. **Central de Segurança → Iniciar verificação**: enviar CNPJ, razão social, site/domínio `freshlogbr.com` e telefone. A Meta pode pedir documento (cartão CNPJ, conta de consumo) e confirma por e-mail do domínio ou ligação.
4. Sem essa verificação, a API funciona em modo limitado (250 conversas/dia) — suficiente para testar, mas a verificação libera o uso pleno e o nome de exibição.

## 7. WhatsApp Cloud API

Com o portfólio criado (verificação pode estar em andamento):

1. https://developers.facebook.com → **Criar app** → tipo **Business** → vincular ao portfólio FreshLog.
2. No app, adicionar o produto **WhatsApp**. Isso cria uma **WhatsApp Business Account (WABA)**.
3. **API Setup → Add phone number**: cadastrar o número dedicado (item 1), verificar por SMS/ligação, definir nome de exibição ("FreshLog Atendimento" — a Meta aprova o nome).
4. Anotar: **Phone number ID** e **WhatsApp Business Account ID** (aparecem na tela API Setup).
5. **Token permanente** (o token de teste expira em 24 h):
   - business.facebook.com → Configurações do negócio → **Usuários do sistema** → criar usuário de sistema (função Administrador);
   - atribuir ativos: o **app** e a **WABA**, com controle total;
   - **Gerar token** com validade "nunca expira" e permissões `whatsapp_business_messaging` + `whatsapp_business_management`;
   - guardar o token em local seguro (é a senha do canal).

## 8. Canal WhatsApp no Chatwoot

1. Chatwoot → Caixas de entrada → **Adicionar** → WhatsApp → provedor **WhatsApp Cloud**.
2. Preencher: número (formato `+55...`), Phone number ID, Business Account ID e o token permanente.
3. O Chatwoot exibe a **URL de callback** e o **verify token**. Copiar os dois.
4. No app da Meta: WhatsApp → **Configuration → Webhook** → colar URL e verify token → **assinar o campo `messages`**.
5. Teste: mandar mensagem de um celular pessoal para o número → deve aparecer no Chatwoot; responder pelo Chatwoot → deve chegar no celular.

## 9. Canal E-mail no Chatwoot

1. Caixas de entrada → **Adicionar** → E-mail → `atendimento@freshlogbr.com`.
2. Configurar **IMAP** (imap.gmail.com, 993, SSL) e **SMTP** (smtp.gmail.com, 587, STARTTLS) com a senha de app do item 5.
3. Teste: enviar e-mail de fora → vira conversa no Chatwoot; responder → chega como e-mail normal.

## 10. Usuários, equipes e caixas

1. **Agentes**: convidar cada atendente por e-mail (função *Agente*).
2. **Gerente**: função *Administrador* — enxerga todas as conversas, relatórios e configurações.
3. **Times**: criar `Destinatários`, `Motoristas`, `Comercial/Financeiro` e alocar os agentes.
4. Ambas as caixas (WhatsApp e E-mail) visíveis para todos os times por enquanto — o roteamento fino vem com o bot da Fase 2.
5. Idioma da conta: Português (Brasil); fuso: São Paulo.

## 11. Teste de aceite

- [ ] WhatsApp de um celular pessoal → aparece no Chatwoot → resposta chega no celular.
- [ ] E-mail externo → vira conversa → resposta chega como e-mail.
- [ ] Dois atendentes logados ao mesmo tempo respondendo conversas diferentes do mesmo número.
- [ ] Gerente enxerga as conversas dos dois.
- [ ] `https://atendimento.freshlogbr.com` com cadeado (HTTPS) válido.

---

## Custos desta fase (referência)

| Item | Valor |
|------|-------|
| VPS | R$ 30–120/mês |
| Chip dedicado | R$ 20–40/mês |
| Chatwoot | R$ 0 (open source) |
| WhatsApp Cloud API | R$ 0 para conversas iniciadas pelo cliente |

## Notas de segurança

- O `.env` preenchido vive **só na VPS** — nunca commitar (o repositório tem apenas `env.exemplo`).
- Token permanente da Meta e senha de app do Google: guardar no gerenciador de senhas.
- Backup do Postgres entra como tarefa da Fase 1 (script diário + cópia para fora da VPS).
