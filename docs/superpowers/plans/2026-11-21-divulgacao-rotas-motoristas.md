# Divulgacao de rotas aos motoristas (recusa ou equipe) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rota enviada so fica aberta aos motoristas por recusa ou por decisao da equipe (nunca por horario) e, aberta, e divulgada em etapas: grupo Rotas FRESHLOG, depois mensagem particular pra quem esta perto de terminar, fixos e esporadicos.

**Architecture:** Tudo continua em `nucleo/ofertas_rota_enviada.py` (timer de 2 min `stokki-ofertar-rotas-sem-aceite`). Sai o gatilho PRAZO; entra o motivo EQUIPE (botao no card da rota enviada do planejamento, endpoint fino no painel) e uma rodada de etapas que manda WhatsApp particular por `notificar_whatsapp.avisar_motorista_oferta` (origem e teto proprios, piloto por `forcar_destino_whatsapp`). Quem ja recebeu fica em `nucleo_ofertas_rota_mensagens`.

**Tech Stack:** Python 3.11, SQLite (`dados/dados.db`), Flask (painel), JS puro no template `planejamento_rotas.html`, OpenWA via `notificar_whatsapp.despachar`.

**Spec:** `docs/superpowers/specs/2026-11-21-divulgacao-rotas-motoristas-design.md`

## Global Constraints

- Python sempre `py -3.11`; testes com `py -3.11 -m unittest`, nunca misturar `nucleo` e `painel_agentes` no mesmo comando.
- Rota so abre por RECUSA ou EQUIPE. Nenhum gatilho por tempo ou horario; `horario_limite` deixa de ser lido.
- Etapas contadas a partir de `aviso_grupo_em` da oferta: PERTO_DO_FIM em 15 min, FIXO 10 min depois da etapa anterior aberta, ESPORADICO 10 min depois da FIXO. Etapa sem ninguem e pulada na hora.
- Perto do fim = rota de hoje do motorista com ate 2 paradas pendentes (`PENDENTE`, `EM_DESLOCAMENTO`, `EM_ROTA`) ou rota de hoje `CONCLUIDA`.
- Fixo = `bd_motoristas.POSICAO = 'FIXO'`; qualquer outro valor ou sem cadastro = esporadico.
- Cada motorista recebe no maximo uma mensagem particular por oferta; oferta que nao esta ABERTA nao manda mais nada.
- Recebe mensagem so motorista com login ativo (`motoristas.ativo = 1`), menos o original, menos quem `motivo_veiculo` barra.
- Mensagem particular: origem `oferta_rota_motorista`, teto `api_motorista.rota_sem_aceite.teto_mensagens` (padrao 60), piloto `api_motorista.rota_sem_aceite.forcar_destino_whatsapp` (numero = so pra la com `[teste → +<destino>]`; `""` = real; ausente = `clientes.forcar_destino`; sem nenhum = nao manda). Texto ate 200 caracteres, link nunca cortado.
- No maximo 5 mensagens particulares por rodada (intervalo de 20 s do gateway; a rodada nao pode passar de 2 min).
- `--modo-teste` nao grava, nao manda, nao consulta a Vuupt.
- `config.yaml` nunca e lido no chat nem alterado por esta tarefa.
- Comentarios e logs sem acento (padrao do arquivo); textos pro motorista com acento.

## Review Focus

1. Equipe oferta rota que o motorista ja ACEITOU: `manter` nao pode retirar na rodada seguinte com "motorista original aceitou" (teste na Task 2).
2. Motorista sem telefone: nao trava a etapa nem repete toda rodada; fica registrado `sem_telefone` (teste na Task 3).
3. Gateway falhou numa mensagem: o motorista tenta de novo na proxima rodada, os outros nao repetem (teste na Task 3).
4. Consulta a Vuupt fora do ar na etapa PERTO_DO_FIM: segue com o andamento do nucleo, sem derrubar a rodada (teste na Task 3).
5. Oferta escolhida entre duas rodadas: nenhuma mensagem particular sai depois da escolha (teste na Task 3).
6. Vuupt recusa a troca no Confirmar (rota iniciada, Lalamove com corrida): nada muda no app nem na oferta e o card mostra o motivo (ordem Vuupt -> app no endpoint da Task 4; conferir no codigo).

---

### Task 1: Tirar o gatilho por horario

**Files:**
- Modify: `nucleo/ofertas_rota_enviada.py` (docstring do topo, `config_ofertas`, `_VUUPT_SEM_ACEITE`, `_aceita_na_vuupt`, `_atribuida_em`, `candidatas`, `manter`, `main --situacao`)
- Modify: `nucleo/banco.py:303-309` (comentario da tabela: `motivo` = `RECUSA | EQUIPE`; antigas `PRAZO` ficam no historico)
- Test: `nucleo/test_ofertas_rota_enviada.py`

**Interfaces:**
- Produces: `candidatas(conn, cfg, agora) -> list[tuple[Row, str]]` devolve so `MOTIVO_RECUSA`. `MOTIVO_PRAZO` continua definido (historico), nada mais o cria.

- [ ] **Step 1: Trocar os testes de prazo**

Apagar `test_prazo_so_publica_depois_das_0530_e_sem_aceite`, `test_rota_enviada_depois_das_0530_so_vira_oferta_se_recusar`, `test_troca_de_motorista_depois_das_0530_vale_o_assigned_at_da_vuupt`, `test_aceite_so_na_vuupt_nao_publica` e o UPDATE de `criado_em` da vespera no `setUp`. Os testes que rodam `_rodar("05:40")` esperando publicacao sem recusa passam a recusar antes (`self.cli.post(f"/api/rotas/{self.rota_id}/recusar", ...)`). Novo:

```python
def test_nunca_abre_por_horario_so_por_recusa(self):
    for hora in ("05:29", "05:30", "09:00", "23:59"):
        self.assertEqual(self._rodar(hora)["publicadas"], 0)
    self.assertIsNone(self._oferta())
    self.cli.post(f"/api/rotas/{self.rota_id}/recusar", json={"motivo": "x"}, headers=self._h(ORIGINAL))
    self.assertEqual(self._rodar("09:02")["publicadas"], 1)
    self.assertEqual(self._oferta()["motivo"], "RECUSA")
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `py -3.11 -m unittest nucleo.test_ofertas_rota_enviada -v`
Expected: FAIL em `test_nunca_abre_por_horario_so_por_recusa` (publica as 05:30).

- [ ] **Step 3: Implementar**

`candidatas` so olha `_recusou`; remover `passou_do_prazo`, `_atribuida_em`, `_aceita_na_vuupt`, `_VUUPT_SEM_ACEITE`, a chave `horario_limite` de `config_ofertas` e do `--situacao`, e o ramo `MOTIVO_PRAZO` de `manter`. Docstring: os dois gatilhos sao RECUSA e EQUIPE.

- [ ] **Step 4: Rodar e ver passar**

Run: `py -3.11 -m unittest nucleo.test_ofertas_rota_enviada -v`
Expected: OK.

- [ ] **Step 5: Commit**

```bash
git add nucleo/ofertas_rota_enviada.py nucleo/banco.py nucleo/test_ofertas_rota_enviada.py
git commit -m "Motorista: rota enviada so vira oferta por recusa (sem prazo por horario)"
```

### Task 2: Equipe oferta (e retira) a rota enviada

**Files:**
- Modify: `nucleo/ofertas_rota_enviada.py` (novo `MOTIVO_EQUIPE`, `ofertar_pela_equipe`, `manter`, `ao_aceitar`)
- Modify: `nucleo/banco.py` (`_migrar_colunas(conn, "nucleo_ofertas_rota", [("ofertada_por", "TEXT")])`)
- Modify: `painel_agentes/painel_agentes.py` (endpoint novo, junto de `api_cancelar_e_notificar`)
- Modify: `painel_agentes/planejamento_rotas.py:1170-1190` (campo `oferta_enviada` no rascunho enviado)
- Modify: `painel_agentes/templates/planejamento_rotas.html` (botao no `.acoes` do card, ao lado de `botao-cancelar-notificar`; handler no listener de clique)
- Modify: `MAPA_DO_SISTEMA.txt` (oferta de rota enviada: gatilhos RECUSA/EQUIPE e o botao)
- Test: `nucleo/test_ofertas_rota_enviada.py`

**Interfaces:**
- Produces: `MOTIVO_EQUIPE = "EQUIPE"`.
- Produces: `ofertar_pela_equipe(conn, rota_id: int, usuario: str, gmaps_key: str | None, resumo=montar_resumo) -> dict` → `{"ok": True, "oferta_id": int}` ou `{"ok": False, "erro": str}`. Commita.
- Produces: `retirar_pela_equipe(conn, rota_id: int, usuario: str) -> dict` → `{"ok": True}` ou `{"ok": False, "erro": str}`. So retira oferta `ABERTA` com motivo `EQUIPE` (escolhida = erro "Alguem ja pegou essa rota: troque o motorista na Vuupt ou fale com a operacao."); grava `RETIRADA` com `motivo_retirada = "retirada pela equipe (<usuario>)"`. Commita. A rota continua com uma oferta so: depois de retirada nao da pra ofertar de novo (decisao minha, igual a regra de hoje).
- Produces: `oferta_da_rota(conn, rota_id)` (ja existe) e usado pelo painel pra preencher `oferta_enviada = {"status": ..., "motivo": ...}` ou `None`.
- Endpoint extra: `POST /api/planejamento/retirar-oferta` body `{"rascunho_id": int}`, mesmos decoradores e mesma busca da rota; devolve `retirar_pela_equipe`.
- Endpoint: `POST /api/planejamento/ofertar-rota` body `{"rascunho_id": int}`, decoradores iguais ao `cancelar-e-notificar` (`requer_auth(niveis=("total", "operador", "logistica"))`, `exige_mesma_origem`, `bloqueia_planejamento_passado`). Acha `nucleo_rotas` pelo `rascunho_id` (maior `id`); sem rota = 404 "Rota ainda nao chegou ao nucleo, tente em 1 minuto."; chave desligada (`config_ofertas(config)["ativo"]` falso) = 400 "Divulgacao de rotas desligada no config.".

- [ ] **Step 1: Testes**

```python
def test_equipe_oferta_rota_enviada_e_o_aviso_sai_no_grupo(self):
    conn = banco.conectar()
    r = ofertas.ofertar_pela_equipe(conn, self.rota_id, "hugo", None, resumo=lambda c, rota, k: {"total_paradas": 2})
    conn.close()
    self.assertTrue(r["ok"])
    o = self._oferta()
    self.assertEqual((o["motivo"], o["status"], o["ofertada_por"]), ("EQUIPE", "ABERTA", "hugo"))
    self._rodar("10:00")
    self.assertEqual([e["tipo"] for e in self.enviados], ["rota_disponivel"])

def test_equipe_nao_oferta_duas_vezes_nem_rota_iniciada(self):
    # 2a chamada -> ok False, erro "ja foi ofertada"; rota EM_ROTA/CONCLUIDA/CANCELADA ou de ontem -> ok False

def test_oferta_da_equipe_nao_cai_quando_a_rota_ja_estava_aceita(self):
    self.cli.post(f"/api/rotas/{self.rota_id}/aceitar", json={}, headers=self._h(ORIGINAL))
    # ofertar_pela_equipe -> ok; self._rodar("10:00") -> oferta segue ABERTA
    # original aceitar de novo (ao_aceitar) tambem nao retira

def test_equipe_retira_oferta_aberta_e_nao_retira_escolhida(self):
    # ofertar -> retirar_pela_equipe ok, status RETIRADA, motivo_retirada cita o usuario, some do app do OUTRO
    # ofertar de novo apos retirar -> ok False; oferta ESCOLHIDA -> retirar ok False e segue ESCOLHIDA
    # oferta por RECUSA (nao EQUIPE) -> retirar ok False

def test_original_nao_ve_a_oferta_da_equipe_e_outro_escolhe(self):
    # listar_para_motorista(ORIGINAL) sem a oferta; OUTRO ve e escolhe -> ESCOLHIDA
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `py -3.11 -m unittest nucleo.test_ofertas_rota_enviada -v`
Expected: FAIL com `AttributeError: ... has no attribute 'ofertar_pela_equipe'`.

- [ ] **Step 3: Implementar `ofertar_pela_equipe` e ajustar `manter`/`ao_aceitar`**

Aceita rota da Vuupt com `status` em `PLANEJADA`/`ACEITA` e `data_rota >= hoje`; usa `publicar(conn, rota, MOTIVO_EQUIPE, motoristas_com_app(conn, rota), resumo(...))` e grava `ofertada_por`. Erros em portugues pra tela ("Essa rota ja foi ofertada.", "Rota ja iniciada, concluida ou cancelada: nao da pra ofertar."). Em `manter`, o ramo "motorista original aceitou" vale so pra `MOTIVO_RECUSA`; em `ao_aceitar`, oferta `EQUIPE` aberta nao e retirada.

- [ ] **Step 4: Rodar e ver passar**

Run: `py -3.11 -m unittest nucleo.test_ofertas_rota_enviada -v`
Expected: OK.

- [ ] **Step 5: Painel: endpoint, campo e botao**

Endpoint conforme Interfaces (`gmaps_key` de `config["google_maps"]["api_key"]`, usuario `session.get("usuario") or g.nivel_acesso`). No card: `enviada && PODE_EDITAR && !rascunho.oferta_enviada` → botao `botao-ofertar-rota` "Ofertar para motoristas" (title: "Abre a rota pros outros motoristas: aviso no grupo Rotas FRESHLOG agora e mensagem particular em etapas"); com `oferta_enviada` ABERTA e motivo EQUIPE → texto "Ofertada" + botao `botao-retirar-oferta` "Retirar oferta" (confirm "Retirar a oferta? Some do app dos motoristas e nao da pra ofertar de novo."); em outro status → so o texto "Ofertada (<status>)". Handler `ofertarRota(rascunhoId, botao)`: `confirm("Ofertar essa rota pros motoristas? Sai aviso no grupo Rotas FRESHLOG em ate 2 minutos.")`, `postJSON("/api/planejamento/ofertar-rota", ...)`, alerta o erro ou recarrega (mesmo padrao de `cancelarENotificar`). A rota nova entra em `acesso_telas.TELAS`? Nao: e acao da tela `/planejamento`, ja coberta.

- [ ] **Step 6: Verificar o painel de verdade**

Run (de dentro de `painel_agentes/`): `py -3.11 -m py_compile painel_agentes.py planejamento_rotas.py` e subir `py -3.11 -c "import painel_agentes; painel_agentes.app.run(host='127.0.0.1', port=8099)"`; abrir `/planejamento` de uma data com rota enviada (Playwright local, ver memoria "Testar painel com Playwright local"): o botao aparece so em rota enviada; POST com a chave desligada no config local devolve o 400.
Expected: tela carrega sem erro no console; botao visivel; 400 com a mensagem.

- [ ] **Step 7: Commit**

```bash
git add nucleo/ofertas_rota_enviada.py nucleo/banco.py nucleo/test_ofertas_rota_enviada.py painel_agentes/painel_agentes.py painel_agentes/planejamento_rotas.py painel_agentes/templates/planejamento_rotas.html MAPA_DO_SISTEMA.txt
git commit -m "Planejamento: equipe oferta rota ja enviada pros motoristas (botao no card)"
```

### Task 3: Etapas por mensagem particular

**Files:**
- Modify: `notificar_whatsapp.py` (origem nova, `_enviados_hoje`, `_teto`, `avisar_motorista_oferta`)
- Modify: `nucleo/banco.py` (tabela `nucleo_ofertas_rota_mensagens`)
- Modify: `nucleo/ofertas_rota_enviada.py` (`ETAPAS`, `etapa_do_motorista`, `texto_particular`, `enviar_etapas`, `rodar`, `--situacao`)
- Modify: `MAPA_DO_SISTEMA.txt` (tabela nova, origem e chaves `teto_mensagens`/`forcar_destino_whatsapp`)
- Test: `test_notificar_whatsapp_cadastro_link.py` (mesmo padrao do boas-vindas) e `nucleo/test_ofertas_rota_enviada.py`

**Interfaces:**
- Produces (notificar_whatsapp): `ORIGEM_OFERTA_MOTORISTA = "oferta_rota_motorista"`, `TETO_OFERTA_MOTORISTA_PADRAO = 60`; `avisar_motorista_oferta(oferta_id: int, agent_id: int, telefone: str, texto: str, config: dict, modo_teste: bool = False, **kw) -> str` com as mesmas situacoes do `avisar_motorista_boas_vindas` (`desligado`, `numero_sem_whatsapp`, `indeterminado`, `modo_teste`, `nao_enviado`, `enviado`, `falhou`); assinatura `oferta_rota:<oferta_id>:<agent_id>`.
- Produces (banco):

```sql
CREATE TABLE IF NOT EXISTS nucleo_ofertas_rota_mensagens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    oferta_id INTEGER NOT NULL,
    agent_id INTEGER NOT NULL,
    etapa TEXT NOT NULL,          -- PERTO_DO_FIM | FIXO | ESPORADICO
    situacao TEXT NOT NULL,       -- situacao do avisar_motorista_oferta, ou sem_telefone
    criado_em TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE (oferta_id, agent_id)
);
```

- Produces (ofertas): `ETAPAS = ("PERTO_DO_FIM", "FIXO", "ESPORADICO")`; `PRIMEIRA_ETAPA_MIN = 15`; `ENTRE_ETAPAS_MIN = 10`; `MAX_MENSAGENS_RODADA = 5`; `etapa_do_motorista(conn, agent_id: int, hoje: date) -> str`; `texto_particular(oferta, nome: str, valor: float | None, link: str) -> str`; `enviar_etapas(conn, config, cfg, agora: datetime, modo_teste=False, enviar=None, atualizar_andamento=None) -> dict` (`{"mensagens": n}`). `rodar(..., enviar=None, atualizar_andamento=None)` repassa e soma `mensagens` nos stats.

Regra da rodada (por oferta ABERTA com `aviso_grupo_em`), o que o construtor nao deduz sozinho:

```
abertura = aviso_grupo_em; se agora < abertura + 15 min: nada
pra cada etapa k em ETAPAS, em ordem:
    aberta_em = min(criado_em) das mensagens da oferta com etapa k
    se aberta_em existe e agora < aberta_em + 10 min: para (espera)
    se aberta_em existe: candidatos = quem ainda nao tem linha e rank(etapa_do_motorista) <= k
        se ha candidatos: manda (ate o limite) com etapa k e para; senao segue pra k+1
    se nao existe: candidatos como acima; vazio -> segue pra k+1 (etapa pulada); senao manda e para
```

`falhou` nao grava linha (tenta de novo); `sem_telefone` (sem `motoristas.telefone` nem `bd_motoristas.TELEFONE_MOTORISTA`) grava linha sem chamar o envio. `atualizar_andamento(conn)` roda uma vez por rodada, so quando alguma oferta chegou numa etapa e fora do modo teste; o padrao chama `sincronizar_vuupt._listar_rotas_do_dia(token, hoje)` + `sincronizar_vuupt.sincronizar_rotas(rotas, conn)` com `token = config["vuupt_api"]["token"]`; excecao = `logger.warning` e segue com o nucleo.

Texto (com acento, ate 200 caracteres, link inteiro no fim):
`Olá, <primeiro nome>! Rota disponível <dd/mm>: <n> entrega(s), saída <hh:mm>, <regiao>. Valor aprox. R$ x. Veja e pegue: <link>` (regiao e valor somem se faltar espaco ou dado, como em `texto_disponivel`).

- [ ] **Step 1: Testes do envio (notificar_whatsapp)**

Em `test_notificar_whatsapp_cadastro_link.py`, espelhando os testes do boas-vindas: piloto com `api_motorista.rota_sem_aceite.forcar_destino_whatsapp = "5511999990000"` manda pra la com `[teste → +55...]` na 1a linha; `""` manda pro telefone do motorista; chave ausente e sem `clientes.forcar_destino` devolve `desligado`; o 61o envio do dia devolve `nao_enviado` com `teto diario atingido` e nao gasta o teto de `motorista_boas_vindas`.

- [ ] **Step 2: Testes das etapas (ofertas)**

No harness existente (oferta aberta com `aviso_grupo_em` = `_as("10:00")`; motoristas OUTRO fixo com telefone, TERCEIRO esporadico com telefone; `enviar` falso que registra `(agent_id, texto)` e devolve `"enviado"`; `atualizar_andamento` falso):

```python
def test_nada_antes_de_15_min_do_grupo(self): ...            # 10:14 -> 0 mensagens
def test_fixo_antes_do_esporadico_com_10_min(self): ...       # 10:15 -> OUTRO; 10:24 -> nada; 10:25 -> TERCEIRO
def test_perto_do_fim_vem_primeiro(self): ...                 # TERCEIRO com rota de hoje e 2 pendentes -> 10:15 so TERCEIRO; 10:25 OUTRO
def test_etapa_vazia_e_pulada_na_hora(self): ...              # ninguem perto do fim -> 10:15 ja manda pro fixo
def test_cada_um_recebe_uma_vez_e_para_quando_alguem_pega(self): ...  # OUTRO escolhe 10:20 -> 10:25 nada pro TERCEIRO
def test_sem_telefone_registra_e_nao_trava(self): ...         # OUTRO sem telefone -> linha sem_telefone; etapa abre e segue
def test_falha_tenta_de_novo_so_quem_falhou(self): ...
def test_vuupt_fora_segue_com_o_nucleo(self): ...             # atualizar_andamento levanta -> mensagens saem igual
def test_veiculo_barrado_e_original_nao_recebem(self): ...
def test_texto_particular_cabe_em_200_e_leva_o_link(self): ...
def test_modo_teste_nao_grava_nem_manda(self): ...
```

- [ ] **Step 3: Rodar e ver falhar**

Run: `py -3.11 -m unittest nucleo.test_ofertas_rota_enviada test_notificar_whatsapp_cadastro_link -v`
Expected: FAIL (funcoes novas nao existem).

- [ ] **Step 4: Implementar** conforme Interfaces e a regra da rodada.

- [ ] **Step 5: Rodar e ver passar, mais os vizinhos**

Run: `py -3.11 -m unittest nucleo.test_ofertas_rota_enviada test_notificar_whatsapp test_notificar_whatsapp_cadastro_link test_notificar_whatsapp_atualizacao nucleo.test_api_motorista -v`
Expected: OK.

- [ ] **Step 6: Commit**

```bash
git add notificar_whatsapp.py test_notificar_whatsapp_cadastro_link.py nucleo/banco.py nucleo/ofertas_rota_enviada.py nucleo/test_ofertas_rota_enviada.py MAPA_DO_SISTEMA.txt
git commit -m "Motorista: rota ofertada chega por mensagem particular em etapas (perto do fim, fixos, esporadicos)"
```

### Task 4: Equipe confirma (ou recusa) quem pegou, sem abrir a Vuupt

Pedido do Hugo (21/11): a troca na Vuupt e consequencia do nosso fluxo. Quem pegou a rota espera o "Confirmar motorista" no card; o clique troca na Vuupt (funcao que ja existe), passa a rota pro motorista novo no app na hora e fecha a oferta.

**Files:**
- Modify: `nucleo/ofertas_rota_enviada.py` (`aplicar_escolha`, `recusar_escolha`, `texto_escolhida`)
- Modify: `painel_agentes/painel_agentes.py` (2 endpoints, junto de `api_trocar_motorista`)
- Modify: `painel_agentes/planejamento_rotas.py` (`oferta_enviada` ganha `escolhido_por` e `escolhido_nome`)
- Modify: `painel_agentes/templates/planejamento_rotas.html` (card: "<nome> quer esta rota" + `botao-confirmar-escolha` "Confirmar motorista" + `botao-recusar-escolha` "Recusar")
- Modify: `MAPA_DO_SISTEMA.txt`
- Test: `nucleo/test_ofertas_rota_enviada.py`

**Interfaces:**
- Consumes: `rascunhos_rota.trocar_motorista(rascunho_id, agent_id, vehicle_id, motorista_nome, token=, placas=, trava_sem_carro=)` (levanta `ValueError` com o motivo da Vuupt; devolve `{"aviso": ...}` quando passa sem carro).
- Produces: `aplicar_escolha(conn, rota_id: int) -> None`: so pra oferta `ESCOLHIDA`; grava `nucleo_rotas.agent_id`, `vehicle_id`, `motorista_nome` de quem escolheu (`motoristas.agent_id/vehicle_id/nome`), volta a rota pra `PLANEJADA` (o novo motorista ainda da o aceite no app), registra evento `ROTA_TROCADA_OFERTA` (`rotas.registrar_evento`, origem do painel) e marca a oferta `APLICADA`. Commita.
- Produces: `recusar_escolha(conn, rota_id: int, usuario: str) -> dict`: oferta `ESCOLHIDA` volta pra `ABERTA` (`escolhido_por`/`escolhido_em` NULL); quem foi recusado nao ve mais essa oferta (coluna nova `recusados_json`, lista de agent_id, respeitada em `listar_para_motorista` e `escolher`). Commita.
- Endpoint `POST /api/planejamento/confirmar-escolha` `{"rascunho_id"}` (mesmos decoradores do `trocar-motorista`): 1) acha a oferta `ESCOLHIDA` da rota; 2) chama `rascunhos_rota.trocar_motorista` com o agente/carro/nome de quem escolheu, `token`, `placas_veiculos_vuupt(config)` e `trava_sem_carro` como em `api_trocar_motorista`; `ValueError` = 400 com o motivo e NADA muda; 3) so depois do ok chama `aplicar_escolha`. Resposta `{"ok": True, "aviso": ...}`.
- Endpoint `POST /api/planejamento/recusar-escolha` `{"rascunho_id"}` → `recusar_escolha`.
- `texto_escolhida` passa a terminar em "Confirme no card da rota, no planejamento." (sai "Troque o motorista na Vuupt").
- `manter` continua marcando `APLICADA` quando o sync ve a rota com quem escolheu (troca feita por fora).

- [ ] **Step 1: Testes**

```python
def test_confirmar_passa_a_rota_pro_novo_motorista_no_app_na_hora(self):
    # OUTRO escolhe; aplicar_escolha -> oferta APLICADA; nucleo_rotas.agent_id == OUTRO, status PLANEJADA;
    # GET /api/rotas do OUTRO traz a rota; do ORIGINAL nao traz mais

def test_recusar_reabre_e_o_recusado_nao_ve_mais(self):
    # OUTRO escolhe; recusar_escolha -> ABERTA; OUTRO nao lista nem consegue escolher de novo; TERCEIRO escolhe

def test_aplicar_sem_escolha_nao_faz_nada(self):
    # oferta ABERTA -> ValueError "Ninguem escolheu essa rota."
```

- [ ] **Step 2: Rodar e ver falhar** — `py -3.11 -m unittest nucleo.test_ofertas_rota_enviada -v` → FAIL (funcoes novas).
- [ ] **Step 3: Implementar** nucleo, endpoints e card conforme Interfaces.
- [ ] **Step 4: Rodar e ver passar** — mesmo comando → OK; `py -3.11 -m py_compile` nos arquivos do painel; card conferido no painel local (porta 8099) com uma oferta ESCOLHIDA criada a mao no banco local.
- [ ] **Step 5: Commit**

```bash
git add nucleo/ofertas_rota_enviada.py nucleo/banco.py nucleo/test_ofertas_rota_enviada.py painel_agentes/painel_agentes.py painel_agentes/planejamento_rotas.py painel_agentes/templates/planejamento_rotas.html MAPA_DO_SISTEMA.txt
git commit -m "Planejamento: equipe confirma no card quem pegou a rota e a troca vai sozinha pra Vuupt"
```

## Depois do plano (nao e tarefa do construtor)

Deploy (`deploy-vps`): restart `painel-agentes` e `motorista-api` (importam o modulo); provar com `ofertas_rota_enviada.py --modo-teste --situacao` e `--ensaiar-rota`. As mensagens particulares saem em piloto (so pro Hugo) ate ele definir `forcar_destino_whatsapp: ""` no config da VPS. Avisar o Hugo: so 10 dos 17 motoristas com app tem telefone cadastrado.
