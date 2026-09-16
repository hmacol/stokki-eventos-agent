# Plano de Execução: Saída da Vuupt (banco próprio como fonte única)

**Status (15/09): etapas 0, 1 e 2 em andamento.** Plano combinado com o Hugo
em 12/09/2026 pra abandonar de vez o banco de dados da Vuupt com segurança.
Complementa o `DOC_EXECUCAO_CLAUDE_APP_MOTORISTAS.md` (que cobre o app e as
fases A–E) — este documento cuida da **troca da fonte de verdade**: tudo que
ainda depende da Vuupt, na ordem certa, com critério de passagem e volta atrás.

Ler antes de mexer em qualquer coisa que leia ou grave na Vuupt.

---

## 1. Decisões do Hugo

| Data | Decisão |
|---|---|
| 12/09 | Abandonar o banco da Vuupt **por etapas reversíveis** (plano da seção 3), nunca big bang. Cada etapa roda em sombra, tem critério objetivo e volta atrás por configuração. |
| 12/09 | **Até a virada de chave real, tudo o que for feito no sistema próprio é replicado na Vuupt** ("tem uma questão de relatórios do meu financeiro"). A Vuupt continua completa enquanto o financeiro depender dela. |
| 15/09 | **Km e valor do motorista: o nosso cálculo é sempre a base; o da Vuupt é ignorado.** (`regras/km_cobrado.py` + `regras/tarifa_motorista.py`; o `done_trip_distance` e o custo configurado na Vuupt não entram em conta nenhuma.) |
| 15/09 | Relatórios do financeiro ficam para uma etapa posterior. Seguir com o plano. |

**Pendentes (perguntadas em 12/09, sem resposta ainda):**
1. Contrato da Vuupt: data de fim, aviso prévio, exportação oficial e acesso só-leitura depois do fim.
2. Congelar integrações novas com a Vuupt (hoje: trava branda por teste, seção 3, etapa 0).
3. Backup contínuo (Litestream) e Postgres antes do piloto real.
4. Motoristas do piloto e aparelhos.
5. Como uma rota Lalamove/transportadora "fecha" no núcleo (hoje ficam PLANEJADA pra sempre).
6. Relatórios do financeiro: quais, pra quê, periodicidade (adiado pelo Hugo em 15/09).

---

## 2. Situação medida em 12/09 (produção)

**O que só existe na Vuupt:** 7.844 rotas, 62.527 serviços, 59.496 checklists e
11.891 customers (desde o 2º semestre/2023). O núcleo só tem de 15/07 em diante.

**O que a Vuupt ainda é dona:**
- **Pool de pedidos**: Planejamento, `criar_rotas_diarias.py` e o pipeline leem `not_assigned` ao vivo; `rascunhos_parada.service_id` é NOT NULL.
- **Execução e status**: Torre, expedição na Stokki, portal do cliente, pedidos parados leem a Vuupt.
- **Canhoto**: `expedir_pedidos.py` e o portal baixam `checklists/{id}/print`; ninguém lê `nucleo_comprovantes`.
- **Ciclo do pedido**: reentrega -R/-C, cancelar, reagendar, endereço, retiradas (agente 50259), Lalamove (50258), coleta Quatro Estrelas — só na Vuupt. Das escritas, só 2 espelham no núcleo (pipeline e envio de rascunho).
- **Identidade**: motorista = `agent_id`, embarcador = `sender_id` (chave do portal), destinatário = `customer_id`.
- **E-mail ao embarcador**: a Vuupt manda e-mail no início e no fim de cada entrega (2.661 de 6.501 "finalizado" abertos). Ao destinatário, nada (0 SMS, 0 aberto). Precisa de substituto na virada.

**Espelho do núcleo com defeitos medidos (12/09):**
1. `nucleo/pedidos.py:84` grava `status or ABERTO` → `status=None` (sync de rota cancelada **e** gancho do pipeline) volta o pedido pra ABERTO. 15 pedidos entregues estavam ABERTO.
2. Parada que sai da rota na Vuupt nunca sai do núcleo: 27 service_id "vivos" em 2 rotas.
3. Código com e sem `#`: 22 pedidos duplicados; `/consulta` não achava `PS-38552`.
4. Horários da Vuupt em UTC (sem fuso) misturados com hora local do app na mesma coluna.
5. Rota de teste (réplica APP) sobrescrevia o status do pedido real pelo código.
6. Sync só olha hoje e ontem; rota excluída/remarcada/sem motorista depois disso nunca chega.
7. 11 rotas passadas presas em PLANEJADA (Iago 50191, Rafael 50199, Lalamove) — não fecharam nem na Vuupt.

**App próprio:** 0 fotos de canhoto no servidor até 12/09; nenhuma rota real.
**Backup:** diário 03h (VACUUM INTO → GCS, 30 dias), sem fotos, sem restauração testada, sem alerta.
**Infra fora do git:** `stokki-reconciliar-retirada.timer` (roda `--cancelar` de 2 em 2 h) só existia na VPS.

**Limites da réplica na Vuupt (medido 12/09):** fechar serviço pela API não fecha a rota
(5 de 6 rotas Lalamove seguem `assigned`, sem km/duração/custo); km real e tempo de rota
(`done_trip_distance`) só o app da Vuupt produz; checklist preenchido não entra pela API
(foto só como anexo).

---

## 3. Etapas e critérios

Etapas 0, 1 e 2 começam juntas. Estimativa total com os critérios: 4 a 6 meses.

### Etapa 0 — Blindar e parar de aumentar a dependência
- [x] Ensaio de restauração cronometrado (`ensaiar_restauracao_backup.py` + timer semanal). **Rodado em produção 15/09: snapshot de 70,2 MB, download 0,8 s, integrity_check ok, 92 tabelas, núcleo sobe e lê — 1,8 s até o banco utilizável.**
- [x] Backup de hora em hora + fotos de `dados/comprovantes` (`backup_dados_gcs.py --horario`, retenção 48 h); diário com `Persistent=true`. *(falta deploy)*
- [x] Alerta por e-mail quando job crítico falhar: `alertar_falha_job.py` + `infra/stokki-alerta-falha@.service`, `OnFailure=` em 18 unidades, janela anti-enxurrada de 2 h. *(falta deploy)*
- [x] Trava branda: `nucleo/test_acoplamento_vuupt.py` (lista congelada de 49 arquivos; arquivo novo chamando a Vuupt reprova).
- [x] Infra no git (`stokki-reconciliar-retirada.*`, que só existia na VPS); `sincronizar_painel_vps.py` aposentado.
- [ ] (antes do piloto real) Backup contínuo (Litestream) + cópia em bucket separado — decisão 3 do Hugo.
- **Critério:** restauração ensaiada com sucesso ✅ + alerta testado (depois do deploy).

### Etapa 1 — Exportar o histórico da Vuupt (agora, não no fim)
- [ ] Exportador resumível e com limite de velocidade: rotas, serviços, customers, agentes, veículos, checklists (JSON) e PDFs dos checklists → GCS `arquivo_vuupt/`.
- **Critério:** contagens batem 100% com `meta.pagination.total`.

### Etapa 2 — Espelho fiel e comprovado
- [x] Comparador Vuupt × núcleo, pedido a pedido (`nucleo/comparar_vuupt.py`, só leitura dos dois lados).
- [x] Corrigidos os defeitos 1–6 da seção 2 (`nucleo/normalizacao.py`, `pedidos.py`, `sincronizar_vuupt.py`, `operacao.py`, `rotas.py`, `consulta.py`) + migração do que já estava gravado (`nucleo/migrar_espelho_15_09.py`, passo a passo, registrado em `nucleo_migracoes`, não roda duas vezes).
- [x] **Provado numa cópia do banco de produção (15/09):** 10 dias, 84 rotas, 687 paradas, 630 pedidos — **687 divergências → 203 (migração) → 0 (sync novo com janela de 8 dias)**.
- [ ] Deploy + backfill `--dias 10` em produção e comparador rodando diariamente.
- [ ] Sync incremental de serviços (pool, cancelados, reagendados, retiradas, reentregas) — em vez de gancho em cada escrita.
- [ ] Ponte núcleo → Vuupt para rotas do app (decisão de 12/09): criar/atribuir/fechar serviços, anexar foto; testar fechamento da rota e e-mail ao embarcador numa rota de teste.
- **Critério:** 10 dias úteis seguidos sem divergência sem explicação.

### Etapa 3 — Leituras passam para o núcleo, uma de cada vez
- Cada job/tela ganha `fonte: vuupt | nucleo` no config e roda em sombra até zerar diferenças.
- Ordem: romaneios e transportadoras → relatórios → Torre → portal → pedidos parados → **expedição na Stokki por último**.

### Etapa 4 — Pedido e rota nascem no núcleo
- IDs próprios (pedido, rota, motorista, embarcador, cliente); ID da Vuupt vira referência.
- Pool do Planejamento lido do núcleo; `provedor` (VUUPT/APP/LALAMOVE) no rascunho.

### Etapa 5 — Motoristas para o app, rota a rota
- Antes do 1º motorista real: canhoto chega e aparece no painel/portal/Stokki; fila offline testada; botão "devolver pra Vuupt"; plano papel; cadastro completo; réplica na Vuupt funcionando (decisão de 12/09).
- Hugo → 1 motorista (1 semana) → 3 (2 semanas) → metade → todos.
- Critério por degrau: 100% com resultado e foto; nenhum entregue sem expedição; extrato conferido; nenhuma reclamação de embarcador.

### Etapa 6 — Vuupt só leitura, depois o corte
- A réplica só desliga na virada de chave (depois do financeiro validado).
- 30 a 60 dias sem rota nova na Vuupt, alerta se algo gravar; exportação final; cancelar contrato.

---

## 4. Armadilhas

- **Trabalho paralelo**: outras sessões commitam ao vivo. Commitar só os próprios arquivos, pelo nome; nunca `git add -A`.
- **VPS**: scripts manuais sempre `sudo -u www-data venv/bin/python` (WAL + dono dos arquivos). `git pull --no-rebase`.
- **Horário da Vuupt**: `start_at`, `created_at`, `started_at`, `arrived_at`, `completed_at`, `scheduled_*`, `canceled_at` vêm **sem fuso e em UTC**. No núcleo, tudo em hora local de São Paulo.
- **Rota cancelada na Vuupt** lista os serviços com status GLOBAL (entregues noutra rota) — nunca contar.
- **Código do pedido**: a Vuupt usa `#PS-x` desde 20/08. No núcleo a chave é sem `#`, maiúscula, com sufixo (`PS-x-R1`).

---

## 5. Log de status

- **12/09** — Levantamento completo (3 sub-agentes + consultas somente leitura na VPS e na API da Vuupt). Plano de 7 etapas entregue no chat. Hugo: replicar tudo na Vuupt até a virada (relatórios do financeiro).
- **15/09** — Hugo: nosso cálculo de km é a base (ignorar Vuupt); relatórios ficam pra depois; seguir com o plano.
  Etapa 0 e primeira metade da Etapa 2 escritas e testadas (163 testes verdes), **aguardando o Hugo autorizar commit e deploy**.
  Medições do dia: ensaio de restauração aprovado em produção (1,8 s); linha de base do comparador = 687 divergências em 10 dias,
  zeradas numa cópia do banco depois da migração + sync novo. As 203 que sobraram depois da migração eram todas das 9 rotas de
  08 e 09/09 fechadas na Vuupt dias depois — fora da janela de 2 dias do sync antigo, que agora é de 8 dias + 1 à frente.
  Ordem de deploy combinada: (1) Etapa 0 (backup/alerta/units), (2) código do núcleo + restart `painel-agentes` e `motorista-api`,
  (3) `migrar_espelho_15_09.py` com backup do dia conferido, (4) `sincronizar_vuupt.py --dias 10`, (5) comparador pra confirmar zero.
