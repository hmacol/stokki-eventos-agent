# Ciência obrigatória do dedicado fora do dia fixo: plano de execução

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Pedido que vira dedicado por data fora do dia de visita gera aviso por chat, e-mail e WhatsApp com link de resposta obrigatória (ciente ou reagendar), lembrete a cada 4 h e histórico completo para a equipe.

**Architecture:** Um módulo de registro (`ciencia_dedicado.py`) guarda pedidos e envios; uma rotina (`notificar_ciencia_dedicado.py`) envia o primeiro aviso e os lembretes por timer; o portal ganha a página pública `/ciencia/<token>`; o painel ganha a tela `/ciencias-dedicado`. A detecção de `roteirizacao/fora_dia_fixo.py` só passa a criar a linha de ciência quando a chave está ligada.

**Tech Stack:** Python 3.11, Flask, SQLite (`dados/dados.db`), itsdangerous, unittest, systemd timer.

**Spec:** `docs/superpowers/specs/2026-11-17-ciencia-dedicado-dia-fixo-design.md` (ler antes de cada tarefa).

## Global Constraints

- Trabalhar no worktree `.claude/worktrees/ciencia-dedicado` (ramo `ciencia-dedicado`). Python sempre `py -3.11`. Testes com `py -3.11 -m unittest <modulo>`; nunca misturar `roteirizacao`/`nucleo` com `painel_agentes` no mesmo comando; testes do painel rodam de dentro de `painel_agentes/`.
- Idioma português; comentários e logs sem acento quando o arquivo já segue esse padrão.
- Nunca ler nem alterar `config.yaml`. Nada de envio real em teste: canais sempre falsos (funções injetadas).
- `ciencia_dedicado.ativo` ausente ou falso = nada deste plano roda e o aviso único antigo continua igual. Todo teste existente de `fora_dia_fixo` e `notificar_fora_dia_fixo` continua passando sem alteração.
- Sem linha na tela Notificações = PILOTO. Piloto: e-mail e WhatsApp para o destino de piloto, chat não abre conversa, botões da página simulados (exceto cliente teste, CNPJ `00000000000191`).
- Valores fixos da spec: intervalo 4 h; janela 08:00–20:00 America/Sao_Paulo, todos os dias; token válido por 30 dias; 4 datas de visita oferecidas; alerta da equipe às 16h da véspera; `removido_por` = `"ciência: reagendado pelo embarcador"`.
- Situações: `PENDENTE`, `CIENTE`, `REAGENDADO`, `ENCERRADO`. Canais: `chat`, `email`, `whatsapp`. Resultados: `enviado`, `piloto`, `nao_enviado`.
- Tela nova entra em `painel_agentes/acesso_telas.TELAS`; URL sempre por `url_for()`.
- Commit local por tarefa no ramo, só dos arquivos da tarefa (`git add` nominal), mensagem `Área: o que mudou e por quê`. Sem push e sem deploy: isso é pedido do Hugo.
- `MAPA_DO_SISTEMA.txt` atualizado na tarefa 8.

## Review Focus

1. Embarcador responde e, no mesmo minuto, o timer está montando o lembrete: nenhum lembrete sai depois da resposta (a rotina relê a situação logo antes de enviar cada embarcador). Teste na tarefa 4.
2. Dois cliques no mesmo botão, ou "ciente" num pedido já reagendado: a segunda resposta não sobrescreve a primeira (nome, hora e IP originais ficam). Teste na tarefa 1.
3. Pedido com código combinado (`#PS-1, PS-2`) ou reentrega (`PS-1-R2`): a linha de ciência usa o primeiro código normalizado por `pedidos_dedicados.codigos_do_servico`, e `-R2` não se confunde com o original. Teste na tarefa 1.
4. Embarcador sem e-mail, sem WhatsApp e sem conta no portal: três envios `nao_enviado` com motivo, o pedido segue `PENDENTE`, o próximo lembrete é agendado e a equipe é alertada uma vez. Teste na tarefa 3.
5. Link de um embarcador com `sender_id` trocado à mão, token expirado ou adulterado: página de erro genérica, sem listar pedido nenhum. Teste na tarefa 6.

---

### Task 1: Registro de ciências e envios

**Files:**
- Create: `ciencia_dedicado.py`
- Test: `test_ciencia_dedicado.py`

**Interfaces:**
- Produces:
  - `DB_PATH`; `conectar(db_path=DB_PATH) -> sqlite3.Connection` (row_factory `sqlite3.Row`, cria `ciencias_dedicado` e `ciencias_dedicado_envios`, campos da seção 1 da spec; `ciencias_dedicado` tem `id` autoincrement e índice em `(codigo, situacao)`: um código pode ter mais de uma linha ao longo do tempo, só uma `PENDENTE`).
  - `PENDENTE, CIENTE, REAGENDADO, ENCERRADO`; `POR_REAGENDADO = "ciência: reagendado pelo embarcador"`.
  - `cfg(config: dict) -> dict` com `ativo: bool`, `intervalo_horas: int` (4), `janela: tuple[time, time]` (08:00, 20:00), `forcar_destino: str`.
  - `abrir(conn, item: dict, cnpj: str | None, agora: datetime) -> int | None`: `item` no formato de `fora_dia_fixo.marcar_fora_dia_fixo` (`servico, regra, data, valor, valor_pendente`). Cria linha `PENDENTE` com `proximo_lembrete_em = agora`; devolve `None` se já há `PENDENTE` do mesmo código.
  - `pendentes_vencidos(conn, agora) -> list[dict]`; `pendentes_do_sender(conn, sender_id) -> list[dict]`; `do_sender(conn, sender_id, dias: int = 30) -> list[dict]`; `buscar(conn, ciencia_id) -> dict | None`; `listar(conn, situacao=None, sender_id=None, limite=300) -> list[dict]`.
  - `registrar_envio(conn, ciencia_id, canal, resultado, destino="", motivo="", rodada=0, agora=None)`; `envios(conn, ciencia_id) -> list[dict]`; `rodadas_enviadas(conn, ciencia_id) -> int`.
  - `agendar_proximo(conn, ciencia_id, agora, intervalo_horas)`.
  - `responder(conn, ciencia_id, situacao, nome, ip, agora, data_nova: date | None = None) -> bool`: só muda linha `PENDENTE`; devolve `False` sem alterar nada nas demais.
  - `encerrar(conn, ciencia_id, motivo, agora) -> bool`; `atualizar_data(conn, ciencia_id, data: date)`; `marcar_alerta_equipe(conn, ciencia_id, agora)`.
  - `proximo_envio_permitido(agora: datetime, janela) -> datetime`: dentro da janela devolve `agora`; antes das 08h devolve 08:00 do dia; depois das 20h devolve 08:00 do dia seguinte.

- [ ] **Step 1: Escrever os testes que falham** em `test_ciencia_dedicado.py` (banco em `tempfile`), um por comportamento:
  - `test_abrir_cria_pendente_com_lembrete_imediato`
  - `test_abrir_nao_duplica_pendente_do_mesmo_codigo`
  - `test_abrir_apos_reagendado_cria_linha_nova_e_mantem_a_antiga`
  - `test_codigo_combinado_usa_o_primeiro_e_reentrega_nao_confunde` (`"#PS-1, PS-2"` grava `PS-1`; `PS-1-R2` é outro pedido)
  - `test_responder_so_muda_pendente` (segunda resposta devolve `False`; nome, hora e IP da primeira ficam)
  - `test_pendentes_vencidos_respeita_proximo_lembrete`
  - `test_agendar_proximo_soma_4_horas`
  - `test_proximo_envio_permitido` (07:59 → 08:00; 12:00 → 12:00; 20:01 → 08:00 do dia seguinte)
  - `test_envios_guardam_canal_resultado_motivo_e_rodada`
  - `test_cfg_padrao_desligado_4h_janela_08_20`
- [ ] **Step 2:** `py -3.11 -m unittest test_ciencia_dedicado` → FAIL (módulo não existe).
- [ ] **Step 3: Implementar `ciencia_dedicado.py`** com as assinaturas acima. Datas em texto `YYYY-MM-DD HH:MM:SS`, como `registro_dia_fixo._quando`.
- [ ] **Step 4:** `py -3.11 -m unittest test_ciencia_dedicado` → OK.
- [ ] **Step 5: Commit** `Ciencia do dedicado: registro de pedidos, respostas e envios por canal`.

### Task 2: Marcar de novo depois de reagendar pela página

**Files:**
- Modify: `pedidos_dedicados.py:163-170` (`codigos_e_servicos_ja_marcados`)
- Test: `test_pedidos_dedicados_remarcar.py` (novo, raiz)

**Interfaces:**
- Consumes: `ciencia_dedicado.POR_REAGENDADO` (copiar o texto como constante `POR_CIENCIA_REAGENDADO` em `pedidos_dedicados.py` para não criar import circular; `ciencia_dedicado.POR_REAGENDADO` passa a apontar para ela).
- Produces: `codigos_e_servicos_ja_marcados(conn)` ignora linhas com `removido_em` preenchido **e** `removido_por == POR_CIENCIA_REAGENDADO`.

- [ ] **Step 1: Testes que falham:**
  - `test_removido_pela_equipe_continua_bloqueando`
  - `test_removido_pela_pagina_de_ciencia_libera_nova_marcacao`
  - `test_ativo_continua_bloqueando`
- [ ] **Step 2:** rodar → FAIL no segundo.
- [ ] **Step 3: Ajustar a consulta** (filtro no SQL).
- [ ] **Step 4:** `py -3.11 -m unittest test_pedidos_dedicados_remarcar` → OK; `py -3.11 -m unittest roteirizacao.test_fora_dia_fixo roteirizacao.test_fora_dia_fixo_integracao` → OK sem alteração.
- [ ] **Step 5: Commit** `Dedicados: pedido reagendado pela pagina de ciencia pode ser marcado de novo se a data voltar pra fora do dia de visita`.

### Task 3: Uma rodada de envio pelos três canais

**Files:**
- Create: `notificar_ciencia_dedicado.py`
- Create: `portal_cliente/link_ciencia.py` (só a parte de token nesta tarefa)
- Modify: `controle_notificacoes.py:53` (`TIPOS`: `"ciencia_dedicado": {"rotulo": "Ciência do dedicado", "destino": "embarcador", ...}` no mesmo formato de `fora_dia_fixo`)
- Modify: `preferencias_notificacao.py` (nova `numero_whatsapp(conn, cnpj) -> str | None`: mesmo que `whatsapp_do_embarcador`, sem exigir `chamado_sem_resposta`; a função antiga passa a chamá-la)
- Modify: `notificar_whatsapp.py` (nova `avisar_cliente_ciencia_dedicado(assinatura, telefone, texto, config, modo_teste=False, destino_forcado=None, **kw) -> str`, cópia do comportamento de `avisar_cliente_fora_dia_fixo`: mesmo teto de cliente, mesmos retornos `"enviado" | "desligado" | "indeterminado" | "teto" ...`; e `texto_ciencia_dedicado(nome, pedidos: list[dict], link: str, lembrete: bool) -> str`)
- Test: `test_notificar_ciencia_dedicado.py`, `portal_cliente/test_link_ciencia.py`

**Interfaces:**
- Consumes: tudo da tarefa 1; `controle_notificacoes.decidir`; `preferencias_notificacao.carregar_embarcadores("agendamento")`; `portal_cliente.chamados` (`criar_chamado`, `mensagem_sistema`, `chamado_ativo_cliente`, `STATUS_RESPONDIDO`, `STATUS_RESOLVIDO`, `ORIGEM_SISTEMA`); `portal_cliente.auth_cliente.buscar_conta`; `email_utils.enviar_email`/`envelope_html`.
- Produces:
  - `link_ciencia.gerar(sender_id: int, segredo: str) -> str`; `link_ciencia.ler(token, segredo, validade_dias=30) -> int | None`; `link_ciencia.url(sender_id, segredo, url_base) -> str` (padrão de `link_canhoto.py`, salt `"ciencia-dedicado"`).
  - `@dataclass Canais`: `email(destinos: list[str], assunto: str, html: str) -> bool`; `whatsapp(assinatura, telefone, texto, destino_forcado) -> str`; `chat_abrir(cnpj, assunto, texto, pedido_ref) -> int | None`; `chat_mensagem(chamado_id, texto) -> bool`; `tem_conta_portal(cnpj) -> bool`; `alerta_equipe(texto) -> None`.
  - `canais_reais(config, db_path=None) -> Canais`.
  - `enviar_embarcador(conn, sender_id, pendentes: list[dict], emb: dict, config, canais: Canais, agora, db_path=None) -> dict`: decide por canal (`decidir("ciencia_dedicado", cnpj, canal="email"|"whatsapp")`; chat segue a decisão do e-mail), monta **uma** mensagem por canal com todos os `pendentes`, grava um envio por pedido e por canal, e chama `agendar_proximo` para cada pedido. Devolve contadores `{"email", "whatsapp", "chat", "nao_enviado"}`.
  - Motivos de `nao_enviado`: `sem_email`, `sem_whatsapp`, `sem_conta_portal`, `teto`, `falha`, `desligado`, `sem_canal` (quando os três falham por falta de cadastro).

Regras desta tarefa (da spec, seções 3 e 7): e-mail ignora `emb["desligado"]`; fallback sem linha na tela = `PILOTO` com destino `cfg(config)["forcar_destino"]` ou `hugo@freshlogbr.com`; em PILOTO o chat registra `piloto` e não chama `chat_abrir`; chat de lembrete usa a conversa aberta no primeiro aviso (id guardado no `destino` do envio de chat da rodada 0) e abre nova se ela estiver resolvida; a conversa nasce com `STATUS_RESPONDIDO` (aguardando o cliente, fora da fila da equipe); a rodada é contada só pelos envios que não são de piloto, então o primeiro envio real depois de rodadas de piloto é a rodada 0 (abre a conversa e sai sem o prefixo de lembrete); falha de um canal não impede os outros; os três `nao_enviado` por falta de cadastro na rodada 0 chamam `alerta_equipe` uma vez.

- [ ] **Step 1: Testes que falham** (canais falsos que gravam as chamadas):
  - `test_token_ida_e_volta`, `test_token_adulterado_ou_expirado_devolve_none` (em `portal_cliente/test_link_ciencia.py`)
  - `test_um_email_por_embarcador_com_todos_os_pedidos_e_link`
  - `test_email_sai_mesmo_com_preferencia_desligada`
  - `test_whatsapp_usa_numero_mesmo_com_aviso_desligado`
  - `test_teto_do_whatsapp_registra_nao_enviado_e_outros_canais_saem`
  - `test_falha_no_email_nao_impede_whatsapp_nem_chat`
  - `test_chat_sem_conta_no_portal_registra_sem_conta_portal`
  - `test_lembrete_usa_a_mesma_conversa_e_abre_nova_se_resolvida`
  - `test_piloto_manda_email_e_whatsapp_ao_piloto_e_nao_abre_conversa`
  - `test_tela_nao_registra_desligado_e_agenda_proximo`
  - `test_virada_de_piloto_para_real_recomeca_na_rodada_0_e_mantem_envios_de_piloto`
  - `test_conversa_nasce_aguardando_o_cliente`
  - `test_sem_canal_nenhum_tres_nao_enviado_alerta_equipe_uma_vez_e_segue_pendente`
  - `test_cada_pedido_ganha_envio_por_canal_com_a_rodada_certa`
  - `test_proximo_lembrete_fica_4h_depois`
- [ ] **Step 2:** rodar os dois módulos → FAIL.
- [ ] **Step 3: Implementar.** E-mail: tabela pedido / destinatário / cidade / data pedida / dias de visita (`regioes_dia_fixo.descricao_dias`) / valor (ou "a confirmar"), um botão "Responder" por e-mail apontando para `link_ciencia.url`; assunto `[Freshlog] Confirme a ciência: N pedido(s) com entrega dedicada` (lembrete: prefixo `Lembrete: `). Textos em português simples, link em destaque.
- [ ] **Step 4:** `py -3.11 -m unittest test_notificar_ciencia_dedicado` e `py -3.11 -m unittest portal_cliente.test_link_ciencia` → OK; `py -3.11 -m unittest portal_cliente.test_notificacoes_portal roteirizacao.test_notificar_fora_dia_fixo` → OK sem alteração.
- [ ] **Step 5: Commit** `Ciencia do dedicado: rodada de aviso por e-mail, WhatsApp e chat com registro por canal`.

### Task 4: Rotina do timer, encerramentos e alerta da equipe

**Files:**
- Modify: `notificar_ciencia_dedicado.py`
- Test: `test_notificar_ciencia_dedicado_rotina.py`

**Interfaces:**
- Consumes: tarefas 1 e 3; `nucleo/banco.py` (tabelas `nucleo_pedidos`, `nucleo_paradas`, `nucleo_rotas` e as constantes de status; ler o arquivo para os nomes exatos de "cancelado", "entregue", "insucesso" e rota iniciada); `pedidos_dedicados.ativos_por_codigo`; `regioes_dia_fixo.data_valida_na_regiao`.
- Produces:
  - `estado_do_pedido(conn_nucleo, codigo) -> dict | None` com `cancelado, finalizado, rota_iniciada, data: date | None` (`None` = núcleo não conhece).
  - `motivo_de_encerramento(ciencia: dict, estado: dict | None, dedicado_ativo: bool, regra: dict | None, hoje: date) -> str | None`: função pura; devolve `"cancelado"`, `"entregue ou insucesso"`, `"rota iniciada"`, `"dedicado removido"`, `"data passou"`, `"data em dia de visita"` ou `None`.
  - `executar(config, agora=None, modo_teste=False, canais: Canais | None = None, db_path=None, embarcadores: dict | None = None) -> dict` (resumo `{"encerrados", "embarcadores", "enviados", "adiados", "alertas"}`).
  - `main(argv=None) -> int` com `--modo-teste`.

Ordem dentro de `executar`: sai sem fazer nada se `cfg(config)["ativo"]` for falso; para cada `PENDENTE` (todos, não só os vencidos) aplica `motivo_de_encerramento` e, se a data mudou e segue fora do dia de visita, `atualizar_data`; dispara o alerta da equipe (16h da véspera, ou já, se passou disso; uma vez por pedido, via `marcar_alerta_equipe`); fora da janela não envia e conta `adiados`; dentro da janela agrupa os vencidos por `sender_id` e, **relendo a situação de cada pedido logo antes**, chama `enviar_embarcador`. `--modo-teste` só loga: não grava envio, não encerra, não mexe em `proximo_lembrete_em`. Nunca levanta por causa de um embarcador: erro em um não impede os outros.

- [ ] **Step 1: Testes que falham:**
  - `test_motivo_de_encerramento` (uma asserção por motivo, mais: núcleo desconhecido e dedicado ativo com data futura → `None`; sumir do pool não é motivo)
  - `test_data_mudou_e_segue_fora_do_dia_atualiza_e_continua_pendente`
  - `test_fora_da_janela_nao_envia_e_conta_adiado` (agora 21:00 e 07:30)
  - `test_lembrete_sai_4h_depois_e_nao_antes`
  - `test_resposta_entre_a_selecao_e_o_envio_nao_recebe_lembrete` (canal falso que chama `responder` no primeiro envio; o segundo pedido respondido não entra na mensagem seguinte)
  - `test_alerta_equipe_as_16h_da_vespera_uma_vez_so`
  - `test_alerta_equipe_imediato_quando_marcado_depois_das_16h_da_vespera`
  - `test_modo_teste_nao_grava_nada`
  - `test_desligado_no_config_nao_faz_nada`
  - `test_erro_num_embarcador_nao_impede_os_outros`
- [ ] **Step 2:** rodar → FAIL.
- [ ] **Step 3: Implementar.** No topo do script, reconfigurar `sys.stdout` para UTF-8 (padrão dos scripts agendados).
- [ ] **Step 4:** `py -3.11 -m unittest test_notificar_ciencia_dedicado_rotina test_notificar_ciencia_dedicado` → OK.
- [ ] **Step 5: Commit** `Ciencia do dedicado: lembrete a cada 4h na janela 08h-20h, encerramento pelo nucleo e alerta da equipe na vespera`.

### Task 5: Ligar na detecção do fora do dia fixo

**Files:**
- Modify: `roteirizacao/fora_dia_fixo.py:209-231` (`tratar_fora_dia_fixo`)
- Test: `roteirizacao/test_fora_dia_fixo_ciencia.py`

**Interfaces:**
- Consumes: `ciencia_dedicado.cfg`, `ciencia_dedicado.abrir`, `notificar_ciencia_dedicado.executar`.
- Produces: com `ciencia_dedicado.ativo` verdadeiro, `tratar_fora_dia_fixo` chama `abrir` para cada item de `marcar_fora_dia_fixo` (CNPJ por `preferencias_notificacao.carregar_embarcadores`), **não** chama `notificar_fora_dia_fixo.avisar`, e chama `notificar_ciencia_dedicado.executar(config)` em seguida (o primeiro aviso sai na mesma rodada, respeitando a janela). Resumo ganha `"ciencias": n`. Com a chave desligada, o corpo atual fica idêntico.

- [ ] **Step 1: Testes que falham:**
  - `test_desligado_segue_o_aviso_antigo_e_nao_abre_ciencia`
  - `test_ligado_abre_ciencia_e_nao_chama_o_aviso_antigo`
  - `test_ligado_falha_na_ciencia_nao_derruba_a_rodada` (resumo com `erro`, marcados preservados)
  - `test_modo_teste_nao_abre_ciencia`
  - `test_remarcado_apos_reagendar_abre_ciencia_nova` (usa a exceção da tarefa 2)
- [ ] **Step 2:** `py -3.11 -m unittest roteirizacao.test_fora_dia_fixo_ciencia` → FAIL.
- [ ] **Step 3: Implementar** o desvio dentro do `try` existente.
- [ ] **Step 4:** `py -3.11 -m unittest roteirizacao.test_fora_dia_fixo_ciencia roteirizacao.test_fora_dia_fixo roteirizacao.test_fora_dia_fixo_integracao roteirizacao.test_notificar_fora_dia_fixo` → OK.
- [ ] **Step 5: Commit** `Fora do dia fixo: com ciencia_dedicado.ativo o pedido marcado abre a ciencia e o aviso unico deixa de sair`.

### Task 6: Página de resposta no portal

**Files:**
- Modify: `portal_cliente/link_ciencia.py` (acrescenta `registrar(app, *, config, secret)` com as rotas)
- Modify: `portal_cliente/app.py` (uma linha de `link_ciencia.registrar(...)`, junto do `link_canhoto` em ~1753)
- Create: `portal_cliente/templates/ciencia.html`, `portal_cliente/templates/ciencia_erro.html`
- Test: `portal_cliente/test_link_ciencia_pagina.py`

**Interfaces:**
- Consumes: tarefas 1 e 3; `alterar_entrega.avaliar/validar/planejar/aplicar/dependencias_reais` e o jeito de montar `servico`, `avaliacao` e `contexto` em `portal_cliente/app.py:805-886`; `pedidos_dedicados.remover(conn, codigo_pedido=..., por=POR_CIENCIA_REAGENDADO)`; `regioes_dia_fixo.proxima_data_valida`; `controle_notificacoes.decidir`.
- Produces:
  - `GET /ciencia/<token>`: lista os pedidos `PENDENTE` do `sender_id` do token e, abaixo, os respondidos nos últimos 30 dias (situação, quem, quando).
  - `POST /ciencia/<token>/ciente` (form `ciencia_id`, `nome`) e `POST /ciencia/<token>/reagendar` (form `ciencia_id`, `nome`, `data`).
  - `datas_de_visita(regra: dict, hoje: date, n: int = 4) -> list[date]` (a partir de amanhã).
  - `registrar(...)` aceita `deps_fabrica=None` e `buscar_servico=None` para os testes injetarem falsos.

Regras: `nome` obrigatório (2 a 80 caracteres, sem só espaços); `ciencia_id` precisa pertencer ao `sender_id` do token e estar `PENDENTE`, senão mensagem "Este pedido já foi respondido" sem alterar nada; `data` precisa estar na lista de `datas_de_visita`; IP por `request.remote_addr`. Reagendar: aplica só a data pelo `alterar_entrega`; se ele recusar (`ErroAlteracao`, avaliação bloqueada ou `aplicar` sem `ok`), mostra a mensagem dele, orienta o chat e o pedido segue `PENDENTE`; se aplicar, `pedidos_dedicados.remover` e `responder(..., REAGENDADO, data_nova=...)`. Toda resposta real posta mensagem do sistema na conversa do chat, quando ela existir. A página não depende de `portal_cliente.alteracao_entrega.ativo`. Piloto (decisão de e-mail = `PILOTO` para o CNPJ) e CNPJ diferente de `00000000000191`: faixa "Modo piloto: nada é gravado", valida tudo, mostra a tela de sucesso e **não** chama `responder`, `aplicar` nem `remover`. Token inválido ou expirado: `ciencia_erro.html` com status 404, sem dado nenhum. Templates seguem o visual das páginas públicas do portal; URLs por `url_for`.

- [ ] **Step 1: Testes que falham** (Flask test client, `registrar` com falsos):
  - `test_token_invalido_ou_expirado_da_404_sem_listar_pedido`
  - `test_lista_so_os_pedidos_do_sender_do_token`
  - `test_ciente_grava_nome_hora_ip_e_para_o_lembrete`
  - `test_ciente_sem_nome_pede_o_nome_e_nao_grava`
  - `test_ciencia_de_outro_sender_e_recusada`
  - `test_segunda_resposta_nao_sobrescreve_a_primeira`
  - `test_datas_de_visita_devolve_4_a_partir_de_amanha_pulando_feriado`
  - `test_reagendar_aplica_data_remove_dedicado_e_grava_reagendado`
  - `test_reagendar_com_data_fora_da_lista_e_recusado`
  - `test_reagendar_recusado_pelo_alterar_entrega_mantem_pendente_e_mostra_a_mensagem`
  - `test_piloto_simula_e_nao_grava_nem_chama_vuupt`
  - `test_piloto_com_cliente_teste_age_de_verdade`
  - `test_pagina_funciona_com_alteracao_entrega_desligada`
- [ ] **Step 2:** `py -3.11 -m unittest portal_cliente.test_link_ciencia_pagina` → FAIL.
- [ ] **Step 3: Implementar.**
- [ ] **Step 4:** `py -3.11 -m unittest portal_cliente.test_link_ciencia_pagina portal_cliente.test_link_ciencia portal_cliente.test_alterar_entrega portal_cliente.test_alterar_entrega_api portal_cliente.test_link_canhoto` → OK.
- [ ] **Step 5:** subir o portal local numa porta livre (não a 8074), abrir `/ciencia/<token>` gerado para o cliente teste com um pedido de teste no banco local e tirar captura das telas (lista, ciente, reagendar, erro) em 390 px e 1280 px de largura; conferir que nada estoura a tela.
- [ ] **Step 6: Commit** `Portal: pagina publica de ciencia do dedicado (ciente ou reagendar pro dia de visita), simulada no piloto`.

### Task 7: Tela da equipe no painel

**Files:**
- Create: `painel_agentes/ciencias_dedicado.py` (dados da tela), `painel_agentes/templates/ciencias_dedicado.html`
- Modify: `painel_agentes/painel_agentes.py` (rotas, seguindo `/canhotos-pendentes` em ~1907), `painel_agentes/acesso_telas.py:53` (linha nova em `TELAS`: `("ciencias_dedicado", "Ciências de dedicado", "Entregas", [r"^/ciencias-dedicado$", r"^/api/ciencias-dedicado/.*$"])`), template do menu lateral (mesma área de "Canhotos pendentes")
- Test: `painel_agentes/test_ciencias_dedicado_tela.py`

**Interfaces:**
- Consumes: tarefa 1 (`listar`, `envios`, `buscar`); `link_ciencia.url`; `portal_cliente.chamados.url_base(config)`.
- Produces:
  - `GET /ciencias-dedicado?situacao=&embarcador=`: tabela com pedido, embarcador, data da entrega, valor, situação, avisos enviados (contagem por canal), última resposta. Níveis iguais aos de `/canhotos-pendentes` (`total`, `operador`, `logistica`).
  - `GET /api/ciencias-dedicado/<int:ciencia_id>`: JSON com a linha e a lista de envios (canal, quando, destino, resultado, motivo, rodada), para o painel lateral de histórico.
  - Botão "Copiar link" por embarcador (link da página dele).

- [ ] **Step 1: Testes que falham** (de dentro de `painel_agentes/`, padrão de `test_canhotos_pendentes_tela.py`):
  - `test_tela_lista_pendentes_e_respondidos_com_filtro_de_situacao`
  - `test_api_devolve_historico_de_envios_em_ordem`
  - `test_tela_exige_login_e_nivel`
  - `test_tela_esta_em_acesso_telas`
  - `test_link_copiado_e_o_do_embarcador_da_linha`
- [ ] **Step 2:** `cd painel_agentes && py -3.11 -m unittest test_ciencias_dedicado_tela` → FAIL.
- [ ] **Step 3: Implementar.**
- [ ] **Step 4:** `cd painel_agentes && py -3.11 -m unittest test_ciencias_dedicado_tela test_acesso_telas test_canhotos_pendentes_tela` → OK.
- [ ] **Step 5:** subir o painel local na porta 8099 e conferir a tela com captura (lista com dados de teste e histórico aberto).
- [ ] **Step 6: Commit** `Painel: tela Ciencias de dedicado com historico de avisos por canal e resposta do embarcador`.

### Task 8: Timer, mapa e verificação final

**Files:**
- Create: `infra/stokki-ciencia-dedicado.service`, `infra/stokki-ciencia-dedicado.timer` (copiar o par `stokki-avisar-cliente-sem-resposta`; `ExecStart=/opt/stokki-eventos/venv/bin/python /opt/stokki-eventos/notificar_ciencia_dedicado.py`; `OnCalendar=*:0/15`)
- Modify: `MAPA_DO_SISTEMA.txt` (módulos novos, tela, rota pública, tabelas, timer, chaves `ciencia_dedicado.*`, tipo na tela Notificações)
- Modify: `painel_agentes/test_notificacoes*.py` só se algum teste listar os tipos e quebrar com o tipo novo

- [ ] **Step 1:** criar as duas units e atualizar o mapa.
- [ ] **Step 2:** `py -3.11 -m py_compile` em todos os arquivos `.py` criados ou alterados → sem saída.
- [ ] **Step 3:** rodar, em comandos separados: os testes novos; `py -3.11 -m unittest discover -s roteirizacao -p "test_*dia_fixo*.py"`; `py -3.11 -m unittest discover -s portal_cliente -p "test_*.py"`; de dentro de `painel_agentes/`, `py -3.11 -m unittest discover -p "test_*.py"`. Comparar as falhas com as da `origin/master` (rodar lá o mesmo comando): nenhuma falha nova.
- [ ] **Step 4:** `py -3.11 notificar_ciencia_dedicado.py --modo-teste` no banco local → termina com código 0 e loga "desligado" (sem a chave no config local) sem gravar nada.
- [ ] **Step 5: Commit** `Infra + mapa: timer stokki-ciencia-dedicado a cada 15 min e registro do fluxo de ciencia do dedicado`.

Fora do plano (pedido do Hugo): push, PR, homologação, deploy, copiar as units, gravar `ciencia_dedicado.ativo` no config da VPS e ligar o tipo na tela Notificações.
