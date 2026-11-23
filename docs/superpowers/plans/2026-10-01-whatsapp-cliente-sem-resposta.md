# WhatsApp pro cliente que não responde o chamado — plano de implementação

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** quando a equipe responde um chamado do portal e o embarcador fica 10 min sem responder, mandar um WhatsApp curto pro número que ele cadastrou no portal, com o link do chamado.

**Architecture:** rotina de lote nova (`avisar_cliente_sem_resposta.py`, timer de 2 min) lê `portal_chamados`/`portal_chamados_mensagens` + `preferencias_notificacao` e chama `notificar_whatsapp.avisar_cliente_sem_resposta`, que reaproveita o despacho pelo OpenWA (registro, disjuntor, intervalo) com teto próprio e destino individual. O portal ganha o campo de telefone e a chave no modal Notificações.

**Tech Stack:** Python 3.11, SQLite, Flask (portal), unittest, systemd timer, OpenWA REST (`send-text`, `contacts/check/{numero}`).

**Spec:** `docs/superpowers/specs/2026-09-30-whatsapp-cliente-sem-resposta-design.md`

## Global Constraints

- Idioma: português; comentários e logs sem acento em arquivo que já segue isso (`notificar_whatsapp.py`, `preferencias_notificacao.py`).
- Python local: `py -3.11`. Testes: `py -3.11 -m unittest <modulo>`.
- Mensagem ≤ 200 caracteres (`notificar_whatsapp.MAX_MENSAGEM`), link nunca cortado.
- Nenhuma mudança em `atendimento/`, `integracao_evolution.py`, fluxo de e-mail dos chamados.
- Tudo desligado por padrão: `whatsapp_notificacoes.clientes.ativo` ausente = desligado.
- Uma tentativa por silêncio (assinatura `msg:<id da mensagem da equipe>`); sem reenvio.
- Trabalho em worktree a partir de `origin/master`; só `git add` dos arquivos da tarefa.

## Review Focus

1. Mensagem da equipe por **e-mail** (canal `email`) também conta como resposta da equipe → teste em Task 3.
2. Mensagem de `sistema`/`assistente` depois da equipe **não** é resposta do cliente → teste em Task 3.
3. Telefone com formatação `(11) 99999-0000` e com `+55` na frente → teste em Task 1.
4. Rodada às 13:30 (almoço) não envia nem grava → teste em Task 3.
5. Gateway responde 503 na consulta de número → não grava, tenta na próxima rodada → teste em Task 2.

---

### Task 1: telefone e chave em `preferencias_notificacao.py`

**Files:**
- Modify: `preferencias_notificacao.py`
- Test: `test_preferencias_whatsapp.py` (novo, raiz)

**Produces:**
- `TIPOS["chamado_sem_resposta"]` (grupo `"whatsapp"`, default True).
- coluna `whatsapp TEXT NOT NULL DEFAULT ''` em `preferencias_notificacao`.
- `normalizar_whatsapp(valor) -> str` (12–13 dígitos começando com 55, ou `""`; `ValueError` se inválido).
- `ler(...)["whatsapp"]`; `salvar(conn, cnpj, emails, flags, por, whatsapp=None)` (None = mantém).
- `whatsapp_do_embarcador(conn, cnpj) -> str | None` (só se tipo ligado e número gravado).

- [ ] Teste: `normalizar_whatsapp` aceita `11999990000`, `(11) 99999-0000`, `+55 11 99999-0000`, `5511999990000`, `1133334444` (fixo, 10 dígitos) → `55…`; rejeita `999990000`, `abc`, `55119999900001`; vazio → `""`.
- [ ] Teste: `_garantir_tabela` em banco que já tinha a tabela sem a coluna adiciona `whatsapp`.
- [ ] Teste: `salvar(..., whatsapp="(11) 99999-0000")` grava `5511999990000`; `salvar(..., whatsapp=None)` mantém; `salvar(..., whatsapp="")` apaga; inválido levanta `ValueError` e nada muda.
- [ ] Teste: `whatsapp_do_embarcador` devolve número só com tipo ligado; sem linha → None; chave desligada → None.
- [ ] Rodar, ver falhar, implementar, ver passar. `py -3.11 -m unittest portal_cliente.test_notificacoes_portal` continua verde (ajustar o teste que conta `["acompanhamento"] * 4 + ["acao"] * 3` → `+ ["whatsapp"]`).
- [ ] Commit: `Portal: telefone de WhatsApp e chave de aviso de chamado nas preferencias`

### Task 2: origem `cliente_sem_resposta` em `notificar_whatsapp.py` + consulta de número em `integracao_openwa.py`

**Files:**
- Modify: `integracao_openwa.py`, `notificar_whatsapp.py`
- Test: `test_notificar_whatsapp.py` (classes novas no fim)

**Produces:**
- `integracao_openwa.numero_existe(cfg, numero) -> bool | None` (`GET /sessions/{sessao}/contacts/check/{numero}`; True/False pelo `exists`; None em qualquer falha).
- `notificar_whatsapp.ORIGEM_CLIENTE = "cliente_sem_resposta"`.
- `notificar_whatsapp.clientes_ligado(config) -> bool`.
- `notificar_whatsapp.texto_cliente_sem_resposta(chamado_id, link) -> str`.
- `notificar_whatsapp.saldo_clientes(conn, config, agora) -> int` (teto dos clientes menos enviados hoje dessa origem).
- `notificar_whatsapp.avisar_cliente_sem_resposta(chamado_id, telefone, link, config, **kw) -> str` (situações: `desligado | modo_teste | nao_enviado | enviado | falhou | numero_sem_whatsapp | indeterminado`).

Regras em `_motivo_para_nao_enviar`: janela de repetição não se aplica a `ORIGEM_CLIENTE`; teto geral conta `origem != ORIGEM_CLIENTE`; teto da origem cliente = `clientes.teto_diario` (padrão 30) contado só sobre ela.

- [ ] Teste texto: `texto_cliente_sem_resposta(31, LINK)` == `"Fresh Log: respondemos o seu chamado #31 e aguardamos o seu retorno.\nResponda pelo portal: " + LINK`; ≤ 200.
- [ ] Teste `numero_existe`: 200 `{"exists": true}` → True; `{"exists": false}` → False; exceção/503 → None.
- [ ] Teste `avisar_cliente_sem_resposta`: envia pra `5511999990000@c.us`, registra `(cliente_sem_resposta, chamado, msg:7, enviado)`.
- [ ] Teste: `forcar_destino` redireciona pra `<forcar>@c.us` e texto começa com `[teste → +5511999990000]`.
- [ ] Teste: `numero_existe` False → situação `numero_sem_whatsapp`, linha `nao_enviado`/`numero sem whatsapp`, `enviar_texto` não chamado. None → `indeterminado`, nenhuma linha, nada enviado.
- [ ] Teste: desligado quando `clientes` ausente, `clientes.ativo` False ou `ativo` geral False.
- [ ] Teste teto: 50 linhas internas `enviado` hoje não barram a origem cliente; `clientes.teto_diario=1` + 1 enviado da origem cliente → `nao_enviado`/`teto diario atingido`; e 1 enviado de cliente não conta no teto interno (`teto_diario=1`, interno ainda envia).
- [ ] Teste: mesma assinatura 10 min depois NÃO é barrada pela janela de 120 min (a dedupe fica na rotina).
- [ ] Teste `saldo_clientes`: teto 30 com 2 enviados hoje → 28; ignora `nao_enviado` e outras origens.
- [ ] Rodar, falhar, implementar, passar (`py -3.11 -m unittest test_notificar_whatsapp`: 88+ testes existentes continuam verdes).
- [ ] Commit: `WhatsApp: origem cliente_sem_resposta com teto proprio e consulta de numero`

### Task 3: rotina `avisar_cliente_sem_resposta.py`

**Files:**
- Create: `avisar_cliente_sem_resposta.py` (raiz), `test_avisar_cliente_sem_resposta.py`
- Create: `infra/stokki-avisar-cliente-sem-resposta.service`, `.timer`

**Consumes:** Task 1 `whatsapp_do_embarcador`; Task 2 `avisar_cliente_sem_resposta`, `saldo_clientes`, `clientes_ligado`; `portal_cliente.chamados` (`situacao_horario`, `url_base`, constantes).

**Produces:**
- `selecionar(conn, config, agora) -> list[dict]` com `{chamado_id, cnpj, msg_id, telefone}` na ordem da mensagem mais antiga.
- `executar(config, conn=None, agora=None, modo_teste=False) -> dict` `{avaliados, enviados, sem_whatsapp, pulados, fora_horario}`.
- CLI `--modo-teste`.

Consulta (SQL único):
```sql
SELECT c.id, c.cnpj_embarcador, m.id AS msg_id, m.criado_em
FROM portal_chamados c
JOIN portal_chamados_mensagens m ON m.id = (
    SELECT MAX(id) FROM portal_chamados_mensagens WHERE chamado_id = c.id AND origem = 'equipe')
WHERE c.tipo = 'CLIENTE' AND c.status != 'RESOLVIDO'
  AND m.criado_em <= :limite_min AND m.criado_em >= :limite_max
  AND NOT EXISTS (SELECT 1 FROM portal_chamados_mensagens r
                  WHERE r.chamado_id = c.id AND r.id > m.id AND r.origem = 'cliente')
  AND NOT EXISTS (SELECT 1 FROM notificacoes_whatsapp n
                  WHERE n.origem = 'cliente_sem_resposta' AND n.assinatura = 'msg:' || m.id)
ORDER BY m.criado_em
```
(`notificacoes_whatsapp` pode não existir: criar com `notificar_whatsapp._SCHEMA` antes.)

- [ ] Fixture: banco temporário com `chamados.conectar()` (patch `DB_PATH`), tabela `interno` mínima + `preferencias_notificacao` via `pn.salvar(..., whatsapp=...)`, `agora` = quarta 10:00. Helper `equipe_respondeu(chamado, minutos_atras, canal="portal")` e `cliente_respondeu(chamado, minutos_atras)` inserindo direto em `portal_chamados_mensagens` com `criado_em` calculado e `ultima_origem` coerente.
- [ ] Teste elegível: equipe há 11 min → 1 envio, destino do cliente, assinatura `msg:<id>`.
- [ ] Teste não elegível (um `subTest` cada): 9 min; cliente respondeu depois; resolvido; `tipo=MOTORISTA`; sem telefone; chave desligada; mensagem há 3 dias + 1 min.
- [ ] Teste: mensagem de `sistema` depois da equipe não conta como resposta; equipe por canal `email` conta como resposta da equipe.
- [ ] Teste: fora do horário (sábado; 13:30) → `fora_horario` True, nenhuma linha em `notificacoes_whatsapp`.
- [ ] Teste: segunda rodada não repete; cliente responde e equipe responde de novo → aviso novo.
- [ ] Teste: `clientes.teto_diario=1`, 2 chamados → 1 enviado, 1 pulado, 0 linhas `nao_enviado`.
- [ ] Teste: `--modo-teste` → `modo_teste` no retorno, nada gravado.
- [ ] Teste: `numero_existe` None → pulado sem gravar (reavaliado na próxima).
- [ ] Rodar, falhar, implementar, passar.
- [ ] Units: `.service` `Type=oneshot`, `User=www-data`, `WorkingDirectory=/opt/stokki-eventos`, `ExecStart=/opt/stokki-eventos/venv/bin/python /opt/stokki-eventos/avisar_cliente_sem_resposta.py`, `OnFailure=stokki-alerta-falha@%n.service`; `.timer` `OnCalendar=*-*-* *:00/2:00`, `Persistent=false`, `AccuracySec=10sec`.
- [ ] Commit: `WhatsApp: aviso ao cliente que fica 10 min sem responder o chamado`

### Task 4: portal — API e modal

**Files:**
- Modify: `portal_cliente/app.py` (`_resposta_notificacoes`, `api_notificacoes_salvar`), `portal_cliente/templates/_notificacoes.html`
- Test: `portal_cliente/test_notificacoes_portal.py`

- [ ] Teste: GET devolve `whatsapp` (`""` por padrão) e o tipo `chamado_sem_resposta` no grupo `whatsapp`.
- [ ] Teste: POST com `whatsapp: "(11) 99999-0000"` grava e devolve `5511999990000`; POST sem a chave `whatsapp` mantém; inválido → 400 com a mensagem.
- [ ] Implementar: `_resposta_notificacoes` += `"whatsapp": prefs["whatsapp"]`; POST lê `corpo.get("whatsapp")` (str ou None) e passa pra `salvar`.
- [ ] Modal: título "Notificações"; subtítulo "Escolha quais avisos a sua empresa recebe, em qual e-mail e em qual WhatsApp."; `GRUPOS.whatsapp = 'Avisos por WhatsApp'`; depois do textarea de e-mail, `<label class="ntf-email" for="ntf-whatsapp">WhatsApp (celular com DDD)</label><input id="ntf-whatsapp" type="tel" inputmode="tel" autocomplete="tel" placeholder="(11) 99999-0000">` + ajuda "Usado só para o aviso de chamado aguardando resposta. Em branco, nenhum WhatsApp é enviado."; `desenhar` preenche/desabilita; submit manda `whatsapp: $('ntf-whatsapp').value`.
- [ ] Rodar `py -3.11 -m unittest portal_cliente.test_notificacoes_portal`; subir o portal numa porta livre e abrir o modal com Playwright pra ver o campo e salvar um número (cliente teste).
- [ ] Commit: `Portal: campo de WhatsApp no botao Notificacoes`

### Task 5: documentação

**Files:**
- Modify: `MAPA_DO_SISTEMA.txt` (rotina + timer na tabela de timers, arquivo na seção de notificações, coluna `whatsapp`, config `clientes`), `infra/openwa/LEIAME.md` (liberar chat individual na chave operator; rota `contacts/check`).

- [ ] Editar, `git diff` conferindo que só as linhas minhas entraram.
- [ ] Commit: `Mapa e LEIAME do OpenWA: aviso ao cliente sem resposta`

### Task 6: push, deploy e prova

- [ ] `git push origin HEAD:master` (fast-forward a partir de origin/master).
- [ ] VPS: `git status --short && git log --oneline -3` → `git pull --no-rebase` → `chown -R www-data:www-data`.
- [ ] Copiar units pra `/etc/systemd/system/`, `daemon-reload`, `enable --now stokki-avisar-cliente-sem-resposta.timer`.
- [ ] `systemctl restart portal-cliente`; `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8074/cliente/login` → 200.
- [ ] Config da VPS: backup `config.yaml.bak-20261001-antes-clientes-whatsapp`; acrescentar `clientes: {ativo: false, minutos: 10, dias_max: 3, teto_diario: 30, forcar_destino: ""}`.
- [ ] Gateway: com a chave admin, `PUT /api/auth/api-keys/<id da operator>` tirando a restrição de chats (ou anotar que ficou pro Hugo). Testar `GET .../contacts/check/5511...` com a chave operator.
- [ ] `sudo -u www-data venv/bin/python avisar_cliente_sem_resposta.py --modo-teste` → sai sem erro, loga `fora_horario` ou lista vazia.
- [ ] `systemctl show -p ActiveState --value stokki-avisar-cliente-sem-resposta.service` depois do 1º disparo; `journalctl -u ... -n 20`.
- [ ] Atualizar memória: estado deployado, desligado, o que falta (ligar + forcar_destino + prova com cliente teste).
