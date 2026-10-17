# Notificações internas por WhatsApp (OpenWA) — especificação

Data: 28/09/2026
Estado: aguardando revisão do Hugo

## 1. Objetivo

Algumas notificações internas que hoje saem só por e-mail passam a sair
**também** por WhatsApp, num grupo interno. É um teste: volume baixo, só
avisos internos, desligado por padrão.

O e-mail não muda. O WhatsApp é um aviso curto; o e-mail continua sendo o
registro completo.

## 2. Decisões já tomadas (Hugo, 28/09)

| Tema | Decisão |
|---|---|
| Relação com a central de atendimento | Nenhuma. `atendimento/` e a Evolution API ficam intocados. |
| Gateway | OpenWA (github.com/rmyndharis/OpenWA), licença MIT, container próprio. |
| Número remetente | O número do próprio Hugo, pareado no gateway. Risco de banimento conhecido e aceito. |
| Destino | Um grupo interno de WhatsApp (criado ou escolhido pelo Hugo). |
| Primeira leva | Falha de job, entregues não expedidos (07h15), resumo das rotinas, rotas sem motorista. |
| Regra do resumo das rotinas | Avisa quando há erro (qualquer rotina) ou quando o script está na lista "sempre avisar". |
| MCP | Desligado. Não é necessário: as rotinas chamam a API REST. |

## 3. Fora do escopo

- Notificações para clientes, motoristas ou transportadoras.
- Receber ou responder mensagens.
- Tela no painel para ligar/desligar ou parear.
- Qualquer alteração em `atendimento/`, `integracao_evolution.py` ou na
  Evolution API.

## 4. Arquitetura

```
rotina (timer) ──► notificar_whatsapp.py ──► integracao_openwa.py ──► OpenWA (127.0.0.1:2785) ──► grupo
       │
       └──► e-mail (como hoje)
```

### 4.1 Arquivos novos

| Arquivo | Papel |
|---|---|
| `integracao_openwa.py` | Transporte. `configurado(cfg)` e `enviar_texto(cfg, destino, texto) -> (ok, id_mensagem)`. Nunca levanta exceção. Mesmo contrato de `integracao_evolution.enviar_texto`. |
| `notificar_whatsapp.py` | Camada chamada pelas rotinas. Decide se envia, monta o texto, aplica os limites, registra o resultado. |
| `test_notificar_whatsapp.py` | Testes unitários (unittest), sem rede. |
| `infra/openwa/docker-compose.override.yml` | Aplicado sobre o compose do próprio OpenWA (clonado em `/opt/openwa`). Prende a porta em 127.0.0.1 e fixa o motor. Sem segredo. |
| `infra/openwa/LEIAME.md` | Como subir, parear e descobrir o grupo. |

### 4.2 Arquivos alterados

| Arquivo | Alteração |
|---|---|
| `alertar_falha_job.py` | Depois do e-mail, chama `notificar_whatsapp.avisar_falha_job(...)`. |
| `verificar_entregues_nao_expedidos.py` | Depois do e-mail, chama `notificar_whatsapp.avisar_nao_expedidos(...)`. |
| `notificar_execucao_agente.py` | Em `notificar_execucao()`, depois do e-mail, chama `notificar_whatsapp.avisar_execucao(...)`. Cobre as 22 rotinas que usam o resumo. |
| `MAPA_DO_SISTEMA.txt` | Arquivos novos, porta 2785, tabela nova, seção de configuração. |

Cada chamada fica dentro de `try/except` próprio: falha no WhatsApp nunca
altera o resultado do e-mail nem da rotina.

### 4.3 Gateway

- Container Docker na VPS, banco SQLite próprio, porta 2785 em 127.0.0.1.
  Nunca exposto pelo Caddy.
- Motor: WhatsApp Web (padrão do OpenWA).
- `MCP_ENABLED` ausente (desligado).
- Chave de API do gateway com papel `operator`, restrita à sessão das
  notificações.
- Pareamento feito pelo Hugo pelo painel do próprio OpenWA, acessado por
  túnel SSH (`ssh -L 2785:127.0.0.1:2785`).

## 5. Configuração

Seção nova no `config.yaml` da VPS (valores reais não entram no git):

```yaml
whatsapp_notificacoes:
  ativo: false                 # ligar é decisão do Hugo
  base_url: "http://127.0.0.1:2785/api"
  api_key: "..."
  sessao: "..."
  grupo_id: "..."
  sempre_avisar:
    - cancelar_rotas_sem_motorista
    - criar_rotas_diarias
    - executar_tudo
  teto_diario: 20
  intervalo_min_seg: 20
  janela_repeticao_min: 120
  falhas_para_alerta: 3
```

Seção ausente ou `ativo: false`: nada é enviado e nada quebra.

## 6. Regras de envio

Aplicadas nesta ordem por `notificar_whatsapp`:

1. **Chave**: `ativo` falso ou configuração incompleta → não envia.
2. **Modo teste**: rotina em `--modo-teste` → não envia; registra no log o
   texto que enviaria.
3. **Relevância** (só para o resumo das rotinas): envia se há etapa com
   erro, ou se o nome do script (`Path(sys.argv[0]).stem`) está em
   `sempre_avisar`. Falha de job e não expedidos sempre passam, porque a
   rotina de origem já filtra.
4. **Repetição**: mesma origem com o mesmo resumo de erro dentro de
   `janela_repeticao_min` → não envia.
5. **Teto diário**: `teto_diario` mensagens enviadas no dia → não envia.
6. **Intervalo**: se a última mensagem saiu há menos de
   `intervalo_min_seg`, espera a diferença antes de enviar.

Toda decisão de não enviar é registrada com o motivo.

Volume esperado: 3 mensagens fixas por dia, mais a das 07h15 quando houver
pendência, mais erros.

## 7. Formato das mensagens

Texto puro com formatação do WhatsApp. Sem log, sem tabela, sem nome de
cliente, endereço ou número de NF. Só contagens e nomes de rotina.

Resumo de rotina, sucesso:
```
✅ *Criação de rotas* · 28/09 18:42
3 etapas concluídas sem erro (4,2 min)
```

Resumo de rotina, com erro (detalhe cortado em 200 caracteres por etapa,
no máximo 3 etapas):
```
❌ *Documentos dos pedidos* · 28/09 18:51
1 de 3 etapas com erro: Anexar DANFE
Anexar DANFE: <detalhe>
Detalhes completos no e-mail.
```

Falha de job:
```
🚨 *Job da VPS falhou*
stokki-backup-gcs.service
Resultado: exit-code (código 1) · 28/09 03:00
Log completo no e-mail.
```

Entregues não expedidos:
```
⚠️ *Checagem da expedição*
4 pedidos entregues sem expedição na Stokki
2 rotas sem terminar · 1 retirada parada há mais de 7 dias
Lista completa no e-mail.
```

Linhas com contagem zero são omitidas. Rotina em modo teste não envia,
então não existe prefixo de modo teste no WhatsApp.

## 8. Falhas

- Gateway fora do ar, sessão desconectada ou erro HTTP: registra e segue.
  **Sem reenvio automático** (lição do erro 463 em 27/08: insistir piora).
- `falhas_para_alerta` falhas seguidas de envio: um e-mail único ao Hugo
  ("WhatsApp das notificações parou"). Novo e-mail só depois de um envio
  bem-sucedido seguido de nova sequência de falhas.
- Timeout de 15 s na chamada ao gateway.

## 9. Registro

Tabela nova em `dados/dados.db`, criada sob demanda:

```sql
CREATE TABLE IF NOT EXISTS notificacoes_whatsapp (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    criado_em    TEXT NOT NULL,      -- horario local, ISO
    origem       TEXT NOT NULL,      -- nome do script ou da unidade systemd
    tipo         TEXT NOT NULL,      -- execucao | falha_job | nao_expedidos
    assinatura   TEXT,               -- resumo do erro, para a regra de repeticao
    situacao     TEXT NOT NULL,      -- enviado | falhou | nao_enviado
    motivo       TEXT,               -- por que nao enviou ou falhou
    id_mensagem  TEXT
);
```

Sustenta o teto diário, a regra de repetição, o intervalo mínimo e a
contagem de falhas seguidas. Escrita sempre como `www-data` na VPS.

## 10. Testes

Unitários (`py -3.11 -m unittest test_notificar_whatsapp`), com o
transporte substituído por um falso e banco em memória:

- texto de cada um dos quatro formatos, incluindo corte de detalhe e
  omissão de contagem zero;
- relevância: erro envia, script da lista envia, sucesso fora da lista não;
- repetição dentro e fora da janela;
- teto diário;
- contagem de falhas seguidas e disparo único do e-mail de alerta;
- modo teste e chave desligada não chamam o transporte;
- exceção no transporte não se propaga.

Os testes existentes de `notificar_execucao_agente` continuam passando.

Prova real na VPS, como `www-data`:

1. mensagem manual ao grupo por `integracao_openwa.enviar_texto`;
2. `alertar_falha_job.py <unidade> --forcar` e conferência no grupo e na
   tabela.

## 11. Ordem de execução

1. **Prova do gateway (antes de qualquer código de produção):** subir o
   container, parear o número, descobrir o identificador do grupo e enviar
   uma mensagem manual. Confirma na prática os pontos da seção 12. Se
   falhar, parar e reavaliar.
2. `integracao_openwa.py`, ajustado ao que a prova mostrou.
3. `notificar_whatsapp.py` e testes.
4. Os três pontos de chamada.
5. Mapa do sistema, commit, deploy com `ativo: false`.
6. Hugo liga a chave; prova real.

## 12. Pontos a confirmar na prova

Conhecidos só pela documentação, não testados:

- rota e corpo exatos do envio de texto na API REST do OpenWA;
- formato do identificador de grupo e como listá-lo;
- se o envio para grupo funciona com o motor WhatsApp Web;
- consumo de memória do container em uso;
- comportamento da sessão após reinício do container (precisa parear de
  novo ou não).

## 13. Riscos

| Risco | Tratamento |
|---|---|
| Banimento do número do Hugo | Aceito por ele. Volume baixo, só grupo interno, teto diário, intervalo mínimo, sem reenvio. |
| Canal parar em silêncio | E-mail único após 3 falhas seguidas. |
| Grupo virar ruído | Regra de relevância e lista curta de "sempre avisar". |
| Dado de cliente em grupo | Mensagens só com contagens e nomes de rotina. |
| Conflito com a central de atendimento | Container, banco, porta e configuração separados. |
