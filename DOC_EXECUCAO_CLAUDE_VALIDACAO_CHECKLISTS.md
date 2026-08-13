# Agente de Validação de Checklists (Canhotos)

Criado em 13/08/2026 a pedido do Hugo: *"criar um agente para fazer a
validação dos checklists dos meus motoristas já com a liberdade de
clonar os pedidos quando a foto do checklist não estiver no padrão
correto"*.

Substitui a validação manual de canhotos na tela do VUUPT. Roda em
`validacao_checklists\validar_checklists.py`.

## O que ele faz

1. **Busca** no VUUPT os serviços entregues com sucesso (`status_done=success`,
   code `PS-*`) na janela de horas cujo checklist foi preenchido mas ainda
   não validado (`checklistAnswers[0].validated_at = null`).
2. **Baixa** o PDF do canhoto (`GET /api/v1/checklists/{id}/print`) e
   **extrai as fotos embutidas em resolução original** (a foto do motorista
   vai a ~2000px; renderizada na página do PDF ela encolhe e virava falso
   "ilegível" — visto no primeiro teste).
3. **Julga** com a visão do Claude (`claude-opus-5`, saída estruturada em
   JSON com enum `aprovada|reprovada|duvida`).
4. Age conforme a decisão:
   - **APROVADA** → `PUT /api/v1/checklists/{id}/validate` (rota descoberta
     em 13/08 — mesmo efeito da validação manual; `validated_by_id` fica o
     usuário do token). O `expedir_pedidos.py` expede na Stokki no ciclo
     seguinte, sozinho.
   - **REPROVADA** → **clona o pedido no VUUPT** pra recoleta de canhoto:
     code com sufixo `-C1` (`-C2`...), título com `(recoleta canhoto)`,
     campo nativo `recreated_order_origin_id` apontando pro original. O
     clone nasce `not_assigned` e entra na roteirização normal. O original
     fica pendente de validação manual. E-mail interno avisa.
   - **DÚVIDA** → não valida nem clona; entra na seção "revisão manual" do
     e-mail e segue o fluxo manual no VUUPT (decisão do Hugo, 13/08: na
     dúvida, deixa pra humano).

## Critérios de reprovação (Hugo, 13/08 — qualquer um reprova)

- Ilegível ou borrada (não dá pra ler o conteúdo)
- Sem assinatura/identificação de quem recebeu
- Documento errado (foto de caixa, fachada, tela de celular...)
- Cortada ou incompleta (parte do canhoto fora do quadro)
- Checklist preenchido **sem nenhuma foto** (reprova direto, sem gastar API)

O prompt esclarece que canhoto é a **tira destacável do DANFE** — formato
estreito é normal e não reprova.

**Exceção NF dispensada:** Padrão Puro, Quatro Estrelas e Pedramoura
entregam sem NF (`SENDERS_SEM_NF`, mesmos sender_ids da canhoteira do
`gerar_pdf_romaneios.py`). Pra eles o documento válido é a folha de
**canhoteira do romaneio** assinada — mesmos critérios de legibilidade/
assinatura/enquadramento, sem reprovar por "não ser canhoto de NF".

## Arquivos

| Arquivo | Papel |
|---|---|
| `validacao_checklists\validar_checklists.py` | Agente principal |
| `validacao_checklists\fingerprint_validacao.py` | Tabela `checklists_analisados` no `dados\dados.db` — cada checklist é analisado UMA vez (evita repagar API e reclonar) |
| `criar_tarefa_validacao_checklists.ps1` | Cria a tarefa agendada (rodar como admin) |
| `dados\validacao_checklists.log` | Log do script |
| `dados\canhotos\` | Cache de PDFs (compartilhado com o expedir) |

## Execução

```
py -3.11 validacao_checklists\validar_checklists.py                # real, janela 48h
py -3.11 validacao_checklists\validar_checklists.py --modo-teste   # só analisa e loga
py -3.11 validacao_checklists\validar_checklists.py --limite 5     # limita a análise
```

Agendamento: tarefa `StokkiEventos_ValidacaoChecklists`, a cada 30 min,
07h50–19h25 — **10 minutos antes** de cada disparo da expedição frequente
(08h00–19h30), pra que canhotos aprovados sejam expedidos no mesmo ciclo.
Criar com `.\criar_tarefa_validacao_checklists.ps1` (admin).

## Decisões de desenho

- **Claude via `requests`** (padrão do projeto — `ler_respostas_insucesso.py`,
  `regras\endereco.py`); SDK `anthropic` não está instalado no 3.11.
- **`fallbacks: "default"`** na chamada: se o classificador de segurança
  recusar (falso positivo raro em imagem), a própria API reexecuta no
  modelo reserva. Recusa da cadeia inteira → decisão vira `duvida`.
- **Erro em qualquer etapa** (PDF, API, validação, clone) → **não grava
  fingerprint** → o checklist é retentado na execução seguinte.
- Clone usa sufixo `-C` pra não confundir com as reentregas `-R` do
  fluxo de insucesso.
- E-mail usa a mesma config `email:` do `config.yaml` e o padrão visual
  das notificações do expedir.

## Validação em produção (13/08)

Primeira execução real (`--limite 3`): 2 canhotos aprovados e validados
no VUUPT (confirmado `validated_at` preenchido via API), 1 reprovado de
verdade — PS-36507/Padrão Puro, foto de um carro na fachada em vez de
documento — clonado como `PS-36507-C1` (id 52121682, `not_assigned`,
origem apontando pro original). E-mail de aviso entregue.
