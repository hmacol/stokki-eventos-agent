# WhatsApp pro cliente que não responde o chamado — especificação

Data: 30/09/2026
Estado: aguardando revisão do Hugo

## 1. Objetivo

Quando a equipe responde um chamado do portal e o cliente fica **10 minutos
sem responder**, o cliente recebe um WhatsApp no número dele avisando que há
uma resposta esperando retorno, com o link do portal.

É a primeira notificação por WhatsApp que sai **para fora** da empresa. O
e-mail que o cliente já recebe quando a equipe responde não muda; o WhatsApp
é um lembrete curto.

## 2. Decisões já tomadas (Hugo, 29/09)

| Tema | Decisão |
|---|---|
| Qual atendimento | Chamados do portal e do app (`portal_cliente/chamados.py`, tela `/painel/atendimento`). A central `atendimento/` (Evolution) fica intocada. |
| Quem recebe | Só embarcadores (chamados `tipo = CLIENTE`). Motoristas ficam de fora. |
| De onde vem o telefone | O próprio cliente informa no botão **Notificações** do portal. Sem número, não recebe. |
| Número remetente | O do Hugo, pelo gateway OpenWA que já está no ar. Risco de bloqueio/banimento por falar com desconhecidos aceito. |
| Horário | Só dentro do horário de atendimento dos chamados (config `portal_cliente.chamados.horario`; hoje seg–sex 08:30–17:00, almoço 13–14). |
| Repetição | Uma vez por silêncio: cada resposta da equipe que fica 10 min sem retorno gera um aviso; a mesma resposta nunca gera dois. |
| Cliente já leu | Não importa. Conta só a resposta. |
| Texto | Aviso + link do portal. Sem conteúdo da conversa. |

Decisões minhas, apresentadas ao Hugo em 29/09 e aceitas com o "pode escrever":

| Tema | Decisão |
|---|---|
| Resposta da equipe fora do horário | O aviso sai na abertura seguinte (ex.: 08:30 do próximo dia útil), se o cliente ainda não respondeu. |
| Chamados antigos | Resposta da equipe com mais de 3 dias não gera aviso (evita rajada no dia em que ligar). |
| Teto diário | Teto próprio pros clientes (padrão 30/dia), separado do teto de 50 dos alertas internos. Nenhum consome a cota do outro. |

## 3. Fora do escopo

- Receber ou ler a resposta que o cliente mandar pelo WhatsApp (cai no
  celular do Hugo e não entra no chamado; o texto pede pra responder no portal).
- Motoristas, transportadoras, chamados abertos pelo sistema sem cliente.
- Aviso quando é a **equipe** que demora (já existe: e-mail e grupo ALERTAS ATENDIMENTO).
- Qualquer mudança em `atendimento/`, Evolution ou no fluxo de e-mail dos chamados.
- Tela interna pra ver/editar o telefone do cliente (o Hugo edita pelo `/equipe` do portal, que já abre o modal de Notificações em modo equipe).

## 4. Arquitetura

```
timer (2 min) ──► avisar_cliente_sem_resposta.py
                      │  lê portal_chamados + portal_chamados_mensagens
                      │  lê preferencias_notificacao (telefone + chave)
                      ▼
                  notificar_whatsapp.despachar(...)  ──► integracao_openwa ──► OpenWA ──► 55DDDNÚMERO@c.us
                      │
                      └──► notificacoes_whatsapp (registro, dedupe, teto)
```

### 4.1 Arquivos novos

| Arquivo | Papel |
|---|---|
| `avisar_cliente_sem_resposta.py` (raiz) | Rotina de lote. Seleciona os chamados em silêncio, monta o texto e chama `notificar_whatsapp.avisar_cliente_sem_resposta`. Aceita `--modo-teste`. |
| `test_avisar_cliente_sem_resposta.py` | Testes unitários (unittest, SQLite em memória, sem rede). |
| `infra/stokki-avisar-cliente-sem-resposta.service` / `.timer` | Timer a cada 2 min, `User=www-data`, `OnFailure=stokki-alerta-falha@%n.service`, mesmo padrão dos outros. |

### 4.2 Arquivos alterados

| Arquivo | Alteração |
|---|---|
| `preferencias_notificacao.py` | Coluna `whatsapp TEXT NOT NULL DEFAULT ''` (migração aditiva em `_garantir_tabela`); tipo novo `chamado_sem_resposta` em `TIPOS` (grupo `whatsapp`); validação/normalização do telefone em `salvar`; `ler` devolve `whatsapp`; função nova `whatsapp_do_embarcador(conn, cnpj) -> str \| None`. |
| `portal_cliente/app.py` | `/api/notificacoes` GET devolve `whatsapp`; POST aceita `whatsapp` no corpo. |
| `portal_cliente/templates/_notificacoes.html` | Título vira "Notificações"; grupo novo "WhatsApp" com a chave do tipo; campo de telefone com ajuda. |
| `notificar_whatsapp.py` | Origem nova `cliente_sem_resposta`: texto, destino individual, `forcar_destino`, teto separado. Detalhes em 6. |
| `MAPA_DO_SISTEMA.txt` | Rotina, timer, coluna nova, seção de config. |
| `infra/openwa/LEIAME.md` | Como liberar conversa individual na chave `operator`. |

## 5. Regras da rotina

### 5.1 Chamado elegível

Um chamado entra quando **todas** valem:

1. `tipo = CLIENTE` e `status != RESOLVIDO`.
2. `ultima_origem = equipe` (a última mensagem não-sistema é da equipe, pelo portal ou por e-mail).
3. A última mensagem da equipe (`portal_chamados_mensagens`, maior `id` com `origem = equipe`) tem `criado_em` entre **10 minutos** e **3 dias** atrás. Os dois números vêm do config (`minutos`, `dias_max`).
4. Nenhuma mensagem de origem `cliente` depois dela (garantia extra além de `ultima_origem`; mensagem de `sistema` ou `assistente` não conta como resposta).
5. Ainda não existe linha em `notificacoes_whatsapp` com `origem = 'cliente_sem_resposta'` e `assinatura = 'msg:<id da mensagem>'`, **em qualquer situação** (enviado, falhou ou nao_enviado). Uma tentativa por silêncio; falha não é reenviada (regra da casa desde o 463).
6. O embarcador (`cnpj_embarcador` do chamado) tem telefone gravado e a chave `chamado_sem_resposta` ligada.
7. `situacao_horario(config)["dentro"]` é verdadeiro **no momento da rodada**. Fora do horário a rotina sai sem gravar nada, então o mesmo chamado é reavaliado na primeira rodada dentro do horário.

Consequência de 3 + 7: resposta às 16:55 → o cliente não respondeu → aviso às 08:30–08:32 do próximo dia útil. Resposta na sexta 16:55 → segunda 08:30 (2,x dias, dentro dos 3). Chamado que ficou parado mais de 3 dias não avisa nunca mais pra aquela mensagem.

### 5.2 Ordem e volume

- Chamados ordenados pela mensagem da equipe mais antiga primeiro.
- Um cliente com vários chamados em silêncio recebe um aviso por chamado, respeitando o intervalo mínimo entre envios (`intervalo_min_seg`, já existe) e o teto do dia.
- Rodada não envia mais que `teto_diario` dos clientes menos o que já saiu no dia; o resto fica pra amanhã (a linha `nao_enviado`/`teto` **não** é gravada nesse caso, senão a regra 5 mataria o aviso). Implementação: a rotina consulta o saldo antes e para quando acaba.

### 5.3 Texto (≤ 200 caracteres, link nunca cortado)

```
Fresh Log: respondemos o seu chamado #31 e aguardamos o seu retorno.
Responda pelo portal: https://app.freshhub.com.br/cliente/?chamado=31
```

Sem nome de empresa do cliente, sem assunto, sem trecho da conversa. O link
é o mesmo do botão "Ver o chamado no portal" dos e-mails
(`chamados._botao_ver`: `url_base(config) + "/?chamado=<id>"`).

## 6. Mudanças em `notificar_whatsapp.py`

Função nova `avisar_cliente_sem_resposta(chamado, telefone, link, config, **kw) -> str`:

- Desligado se `whatsapp_notificacoes.ativo` for falso **ou** `whatsapp_notificacoes.clientes.ativo` for falso. Duas chaves: a geral continua desligando tudo; a dos clientes desliga só isto.
- Destino: `55<DDD><número>@c.us`. Se `clientes.forcar_destino` estiver preenchido, o destino vira esse número e o texto ganha uma primeira linha `[teste → +55…]` com o número real (só nesse modo o limite de 200 pode estourar).
- `origem = 'cliente_sem_resposta'`, `tipo = 'chamado'`, `assinatura = 'msg:<id>'`.

Ajustes em `_motivo_para_nao_enviar`:

- **Janela de repetição** (120 min) não se aplica a esta origem: a rotina já garante uma tentativa por mensagem, e a janela deixaria repetir depois de 2 h.
- **Teto diário**: hoje conta todo `enviado` do dia. Passa a contar só as origens internas (`origem != 'cliente_sem_resposta'`) pro teto de 50; a origem dos clientes tem o próprio `clientes.teto_diario` (padrão 30) contado só sobre ela.
- **Disjuntor** (3 falhas seguidas → pausa de 60 min) e **intervalo mínimo** continuam compartilhados: o gateway e o número são os mesmos.

Tudo o mais (registro, log, "nunca levanta exceção", e-mail quando o canal para) é reaproveitado.

### 6.1 Config (`config.yaml`, seção que já existe)

```yaml
whatsapp_notificacoes:
  ativo: true                  # já está assim na VPS
  ...
  clientes:
    ativo: false               # ligar é decisão do Hugo
    minutos: 10
    dias_max: 3
    teto_diario: 30
    forcar_destino: "5511XXXXXXXXX"   # número do Hugo enquanto for piloto; vazio = envio real
```

## 7. Telefone no portal

- **Onde**: modal Notificações (`_notificacoes.html`). Grupo novo "WhatsApp" com a chave "Chamado aguardando sua resposta" (descrição: "Se a Fresh Log responder um chamado e você não retornar em 10 minutos, avisamos neste WhatsApp com o link do chamado. Só em horário de atendimento.") e um campo "WhatsApp (celular com DDD)".
- **Validação** (`preferencias_notificacao._validar_whatsapp`): só dígitos; aceita 10 ou 11 dígitos (DDD + número) ou 12–13 começando com 55; grava sempre `55` + DDD + número (12–13 dígitos). Vazio apaga. Qualquer outra coisa: `ValueError` com mensagem pronta pra tela ("Informe o celular com DDD, ex.: (11) 99999-0000.").
- **Chave**: tipo `chamado_sem_resposta` em `TIPOS`, `default: True` (preencher o número basta; a chave serve pra pausar sem apagar o número). Sem número gravado, ligado ou não, nenhum aviso sai — a ajuda do campo diz isso.
- **Grupo econômico**: o telefone é do CNPJ que loga (`g.cliente["cnpj"]`), igual aos e-mails. O chamado guarda `cnpj_embarcador` desse mesmo login.
- **Equipe**: quem tem nível de envio no `/equipe` edita; leitura só vê. Mesma regra dos e-mails.
- `carregar_embarcadores` não muda (é só pra e-mail). A rotina usa `whatsapp_do_embarcador(conn, cnpj)`: devolve o número só se o tipo estiver ligado e houver número.

## 8. Gateway (VPS, fora do código)

- A chave `operator` do OpenWA está restrita aos dois grupos (`allowedChats`). Pra conversa individual precisa liberar: ou tirar a restrição de chats da chave (mantendo a restrição à sessão), ou criar uma segunda chave só pra esta origem. Decisão no deploy, com a chave admin de `/opt/openwa/data/.api-key`. Registrar no `LEIAME.md`.
- **Nono dígito**: parte dos números brasileiros existe no WhatsApp sem o 9. Antes de implementar, verificar se o OpenWA expõe consulta de número (whatsapp-web.js tem `getNumberId`). Se houver, a rotina consulta antes de enviar e registra `nao_enviado` / `numero_sem_whatsapp` se não existir (esse registro **conta** como tentativa, regra 5.1-5). Se não houver, envia como informado e a limitação fica documentada no MAPA.
- `forcar_destino` apontando pro número do Hugo envia de um número pra ele mesmo (conversa "Você"). O WhatsApp aceita; se o gateway recusar, o teste usa o grupo ALERTAS ATENDIMENTO como destino forçado.

## 9. Erros e proteções

| Situação | Comportamento |
|---|---|
| Gateway fora do ar / 4xx | `falhou` registrado; sem reenvio pra essa mensagem; disjuntor compartilhado após 3 seguidas + e-mail que já existe. |
| Teto dos clientes atingido | Rotina para antes de gravar; chamados restantes ficam pro próximo dia útil (ou saem do prazo de 3 dias). |
| Cliente responde entre a seleção e o envio | Janela de segundos; aceito. |
| Telefone inválido no banco (importado à mão) | `whatsapp_do_embarcador` devolve `None` se não bater a regra; log de aviso. |
| Feriado | Não há calendário de feriados; o horário só olha dia da semana. Mesmo comportamento dos chamados hoje. |
| Falha na rotina | `OnFailure` do systemd manda o alerta de job como nas outras. Falha aqui nunca afeta o portal nem o painel: a rotina é separada. |

## 10. Testes

`test_avisar_cliente_sem_resposta.py` (unittest, banco em memória com as tabelas reais criadas por `chamados.conectar`-equivalente e `preferencias_notificacao._garantir_tabela`):

- Elegível: equipe respondeu há 11 min, cliente com número → 1 envio, assinatura `msg:<id>`.
- Não elegível: 9 min; cliente respondeu depois; resolvido; motorista; sem número; chave desligada; mensagem com mais de 3 dias; fora do horário (nada gravado).
- Uma tentativa por silêncio: segunda rodada não repete; nova resposta da equipe depois de o cliente falar gera aviso novo.
- Mensagem de `sistema` depois da equipe não conta como resposta do cliente.
- Teto: `clientes.teto_diario = 1` com 2 chamados → 1 envio, 0 linhas `nao_enviado`.
- Texto ≤ 200 e com link inteiro.
- `forcar_destino` redireciona e prefixa.
- `notificar_whatsapp`: teto interno ignora a origem dos clientes e vice-versa; janela de repetição não barra `cliente_sem_resposta`.
- `preferencias_notificacao`: normalização (`11999990000` → `5511999990000`, `(11) 99999-0000` idem, `5511…` mantido), rejeições, vazio apaga, `ler`/`salvar` com a coluna nova, migração em banco que já tinha a tabela.
- `--modo-teste`: mostra o que enviaria e não grava.

Prova na VPS antes de ligar: rodar `--modo-teste` como `www-data`; depois, com `clientes.ativo: true` e `forcar_destino` = número do Hugo, um chamado real do cliente teste (CNPJ 00.000.000/0001-91) e ver a mensagem chegar no celular dele.

## 11. Entrega e ligar

1. Código + testes locais → commit → deploy (pull, chown, timer novo, restart do `portal-cliente` porque o modal e a API mudam).
2. Liberar a chave do gateway pra conversa individual.
3. `clientes.ativo: true` + `forcar_destino` do Hugo → prova real com o cliente teste.
4. Hugo esvazia `forcar_destino` quando quiser o envio de verdade. Até lá nenhum cliente recebe nada.
