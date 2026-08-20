# Fase 0 — Fundação da Central de Atendimento

Objetivo: infraestrutura no ar e canais conectados.
**Pronto quando:** uma mensagem de WhatsApp e um e-mail aparecem na mesma tela do Chatwoot e dois atendentes respondem pelo mesmo número ao mesmo tempo.

Plano geral do projeto: https://claude.ai/code/artifact/e06e4d13-79b6-4aff-acd2-7f0c07f48b7e

## Checklist geral

| # | Tarefa | Quem | Status |
|---|--------|------|--------|
| 1 | Comprar chip/número dedicado do atendimento | Hugo | ☑ |
| 2 | VPS (reaproveitada a VPS de produção já existente, `187.127.52.197`) | Hugo | ☑ |
| 3 | Criar DNS `atendimento.freshhub.com.br` → IP da VPS | Hugo | ☐ |
| 4 | Instalar Chatwoot na VPS (`infra/instalar_vps.sh`) + bloco no Caddy nativo | Claude + Hugo | ☑ |
| 5 | Senha de app do Google pra `hugo@freshlogbr.com` (SMTP técnico) | Hugo | ☐ |
| 6 | Verificação da empresa no Meta Business (CNPJ) | Hugo | ☐ (em revisão desde 18/08, Meta estima ~2 dias úteis) |
| 7 | App na Meta + número na WhatsApp Cloud API + token permanente | Hugo (com roteiro abaixo) | ☑ |
| 8 | Conectar canal WhatsApp no Chatwoot | Claude + Hugo | ☑ |
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

Decisão (18/08): em vez de contratar uma VPS nova só para o Chatwoot, reaproveitamos a VPS Hostinger de produção que já hospeda o resto do Stokki Eventos (`187.127.52.197`, Ubuntu 24.04.4, 4 vCPU/15 GB RAM, ~188 GB livres — folga de sobra pro Chatwoot). Chave SSH dedicada gerada em `~/.ssh/atendimento_vps` e instalada no `authorized_keys` da VPS.

Diferença importante em relação ao plano original: essa VPS já roda **Caddy nativo** (systemd, não em Docker) na frente de `app.freshhub.com.br` (guarda-chuva `/painel`, `/insucesso`) e `confirmacao.freshhub.com.br`. O Chatwoot segue o mesmo padrão de exceção do `confirmacao_motoristas`: **subdomínio próprio** (`atendimento.freshhub.com.br`), não path-prefix — Chatwoot não roda bem atrás de um path (assets/websockets esperam a raiz do domínio). O `docker-compose.yml` do Chatwoot **não inclui mais Caddy** — o rails publica só em `127.0.0.1:3000` e quem expõe pra internet é o Caddy nativo, via `Caddyfile-atendimento`.

Docker não estava instalado nessa VPS — o `instalar_vps.sh` cuida disso (`get.docker.com`). `ufw` já está ativo com 22/80/443 liberados, nada a mudar aí.

## 3. DNS

O domínio `freshhub.com.br` é gerenciado no **Registro.br** (nameservers `dns.br`). Lá, na zona do domínio:

```
Tipo A | Nome: atendimento | Valor: 187.127.52.197 | TTL: 300
```

Fazer isso **antes** de acrescentar o bloco no Caddy — o certificado HTTPS só é emitido com o DNS já apontando.

## 4. Instalação do Chatwoot

Arquivos prontos em [infra/](infra/): `docker-compose.yml` (Chatwoot + Postgres + Redis, sem Caddy embutido), `env.exemplo`, `instalar_vps.sh` e `Caddyfile-atendimento` (bloco a acrescentar no Caddy nativo da VPS).

```bash
# do Windows (PowerShell ou Git Bash), na pasta atendimento/:
scp -r infra/ root@187.127.52.197:/opt/stokki-eventos/atendimento/infra

ssh root@187.127.52.197
cd /opt/stokki-eventos/atendimento/infra
bash instalar_vps.sh        # 1ª execução: cria o .env com segredos e para
nano .env                   # revisar SMTP (senha de app do Google — item 5)
bash instalar_vps.sh        # 2ª execução: baixa imagens, prepara banco, sobe tudo (rails em 127.0.0.1:3000)
```

Depois, com o DNS já propagado, acrescentar o conteúdo de `Caddyfile-atendimento` ao `/etc/caddy/Caddyfile` nativo da VPS e rodar `caddy validate` + `systemctl reload caddy` (o Caddy emite o certificado Let's Encrypt automaticamente no reload).

Primeiro acesso em `https://atendimento.freshhub.com.br`: criar a conta **FreshLog** (o primeiro usuário vira administrador — usar o e-mail do gerente ou do Hugo). Em seguida trocar `ENABLE_ACCOUNT_SIGNUP=false` no `.env` e rodar `docker compose up -d`.

## 5. SMTP técnico (e-mails transacionais do Chatwoot)

Decisão (18/08): em vez de criar uma conta nova `atendimento@freshlogbr.com` só pra isso, o SMTP técnico (convites de usuário, reset de senha, notificações do próprio Chatwoot) usa a conta que já existe, `hugo@freshlogbr.com`. Isso é separado da caixa de entrada do suporte (essa sim, criada mais adiante — ver item 9).

1. Confirmar que `hugo@freshlogbr.com` tem verificação em 2 etapas ativa.
2. Gerar **senha de app** (https://myaccount.google.com/apppasswords) pra essa conta — vai no `SMTP_PASSWORD` do `.env`.

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

1. No Google Workspace Admin, criar a conta `atendimento@freshlogbr.com` (essa sim é a caixa de entrada real do suporte, separada do `hugo@freshlogbr.com` usado no SMTP técnico do item 5).
2. Na conta nova: ativar verificação em 2 etapas → gerar **senha de app** própria (https://myaccount.google.com/apppasswords) e ativar IMAP (Gmail → Configurações → Encaminhamento e POP/IMAP).
3. No Chatwoot: Caixas de entrada → **Adicionar** → E-mail → `atendimento@freshlogbr.com`.
4. Configurar **IMAP** (imap.gmail.com, 993, SSL) e **SMTP** (smtp.gmail.com, 587, STARTTLS) com a senha de app gerada no passo 2.
5. Teste: enviar e-mail de fora → vira conversa no Chatwoot; responder → chega como e-mail normal.

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
- [ ] `https://atendimento.freshhub.com.br` com cadeado (HTTPS) válido.

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
