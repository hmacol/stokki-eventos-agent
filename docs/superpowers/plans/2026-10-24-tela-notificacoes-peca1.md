# Tela Notificações — Peça 1 (tela e travas) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ligar, pôr em piloto ou desligar cada notificação externa (por tipo, por embarcador, por transportadora, e um interruptor geral) numa tela do painel, sem editar o `config.yaml`, com disparo manual e e-mails do embarcador editáveis.

**Architecture:** Um módulo central na raiz, `controle_notificacoes.py`, guarda o estado no `dados.db` e expõe `decidir(...)`. Cada rotina que manda para fora continua calculando o que faria hoje pelo config (o *fallback*) e pergunta ao módulo; sem linha na tela vale o fallback, então nada muda até o Hugo mexer. A tela `/notificacoes` (só nível `total`, fora do catálogo de acessos, como `/acessos`) lê e grava pelo módulo; o disparo manual roda o script da rotina como subprocesso.

**Tech Stack:** Python 3.11, Flask (painel), SQLite (`dados/dados.db`), `unittest` (sem pytest).

**Spec:** `docs/superpowers/specs/2026-10-24-tela-notificacoes-design.md` (ler inteiro antes de começar; seções "Regra de decisão", "O que não saiu, some", "Cópia de Piloto", "Escopo: só o que vai para fora").

## Global Constraints

- Python: `py -3.11` local; na VPS `/opt/stokki-eventos/venv/bin/python`. Testes: `py -3.11 -m unittest <modulo>` da raiz; testes de `painel_agentes` rodam de dentro de `painel_agentes/` e nunca no mesmo comando que `roteirizacao`/`nucleo`.
- O painel **nunca** lê para exibir nem escreve o `config.yaml` por causa desta tela; o controle novo mora só no `dados.db`.
- Estados de tipo: exatamente `DESLIGADO`, `PILOTO`, `LIGADO`. Geral: `DESLIGADO` ou `LIGADO`. Exceção: `LIBERADO` ou `BLOQUEADO`.
- Ações de `decidir`: exatamente `ENVIAR`, `PILOTO`, `NAO`.
- Sem linha do tipo na tela → vale o config de hoje. Erro ao ler o controle → vale o config e `logger.warning`. Nunca cair em "ligado" por erro.
- Só destinatários **externos** (embarcador, transportadora, futuro cliente). E-mail para equipe, motorista e financeiro nunca passa pela trava.
- Evento descartado pela trava (NAO) é **consumido**: não sai depois quando ligar. Cópia de piloto fica registrada à parte e não conta como "recebido pelo cliente".
- Transação SQLite curta (abrir, ler/gravar, fechar); nada de conexão aberta durante envio de e-mail.
- Comentários e logs no padrão do arquivo que está sendo editado (vários sem acento). Commits `Área: o que mudou`, em português, `git add` só dos arquivos da tarefa; push e deploy só quando o Hugo pedir.
- Rota nova do painel: `@requer_auth` sem `niveis` (= só total), `@exige_mesma_origem` em todo POST, `url_for()` no template; endpoints entram em `FORA_DO_CATALOGO` de `painel_agentes/test_acesso_telas.py`, não em `acesso_telas.TELAS`.
- Não editar `insucesso_entrega/expedir_pedidos.py` (cópia antiga); produção usa `expedir_pedidos.py` da raiz.

## Escopo dos tipos (decisões do plano)

Tipos controlados pela tela (chave → rotina):

| chave | rotina(s) | destino da exceção |
|---|---|---|
| `nfs_em_rota` | `notificar_nfs_em_rota.py` | cnpj do embarcador |
| `entrega_concluida` | `notificacao_entregas/notificar_entrega_concluida.py` | cnpj |
| `resumo_diario` | `resumo_diario_embarcador.py` | cnpj |
| `insucesso` | `insucesso_entrega/notificar_insucesso_aguardando_resposta.py::notificar_remetentes` | cnpj |
| `pedidos_em_espera` | `notificar_pedidos_em_espera.py` | cnpj |
| `agendamento` | `roteirizacao/notificar_agendamento_pendente.py`, `roteirizacao/notificar_agendamento_dia_fixo.py`, `agendamento_confirmacao.py::enviar_solicitacao` | cnpj |
| `redespacho` | `redespacho_confirmacao.py::enviar_solicitacao` | cnpj |
| `fora_dia_fixo` | `roteirizacao/notificar_fora_dia_fixo.py` (e-mail e WhatsApp) | cnpj |
| `faltas_recebimento` | `painel_agentes/wms_faltas_recebimento.py` | cnpj |
| `whatsapp_sem_resposta` | `avisar_cliente_sem_resposta.py` / `notificar_whatsapp.avisar_cliente_sem_resposta` | cnpj |
| `chamados` | `portal_cliente/chamados.py` — só os e-mails ao **cliente** | cnpj |
| `area_nao_atendida` | `portal_cliente/bloqueio_area.py` — só o e-mail ao cliente | cnpj |
| `transportadoras` | `notificacao_transportadoras/notificar_transportadoras.py` | `ponto.nome_normalizado` |
| `convite_embarcador` | `embarcadores_stokki.py::convidar` (e-mail e WhatsApp) | cnpj |
| `consulta_transportadora` | `consulta_transportadora.py::enviar_aviso` (e-mail e WhatsApp) | cnpj |

Fora da tela (resposta a uma ação do próprio cliente ou da equipe, ou só interno): cotação (`portal_cliente/cotacao.py`), erro de envio à Stokki (`portal_cliente/enviar_stokki.py`), confirmação de reentrega (`aplicar_resposta_insucesso.py`), "aguardando retirada" manual (`pedidos_parados_triagem.py`), convites de PIN (`portal_cliente/app.py`, `gerenciar_clientes.py`), informativo mensal, `notificar_area_nao_atendida.py` da roteirização (já fixo no Hugo por código), financeiro dos dedicados, e-mails à equipe/motorista dos chamados.

## Review Focus

- Interruptor geral desligado com tipo **sem linha** na tela: tem de calar o tipo (geral vence o fallback) — teste `test_geral_desligado_cala_tipo_sem_linha` (Task 1).
- Tipo em PILOTO para WhatsApp sem número de piloto cadastrado: não pode mandar para o cliente nem quebrar — vira `NAO` com motivo — teste `test_piloto_whatsapp_sem_numero_vira_nao` (Task 1).
- Ligar um tipo por evento (entrega concluída) depois de dias desligado não pode soltar acumulado — teste `test_desligado_pela_tela_grava_ignorado_e_nao_volta` (Task 3).
- Banco do controle indisponível no meio da rodada (arquivo travado/tabela ausente): rotina segue como hoje pelo config — teste `test_erro_de_banco_usa_fallback` (Task 1) e `test_rotina_sem_tabelas_de_controle_segue_config` (Task 2).
- Disparo manual clicado duas vezes / enquanto o timer roda: não pode rodar duas vezes junto — teste `test_disparo_em_andamento_recusa_segundo` (Task 9).

---

### Task 1: Módulo central `controle_notificacoes.py`

**Files:**
- Create: `controle_notificacoes.py` (raiz, ao lado de `preferencias_notificacao.py`)
- Test: `test_controle_notificacoes.py` (raiz)

**Interfaces:**
- Produces:
  - `TIPOS: dict[str, dict]` — chaves da tabela "Escopo dos tipos"; cada valor `{"rotulo": str, "destino": "embarcador"|"transportadora", "canais": ("email",)|("email","whatsapp"), "quando": str}` (`quando` = texto livre exibido, ex. "timer 07h", "dentro da sequência 16h/22h").
  - `ACOES = ("ENVIAR", "PILOTO", "NAO")`, `ESTADOS = ("DESLIGADO", "PILOTO", "LIGADO")`.
  - `@dataclass(frozen=True) class Decisao: acao: str; destino_piloto: str; origem: str; motivo: str` (`origem` ∈ `"tela"`, `"config"`).
  - `acao_do_config(ativo: bool, forcar: str) -> str` — `"NAO"` se não ativo; `"PILOTO"` se `forcar` não vazio; senão `"ENVIAR"`.
  - `decidir(tipo: str, destino: str | None, *, fallback_acao: str, fallback_piloto: str = "", canal: str = "email", db_path=None) -> Decisao`.
  - `conectar(db_path=None) -> sqlite3.Connection` (cria as tabelas se faltarem).
  - Mutações (todas gravam histórico): `definir_geral(conn, ligado: bool, por: str)`, `definir_estado(conn, tipo: str, estado: str, por: str)`, `definir_excecao(conn, tipo: str, destino: str, regra: str | None, por: str)` (`None` remove), `definir_piloto(conn, email: str, whatsapp: str, por: str)`, `desfazer(conn, historico_id: int, por: str)`.
  - Leitura: `estado_geral(conn) -> str | None`, `estado_tipo(conn, tipo) -> str | None`, `excecoes(conn, tipo) -> dict[str, str]`, `piloto(conn) -> dict` (`{"email": str, "whatsapp": str}`, padrão `{"email": "hugo@freshlogbr.com", "whatsapp": ""}`), `historico(conn, limite=50) -> list[dict]`.

Tabelas (nomes fixos, para as tasks seguintes e a tela):
`notificacoes_controle(tipo TEXT PRIMARY KEY, estado TEXT NOT NULL, atualizado_em TEXT, atualizado_por TEXT)` — o geral é a linha `tipo='_geral'`;
`notificacoes_controle_excecao(tipo TEXT, destino TEXT, regra TEXT NOT NULL, atualizado_em TEXT, atualizado_por TEXT, PRIMARY KEY(tipo, destino))`;
`notificacoes_controle_param(chave TEXT PRIMARY KEY, valor TEXT)` — `piloto_email`, `piloto_whatsapp`;
`notificacoes_controle_historico(id INTEGER PRIMARY KEY AUTOINCREMENT, quando TEXT, por TEXT, alvo TEXT, destino TEXT, de TEXT, para TEXT)` — `alvo` é o tipo, `_geral` ou `_piloto`; `de`/`para` `NULL` quando não havia/removeu.

Regra de `decidir` (nesta ordem): (1) geral `DESLIGADO` → `NAO` (origem tela); (2) tipo sem linha → `Decisao(fallback_acao, fallback_piloto se PILOTO senão "", "config", "sem linha na tela")`; (3) exceção `BLOQUEADO` → `NAO`; (4) estado `DESLIGADO` → `NAO`; `LIGADO` → `ENVIAR`; `PILOTO` → `ENVIAR` se exceção `LIBERADO`, senão `PILOTO` com `destino_piloto = piloto(conn)[canal]`, e `NAO` (motivo "piloto sem destino") se esse destino estiver vazio. Qualquer `sqlite3.Error` → fallback com `logger.warning`. `destino` é normalizado só com dígitos quando o tipo é de embarcador.

- [ ] **Step 1: Write the failing tests** em `test_controle_notificacoes.py` (banco temporário por teste via `db_path`):
  - `test_sem_linha_usa_fallback` — `decidir("nfs_em_rota", "123", fallback_acao="PILOTO", fallback_piloto="x@y")` → `Decisao("PILOTO", "x@y", "config", ...)`.
  - `test_geral_desligado_cala_tipo_sem_linha` — `definir_geral(conn, False, "hugo")`; `decidir(..., fallback_acao="ENVIAR").acao == "NAO"`.
  - `test_estados` — DESLIGADO→NAO, LIGADO→ENVIAR, PILOTO→PILOTO com `destino_piloto == "hugo@freshlogbr.com"`.
  - `test_liberado_fura_piloto_e_bloqueado_vence_ligado` — PILOTO + LIBERADO p/ "11.222.333/0001-81" (normaliza p/ "11222333000181") → ENVIAR; LIGADO + BLOQUEADO → NAO.
  - `test_piloto_whatsapp_sem_numero_vira_nao` — PILOTO, `canal="whatsapp"`, sem `definir_piloto` → `acao == "NAO"`, `"piloto sem destino" in motivo`.
  - `test_erro_de_banco_usa_fallback` — `db_path` apontando para um diretório (abre com erro) → fallback, origem "config".
  - `test_historico_e_desfazer` — `definir_estado` LIGADO depois PILOTO; `desfazer(id_da_segunda)` volta a LIGADO e grava nova linha de histórico; desfazer criação de exceção a remove.
  - `test_acao_do_config` — `(False,"")→NAO`, `(True,"h@x")→PILOTO`, `(True,"")→ENVIAR`.
  - `test_estado_invalido_recusa` — `definir_estado(conn, "nfs_em_rota", "QUALQUER", "h")` levanta `ValueError`; tipo fora de `TIPOS` também.
  - `test_transportadora_nao_aceita_bloqueado` — `definir_excecao(conn, "transportadoras", "taff", "BLOQUEADO", "h")` levanta `ValueError` (o bloqueio de transportadora é só o "NÃO ENVIAR" do cadastro); `"LIBERADO"` é aceito.
- [ ] **Step 2:** `py -3.11 -m unittest test_controle_notificacoes -v` → FAIL (módulo não existe).
- [ ] **Step 3:** Implementar `controle_notificacoes.py` com as interfaces acima (padrão de `preferencias_notificacao.py`: `DB_PATH = Path(__file__).parent / "dados" / "dados.db"`, `_garantir_tabelas`, `datetime.now().isoformat(timespec="seconds")`).
- [ ] **Step 4:** `py -3.11 -m unittest test_controle_notificacoes -v` → PASS.
- [ ] **Step 5: Commit** `git add controle_notificacoes.py test_controle_notificacoes.py` — `Notificações: módulo de controle (geral, tipo, exceção, piloto, histórico)`.

---

### Task 2: Rotinas de resumo periódico — notas em rota, resumo diário, pedidos em espera

**Files:**
- Modify: `notificar_nfs_em_rota.py` (`executar` :261, gate :267-275, laço por embarcador :295-313, `resolver_destinos` :100, CLI :378-383)
- Modify: `resumo_diario_embarcador.py` (`executar` :406, :412-462, CLI :527-533)
- Modify: `notificar_pedidos_em_espera.py` (`main` :205, gate :265, laço :295-317)
- Test: `test_controle_nas_rotinas_resumo.py` (raiz)

**Interfaces:**
- Consumes: `controle_notificacoes.decidir`, `acao_do_config`, `Decisao`.
- Produces: CLI nova `--reenviar` em `notificar_nfs_em_rota.py` e `resumo_diario_embarcador.py` (ignora o "já enviado hoje" só para o `--sender-id` dado; exige `--sender-id`). Usada pela Task 9.

Integração em cada rotina: calcular `fallback = acao_do_config(<ativo de hoje, incluindo a mestra onde ela vale>, forcar)` como o código já faz; para cada embarcador, `d = decidir(TIPO, emb["cnpj"], fallback_acao=fallback, fallback_piloto=forcar)`; `NAO` → pula (logar `d.motivo`); `PILOTO` → destinos `[d.destino_piloto]` com o aviso "iria para X" que a rotina já monta e a chave de dedup `|redirecionado` que já existe; `ENVIAR` → destinos reais. O gate global que hoje retorna cedo quando a rotina está desligada passa a retornar cedo só se **nenhum** embarcador puder receber (ex.: geral desligado, ou tipo sem linha e config desligado), para que uma linha LIGADO/PILOTO na tela funcione mesmo com a seção do config desligada. `pedidos_em_espera` hoje não tem forçar destino: fallback `acao_do_config(notificacao_pedidos_em_espera_ativa(config), "")`; em PILOTO manda para `d.destino_piloto` com o mesmo cabeçalho de redirecionamento usado em `notificar_nfs_em_rota`.

- [ ] **Step 1: Write the failing tests** (mockar `enviar`, banco temporário com `interno` + `preferencias_notificacao` mínimos, rotas falsas):
  - `test_nfs_tela_ligado_envia_mesmo_com_config_desligado` — config com mestra `false`, linha `nfs_em_rota=LIGADO` → `enviar` chamado com e-mails do embarcador.
  - `test_nfs_tela_piloto_vai_so_pro_piloto_com_chave_redirecionado` — destino `["hugo@freshlogbr.com"]`; `notificacoes_enviadas` tem chave terminando em `|redirecionado`; depois `LIGADO` no mesmo dia → envia de novo ao cliente.
  - `test_nfs_bloqueado_nao_envia` — LIGADO + BLOQUEADO no cnpj → `enviar` não chamado para ele.
  - `test_rotina_sem_tabelas_de_controle_segue_config` — sem linha nenhuma: mesmas chamadas de `enviar` que o código de hoje (mestra false + forcar hugo@ → só hugo@).
  - `test_nfs_reenviar_ignora_ja_enviado_do_sender` — após envio, `executar(..., sender_id=X, reenviar=True)` envia de novo só para X.
  - `test_resumo_diario_respeita_tela` — LIGADO envia; DESLIGADO não.
  - `test_espera_piloto_vai_pro_piloto` — PILOTO → destino `["hugo@freshlogbr.com"]`.
- [ ] **Step 2:** `py -3.11 -m unittest test_controle_nas_rotinas_resumo -v` → FAIL.
- [ ] **Step 3:** Integrar nas três rotinas; `executar(..., reenviar: bool = False)` nas duas primeiras e flag `--reenviar` na CLI.
- [ ] **Step 4:** `py -3.11 -m unittest test_controle_nas_rotinas_resumo test_preferencias_nas_rotinas -v` → PASS (o segundo garante que as preferências do cliente seguem valendo).
- [ ] **Step 5: Commit** — `Notificações: notas em rota, resumo diário e pedidos em espera obedecem a tela`.

---

### Task 3: Entrega concluída

**Files:**
- Modify: `notificacao_entregas/regras_entrega.py` (`decidir` :147, `destinos_finais` :138, estados :28-34)
- Modify: `notificacao_entregas/notificar_entrega_concluida.py` (`processar` :154, registro :195-202 e :243-257)
- Test: `notificacao_entregas/test_controle_entrega.py`

**Interfaces:**
- Consumes: `controle_notificacoes.decidir`, `acao_do_config`.
- Produces: estado novo `ESTADO_PILOTO = "PILOTO"` em `regras_entrega.py`, incluído em `ESTADOS_FINAIS`.

Integração: o fallback é `acao_do_config(cfg.ativo, cfg.forcar_destino)`; `embarcadores_piloto` continua valendo **só** no caminho de fallback (origem config). Decisão `NAO` vinda da tela grava `ESTADO_IGNORADO` com motivo `"tela: <d.motivo>"` (consumido, como hoje com `ativo=false`). `PILOTO` envia para `d.destino_piloto` com o aviso de piloto já existente (:237-238) e grava `ESTADO_PILOTO` (final: não reenvia esse pedido ao cliente depois — regra "o que não saiu some"; e não conta como recebido). `ENVIAR` segue o fluxo atual. O identificador do embarcador para `decidir` é o cnpj do dict de `aplicar_preferencias`.

- [ ] **Step 1: Write the failing tests:**
  - `test_desligado_pela_tela_grava_ignorado_e_nao_volta` — DESLIGADO → estado IGNORADO motivo começa com "tela:"; troca para LIGADO e roda de novo → `enviar` não chamado para esse `service_id`.
  - `test_piloto_pela_tela_grava_estado_piloto` — destino `["hugo@freshlogbr.com"]`, estado `PILOTO`.
  - `test_ligado_pela_tela_ignora_embarcadores_piloto_do_config` — `cfg.embarcadores_piloto=[999]`, embarcador 123, tela LIGADO → envia para 123.
  - `test_sem_linha_comportamento_atual` — sem linha, `cfg.ativo=False` → IGNORADO "desligado" (motivo de hoje).
- [ ] **Step 2:** `py -3.11 -m unittest notificacao_entregas.test_controle_entrega -v` → FAIL.
- [ ] **Step 3:** Implementar.
- [ ] **Step 4:** `py -3.11 -m unittest discover -s notificacao_entregas -p "test_*.py"` → todos PASS.
- [ ] **Step 5: Commit** — `Notificações: entrega concluída obedece a tela (piloto vira estado próprio)`.

---

### Task 4: Envios dentro das sequências — insucesso, agendamento, redespacho, fora do dia fixo

**Files:**
- Modify: `insucesso_entrega/notificar_insucesso_aguardando_resposta.py` (`notificar_remetentes` :269, :288-329)
- Modify: `expedir_pedidos.py` (gate :1435)
- Modify: `roteirizacao/notificar_agendamento_pendente.py` (`notificar_remetentes` :155-189), `roteirizacao/notificar_agendamento_dia_fixo.py` (:119-156)
- Modify: `roteirizacao/criar_rotas_diarias.py` (:888, :951), `roteirizacao/incrementar_rotas.py` (:511), `pipeline.py` (:1307)
- Modify: `agendamento_confirmacao.py` (`enviar_solicitacao` :127, envio :217), `redespacho_confirmacao.py` (`enviar_solicitacao` :94, envio :180), `pipeline.py` (:659, :786)
- Modify: `roteirizacao/notificar_fora_dia_fixo.py` (`avisar` :127-186)
- Test: `test_controle_nas_sequencias.py` (raiz) e `roteirizacao/test_controle_fora_dia_fixo.py`

**Interfaces:**
- Consumes: `controle_notificacoes.decidir`, `acao_do_config`.

Integração: os gates `notificacoes_automaticas_ativas(config)` dos chamadores saem dali e viram o **fallback** dentro de cada função de envio (`acao_do_config(notificacoes_automaticas_ativas(config), "")`), para que a tela consiga ligar o tipo mesmo com a mestra desligada; cada função recebe `config` (ou o booleano da mestra) como parâmetro novo `mestra: bool` com default `True` para não quebrar chamadores de teste. Por embarcador: `NAO` pula; `PILOTO` manda para `d.destino_piloto` com uma linha "Piloto: iria para X" no topo do corpo; `ENVIAR` como hoje. `insucesso` em PILOTO **não** chama `marcar_notificado` nem `registrar_evento` (o cliente ainda não foi perguntado). O botão "Notificar" da Torre (`ignorar_preferencia=True`) continua ignorando também a tela (ação manual da equipe). `agendamento_confirmacao`/`redespacho_confirmacao`: fallback `"ENVIAR"` quando `notificar_email` e e-mail existem (comportamento de hoje), tipos `agendamento` e `redespacho`. `fora_dia_fixo`: e-mail com `canal="email"` e WhatsApp com `canal="whatsapp"` decididos separadamente; quando **os dois** canais dão `NAO` pela tela, gravar `registrar_aviso(conn, servico, data, ["bloqueado"])` (consumido); cópia de piloto grava `registrar_aviso(..., ["piloto"])` e `ja_avisado` passa a ignorar linhas cujo único canal é `"piloto"` (vale o "não conta como recebido") — mas como é evento, o piloto não é reenviado ao cliente pela própria rotina (só pelo disparo manual futuro).

- [ ] **Step 1: Write the failing tests:**
  - `test_insucesso_tela_ligado_com_mestra_desligada_envia` e `test_insucesso_piloto_nao_marca_notificado`.
  - `test_agendamento_pendente_desligado_nao_envia`, `test_dia_fixo_piloto_vai_pro_piloto`.
  - `test_solicitacao_agendamento_sem_linha_envia_como_hoje`, `test_redespacho_bloqueado_nao_envia`.
  - `test_fora_dia_fixo_bloqueado_nos_dois_canais_consome` (linha `avisos_fora_dia_fixo` com canal `bloqueado`; segunda chamada não envia), `test_fora_dia_fixo_whatsapp_piloto_sem_numero_nao_envia_whatsapp`.
- [ ] **Step 2:** rodar `py -3.11 -m unittest test_controle_nas_sequencias -v` e `py -3.11 -m unittest roteirizacao.test_controle_fora_dia_fixo -v` → FAIL.
- [ ] **Step 3:** Implementar.
- [ ] **Step 4:** novos testes PASS e regressão: `py -3.11 -m unittest roteirizacao.test_notificar_fora_dia_fixo roteirizacao.test_fora_dia_fixo test_preferencias_nas_rotinas` (e os `test_*` de `insucesso_entrega/` que existirem) → PASS.
- [ ] **Step 5: Commit** — `Notificações: insucesso, agendamento, redespacho e fora do dia fixo obedecem a tela`.

---

### Task 5: Faltas de recebimento, chamados (cliente) e área não atendida (cliente)

**Files:**
- Modify: `painel_agentes/wms_faltas_recebimento.py` (`notificar_faltas` :319, :356-392)
- Modify: `portal_cliente/chamados.py` (`_mandar` :982; chamadores de cliente `email_confirmacao_cliente` :1040, `email_resposta_cliente` :1055, `email_historico` :1068 quando não é motorista)
- Modify: `portal_cliente/bloqueio_area.py` (`abrir_bloqueio` :84, envio ao cliente :133-137)
- Test: `painel_agentes/test_controle_faltas.py` (rodar de dentro de `painel_agentes/`), `portal_cliente/test_controle_chamados_area.py`

**Interfaces:**
- Consumes: `controle_notificacoes.decidir`, `acao_do_config`.
- Produces: `_mandar(..., tipo_controle: str | None = None, cnpj: str | None = None)` — só quando `tipo_controle` vem preenchido a trava é aplicada; `email_para_atendimento` e e-mails ao motorista não passam.

Integração: `faltas_recebimento` igual à Task 2 (dedup `|redirecionado` já existe; `cc=[COPIA_INTERNA]` continua só no envio real). `chamados`: fallback `acao_do_config(True, cfg_chamados(config).get("forcar_destino") or "")`; `NAO` não manda ao cliente mas o chamado segue normal; `PILOTO` manda para `d.destino_piloto`. `bloqueio_area`: só `destinos_cliente` passa por `decidir` (fallback `acao_do_config(True, forcar_destino(config))`); o e-mail interno (:138-143) continua usando o `forcar` atual sem a trava nova.

- [ ] **Step 1: Write the failing tests:**
  - `test_faltas_tela_desligado_nao_envia`, `test_faltas_piloto_sem_copia_interna`.
  - `test_chamado_cliente_bloqueado_nao_recebe_mas_equipe_recebe` (geral desligado: e-mail à equipe sai, ao cliente não).
  - `test_area_cliente_piloto_e_interno_inalterado`.
- [ ] **Step 2:** rodar os dois arquivos → FAIL.
- [ ] **Step 3:** Implementar.
- [ ] **Step 4:** PASS + regressão `cd portal_cliente && py -3.11 -m unittest discover -p "test_chamados*.py"` e o teste existente de bloqueio de área, e `cd painel_agentes && py -3.11 -m unittest test_wms_faltas_recebimento` (nomes reais: conferir com `ls`).
- [ ] **Step 5: Commit** — `Notificações: faltas, chamados e área não atendida (só o cliente) obedecem a tela`.

---

### Task 6: WhatsApp sem resposta, convite de embarcador e consulta de transportadora

**Files:**
- Modify: `avisar_cliente_sem_resposta.py` (`main` :144, `executar` :104, `selecionar` :85)
- Modify: `notificar_whatsapp.py` (`avisar_cliente_sem_resposta` :758, `avisar_cliente_cadastro` :825, `avisar_cliente_transportadora` :860)
- Modify: `embarcadores_stokki.py` (`destino_email` :229, `convidar` :272-317)
- Modify: `consulta_transportadora.py` (`enviar_aviso` :322, `config_consulta` :232)
- Test: `test_controle_whatsapp_convite.py` (raiz)

**Interfaces:**
- Consumes: `controle_notificacoes.decidir`, `acao_do_config`.
- Produces: as três funções de `notificar_whatsapp` ganham `destino_forcado: str | None = None`; quando preenchido, substitui o `forcar_destino` do config naquela chamada (é o `d.destino_piloto`).

Integração: `whatsapp_sem_resposta` — fallback `acao_do_config(nw.clientes_ligado(config), _cfg_clientes(config).get("forcar_destino") or "")`; a checagem `clientes_ligado` de `main` deixa de retornar cedo quando há linha na tela (gateway OpenWA desconfigurado continua impedindo). Decisão `NAO` pela tela grava a assinatura `msg:<id>` em `notificacoes_whatsapp` com situação `bloqueado` (consumido; o `NOT EXISTS` já exclui). PILOTO grava com origem `cliente_sem_resposta_piloto` (não conta como recebido). `convite_embarcador` — e-mail e WhatsApp com `canal` próprio; PILOTO **não** marca CONVIDADO (o futuro cliente ainda não recebeu); `NAO` pela tela também não marca (convite pendente não é "evento velho": continua pendente para quando ligar — exceção consciente à regra "some", porque convite é estado, não evento). `consulta_transportadora` — fallback `acao_do_config(cfg["ativo"], cfg["forcar_destino_email"] or "")` (ausente = envio real, como hoje); `NAO` grava `enviado_em` (consumido).

- [ ] **Step 1: Write the failing tests:**
  - `test_sem_resposta_tela_desligado_consome_msg`, `test_sem_resposta_piloto_vai_pro_numero_do_piloto`.
  - `test_convite_piloto_nao_marca_convidado`, `test_convite_ligado_marca_convidado`.
  - `test_consulta_transportadora_bloqueado_consome`.
- [ ] **Step 2:** `py -3.11 -m unittest test_controle_whatsapp_convite -v` → FAIL.
- [ ] **Step 3:** Implementar.
- [ ] **Step 4:** PASS + regressão dos `test_*` existentes de `notificar_whatsapp`, `avisar_cliente_sem_resposta`, `embarcadores_stokki`, `consulta_transportadora` (localizar com `ls test_*whatsapp* test_*embarcadores* test_*consulta*`).
- [ ] **Step 5: Commit** — `Notificações: WhatsApp sem resposta, convite e consulta de transportadora obedecem a tela`.

---

### Task 7: Transportadoras

**Files:**
- Modify: `notificacao_transportadoras/notificar_transportadoras.py` (gate :309, laço por ponto :364-381, destinos :437-438)
- Test: `notificacao_transportadoras/test_trava_transportadoras.py` (estender; já existe)

**Interfaces:**
- Consumes: `controle_notificacoes.decidir`, `acao_do_config`; `CatalogoTransportadoras.pontos_redespacho()` (regras/transportadoras.py:177).

Integração: fallback `acao_do_config(notificacoes_automaticas_ativas(config) and notificacao_transportadoras_ativa(config), "")`. Por ponto, depois do `ponto.nao_enviar` (que continua sendo o bloqueio por ponto — a tela não grava outro): `d = decidir("transportadoras", ponto.nome_normalizado, ...)`; `NAO` → pula **e** `registrar_notificacao(pedido, transportadora)` para cada pedido (consumido); `PILOTO` → `destinos=[d.destino_piloto]`, `cc=[]`, sem `registrar_notificacao`; `ENVIAR` → como hoje. O gate global só retorna cedo se o geral estiver desligado ou se não houver linha e o fallback for `NAO`.

- [ ] **Step 1: Write the failing tests** (no arquivo existente, mesmo `_rodar_main`):
  - `test_tela_ligado_envia_mesmo_sem_trava_do_config`.
  - `test_tela_piloto_vai_pro_piloto_sem_cc`.
  - `test_liberado_no_piloto_envia_pra_transportadora`.
  - os 4 testes atuais continuam passando sem alteração (sem linha = config).
- [ ] **Step 2:** `py -3.11 -m unittest notificacao_transportadoras.test_trava_transportadoras -v` → FAIL nos novos.
- [ ] **Step 3:** Implementar.
- [ ] **Step 4:** PASS.
- [ ] **Step 5: Commit** — `Notificações: transportadoras obedecem a tela (NÃO ENVIAR do cadastro segue valendo)`.

---

### Task 8: Tela `/notificacoes`

**Files:**
- Create: `painel_agentes/notificacoes_tela.py` (montagem dos dados da tela; sem Flask)
- Create: `painel_agentes/templates/notificacoes.html`
- Modify: `painel_agentes/painel_agentes.py` (rotas, perto de `/acessos` :1618)
- Modify: `painel_agentes/templates/_menu_lateral_nav.html` (item ao lado de `acessos`, `'niveis': ('total',)`)
- Modify: `painel_agentes/test_acesso_telas.py` (`FORA_DO_CATALOGO`)
- Test: `painel_agentes/test_notificacoes_tela.py` (padrão de `test_transportadoras_tela.py`: carrega o app com `_open_sem_config`, banco temporário, login)

**Interfaces:**
- Consumes: tudo de `controle_notificacoes`; `preferencias_notificacao.ler(conn, cnpj)` / `salvar(conn, cnpj, emails, {}, por, whatsapp=None)`; `CatalogoTransportadoras.carregar_do_banco(DB_PATH).pontos_redespacho()`.
- Produces (rotas):
  - `GET /notificacoes` → endpoint `notificacoes`.
  - `GET /api/notificacoes/tipo/<tipo>` → `{"tipo", "estado", "origem", "destinos": [{"chave","nome","excecao","preferencia_cliente","nao_enviar"}]}` (`nao_enviar` só p/ transportadoras, **somente leitura**: a tela mostra "Não enviar (cadastro)" com link para `/transportadoras`, onde ele é editado; o painel de transportadoras só oferece Liberar no piloto / Limpar).
  - `POST /api/notificacoes/geral` `{"ligado": bool, "confirmado": bool}`.
  - `POST /api/notificacoes/estado` `{"tipo", "estado", "confirmado": bool}` — para `LIGADO` sem `confirmado` responde `409` com `{"destinatarios": N}` (quantos receberiam) e não grava.
  - `POST /api/notificacoes/excecao` `{"tipo", "destino", "regra": "LIBERADO"|"BLOQUEADO"|null}`.
  - `POST /api/notificacoes/piloto` `{"email", "whatsapp"}`.
  - `POST /api/notificacoes/desfazer` `{"id"}`.
  - `GET|POST /api/notificacoes/embarcador/<cnpj>` — lê/grava `emails` e `whatsapp` via `preferencias_notificacao` (mesma lista do portal; `por = "painel:" + usuario`).
- `notificacoes_tela.montar_lista(conn) -> list[dict]` — uma linha por `TIPOS`: `{"tipo","rotulo","estado" (ou "config" quando sem linha),"resumo_excecoes" (ex. "2 bloqueados · 1 liberado"),"quando","ultimo_envio"}` (`ultimo_envio` = maior `enviado_em` de `notificacoes_enviadas` para o tipo, quando houver; senão "—").
- `notificacoes_tela.contar_destinatarios(conn, tipo) -> int` — embarcadores com e-mail e preferência ligada para o tipo (via `preferencias_notificacao.carregar_embarcadores`), menos os BLOQUEADOS; transportadoras = pontos sem `nao_enviar`.

Layout (esboço A escolhido pelo Hugo, `.superpowers/brainstorm/47419-1792878314/content/layout-notificacoes.html`): faixa do geral no topo (vermelha quando desligado); tabela Tipo · Estado · Exceções · Quando · Último envio; clicar abre painel lateral com o seletor de três estados, a lista de destinos com busca e botões Liberar/Bloquear/Limpar, e (Task 9) "Disparar agora"; aba/área "Histórico" com Desfazer; bloco "Destino do piloto" (e-mail e WhatsApp). Tipo sem linha mostra "segue o config" até a primeira mudança. Seguir o visual das telas existentes (`acessos.html`, `transportadoras.html`).

- [ ] **Step 1: Write the failing tests:**
  - `test_so_total_abre` — login `operador` → 403/redirect em `/notificacoes` e nas APIs; `total` → 200.
  - `test_ligar_pede_confirmacao_com_contagem` — `POST estado LIGADO` sem `confirmado` → 409 com `destinatarios`; com `confirmado` → 200 e `estado_tipo == "LIGADO"`.
  - `test_excecao_e_desfazer_pela_api`.
  - `test_emails_do_embarcador_sao_os_do_portal` — POST grava; `preferencias_notificacao.ler` devolve os mesmos e-mails.
  - `test_post_sem_origem_recusado` (`@exige_mesma_origem`).
  - `test_toda_rota_do_painel_cai_em_alguma_tela` continua passando (endpoints novos em `FORA_DO_CATALOGO`).
- [ ] **Step 2:** `cd painel_agentes && py -3.11 -m unittest test_notificacoes_tela test_acesso_telas -v` → FAIL.
- [ ] **Step 3:** Implementar módulo, rotas, template e menu.
- [ ] **Step 4:** mesmo comando → PASS. Subir em outra porta (`py -3.11 -c "import painel_agentes; painel_agentes.app.run(host='127.0.0.1', port=8099)"` de dentro de `painel_agentes/`) e abrir `/notificacoes` logado como total: lista aparece, ligar pede confirmação, histórico mostra a mudança (banco local).
- [ ] **Step 5: Commit** — `Painel: tela Notificações (geral, tipos, exceções, piloto, histórico, e-mails do embarcador)`.

---

### Task 9: Disparar agora

**Files:**
- Modify: `painel_agentes/notificacoes_tela.py`, `painel_agentes/painel_agentes.py`, `painel_agentes/templates/notificacoes.html`
- Test: `painel_agentes/test_notificacoes_tela.py` (estender)

**Interfaces:**
- Consumes: CLIs `notificar_nfs_em_rota.py` e `resumo_diario_embarcador.py` (`--modo-teste`, `--sender-id`, `--reenviar`, `--data hoje`), `notificar_pedidos_em_espera.py`, `notificacao_entregas/notificar_entrega_concluida.py`, `notificacao_transportadoras/notificar_transportadoras.py` (`--modo-teste`, `--data hoje`), `avisar_cliente_sem_resposta.py` (`--modo-teste`).
- Produces:
  - `DISPARAVEIS: dict[str, dict]` em `notificacoes_tela.py` — por tipo: `{"script": caminho relativo à raiz, "alvos": ("teste","todos") ou ("teste","todos","um")}`; `"um"` só para `nfs_em_rota` e `resumo_diario`.
  - tabela `notificacoes_disparos(id INTEGER PRIMARY KEY AUTOINCREMENT, tipo TEXT, alvo TEXT, destino TEXT, por TEXT, iniciado_em TEXT, terminado_em TEXT, codigo_saida INTEGER, saida TEXT)` (`saida` = últimas 40 linhas).
  - `montar_comando(tipo: str, alvo: str, sender_id: int | None, reenviar: bool) -> list[str]` — `[sys.executable, <script>, ...]`; `"teste"` → `--modo-teste`; `"um"` → `--sender-id N` (+ `--reenviar` se pedido); `"todos"` → sem flags extras.
  - `ja_recebeu_hoje(conn, tipo) -> dict[int, str]` — `sender_id → hora` a partir de `notificacoes_enviadas` (só chaves **sem** `|redirecionado`), para `nfs_em_rota` e `resumo_diario`.
  - `iniciar_disparo(conn, tipo, alvo, sender_id, reenviar, por) -> int` — recusa (`RuntimeError`) se houver disparo do mesmo tipo sem `terminado_em` iniciado há menos de 30 min; grava a linha e roda o subprocesso numa thread (`subprocess.run(..., cwd=raiz, timeout=1800, capture_output=True)`), atualizando a linha ao terminar.
  - Rotas: `GET /api/notificacoes/disparo/<tipo>` → `{"alvos", "ja_recebeu_hoje", "ultimos": [...]}`; `POST /api/notificacoes/disparo` `{"tipo","alvo","sender_id","reenviar"}` → `{"id"}` ou 409.

UI: botão "Disparar agora" no painel lateral só para tipos em `DISPARAVEIS`; janela pergunta **para quem** (Só para mim (teste) · Todos os liberados · Um embarcador [lista]); para "um", se ele já recebeu hoje mostra "enviado hoje às HH:MM" e a caixa "reenviar" desmarcada; mostra o resultado do último disparo (código de saída e saída). Para os demais tipos o painel mostra "sai dentro de <rotina>" (insucesso: "use o botão Notificar da Torre").

- [ ] **Step 1: Write the failing tests:**
  - `test_montar_comando` — `("nfs_em_rota","teste",None,False)` termina em `["--modo-teste"]`; `("nfs_em_rota","um",42,True)` contém `"--sender-id","42","--reenviar"`; `("insucesso", ...)` levanta `ValueError`.
  - `test_ja_recebeu_hoje_ignora_piloto` — chaves `42|<hoje>` e `43|<hoje>|redirecionado` → só 42.
  - `test_disparo_em_andamento_recusa_segundo` — com `subprocess.run` mockado bloqueando, segundo `iniciar_disparo` do mesmo tipo → `RuntimeError`; API → 409.
  - `test_disparo_so_total`.
- [ ] **Step 2:** `cd painel_agentes && py -3.11 -m unittest test_notificacoes_tela -v` → FAIL nos novos.
- [ ] **Step 3:** Implementar.
- [ ] **Step 4:** PASS; na porta 8099, disparar `nfs_em_rota` "Só para mim" com banco local e ver a linha em `notificacoes_disparos` com saída.
- [ ] **Step 5: Commit** — `Painel: Disparar agora na tela Notificações (pergunta pra quem, avisa quem já recebeu)`.

---

### Task 10: Mapa, verificação geral e roteiro de deploy

**Files:**
- Modify: `MAPA_DO_SISTEMA.txt` (tela nova, módulo `controle_notificacoes.py`, tabelas `notificacoes_controle*` e `notificacoes_disparos`; trocar a frase "Envio real para clientes/motoristas é desligado por padrão (notificacoes_automaticas.ativo, forcar_destino)" por "… desligado por padrão; ligar pela tela /notificacoes (o config vale enquanto o tipo não tem linha na tela)")

- [ ] **Step 1:** `py -3.11 -m py_compile` em todos os arquivos tocados; rodar todos os testes novos e os de regressão citados nas Tasks 2–9 (respeitando a separação painel × roteirização).
- [ ] **Step 2:** Atualizar o mapa.
- [ ] **Step 3: Commit** — `Mapa: tela Notificações e controle central`.
- [ ] **Step 4 (só com o Hugo pedindo deploy):** deploy pela skill `deploy-vps` com restart de `painel-agentes` e `portal-cliente` (chamados/área leem o módulo; o portal precisa recarregar o código). Provar: abrir `/painel/notificacoes` logado como total; todos os tipos aparecem como "segue o config" (nenhuma linha criada); `vps_ops rodar --real` de `test_controle_notificacoes.py` na VPS. Não ligar nada: a primeira mudança na tela é do Hugo.
