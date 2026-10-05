# Dias fixos v2: plano de implementação

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** As regiões de dia fixo passam a ter frequência (ABCD 2x, Transfrios Ter/Qui, Sorocaba quinzenal) e prazo por nível; a data do embarcador fora do dia de visita vira pedido dedicado com valor da calculadora, o embarcador é avisado uma vez (e-mail, WhatsApp, portal) e recebe um informativo em PDF.

**Architecture:** A configuração e as regras puras de data ficam em `roteirizacao/regioes_dia_fixo.py` (`data_valida_na_regiao`, `proxima_data_valida`, nível/prazo, cidade truncada). Duas tabelas de controle moram num módulo pequeno (`roteirizacao/registro_dia_fixo.py`). A tabela `agendamentos_origem` separa a data gravada pelo dia fixo ou pela equipe (Planejamento) da data do cliente. A detecção e a marcação ficam em `roteirizacao/fora_dia_fixo.py`, o aviso em `roteirizacao/notificar_fora_dia_fixo.py`; o job das 18h e o incremento só ganham uma chamada (`tratar_fora_dia_fixo`) ao lado do bloco de dedicados. Portal, Planejamento, vigia, replay e PDF consomem as mesmas funções.

**Tech Stack:** Python 3.11, SQLite, unittest, Flask + Jinja (portal e painel), Pillow (PDF).

**Spec:** `docs/superpowers/specs/2026-10-03-dias-fixos-v2-design.md`

## Decisões do plano

Onde a spec deixou em aberto, o plano decidiu assim (revisar antes de executar):

1. **Datas gravadas antes do deploy.** O dia fixo antigo gravou `scheduled_start` (ABCD quarta/sexta, Transfrios segunda/quarta) que a tabela nova não conhece e que pareceriam "do embarcador". A detecção só olha pedido criado (`created_at`, Brasília) a partir de `fora_dia_fixo.DETECCAO_A_PARTIR_DE` (código: `date(2026, 10, 6)`; ajustar para o dia do deploy no commit do deploy).
2. **Âncora de Sorocaba** = `"2026-10-13"` (terça). Se o deploy cair em 13/10 ou depois, trocar no commit do deploy pela terça seguinte ao deploy.
3. **Km do valor:** linha reta (haversine) base → destino → base, por `km_rodoviario.calcular_trajeto(..., api_key=None, voltar=True)`. A Google Routes não é chamada (403 desde 30/09, faturamento fechado). O valor sai ~25-35% abaixo do rodoviário; o financeiro corrige.
4. **Coordenada:** só a embutida no serviço (`roteirizacao_dados.coordenada_embutida`), sem geocodificar. Sem coordenada, carga acima da tabela ou erro da calculadora → valor 0 e "valor pendente".
5. **Motivo sem coluna nova:** vai em `pedidos_dedicados.marcado_por` = `"automatico: fora do dia fixo"` ou `"automatico: fora do dia fixo (valor pendente)"` (constante `pedidos_dedicados.POR_FORA_DIA_FIXO`).
6. **Pedido combinado** (`#PS-1, PS-2`): marca só o primeiro código, igual ao `marcar_dedicados` do painel. `separar_dedicados` já tira o serviço inteiro.
7. **Só data de hoje em diante.** Data vencida ou malformada não marca. `--modo-teste` só loga os candidatos: não marca, não avisa.
8. **E-mail:** destinatários das preferências do portal tipo `"agendamento"` (os mesmos dos avisos de dia fixo, respeita quem desligou); só sai com `notificacoes_automaticas.ativo`; chave nova `fora_dia_fixo.forcar_destino` (ausente = hugo@, piloto; `""` = envio real). Um e-mail por embarcador por rodada.
9. **WhatsApp:** número de `preferencias_notificacao.whatsapp_do_embarcador` (o mesmo do aviso de chamado, exige a chave `chamado_sem_resposta` ligada), pelo canal `whatsapp_notificacoes.clientes` (ativo, `forcar_destino`, teto diário).
10. **Um aviso por pedido:** grava em `avisos_fora_dia_fixo` quando pelo menos um canal saiu, ou quando o embarcador não tem canal nenhum (`canais = "nenhum"`). E-mail que falhou sem WhatsApp enviado não grava: tenta de novo na próxima rodada.
11. **Portal:** a data checada no envio é `agendamento_data` (a data de entrega escolhida, na máscara XML e na planilha). `data_expedicao` da planilha é a saída na Stokki e não entra. A checagem é no servidor (`/api/envios/confirmar` devolve 409 com a lista; o embarcador troca a data ou confirma com `aceita_fora_dia_fixo`). O "Reagendar" de um envio já existente fica fora desta entrega.
12. **Chip do portal:** passa a casar também por `codigo_pedido` (a marcação automática não tem `envio_id`).
13. **Nível:** interna = regiões não externas e todas as regras por endereço (Barueri, ABCD, Centrosul, Transfrios, Superfrio/TAC, TAFF) → 3 dias úteis; semanal = externas → 7 corridos; quinzenal → 15 corridos. O vigia só muda semanal e quinzenal sem agendamento.
14. `proxima_data_dias_semana` fica como está (só dia da semana); a função nova `proxima_data_valida(regra, data)` respeita a frequência e passa a ser usada por `aplicar_regioes_dia_fixo` e `ajustar_data_por_dia_fixo`.
15. **PDF com Pillow:** `reportlab` não está no `requirements.txt` nem instalado. Mesmo padrão de `portal_cliente/cotacao_pdf.py` (reaproveita fontes, logo e cores de lá). Contato: `entregas@freshlogbr.com`.
16. **Replay `--dias-fixos-v2`:** move para a próxima data de visita só o pedido que caiu num dia válido na regra antiga e inválido na nova. Pedido com data do embarcador fora do dia continua no dia dele (o replay não simula a marcação como dedicado).
17. **Origem da data (decisão do Hugo, 03/10):** só a data do CLIENTE vira dedicado. A tabela `agendamentos_origem` (módulo `roteirizacao/registro_dia_fixo.py`) guarda a origem de cada data gravada pelo sistema: `DIA_FIXO` (`aplicar_regioes_dia_fixo`) e `EQUIPE` (Planejamento: `reagendar_pedido` do menu de contexto e `reagendar_pedidos` do lote, as duas únicas rotinas da equipe que gravam `scheduled_start`). Data sem linha `DIA_FIXO`/`EQUIPE` para o pedido (por código ou por `service_id`) conta como do cliente: data do envio no portal, confirmação do embarcador (`atualizar_agendamentos_confirmados`), resposta de insucesso. **Edição direta na tela da Vuupt não passa pelo sistema, não dá pra distinguir e conta como do cliente.**
18. A linha `EQUIPE` é gravada por `service_id` (a tela só manda o id); a busca casa por código OU por `service_id`. O registro nunca impede o reagendamento: falha só vira WARNING.
19. **Reagendamento pela equipe fora do dia (spec 5.4, Task 4A):** fica depois da Task 4 (usa `calcular_valor`). Checagem por `POST /api/planejamento/checar-dia-fixo` (nível total/operador, igual ao reagendar), que busca cada serviço na Vuupt (`buscar_servico_por_id`). "Sim" salva a data primeiro e depois abre o modal de dedicado já existente, com a SOMA dos valores da calculadora (campo vazio se algum valor faltar); a marca sai com o usuário em `marcado_por` (não é a regra automática, então o chip não mostra "fora do dia fixo"). Fechar o modal sem marcar mantém a data (como `EQUIPE`) e recarrega. No lote, só os pedidos que a Vuupt aceitou entram no modal. Falha na checagem não trava o reagendamento.

## Global Constraints

- **Sequência:** executar só DEPOIS que o ramo `rotas-fracas-v2` estiver mergeado no master. Criar o worktree `.claude/worktrees/dias-fixos-v2`, ramo `dias-fixos-v2`, a partir de `origin/master` no momento da execução.
- **`criar_rotas_diarias.main` com edição cirúrgica:** só um bloco `try` com a chamada `tratar_fora_dia_fixo(...)` logo antes do bloco de dedicados. Nada mais nesse arquivo (o `rotas-fracas-v2` mexe nele).
- Python sempre `py -3.11`. Testes com `py -3.11 -m unittest <modulo> -v`, a partir da raiz do worktree. Não usar pytest.
- Rodar os pacotes `roteirizacao` e `painel_agentes` em comandos separados (misturar dá AttributeError).
- Código, comentários e mensagens em português. Sem acento nos comentários dos arquivos que já seguem esse padrão (`pedidos_dedicados.py`, `notificar_whatsapp.py`, `registro_dia_fixo.py` novo, `dedicados.py`); com acento nos que já usam (`regioes_dia_fixo.py`, `criar_rotas_diarias.py`, `incrementar_rotas.py`, `planejamento_rotas.py`, `envio_pedidos.py`, `vigia/regras.py`).
- Configuração das regiões (spec 2 e 3.1): ABCD **segunda e quinta**; Transfrios **terça e quinta**; Barueri terça e quinta; Vale do Paraíba segunda; Campinas quarta; Baixada Santista terça; Piracicaba quarta; Sorocaba **terça, quinzenal**.
- Prazo por nível (spec 3.1): interna/diária = **3 dias úteis**; semanal = **7 dias corridos**; quinzenal = **15 dias corridos**.
- Cidade truncada (spec 3.3): nome lido com **pelo menos 10 letras** e prefixo de **exatamente uma** cidade cadastrada.
- Marcação: `por = "automatico: fora do dia fixo"`; falha da calculadora → valor **0** e "valor pendente"; dedicado ativo não é remarcado.
- WhatsApp ao embarcador: mensagem inteira em até `MAX_MENSAGEM` (**200**) caracteres.
- Avisos externos respeitam os desvios existentes (`forcar_destino`) e saem **uma vez por pedido**. Nada é enviado aos embarcadores sem o Hugo ligar.
- Nunca editar nem commitar `config.yaml`.
- Commits locais no ramo, um por tarefa, com `git add` só dos arquivos da tarefa. Sem push, sem merge e sem deploy até o Hugo pedir.
- Mensagem de commit no formato `Área: o que mudou e por quê`, terminando com `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. **Pedido agendado pelo dia fixo antigo, antes do deploy** (ABCD quarta, sem linha em `agendamentos_origem`). Não pode virar dedicado. Teste na Task 4 (`test_pedido_criado_antes_do_corte_nao_marca`).
2. **Job das 18h rodado de novo, e o incremento logo depois.** Uma marcação e um aviso por pedido, mesmo em três rodadas. Teste na Task 6 (`test_tratar_duas_vezes_marca_e_avisa_uma_vez`).
3. **E-mail que falhou (SMTP fora).** O pedido já foi marcado; o aviso não pode se perder e tem que sair na rodada seguinte, uma vez. Teste na Task 6 (`test_aviso_que_falhou_sai_na_rodada_seguinte`).
4. **Data reagendada pela equipe no Planejamento** (Hugo move um pedido de Campinas pra quinta). Não pode virar dedicado nem gerar aviso ao embarcador. Testes na Task 3A (`test_reagendar_um_registra_equipe`, `test_lote_registra_so_os_que_deram_certo`) e na Task 4 (`test_data_reagendada_pela_equipe_nao_marca`). (Data vencida/malformada continua coberta por `test_data_vencida_ou_malformada_nao_marca`.) Pela pergunta da spec 5.4 (Task 4A), a calculadora sem valor não pode travar o popup: o valor vem `null` e o modal de dedicado abre vazio (`test_falha_da_calculadora_devolve_valor_nulo`).
5. **Código combinado com um dos pedidos já dedicado à mão** (`#PS-1001, PS-2002`, PS-2002 marcado pelo Hugo). Não remarca nem troca o valor. Teste na Task 4 (`test_codigo_combinado_com_um_ja_dedicado`).

## Mapa de arquivos

| Arquivo | Ação | Responsabilidade |
|---|---|---|
| `roteirizacao/regioes_dia_fixo.py` | modificar | Configuração nova, frequência, nível/prazo, `data_valida_na_regiao`, `proxima_data_valida`, `descricao_dias`, cidade truncada, registro no `aplicar` |
| `roteirizacao/test_regioes_dia_fixo.py` | criar | Testes das regras de região |
| `roteirizacao/registro_dia_fixo.py` | criar | Tabelas `agendamentos_origem` (DIA_FIXO / EQUIPE) e `avisos_fora_dia_fixo` |
| `roteirizacao/test_registro_dia_fixo.py` | criar | Testes das tabelas e do registro no `aplicar` |
| `painel_agentes/planejamento_rotas.py` (`reagendar_pedido`, `reagendar_pedidos`) | modificar | Registra a data agendada pela equipe como `EQUIPE` (Task 3A) |
| `painel_agentes/test_agendamento_equipe.py` | criar | Testes do registro `EQUIPE` |
| `painel_agentes/test_reagendar_pedidos_lote.py` | modificar | Isola o registro do banco real |
| `painel_agentes/planejamento_rotas.py` (`checar_reagendamento_dia_fixo`), `painel_agentes/painel_agentes.py` (rota `/api/planejamento/checar-dia-fixo`), `painel_agentes/templates/planejamento_rotas.html` (modal `modal-fora-dia-fixo`, fluxo do reagendar) | modificar | Pergunta "Marcar como dedicado?" no reagendamento fora do dia (Task 4A) |
| `painel_agentes/test_reagendar_fora_dia_fixo.py` | criar | Testes da checagem e da rota |
| `pedidos_dedicados.py` | modificar | Constante `POR_FORA_DIA_FIXO` |
| `roteirizacao/fora_dia_fixo.py` | criar | Detecção, valor, marcação, pendentes de aviso, `tratar_fora_dia_fixo` |
| `roteirizacao/test_fora_dia_fixo.py` | criar | Testes da detecção e da marcação |
| `notificar_whatsapp.py` | modificar | `texto_fora_dia_fixo`, `avisar_cliente_fora_dia_fixo` |
| `test_whatsapp_fora_dia_fixo.py` | criar | Testes do WhatsApp |
| `roteirizacao/notificar_fora_dia_fixo.py` | criar | E-mail + WhatsApp + registro do aviso |
| `roteirizacao/test_notificar_fora_dia_fixo.py` | criar | Testes do aviso e do `tratar` |
| `roteirizacao/criar_rotas_diarias.py`, `roteirizacao/incrementar_rotas.py` | modificar | Uma chamada antes do bloco de dedicados |
| `roteirizacao/test_fora_dia_fixo_integracao.py` | criar | Ordem das chamadas no `main` |
| `roteirizacao/dedicados.py`, `roteirizacao/test_dedicado_fora_rota.py` | modificar | Motivo no mapa do pool |
| `painel_agentes/planejamento_rotas.py`, `painel_agentes/templates/planejamento_rotas.html`, `painel_agentes/test_aviso_dia_fixo.py` | modificar | Aviso com frequência, chip com motivo |
| `portal_cliente/envio_pedidos.py`, `portal_cliente/app.py`, `portal_cliente/templates/_envios.html` | modificar | Chip por código + motivo, aviso no envio |
| `portal_cliente/test_fora_dia_fixo_envio.py` | criar | Testes do portal |
| `portal_cliente/test_bloqueio_area.py` | modificar | Esquema real de `pedidos_dedicados` no teste do chip |
| `vigia/regras.py`, `vigia/vigiar.py`, `vigia/test_regras.py`, `vigia/test_vigiar.py` | modificar | Prazo de 7/15 dias no pool |
| `gerar_informativo_regioes.py`, `test_gerar_informativo_regioes.py` | criar | PDF do informativo |
| `roteirizacao/replay_rotas.py`, `roteirizacao/test_replay_rotas.py` | modificar | `--dias-fixos-v2` |
| `MAPA_DO_SISTEMA.txt` | modificar | Módulos, tabelas, regra e armadilha |

---

### Task 0: Preparar o worktree

**Files:** nenhum arquivo de código.

- [ ] **Step 1: Confirmar que o `rotas-fracas-v2` já entrou no master**

```bash
cd /c/agente_stokki_eventos
git fetch origin
git log --oneline origin/master -15 | grep -i "rotas fracas\|rotas-fracas"
git branch -r --merged origin/master | grep rotas-fracas-v2
```

Esperado: o ramo `origin/rotas-fracas-v2` aparece como mergeado. Se não aparecer, PARAR e avisar o controlador: este plano só roda depois desse merge.

- [ ] **Step 2: Criar o worktree a partir do origin/master**

```bash
cd /c/agente_stokki_eventos
git worktree add .claude/worktrees/dias-fixos-v2 -b dias-fixos-v2 origin/master
cp docs/superpowers/specs/2026-10-03-dias-fixos-v2-design.md .claude/worktrees/dias-fixos-v2/docs/superpowers/specs/
cp docs/superpowers/plans/2026-10-03-dias-fixos-v2.md .claude/worktrees/dias-fixos-v2/docs/superpowers/plans/
cd .claude/worktrees/dias-fixos-v2
git add docs/superpowers/specs/2026-10-03-dias-fixos-v2-design.md docs/superpowers/plans/2026-10-03-dias-fixos-v2.md
git commit -m "Docs: especificacao e plano dos dias fixos v2

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

Todos os passos seguintes rodam dentro de `/c/agente_stokki_eventos/.claude/worktrees/dias-fixos-v2`.

- [ ] **Step 3: Conferir as âncoras que o plano usa no código pós-merge**

```bash
grep -n "# Dedicado (Hugo, 23/09)" roteirizacao/criar_rotas_diarias.py roteirizacao/incrementar_rotas.py
grep -n "aplicar_regioes_dia_fixo(" roteirizacao/criar_rotas_diarias.py roteirizacao/incrementar_rotas.py
grep -n "^def data_entrada" roteirizacao/rotas_fracas.py
```

Esperado: um comentário `# Dedicado (Hugo, 23/09)` em cada arquivo, uma chamada de `aplicar_regioes_dia_fixo(` em cada um, e `def data_entrada(servico: dict) -> date | None` em `rotas_fracas.py`. Se `data_entrada` tiver mudado de nome ou de módulo no `rotas-fracas-v2`, usar o nome novo na Task 4 (o comportamento esperado é: dia de Brasília do `created_at`, que vem em UTC sem fuso).

- [ ] **Step 4: Rodar a base de testes antes de mexer**

```bash
py -3.11 -m unittest discover -s roteirizacao -p "test_*.py" 2>&1 | tail -3
py -3.11 -m unittest discover -s painel_agentes -p "test_*.py" 2>&1 | tail -3
py -3.11 -m unittest vigia.test_regras vigia.test_vigiar test_notificar_whatsapp portal_cliente.test_bloqueio_area 2>&1 | tail -3
```

Esperado: `OK` nos três. Anotar a contagem de cada um para comparar na Task 13. Se algo já falhar aqui, parar e avisar o Hugo antes de seguir.

---

### Task 1: Configuração nova, frequência, nível e data de visita

**Files:**
- Modify: `roteirizacao/regioes_dia_fixo.py`
- Test: `roteirizacao/test_regioes_dia_fixo.py`

**Interfaces:**
- Consumes: nada de tarefas anteriores.
- Produces:
  - constantes `FREQUENCIA_SEMANAL = "semanal"`, `FREQUENCIA_QUINZENAL = "quinzenal"`, `NIVEL_INTERNA = "interna"`, `NIVEL_SEMANAL = "semanal"`, `NIVEL_QUINZENAL = "quinzenal"`, `PRAZO_POR_NIVEL: dict[str, tuple[int, bool]]` (`(dias, só dia útil?)`)
  - `regra_dia_fixo_do_servico(servico) -> dict | None` agora com as chaves `nome, dias, origem, regiao, externa, frequencia, ancora, nivel, prazo_dias, prazo_dias_uteis`
  - `nivel_da_regra(regra: dict) -> str`
  - `data_valida_na_regiao(regra: dict, data: date) -> bool`
  - `proxima_data_valida(regra: dict, a_partir_de: date) -> date` (estritamente depois)
  - `descricao_dias(regra: dict) -> str` (ex.: `"Terças (quinzenal)"`, `"Segundas e Quintas"`)

- [ ] **Step 1: Escrever os testes**

Criar `roteirizacao/test_regioes_dia_fixo.py`:

```python
# -*- coding: utf-8 -*-
"""
Regras de região de dia fixo v2 (Hugo, 03/10/2026): configuração nova,
frequência quinzenal, nível/prazo e data de visita.
Spec: docs/superpowers/specs/2026-10-03-dias-fixos-v2-design.md
Rodar (da raiz): py -3.11 -m unittest roteirizacao.test_regioes_dia_fixo -v
"""
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import regioes_dia_fixo as rdf

ABCD = {"address": "Rua das Figueiras 100, Jardim, Santo André - SP, 09080-300, Brasil"}
TRANSFRIOS = {"address": "Estrada Francisco Hengles, 591, Potuvera, Itapecerica da Serra - SP, 06885-160, Brasil"}
SOROCABA = {"address": "Rua XV de Novembro 10, Centro, Sorocaba - SP, 18010-080, Brasil"}
CAMPINAS = {"address": "Rua Barão de Jaguara 900, Centro, Campinas - SP, 13015-001, Brasil"}
SAO_PAULO = {"address": "Rua Augusta 100, Consolação, São Paulo - SP, 01304-000, Brasil"}

QUINZENAL = {"nome": "Sorocaba", "dias": [rdf.TERCA], "frequencia": rdf.FREQUENCIA_QUINZENAL,
             "ancora": "2026-10-13"}


class TestConfiguracao(unittest.TestCase):
    def test_abcd_segunda_e_quinta_nivel_interno(self):
        regra = rdf.regra_dia_fixo_do_servico(ABCD)
        self.assertEqual(regra["dias"], [rdf.SEGUNDA, rdf.QUINTA])
        self.assertEqual(regra["regiao"], "ABCD")
        self.assertEqual(regra["nome"], "Santo André")
        self.assertEqual(regra["nivel"], rdf.NIVEL_INTERNA)
        self.assertEqual((regra["prazo_dias"], regra["prazo_dias_uteis"]), (3, True))

    def test_transfrios_terca_e_quinta(self):
        regra = rdf.regra_dia_fixo_do_servico(TRANSFRIOS)
        self.assertEqual(regra["dias"], [rdf.TERCA, rdf.QUINTA])
        self.assertEqual((regra["origem"], regra["regiao"], regra["nivel"]), ("endereco", "Transfrios", rdf.NIVEL_INTERNA))

    def test_sorocaba_quinzenal_com_ancora_numa_terca(self):
        regra = rdf.regra_dia_fixo_do_servico(SOROCABA)
        self.assertEqual(regra["frequencia"], rdf.FREQUENCIA_QUINZENAL)
        self.assertEqual(date.fromisoformat(regra["ancora"]).weekday(), rdf.TERCA)
        self.assertEqual(regra["nivel"], rdf.NIVEL_QUINZENAL)
        self.assertEqual((regra["prazo_dias"], regra["prazo_dias_uteis"]), (15, False))

    def test_campinas_semanal(self):
        regra = rdf.regra_dia_fixo_do_servico(CAMPINAS)
        self.assertEqual((regra["dias"], regra["frequencia"]), ([rdf.QUARTA], rdf.FREQUENCIA_SEMANAL))
        self.assertEqual((regra["nivel"], regra["prazo_dias"], regra["prazo_dias_uteis"]), (rdf.NIVEL_SEMANAL, 7, False))

    def test_grande_sp_sem_regra(self):
        self.assertIsNone(rdf.regra_dia_fixo_do_servico(SAO_PAULO))


class TestDataValida(unittest.TestCase):
    def test_semanal(self):
        regra = rdf.regra_dia_fixo_do_servico(CAMPINAS)
        self.assertTrue(rdf.data_valida_na_regiao(regra, date(2026, 10, 7)))    # quarta
        self.assertFalse(rdf.data_valida_na_regiao(regra, date(2026, 10, 8)))   # quinta

    def test_duas_vezes_por_semana(self):
        regra = rdf.regra_dia_fixo_do_servico(ABCD)
        self.assertTrue(rdf.data_valida_na_regiao(regra, date(2026, 10, 5)))    # segunda
        self.assertTrue(rdf.data_valida_na_regiao(regra, date(2026, 10, 8)))    # quinta
        self.assertFalse(rdf.data_valida_na_regiao(regra, date(2026, 10, 7)))   # quarta
        self.assertFalse(rdf.data_valida_na_regiao(regra, date(2026, 10, 9)))   # sexta

    def test_quinzenal_semana_par_desde_a_ancora(self):
        self.assertTrue(rdf.data_valida_na_regiao(QUINZENAL, date(2026, 10, 13)))   # a própria âncora
        self.assertFalse(rdf.data_valida_na_regiao(QUINZENAL, date(2026, 10, 20)))  # semana ímpar
        self.assertTrue(rdf.data_valida_na_regiao(QUINZENAL, date(2026, 10, 27)))
        self.assertFalse(rdf.data_valida_na_regiao(QUINZENAL, date(2026, 11, 3)))   # virada do mês
        self.assertTrue(rdf.data_valida_na_regiao(QUINZENAL, date(2026, 11, 10)))
        self.assertFalse(rdf.data_valida_na_regiao(QUINZENAL, date(2026, 10, 6)))   # antes da âncora, ímpar
        self.assertTrue(rdf.data_valida_na_regiao(QUINZENAL, date(2026, 9, 29)))    # antes da âncora, par

    def test_quinzenal_dia_da_semana_errado(self):
        self.assertFalse(rdf.data_valida_na_regiao(QUINZENAL, date(2026, 10, 14)))  # quarta da semana válida

    def test_regra_sem_frequencia_e_semanal(self):
        self.assertTrue(rdf.data_valida_na_regiao({"nome": "X", "dias": [rdf.TERCA]}, date(2026, 10, 20)))


class TestProximaData(unittest.TestCase):
    def test_quinzenal_pula_a_semana_sem_visita(self):
        self.assertEqual(rdf.proxima_data_valida(QUINZENAL, date(2026, 10, 13)), date(2026, 10, 27))
        self.assertEqual(rdf.proxima_data_valida(QUINZENAL, date(2026, 10, 14)), date(2026, 10, 27))
        self.assertEqual(rdf.proxima_data_valida(QUINZENAL, date(2026, 10, 12)), date(2026, 10, 13))

    def test_ajustar_data_usa_os_dias_novos(self):
        data, regra = rdf.ajustar_data_por_dia_fixo(ABCD, date(2026, 10, 7))   # quarta
        self.assertEqual(data, date(2026, 10, 8))                               # quinta
        self.assertEqual(regra["regiao"], "ABCD")
        self.assertEqual(rdf.ajustar_data_por_dia_fixo(ABCD, date(2026, 10, 8)), (date(2026, 10, 8), None))

    def test_descricao_dias(self):
        self.assertEqual(rdf.descricao_dias(QUINZENAL), "Terças (quinzenal)")
        self.assertEqual(rdf.descricao_dias(rdf.regra_dia_fixo_do_servico(ABCD)), "Segundas e Quintas")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `py -3.11 -m unittest roteirizacao.test_regioes_dia_fixo -v`
Expected: FAIL/ERROR (`AttributeError: module 'regioes_dia_fixo' has no attribute 'FREQUENCIA_QUINZENAL'` e dias do ABCD diferentes).

- [ ] **Step 3: Implementar em `roteirizacao/regioes_dia_fixo.py`**

3a. No docstring do topo, trocar as duas linhas de dias:

```
  Sorocaba (Sorocaba, Votorantim, São Roque, Itu, Salto) -- Terça
```
por
```
  Sorocaba (Sorocaba, Votorantim, São Roque, Itu, Salto) -- Terça,
  quinzenal (03/10/2026)
```
e
```
  Diadema, Ribeirão Pires, Mauá) -- Segunda, Quarta e Sexta
```
por
```
  Diadema, Ribeirão Pires, Mauá) -- Segunda e Quinta (03/10/2026)
```

3b. Logo depois do dicionário `DIAS_NOMES`, acrescentar:

```python
# Dias fixos v2 (Hugo, 03/10/2026 -- spec docs/superpowers/specs/
# 2026-10-03-dias-fixos-v2-design.md). Região sem o campo "frequencia" é
# semanal. Quinzenal visita só nas semanas PARES contadas a partir de
# "ancora" (primeira terça de visita, gravada no deploy).
FREQUENCIA_SEMANAL = "semanal"
FREQUENCIA_QUINZENAL = "quinzenal"

# Prazo de entrega por nível: (dias, conta só dia útil?). Interna = entrega
# diária da Grande SP e regiões internas de dia fixo (Barueri, ABCD,
# galpões por endereço); semanal = regiões externas; quinzenal = Sorocaba.
NIVEL_INTERNA = "interna"
NIVEL_SEMANAL = "semanal"
NIVEL_QUINZENAL = "quinzenal"
PRAZO_POR_NIVEL = {
    NIVEL_INTERNA: (3, True),
    NIVEL_SEMANAL: (7, False),
    NIVEL_QUINZENAL: (15, False),
}
```

3c. Em `REGIOES`, trocar a linha da Sorocaba e a do ABCD:

```python
    {"nome": "Sorocaba", "dias": [TERCA], "externa": True,
     "frequencia": FREQUENCIA_QUINZENAL, "ancora": "2026-10-13",  # 1ª terça de visita (ajustar no deploy)
     "cidades": ["SOROCABA", "VOTORANTIM", "SAO ROQUE", "ITU", "SALTO"]},
```

```python
    {"nome": "ABCD", "dias": [SEGUNDA, QUINTA], "externa": False,
     "cidades": ["SANTO ANDRE", "SAO BERNARDO DO CAMPO", "SAO CAETANO DO SUL",
                 "DIADEMA", "RIBEIRAO PIRES", "MAUA"]},
```

3d. Em `ENDERECOS_DIA_FIXO`, trocar a regra da Transfrios:

```python
    # Estrada Francisco Hengles, 591 - Potuvera, Itapecerica da Serra/SP
    # 03/10/2026: Terça e Quinta (a maioria das entregas já caía nesses dias).
    {"nome": "Transfrios", "dias": [TERCA, QUINTA],
     "padroes": ["FRANCISCO HENGLES", "06885-160", "06885160"]},
```

3e. Em `_montar_indice`, trocar o dict de cada cidade por:

```python
            indice[_normalizar_texto(cidade)] = {
                "dias": regiao["dias"], "regiao": regiao["nome"],
                "externa": regiao.get("externa", False),
                "frequencia": regiao.get("frequencia", FREQUENCIA_SEMANAL),
                "ancora": regiao.get("ancora"),
            }
```

3f. Substituir a função `regra_dia_fixo_do_servico` inteira por:

```python
def nivel_da_regra(regra: dict) -> str:
    """interna | semanal | quinzenal -- define o prazo (PRAZO_POR_NIVEL)."""
    if regra.get("frequencia") == FREQUENCIA_QUINZENAL:
        return NIVEL_QUINZENAL
    if regra.get("externa"):
        return NIVEL_SEMANAL
    return NIVEL_INTERNA


def _regra_completa(regra: dict) -> dict:
    nivel = nivel_da_regra(regra)
    regra["nivel"] = nivel
    regra["prazo_dias"], regra["prazo_dias_uteis"] = PRAZO_POR_NIVEL[nivel]
    return regra


def regra_dia_fixo_do_servico(servico: dict) -> dict | None:
    """
    Resolve a regra de dia fixo que vale pra ESTE serviço, olhando o
    campo 'address': primeiro as regiões por ENDEREÇO (mais
    específicas -- galpão/operador logístico), depois a cidade.

    Retorna {"nome", "dias", "origem": "endereco"|"cidade", "regiao",
    "externa", "frequencia", "ancora", "nivel", "prazo_dias",
    "prazo_dias_uteis"} ou None se nenhuma regra se aplica (entrega sem
    restrição de dia). "nome" é o galpão ou a cidade (mensagens curtas);
    "regiao" é o nome da região (Vale do Paraíba, ABCD...).
    """
    endereco_norm = _normalizar_texto(servico.get("address") or "")
    if endereco_norm:
        for regra in ENDERECOS_DIA_FIXO:
            if any(_normalizar_texto(p) in endereco_norm for p in regra["padroes"] if p):
                return _regra_completa({
                    "nome": regra["nome"], "dias": regra["dias"], "origem": "endereco",
                    "regiao": regra["nome"], "externa": False,
                    "frequencia": regra.get("frequencia", FREQUENCIA_SEMANAL), "ancora": regra.get("ancora"),
                })

    cidade = extrair_cidade(servico)
    info = _INDICE_CIDADES.get(_normalizar_texto(cidade)) if cidade else None
    if info:
        return _regra_completa({
            "nome": cidade.title(), "dias": info["dias"], "origem": "cidade",
            "regiao": info["regiao"], "externa": info["externa"],
            "frequencia": info["frequencia"], "ancora": info["ancora"],
        })
    return None
```

3g. Depois de `proxima_data_dias_semana`, acrescentar:

```python
def _inicio_da_semana(d: date) -> date:
    return d - timedelta(days=d.weekday())


def data_valida_na_regiao(regra: dict, data: date) -> bool:
    """`data` é dia de visita da região? Dia da semana certo e, se a região
    é quinzenal, semana PAR contada a partir da âncora (semanas de segunda
    a domingo -- vale antes da âncora também)."""
    if data.weekday() not in regra["dias"]:
        return False
    if regra.get("frequencia") != FREQUENCIA_QUINZENAL or not regra.get("ancora"):
        return True
    ancora = date.fromisoformat(regra["ancora"])
    semanas = (_inicio_da_semana(data) - _inicio_da_semana(ancora)).days // 7
    return semanas % 2 == 0


def proxima_data_valida(regra: dict, a_partir_de: date) -> date:
    """Primeiro dia de visita da região estritamente APÓS `a_partir_de`
    (respeita a frequência; proxima_data_dias_semana só olha o dia da
    semana)."""
    d = a_partir_de
    for _ in range(28):
        d += timedelta(days=1)
        if data_valida_na_regiao(regra, d):
            return d
    raise ValueError(f"região sem dia de visita em 4 semanas: {regra.get('nome')}")


def descricao_dias(regra: dict) -> str:
    """nomes_dias + "(quinzenal)" quando for o caso -- pra mensagens."""
    texto = nomes_dias(regra["dias"])
    return f"{texto} (quinzenal)" if regra.get("frequencia") == FREQUENCIA_QUINZENAL else texto
```

3h. Em `ajustar_data_por_dia_fixo`, trocar as três últimas linhas por:

```python
    regra = regra_dia_fixo_do_servico(servico)
    if not regra or data_valida_na_regiao(regra, data):
        return data, None
    return proxima_data_valida(regra, data), regra
```

3i. Em `aplicar_regioes_dia_fixo`, trocar `data_alvo = proxima_data_dias_semana(regra["dias"], hoje)` por:

```python
        data_alvo = proxima_data_valida(regra, hoje)
```

e, no `logger.info` do mesmo laço, trocar `f"(entrega às {nomes_dias(regra['dias'])}) -- "` por `f"(entrega às {descricao_dias(regra)}) -- "`.

- [ ] **Step 4: Rodar e ver passar**

```bash
py -3.11 -m unittest roteirizacao.test_regioes_dia_fixo -v 2>&1 | tail -3
py -3.11 -m unittest painel_agentes.test_aviso_dia_fixo -v 2>&1 | tail -3
```

Expected: `OK` nos dois (o segundo continua igual: Americana e TAFF não mudaram).

- [ ] **Step 5: Commit**

```bash
git add roteirizacao/regioes_dia_fixo.py roteirizacao/test_regioes_dia_fixo.py
git commit -m "Dia fixo: ABCD seg/qui, Transfrios ter/qui, Sorocaba quinzenal e prazo por nivel

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Cidade truncada no endereço

**Files:**
- Modify: `roteirizacao/regioes_dia_fixo.py`
- Test: `roteirizacao/test_regioes_dia_fixo.py`

**Interfaces:**
- Consumes: `_INDICE_CIDADES`, `regra_dia_fixo_do_servico` (Task 1).
- Produces: `MIN_LETRAS_CIDADE_TRUNCADA = 10`; `_info_cidade(cidade) -> dict | None` usada por `dias_fixos_da_cidade`, `regiao_da_cidade`, `regiao_externa_da_cidade` e `regra_dia_fixo_do_servico`. A entrada do índice ganha `"cidade"` (nome normalizado completo).

- [ ] **Step 1: Acrescentar os testes**

No fim de `roteirizacao/test_regioes_dia_fixo.py`, antes do `if __name__`, acrescentar (e `from unittest import mock` nos imports do topo):

```python
SJC_TRUNCADO = {"address": "Av. Cassiano Ricardo 601, Jardim Aquarius, SAO JOSE DOS CA - SP, 12246-870, Brasil"}


class TestCidadeTruncada(unittest.TestCase):
    def test_nome_truncado_reconhece_a_cidade(self):
        regra = rdf.regra_dia_fixo_do_servico(SJC_TRUNCADO)
        self.assertEqual(regra["regiao"], "Vale do Paraíba")
        self.assertEqual(regra["nome"], "Sao Jose Dos Campos")
        self.assertEqual(regra["dias"], [rdf.SEGUNDA])

    def test_truncado_vale_pra_viagem_e_mensagens(self):
        self.assertEqual(rdf.regiao_externa_da_cidade("SAO JOSE DOS CA"), "Vale do Paraíba")
        self.assertEqual(rdf.regiao_da_cidade("Sao Jose dos Ca"), "Vale do Paraíba")
        self.assertEqual(rdf.dias_fixos_da_cidade("SAO JOSE DOS CA"), [rdf.SEGUNDA])

    def test_prefixo_curto_nao_reconhece(self):
        self.assertIsNone(rdf.regiao_da_cidade("SAO JOSE"))      # 7 letras
        self.assertIsNone(rdf.regiao_da_cidade("SANTO"))

    def test_prefixo_ambiguo_nao_reconhece(self):
        extra = {"SAO JOSE DOS CAMPINHOS": {"dias": [rdf.SEXTA], "regiao": "Teste", "externa": True,
                                            "frequencia": rdf.FREQUENCIA_SEMANAL, "ancora": None,
                                            "cidade": "SAO JOSE DOS CAMPINHOS"}}
        with mock.patch.dict(rdf._INDICE_CIDADES, extra):
            self.assertIsNone(rdf.regiao_da_cidade("SAO JOSE DOS CA"))

    def test_nome_completo_continua_igual(self):
        self.assertEqual(rdf.regiao_da_cidade("São José dos Campos"), "Vale do Paraíba")
        self.assertEqual(rdf.regra_dia_fixo_do_servico(CAMPINAS)["nome"], "Campinas")
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `py -3.11 -m unittest roteirizacao.test_regioes_dia_fixo -v`
Expected: FAIL em `test_nome_truncado_reconhece_a_cidade` (`None` não é subscriptable) e nos dois seguintes.

- [ ] **Step 3: Implementar em `roteirizacao/regioes_dia_fixo.py`**

3a. Em `_montar_indice`, acrescentar a chave `"cidade"` no dict de cada cidade (fica assim):

```python
            indice[_normalizar_texto(cidade)] = {
                "dias": regiao["dias"], "regiao": regiao["nome"],
                "externa": regiao.get("externa", False),
                "frequencia": regiao.get("frequencia", FREQUENCIA_SEMANAL),
                "ancora": regiao.get("ancora"),
                "cidade": _normalizar_texto(cidade),
            }
```

3b. Logo depois de `_INDICE_CIDADES = _montar_indice()`, acrescentar:

```python
# Cidade truncada no endereço (Hugo, 03/10: "SAO JOSE DOS CA - SP" escapava
# do dia fixo): nome com pelo menos MIN_LETRAS_CIDADE_TRUNCADA letras que é
# prefixo de UMA só cidade cadastrada vale essa cidade.
MIN_LETRAS_CIDADE_TRUNCADA = 10


def _info_cidade(cidade) -> dict | None:
    nome = _normalizar_texto(cidade)
    if not nome:
        return None
    info = _INDICE_CIDADES.get(nome)
    if info or sum(c.isalpha() for c in nome) < MIN_LETRAS_CIDADE_TRUNCADA:
        return info
    candidatas = [k for k in _INDICE_CIDADES if k.startswith(nome)]
    return _INDICE_CIDADES[candidatas[0]] if len(candidatas) == 1 else None
```

3c. Nas três funções `dias_fixos_da_cidade`, `regiao_da_cidade` e `regiao_externa_da_cidade`, trocar `info = _INDICE_CIDADES.get(_normalizar_texto(cidade))` por:

```python
    info = _info_cidade(cidade)
```

3d. Em `regra_dia_fixo_do_servico`, trocar o bloco da cidade por:

```python
    cidade = extrair_cidade(servico)
    info = _info_cidade(cidade) if cidade else None
    if info:
        exata = _normalizar_texto(cidade) in _INDICE_CIDADES
        return _regra_completa({
            "nome": cidade.title() if exata else info["cidade"].title(), "dias": info["dias"],
            "origem": "cidade", "regiao": info["regiao"], "externa": info["externa"],
            "frequencia": info["frequencia"], "ancora": info["ancora"],
        })
    return None
```

- [ ] **Step 4: Rodar e ver passar**

```bash
py -3.11 -m unittest roteirizacao.test_regioes_dia_fixo -v 2>&1 | tail -3
py -3.11 -m unittest discover -s roteirizacao -p "test_*.py" 2>&1 | tail -3
```

Expected: `OK` nos dois.

- [ ] **Step 5: Commit**

```bash
git add roteirizacao/regioes_dia_fixo.py roteirizacao/test_regioes_dia_fixo.py
git commit -m "Dia fixo: cidade truncada no endereco (SAO JOSE DOS CA) cai na regiao certa

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Origem da data (`agendamentos_origem`) e tabela de avisos

**Files:**
- Create: `roteirizacao/registro_dia_fixo.py`
- Modify: `roteirizacao/regioes_dia_fixo.py` (`aplicar_regioes_dia_fixo`)
- Test: `roteirizacao/test_registro_dia_fixo.py`

**Interfaces:**
- Consumes: `pedidos_dedicados.codigos_do_servico(servico) -> list[str]`; `regioes_dia_fixo.proxima_data_valida`, `data_valida_na_regiao`, `regra_dia_fixo_do_servico` (Task 1).
- Produces:
  - `registro_dia_fixo.DB_PATH`, `ORIGEM_DIA_FIXO = "DIA_FIXO"`, `ORIGEM_EQUIPE = "EQUIPE"`, `ORIGENS_NAO_CLIENTE = (ORIGEM_DIA_FIXO, ORIGEM_EQUIPE)`
  - `conectar(db_path=DB_PATH) -> sqlite3.Connection` (cria `agendamentos_origem` e `avisos_fora_dia_fixo`)
  - `registrar_origem(conn, servico: dict, data: date, origem: str, agora: datetime | None = None) -> int` (uma linha por código; serviço sem código grava uma linha só com `service_id`)
  - `data_nao_e_do_cliente(conn, servico: dict, data: date) -> bool` (há linha `DIA_FIXO`/`EQUIPE` com essa data, casando por código OU por `service_id`)
  - `registrar_origens(itens: list[dict], origem: str, db_path=DB_PATH) -> int` (tolerante a falha; cada item tem `"servico"` e `"data"`)
  - `registrar_aviso(conn, servico: dict, data: date | None, canais: list[str], agora: datetime | None = None) -> None`
  - `ja_avisado(conn, servico: dict) -> bool`
  - `aplicar_regioes_dia_fixo(servicos, vuupt, hoje=None, db_path=None)` registra o que gravou como `DIA_FIXO`.

- [ ] **Step 1: Escrever os testes**

Criar `roteirizacao/test_registro_dia_fixo.py`:

```python
# -*- coding: utf-8 -*-
"""
Origem da data agendada e controle de avisos (dias fixos v2, Hugo 03/10).
Rodar (da raiz): py -3.11 -m unittest roteirizacao.test_registro_dia_fixo -v
"""
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import registro_dia_fixo as reg
import regioes_dia_fixo as rdf

SOROCABA = "Rua XV de Novembro 10, Centro, Sorocaba - SP, 18010-080, Brasil"
QUINTA = date(2026, 10, 8)


class FakeVuupt:
    def __init__(self):
        self.chamadas = []

    def atualizar_servico(self, service_id, payload):
        self.chamadas.append((service_id, payload))


class TestRegistro(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "t.db"
        self.conn = reg.conectar(self.db)
        self.addCleanup(self.conn.close)

    def test_origem_registrada_so_vale_pra_mesma_data(self):
        s = {"id": 1, "code": "#PS-1001"}
        self.assertEqual(reg.registrar_origem(self.conn, s, QUINTA, reg.ORIGEM_DIA_FIXO), 1)
        self.assertTrue(reg.data_nao_e_do_cliente(self.conn, s, QUINTA))
        self.assertFalse(reg.data_nao_e_do_cliente(self.conn, s, QUINTA + timedelta(days=1)))

    def test_equipe_por_service_id_sem_codigo(self):
        # a tela do Planejamento só manda o service_id
        reg.registrar_origem(self.conn, {"id": 55}, QUINTA, reg.ORIGEM_EQUIPE)
        self.assertTrue(reg.data_nao_e_do_cliente(self.conn, {"id": 55, "code": "#PS-1001"}, QUINTA))
        self.assertFalse(reg.data_nao_e_do_cliente(self.conn, {"id": 56, "code": "#PS-1002"}, QUINTA))

    def test_codigo_combinado_e_reentrega(self):
        reg.registrar_origem(self.conn, {"id": 1, "code": "#PS-1001, PS-2002"}, QUINTA, reg.ORIGEM_DIA_FIXO)
        self.assertTrue(reg.data_nao_e_do_cliente(self.conn, {"id": 9, "code": "PS-2002"}, QUINTA))
        self.assertTrue(reg.data_nao_e_do_cliente(self.conn, {"id": 8, "code": "#PS-1001-R1"}, QUINTA))

    def test_origem_desconhecida_conta_como_cliente(self):
        reg.registrar_origem(self.conn, {"id": 1, "code": "PS-1001"}, QUINTA, "CLIENTE")
        self.assertFalse(reg.data_nao_e_do_cliente(self.conn, {"id": 1, "code": "PS-1001"}, QUINTA))

    def test_servico_sem_codigo_e_sem_id(self):
        self.assertFalse(reg.data_nao_e_do_cliente(self.conn, {}, QUINTA))
        self.assertEqual(reg.registrar_origem(self.conn, {}, QUINTA, reg.ORIGEM_EQUIPE), 0)

    def test_registrar_duas_vezes_nao_duplica(self):
        s = {"id": 1, "code": "PS-1001"}
        reg.registrar_origem(self.conn, s, QUINTA, reg.ORIGEM_EQUIPE)
        self.assertEqual(reg.registrar_origem(self.conn, s, QUINTA, reg.ORIGEM_EQUIPE), 0)

    def test_aviso_uma_vez_por_pedido(self):
        s = {"id": 1, "code": "#PS-1001"}
        self.assertFalse(reg.ja_avisado(self.conn, s))
        reg.registrar_aviso(self.conn, s, QUINTA, ["email"], agora=datetime(2026, 10, 6, 18, 5))
        reg.registrar_aviso(self.conn, s, QUINTA, ["email", "whatsapp"])
        self.assertTrue(reg.ja_avisado(self.conn, s))
        linha = self.conn.execute("SELECT * FROM avisos_fora_dia_fixo").fetchall()
        self.assertEqual(len(linha), 1)
        self.assertEqual((linha[0]["codigo"], linha[0]["data"], linha[0]["canais"], linha[0]["enviado_em"]),
                         ("PS-1001", "2026-10-08", "email", "2026-10-06 18:05:00"))

    def test_aviso_sem_canal_grava_nenhum(self):
        reg.registrar_aviso(self.conn, {"id": 1, "code": "PS-7"}, QUINTA, [])
        self.assertEqual(self.conn.execute("SELECT canais FROM avisos_fora_dia_fixo").fetchone()[0], "nenhum")

    def test_registrar_origens_tolerante(self):
        ruim = Path(self.tmp.name) / "nao_existe" / "t.db"
        itens = [{"servico": {"id": 1, "code": "PS-1"}, "data": QUINTA}]
        self.assertEqual(reg.registrar_origens(itens, reg.ORIGEM_DIA_FIXO, ruim), 0)


class TestAplicarRegistra(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "t.db"

    def test_sorocaba_cai_so_em_terca_de_semana_valida_e_registra(self):
        hoje = date(2026, 10, 3)
        servico = {"id": 1, "code": "#PS-1001", "address": SOROCABA}
        vuupt = FakeVuupt()
        feitos = rdf.aplicar_regioes_dia_fixo([servico], vuupt, hoje=hoje, db_path=self.db)
        data = feitos[0]["data"]
        regra = rdf.regra_dia_fixo_do_servico(servico)
        self.assertEqual(data.weekday(), rdf.TERCA)
        self.assertTrue(rdf.data_valida_na_regiao(regra, data))
        self.assertFalse(rdf.data_valida_na_regiao(regra, data - timedelta(days=7)))
        self.assertLessEqual((data - hoje).days, 14)
        self.assertTrue(vuupt.chamadas[0][1]["scheduled_start"].startswith(data.isoformat()))
        conn = reg.conectar(self.db)
        try:
            self.assertTrue(reg.data_nao_e_do_cliente(conn, servico, data))
            self.assertEqual(conn.execute("SELECT origem FROM agendamentos_origem").fetchone()[0], reg.ORIGEM_DIA_FIXO)
        finally:
            conn.close()

    def test_nao_mexe_em_quem_ja_tem_data(self):
        servico = {"id": 1, "code": "#PS-1001", "address": SOROCABA, "scheduled_start": "2026-10-09T08:00:00-03:00"}
        self.assertEqual(rdf.aplicar_regioes_dia_fixo([servico], FakeVuupt(), hoje=date(2026, 10, 3), db_path=self.db), [])

    def test_falha_no_registro_nao_desfaz_o_agendamento(self):
        ruim = Path(self.tmp.name) / "nao_existe" / "t.db"
        servico = {"id": 1, "code": "#PS-1001", "address": SOROCABA}
        feitos = rdf.aplicar_regioes_dia_fixo([servico], FakeVuupt(), hoje=date(2026, 10, 3), db_path=ruim)
        self.assertEqual(len(feitos), 1)
        self.assertTrue(servico["scheduled_start"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `py -3.11 -m unittest roteirizacao.test_registro_dia_fixo -v`
Expected: `ModuleNotFoundError: No module named 'registro_dia_fixo'`.

- [ ] **Step 3: Criar `roteirizacao/registro_dia_fixo.py`**

```python
# -*- coding: utf-8 -*-
"""
registro_dia_fixo.py

Duas tabelas de controle da regra de dia fixo v2 (Hugo, 03/10/2026 --
spec docs/superpowers/specs/2026-10-03-dias-fixos-v2-design.md):

  agendamentos_origem   origem de cada scheduled_start que o PROPRIO sistema
                        gravou: DIA_FIXO (regioes_dia_fixo.aplicar_regioes_dia_fixo)
                        ou EQUIPE (Planejamento: reagendar_pedido/reagendar_pedidos).
                        Data sem linha DIA_FIXO/EQUIPE = data do CLIENTE
                        (fora_dia_fixo.py). Edicao direta na tela da Vuupt nao
                        passa por aqui e conta como do cliente.
  avisos_fora_dia_fixo  um aviso por pedido ao embarcador quando a data dele
                        cai fora do dia de visita (notificar_fora_dia_fixo.py).

Chave: codigo base do pedido (PS-NNNNN, pedidos_dedicados.normalizar_codigo:
'-R1' de reentrega cai no mesmo codigo) e/ou service_id (a tela do
Planejamento so manda o id). Linhas nunca sao apagadas.
"""
import logging
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
if str(_RAIZ) not in sys.path:
    sys.path.insert(0, str(_RAIZ))
import pedidos_dedicados  # noqa: E402  (normalizacao do codigo do pedido)

logger = logging.getLogger(__name__)

DB_PATH = _RAIZ / "dados" / "dados.db"

ORIGEM_DIA_FIXO = "DIA_FIXO"
ORIGEM_EQUIPE = "EQUIPE"
ORIGENS_NAO_CLIENTE = (ORIGEM_DIA_FIXO, ORIGEM_EQUIPE)


def conectar(db_path=DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS agendamentos_origem (
            codigo TEXT NOT NULL DEFAULT '',
            service_id INTEGER NOT NULL DEFAULT 0,
            data TEXT NOT NULL,
            origem TEXT NOT NULL,
            gravado_em TEXT NOT NULL,
            PRIMARY KEY (codigo, service_id, data, origem)
        );
        CREATE INDEX IF NOT EXISTS idx_agendamentos_origem_sid ON agendamentos_origem (service_id, data);
        CREATE INDEX IF NOT EXISTS idx_agendamentos_origem_data ON agendamentos_origem (data, codigo);
        CREATE TABLE IF NOT EXISTS avisos_fora_dia_fixo (
            codigo TEXT PRIMARY KEY,
            service_id INTEGER,
            data TEXT,
            enviado_em TEXT NOT NULL,
            canais TEXT NOT NULL
        );
    """)
    conn.commit()
    return conn


def _quando(agora: datetime | None) -> str:
    return (agora or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")


def _service_id(servico: dict) -> int:
    try:
        return int(servico.get("id") or 0)
    except (TypeError, ValueError):
        return 0


def registrar_origem(conn: sqlite3.Connection, servico: dict, data: date, origem: str,
                     agora: datetime | None = None) -> int:
    """Uma linha por codigo do servico (com o service_id junto); servico sem
    codigo grava uma linha so com o service_id. Devolve quantas linhas novas."""
    sid = _service_id(servico)
    codigos = pedidos_dedicados.codigos_do_servico(servico) or ([""] if sid else [])
    gravados = 0
    for codigo in codigos:
        cur = conn.execute(
            "INSERT OR IGNORE INTO agendamentos_origem (codigo, service_id, data, origem, gravado_em) "
            "VALUES (?, ?, ?, ?, ?)", (codigo, sid, data.isoformat(), origem, _quando(agora)))
        gravados += cur.rowcount
    conn.commit()
    return gravados


def data_nao_e_do_cliente(conn: sqlite3.Connection, servico: dict, data: date) -> bool:
    """A data foi gravada pelo dia fixo ou pela equipe pra este pedido
    (qualquer codigo do servico OU o mesmo service_id)?"""
    codigos = pedidos_dedicados.codigos_do_servico(servico)
    sid = _service_id(servico)
    if not codigos and not sid:
        return False
    condicoes, params = [], [data.isoformat(), *ORIGENS_NAO_CLIENTE]
    if codigos:
        condicoes.append(f"codigo IN ({','.join('?' * len(codigos))})")
        params += codigos
    if sid:
        condicoes.append("service_id = ?")
        params.append(sid)
    return conn.execute(
        f"SELECT 1 FROM agendamentos_origem WHERE data = ? AND origem IN (?, ?) AND ({' OR '.join(condicoes)}) LIMIT 1",
        params).fetchone() is not None


def registrar_origens(itens: list[dict], origem: str, db_path=DB_PATH) -> int:
    """Tolerante: falha so loga (o agendamento na Vuupt ja foi gravado e nao
    pode ser desfeito por causa do registro). Cada item: {"servico", "data"}."""
    if not itens:
        return 0
    try:
        conn = conectar(db_path)
        try:
            return sum(registrar_origem(conn, i["servico"], i["data"], origem) for i in itens)
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"nao registrou a origem ({origem}) de {len(itens)} agendamento(s) ({e})")
        return 0


def registrar_aviso(conn: sqlite3.Connection, servico: dict, data: date | None, canais: list[str],
                    agora: datetime | None = None) -> None:
    """Um registro por codigo; o segundo e ignorado (INSERT OR IGNORE)."""
    for codigo in pedidos_dedicados.codigos_do_servico(servico):
        conn.execute(
            "INSERT OR IGNORE INTO avisos_fora_dia_fixo (codigo, service_id, data, enviado_em, canais) "
            "VALUES (?, ?, ?, ?, ?)",
            (codigo, servico.get("id"), data.isoformat() if data else None, _quando(agora),
             ",".join(canais) or "nenhum"))
    conn.commit()


def ja_avisado(conn: sqlite3.Connection, servico: dict) -> bool:
    codigos = pedidos_dedicados.codigos_do_servico(servico)
    if not codigos:
        return False
    marcadores = ",".join("?" * len(codigos))
    return conn.execute(f"SELECT 1 FROM avisos_fora_dia_fixo WHERE codigo IN ({marcadores}) LIMIT 1",
                        tuple(codigos)).fetchone() is not None
```

- [ ] **Step 4: Registrar no `aplicar_regioes_dia_fixo`**

Em `roteirizacao/regioes_dia_fixo.py`, trocar a assinatura:

```python
def aplicar_regioes_dia_fixo(servicos: list[dict], vuupt, hoje: date | None = None,
                             db_path=None) -> list[dict]:
```

Acrescentar ao fim da docstring dela:

```
    03/10/2026 (dias fixos v2): cada data gravada aqui vai pra tabela
    agendamentos_origem com origem DIA_FIXO (registro_dia_fixo.py) -- é ela
    que separa a data do sistema da data do cliente. `db_path` só pra teste.
```

E trocar o `return atualizados` final por:

```python
    if atualizados:
        try:
            try:
                import registro_dia_fixo
            except ImportError:
                from roteirizacao import registro_dia_fixo
            registro_dia_fixo.registrar_origens(atualizados, registro_dia_fixo.ORIGEM_DIA_FIXO,
                                                db_path or registro_dia_fixo.DB_PATH)
        except Exception as e:
            logger.warning(f"Falha ao registrar a origem dos agendamentos por dia fixo: {e}")
    return atualizados
```

- [ ] **Step 5: Rodar e ver passar**

```bash
py -3.11 -m unittest roteirizacao.test_registro_dia_fixo roteirizacao.test_regioes_dia_fixo -v 2>&1 | tail -3
```

Expected: `OK`.

- [ ] **Step 6: Commit**

```bash
git add roteirizacao/registro_dia_fixo.py roteirizacao/regioes_dia_fixo.py roteirizacao/test_registro_dia_fixo.py
git commit -m "Dia fixo: registra a origem da data gravada pelo sistema, pra separar da data do cliente

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3A: Data agendada pela equipe no Planejamento (`EQUIPE`)

**Files:**
- Modify: `painel_agentes/planejamento_rotas.py` (`reagendar_pedido`, `reagendar_pedidos`)
- Modify: `painel_agentes/test_reagendar_pedidos_lote.py`
- Test: `painel_agentes/test_agendamento_equipe.py`

**Interfaces:**
- Consumes: `registro_dia_fixo.registrar_origens(itens, origem, db_path)`, `ORIGEM_EQUIPE`, `DB_PATH`, `conectar`, `data_nao_e_do_cliente` (Task 3).
- Produces: `planejamento_rotas._registrar_agendamento_equipe(service_ids: list[int], scheduled_start: str) -> None` (nunca levanta), chamada depois de cada reagendamento bem-sucedido na Vuupt.

As duas rotinas da equipe que gravam `scheduled_start` são `reagendar_pedido` (menu de contexto "Agendar / reagendar", rota `/api/planejamento/reagendar-pedido`) e `reagendar_pedidos` (botão "Agendar" da seleção múltipla, rota `/api/planejamento/reagendar-pedidos`). As rotas em `painel_agentes.py` só chamam essas funções e não mudam. Antes de editar, confirmar que não surgiu outra rotina de equipe:

```bash
grep -rn "\"scheduled_start\":" painel_agentes/*.py | grep -v test_
```

Expected: só as duas linhas de `planejamento_rotas.py` (dentro de `reagendar_pedido` e `reagendar_pedidos`) e a de `pedidos_parados_triagem.py` (essa só monta uma sugestão, não grava). Se aparecer outra gravação de `scheduled_start` feita pela equipe, chamar `_registrar_agendamento_equipe` nela do mesmo jeito e acrescentar um teste igual ao `test_reagendar_um_registra_equipe`.

- [ ] **Step 1: Escrever os testes**

Criar `painel_agentes/test_agendamento_equipe.py`:

```python
# -*- coding: utf-8 -*-
"""
Data agendada pela equipe no Planejamento não é data do cliente (Hugo,
03/10 -- dias fixos v2): reagendar_pedido e reagendar_pedidos registram a
origem EQUIPE em agendamentos_origem.
Rodar (da raiz): py -3.11 -m unittest painel_agentes.test_agendamento_equipe -v
"""
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(Path(__file__).parent))

import planejamento_rotas
import registro_dia_fixo as reg

QUINTA = date(2026, 10, 8)


class AgendamentoEquipe(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "t.db"
        for alvo, obj, kw in (("DB_PATH", reg, {"new": self.db}),
                              ("_carregar_config", planejamento_rotas, {"return_value": {"vuupt_api": {"token": "t"}}}),
                              ("_ressincronizar", planejamento_rotas, {"return_value": None})):
            p = mock.patch.object(obj, alvo, **kw)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(planejamento_rotas, "VuuptClient")
        self.vuupt = p.start().return_value
        self.addCleanup(p.stop)

    def _equipe(self, service_id, data=QUINTA):
        conn = reg.conectar(self.db)
        try:
            return reg.data_nao_e_do_cliente(conn, {"id": service_id, "code": f"#PS-{service_id}"}, data)
        finally:
            conn.close()

    def test_reagendar_um_registra_equipe(self):
        self.assertEqual(planejamento_rotas.reagendar_pedido(111, "2026-10-08", "08:00", "12:00"), {"ok": True})
        self.assertTrue(self._equipe(111))
        conn = reg.conectar(self.db)
        try:
            self.assertEqual(conn.execute("SELECT origem FROM agendamentos_origem").fetchone()[0], reg.ORIGEM_EQUIPE)
        finally:
            conn.close()

    def test_falha_na_vuupt_nao_registra(self):
        from vuupt_client import VuuptAPIError
        self.vuupt.atualizar_servico.side_effect = VuuptAPIError("recusado")
        self.assertFalse(planejamento_rotas.reagendar_pedido(111, "2026-10-08", "08:00", "12:00")["ok"])
        self.assertFalse(self._equipe(111))

    def test_lote_registra_so_os_que_deram_certo(self):
        def atualizar(service_id, payload):
            if service_id == 222:
                raise RuntimeError("timeout")
        self.vuupt.atualizar_servico.side_effect = atualizar
        r = planejamento_rotas.reagendar_pedidos([{"service_id": 111}, {"service_id": 222}], "2026-10-08", "08:00", "12:00")
        self.assertEqual([f["service_id"] for f in r["falhas"]], [222])
        self.assertTrue(self._equipe(111))
        self.assertFalse(self._equipe(222))

    def test_falha_no_registro_nao_derruba_o_reagendamento(self):
        with mock.patch.object(reg, "DB_PATH", Path(self.tmp.name) / "nao_existe" / "t.db"):
            self.assertEqual(planejamento_rotas.reagendar_pedido(111, "2026-10-08", "08:00", "12:00"), {"ok": True})


if __name__ == "__main__":
    unittest.main()
```

Em `painel_agentes/test_reagendar_pedidos_lote.py`, no fim do `setUp` existente, acrescentar (o teste antigo não pode gravar no `dados/dados.db` real):

```python
        patch_origem = mock.patch.object(planejamento_rotas, "_registrar_agendamento_equipe")
        patch_origem.start()
        self.addCleanup(patch_origem.stop)
```

Conferir se há outro teste que chama `reagendar_pedido` sem mock do banco e acrescentar o mesmo patch no `setUp` dele:

Run: `grep -ln "reagendar_pedido\b\|reagendar_pedido(" painel_agentes/test_*.py`

- [ ] **Step 2: Rodar e ver falhar**

Run: `py -3.11 -m unittest painel_agentes.test_agendamento_equipe painel_agentes.test_reagendar_pedidos_lote -v`
Expected: FAIL em `test_reagendar_um_registra_equipe` e `test_lote_registra_so_os_que_deram_certo` (nenhuma linha gravada) e ERROR no `setUp` do teste do lote (`AttributeError: ... '_registrar_agendamento_equipe'`).

- [ ] **Step 3: Implementar em `painel_agentes/planejamento_rotas.py`**

3a. Logo antes de `def reagendar_pedido(`, acrescentar:

```python
def _registrar_agendamento_equipe(service_ids: list[int], scheduled_start: str) -> None:
    """Data agendada pela equipe não é data do cliente (Hugo, 03/10 -- dias
    fixos v2): grava a origem EQUIPE em agendamentos_origem, pra regra de
    data fora do dia fixo (roteirizacao/fora_dia_fixo.py) não transformar o
    pedido em dedicado. Nunca levanta: o reagendamento na Vuupt já foi feito."""
    if not service_ids:
        return
    try:
        import registro_dia_fixo
        data = date.fromisoformat(str(scheduled_start)[:10])
        registro_dia_fixo.registrar_origens([{"servico": {"id": sid}, "data": data} for sid in service_ids],
                                            registro_dia_fixo.ORIGEM_EQUIPE, registro_dia_fixo.DB_PATH)
    except Exception as e:
        logger.warning(f"Não registrou a origem EQUIPE do reagendamento {service_ids}: {e}")
```

(`date` já é importado em `planejamento_rotas.py`; conferir com `grep -n "^from datetime" painel_agentes/planejamento_rotas.py` e acrescentar `date` ao import se faltar. `registro_dia_fixo` é achado porque `planejamento_rotas` já põe `roteirizacao/` no `sys.path` para importar `regioes_dia_fixo`.)

3b. Em `reagendar_pedido`, trocar

```python
    _ressincronizar(vuupt, [service_id])

    return {"ok": True}


def _converter_janela_reagendamento(
```

por

```python
    _ressincronizar(vuupt, [service_id])
    _registrar_agendamento_equipe([service_id], scheduled_start)

    return {"ok": True}


def _converter_janela_reagendamento(
```

E na docstring de `reagendar_pedido`, depois de "vai pra VUUPT como digitada.", acrescentar: `03/10: a data fica registrada como EQUIPE (não vira dedicado por data fora do dia fixo).`

3c. Em `reagendar_pedidos`, trocar

```python
    if ok_ids:
        _ressincronizar(vuupt, ok_ids)
    return {"ok": True, "falhas": falhas}
```

por

```python
    if ok_ids:
        _ressincronizar(vuupt, ok_ids)
        _registrar_agendamento_equipe(ok_ids, scheduled_start)
    return {"ok": True, "falhas": falhas}
```

- [ ] **Step 4: Rodar e ver passar**

```bash
py -3.11 -m unittest painel_agentes.test_agendamento_equipe painel_agentes.test_reagendar_pedidos_lote -v 2>&1 | tail -3
py -3.11 -m unittest discover -s painel_agentes -p "test_*.py" 2>&1 | tail -3
```

Expected: `OK` nos dois.

- [ ] **Step 5: Commit**

```bash
git add painel_agentes/planejamento_rotas.py painel_agentes/test_agendamento_equipe.py painel_agentes/test_reagendar_pedidos_lote.py
git commit -m "Planejamento: data reagendada pela equipe fica registrada como EQUIPE e nao vira dedicado

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Detecção e marcação como dedicado

**Files:**
- Modify: `pedidos_dedicados.py`
- Create: `roteirizacao/fora_dia_fixo.py`
- Test: `roteirizacao/test_fora_dia_fixo.py`

**Interfaces:**
- Consumes: `registro_dia_fixo.conectar`, `data_nao_e_do_cliente`, `registrar_origem`, `ORIGEM_DIA_FIXO`, `ORIGEM_EQUIPE`, `ja_avisado` (Task 3; a origem `EQUIPE` é gravada pela Task 3A); `regioes_dia_fixo.regra_dia_fixo_do_servico`, `data_valida_na_regiao` (Task 1); `pedidos_dedicados.conectar`, `marcar(conn, pedidos, valor_total, por) -> str`, `ativos_por_codigo(conn) -> dict[str, dict]`, `codigos_do_servico`; `rotas_fracas.data_entrada(servico) -> date | None`; `roteirizacao_dados.coordenada_embutida(servico) -> tuple | None`, `extrair_volume_caixas(servico) -> int`; `km_rodoviario.calcular_trajeto(origem, paradas, api_key, voltar=...) -> ResultadoTrajeto | None` (atributo `km_total`); `portal_cliente.cotacao.regras_de(config)`, `calcular(entrada, regras) -> dict` (chave `total`), `ErroCotacao`; `regras.tipo_carga_embarcador.carregar_tipos_carga_por_sender(db_path)`, `classificar_tipo_carga(sender_id, mapa) -> (str, bool)`; `preferencias_notificacao.carregar_embarcadores(tipo, db_path=...)`.
- Produces:
  - `pedidos_dedicados.POR_FORA_DIA_FIXO = "automatico: fora do dia fixo"`
  - `fora_dia_fixo.DETECCAO_A_PARTIR_DE: date`, `POR`, `POR_VALOR_PENDENTE`
  - `data_agendada(servico) -> date | None`
  - `detectar(servico, hoje: date, conn_registro) -> tuple[dict, date] | None`
  - `calcular_valor(servico, config: dict, tipos_carga: dict) -> float | None`
  - `carregar_tipos_carga(db_path) -> dict` (tolerante a falha; usada também pela Task 4A)
  - `marcar_fora_dia_fixo(servicos, config, hoje: date | None = None, db_path=None) -> list[dict]` (itens `{"servico", "regra", "data", "valor", "valor_pendente"}`)
  - `pendentes_de_aviso(servicos, db_path=None) -> list[dict]` (mesmo formato de item)

- [ ] **Step 1: Escrever os testes**

Criar `roteirizacao/test_fora_dia_fixo.py`:

```python
# -*- coding: utf-8 -*-
"""
Data do embarcador fora do dia de visita da região vira dedicado (dias
fixos v2, Hugo 03/10). Rodar (da raiz):
    py -3.11 -m unittest roteirizacao.test_fora_dia_fixo -v
"""
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import fora_dia_fixo as fdf
import km_rodoviario
import pedidos_dedicados
import registro_dia_fixo as reg

CAMPINAS = "Rua Barão de Jaguara 900, Centro, Campinas - SP, 13015-001, Brasil"   # só quarta
SAO_PAULO = "Rua Augusta 100, Consolação, São Paulo - SP, 01304-000, Brasil"
HOJE = date(2026, 10, 6)        # terça
QUARTA, QUINTA = "2026-10-07", "2026-10-08"


def _servico(i=1, endereco=CAMPINAS, data=QUINTA, criado="2026-10-06 15:00:00", code=None, **extra):
    s = {"id": i, "code": code or f"#PS-{1000 + i}", "address": endereco, "sender_id": 7, "dimension_3": 5,
         "latitude": -22.905, "longitude": -47.060, "created_at": criado, "title": f"Cliente {i}"}
    if data:
        s["scheduled_start"] = f"{data}T08:00:00-03:00"
    s.update(extra)
    return s


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "t.db"
        for alvo, valor in (("DETECCAO_A_PARTIR_DE", date(2026, 10, 6)), ("calcular_valor", lambda s, c, t: 784.09)):
            p = mock.patch.object(fdf, alvo, valor)
            p.start()
            self.addCleanup(p.stop)

    def _marcar(self, servicos):
        return fdf.marcar_fora_dia_fixo(servicos, {}, hoje=HOJE, db_path=self.db)

    def _ativos(self):
        conn = pedidos_dedicados.conectar(self.db)
        try:
            return pedidos_dedicados.ativos_por_codigo(conn)
        finally:
            conn.close()


class TestDeteccaoEMarcacao(Base):
    def test_data_do_embarcador_fora_do_dia_marca(self):
        marcados = self._marcar([_servico()])
        self.assertEqual(len(marcados), 1)
        self.assertEqual((marcados[0]["data"], marcados[0]["valor"], marcados[0]["valor_pendente"]),
                         (date(2026, 10, 8), 784.09, False))
        self.assertEqual(marcados[0]["regra"]["regiao"], "Campinas")
        linha = self._ativos()["PS-1001"]
        self.assertEqual((linha["valor"], linha["marcado_por"], linha["service_id"], linha["sender_id"]),
                         (784.09, fdf.POR, 1, 7))
        self.assertEqual(fdf.POR, pedidos_dedicados.POR_FORA_DIA_FIXO)

    def test_data_no_dia_de_visita_nao_marca(self):
        self.assertEqual(self._marcar([_servico(data=QUARTA)]), [])
        self.assertEqual(self._ativos(), {})

    def test_data_gravada_pelo_dia_fixo_nao_marca(self):
        s = _servico()
        conn = reg.conectar(self.db)
        reg.registrar_origem(conn, s, date(2026, 10, 8), reg.ORIGEM_DIA_FIXO)
        conn.close()
        self.assertEqual(self._marcar([s]), [])

    def test_data_reagendada_pela_equipe_nao_marca(self):
        # Hugo, 03/10: data posta pela equipe no Planejamento (só service_id) não vira dedicado
        conn = reg.conectar(self.db)
        reg.registrar_origem(conn, {"id": 1}, date(2026, 10, 8), reg.ORIGEM_EQUIPE)
        conn.close()
        self.assertEqual(self._marcar([_servico()]), [])
        self.assertEqual(self._ativos(), {})

    def test_equipe_em_outra_data_nao_protege_a_data_nova_do_cliente(self):
        conn = reg.conectar(self.db)
        reg.registrar_origem(conn, {"id": 1}, date(2026, 10, 7), reg.ORIGEM_EQUIPE)
        conn.close()
        self.assertEqual(len(self._marcar([_servico()])), 1)   # cliente trocou pra 08/10 depois

    def test_sem_regiao_de_dia_fixo_nao_marca(self):
        self.assertEqual(self._marcar([_servico(endereco=SAO_PAULO)]), [])

    def test_sem_agendamento_nao_marca(self):
        self.assertEqual(self._marcar([_servico(data=None)]), [])

    def test_ja_dedicado_nao_remarca(self):
        conn = pedidos_dedicados.conectar(self.db)
        pedidos_dedicados.marcar(conn, [{"codigo_pedido": "PS-1001"}], 500.0, "hugo")
        antes = pedidos_dedicados.ativos_por_codigo(conn)["PS-1001"]
        conn.close()
        self.assertEqual(self._marcar([_servico()]), [])
        depois = self._ativos()["PS-1001"]
        self.assertEqual((depois["valor"], depois["marcado_por"], depois["marcado_em"]),
                         (500.0, "hugo", antes["marcado_em"]))

    def test_codigo_combinado_com_um_ja_dedicado(self):
        conn = pedidos_dedicados.conectar(self.db)
        pedidos_dedicados.marcar(conn, [{"codigo_pedido": "PS-2002"}], 300.0, "hugo")
        conn.close()
        self.assertEqual(self._marcar([_servico(code="#PS-1001, PS-2002")]), [])
        self.assertEqual(set(self._ativos()), {"PS-2002"})

    def test_falha_da_calculadora_marca_com_zero_e_valor_pendente(self):
        with mock.patch.object(fdf, "calcular_valor", lambda s, c, t: None):
            marcados = self._marcar([_servico()])
        self.assertTrue(marcados[0]["valor_pendente"])
        linha = self._ativos()["PS-1001"]
        self.assertEqual((linha["valor"], linha["marcado_por"]), (0.0, fdf.POR_VALOR_PENDENTE))

    def test_pedido_criado_antes_do_corte_nao_marca(self):
        # dia fixo antigo gravou datas que a tabela nova nao conhece
        self.assertEqual(self._marcar([_servico(criado="2026-10-05 15:00:00")]), [])

    def test_created_at_em_utc_conta_o_dia_de_brasilia(self):
        # 02:30 UTC de terca = 23:30 de segunda em Brasilia -> antes do corte
        self.assertEqual(self._marcar([_servico(criado="2026-10-06 02:30:00")]), [])

    def test_data_vencida_ou_malformada_nao_marca(self):
        self.assertEqual(self._marcar([_servico(data="2026-10-01")]), [])
        self.assertEqual(self._marcar([_servico(i=2, data=None, scheduled_start="amanha")]), [])

    def test_reentrega_segue_a_mesma_regra(self):
        marcados = self._marcar([_servico(code="#PS-1001-R1")])
        self.assertEqual(len(marcados), 1)
        self.assertIn("PS-1001", self._ativos())

    def test_rodado_duas_vezes_marca_uma(self):
        self._marcar([_servico()])
        self.assertEqual(self._marcar([_servico()]), [])


class TestPendentesDeAviso(Base):
    def test_marcado_e_nao_avisado_e_pendente(self):
        self._marcar([_servico()])
        pend = fdf.pendentes_de_aviso([_servico(), _servico(i=2, endereco=SAO_PAULO)], db_path=self.db)
        self.assertEqual([p["servico"]["id"] for p in pend], [1])
        self.assertEqual((pend[0]["valor"], pend[0]["valor_pendente"], pend[0]["data"]), (784.09, False, date(2026, 10, 8)))

    def test_avisado_sai_da_lista(self):
        self._marcar([_servico()])
        conn = reg.conectar(self.db)
        reg.registrar_aviso(conn, _servico(), date(2026, 10, 8), ["email"])
        conn.close()
        self.assertEqual(fdf.pendentes_de_aviso([_servico()], db_path=self.db), [])

    def test_dedicado_manual_nao_e_pendente(self):
        conn = pedidos_dedicados.conectar(self.db)
        pedidos_dedicados.marcar(conn, [{"codigo_pedido": "PS-1001"}], 500.0, "hugo")
        conn.close()
        self.assertEqual(fdf.pendentes_de_aviso([_servico()], db_path=self.db), [])

    def test_valor_pendente_vem_marcado(self):
        with mock.patch.object(fdf, "calcular_valor", lambda s, c, t: None):
            self._marcar([_servico()])
        self.assertTrue(fdf.pendentes_de_aviso([_servico()], db_path=self.db)[0]["valor_pendente"])


class TestCalcularValor(unittest.TestCase):
    def test_usa_a_calculadora_com_km_em_linha_reta_ida_e_volta(self):
        from portal_cliente import cotacao
        s = _servico()
        regras = cotacao.regras_de({})
        km = km_rodoviario.calcular_trajeto(tuple(regras["origem_coords"]), [(-22.905, -47.060)], None, voltar=True).km_total
        esperado = cotacao.calcular({"caixas": 5, "peso_kg": 0, "tipo_carga": "REFRIGERADO", "urgente": False,
                                     "valor_nf": None, "km_total": km, "pedagio": None}, regras)["total"]
        with mock.patch.object(km_rodoviario.requests, "post", side_effect=AssertionError("nao chama a Routes")):
            self.assertEqual(fdf.calcular_valor(s, {}, {7: "Refrigerado"}), esperado)

    def test_sem_coordenada(self):
        self.assertIsNone(fdf.calcular_valor(_servico(latitude=None), {}, {}))

    def test_carga_acima_da_tabela(self):
        self.assertIsNone(fdf.calcular_valor(_servico(dimension_3=5000), {}, {}))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `py -3.11 -m unittest roteirizacao.test_fora_dia_fixo -v`
Expected: `ModuleNotFoundError: No module named 'fora_dia_fixo'`.

- [ ] **Step 3: Constante em `pedidos_dedicados.py`**

Logo depois de `_CAMPOS_IDENTIDADE = (...)`, acrescentar:

```python
# Marcacao automatica de data fora do dia fixo (Hugo, 03/10/2026 --
# roteirizacao/fora_dia_fixo.py). Quem le o motivo (pool do planejamento,
# portal) compara o inicio de marcado_por com isto.
POR_FORA_DIA_FIXO = "automatico: fora do dia fixo"
```

- [ ] **Step 4: Criar `roteirizacao/fora_dia_fixo.py`**

```python
# -*- coding: utf-8 -*-
"""
fora_dia_fixo.py

Data do embarcador fora do dia de visita da região (Hugo, 03/10/2026 --
spec docs/superpowers/specs/2026-10-03-dias-fixos-v2-design.md, seção 5):
a data do cliente manda, o pedido vira DEDICADO (sai da rota compartilhada
pelo fluxo de roteirizacao/dedicados.py) com o valor da calculadora de
frete dedicado, e o embarcador é avisado uma vez (notificar_fora_dia_fixo.py).

Quem chama: criar_rotas_diarias.main e incrementar_rotas.main, logo depois
de aplicar_regioes_dia_fixo e antes de separar_dedicados
(tratar_fora_dia_fixo -- nunca levanta).

Data "do cliente" = scheduled_start sem linha DIA_FIXO nem EQUIPE em
agendamentos_origem (registro_dia_fixo.py) -- decisão do Hugo, 03/10: data
posta pela equipe no Planejamento não vira dedicado. Edição direta na tela
da Vuupt não passa pelo sistema e conta como do cliente. Pedido criado antes
de DETECCAO_A_PARTIR_DE fica de fora: o dia fixo antigo gravou datas que a
tabela não conhece (ABCD quarta/sexta, Transfrios segunda/quarta).

Km do valor: linha reta (haversine) da base ao destino e volta. A Google
Routes devolve 403 desde 30/09 (faturamento fechado), então nem é chamada;
o valor sai menor que o rodoviário e o financeiro corrige.
"""
import logging
import sys
from datetime import date
from pathlib import Path

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import km_rodoviario  # noqa: E402
import pedidos_dedicados  # noqa: E402
import registro_dia_fixo  # noqa: E402
from regioes_dia_fixo import data_valida_na_regiao, regra_dia_fixo_do_servico  # noqa: E402
from roteirizacao_dados import coordenada_embutida, extrair_volume_caixas  # noqa: E402
from rotas_fracas import data_entrada  # noqa: E402
from regras.tipo_carga_embarcador import carregar_tipos_carga_por_sender, classificar_tipo_carga  # noqa: E402

logger = logging.getLogger(__name__)

# Dia do deploy (ajustar no commit do deploy): pedido criado antes não é marcado.
DETECCAO_A_PARTIR_DE = date(2026, 10, 6)
POR = pedidos_dedicados.POR_FORA_DIA_FIXO
POR_VALOR_PENDENTE = POR + " (valor pendente)"


def data_agendada(servico: dict) -> date | None:
    try:
        return date.fromisoformat(str(servico.get("scheduled_start") or "")[:10])
    except ValueError:
        return None


def detectar(servico: dict, hoje: date, conn_registro) -> tuple[dict, date] | None:
    """(regra, data) quando a data do pedido é do cliente (não foi gravada
    pelo dia fixo nem pela equipe) e cai fora do dia de visita da região;
    None caso contrário."""
    data = data_agendada(servico)
    if not data or data < hoje:
        return None
    entrada = data_entrada(servico)
    if not entrada or entrada < DETECCAO_A_PARTIR_DE:
        return None
    regra = regra_dia_fixo_do_servico(servico)
    if not regra or data_valida_na_regiao(regra, data):
        return None
    if registro_dia_fixo.data_nao_e_do_cliente(conn_registro, servico, data):
        return None
    return regra, data


def calcular_valor(servico: dict, config: dict, tipos_carga: dict) -> float | None:
    """Total da calculadora de frete dedicado (portal_cliente/cotacao.py):
    base -> destino -> base em linha reta, caixas do pedido, tipo de carga do
    embarcador. None quando não dá pra calcular (sem coordenada, carga acima
    da tabela, entrada inválida)."""
    from portal_cliente import cotacao
    coords = coordenada_embutida(servico)
    if not coords:
        return None
    regras = cotacao.regras_de(config)
    trajeto = km_rodoviario.calcular_trajeto(tuple(regras["origem_coords"]), [coords], None,
                                             voltar=bool(regras.get("considerar_retorno", True)))
    if trajeto is None:
        return None
    tipo, _ = classificar_tipo_carga(servico.get("sender_id"), tipos_carga)
    try:
        r = cotacao.calcular({"caixas": extrair_volume_caixas(servico), "peso_kg": 0, "tipo_carga": tipo.upper(),
                              "urgente": False, "valor_nf": None, "km_total": trajeto.km_total, "pedagio": None},
                             regras)
    except cotacao.ErroCotacao as e:
        logger.info(f"  {servico.get('code')}: calculadora de frete dedicado sem valor ({e})")
        return None
    return r["total"]


def carregar_tipos_carga(db_path) -> dict:
    """{sender_id: tipo de carga}; falha -> {} (valor sai como carga seca).
    Pública: o Planejamento usa no aviso de reagendamento (Task 4A)."""
    try:
        return carregar_tipos_carga_por_sender(db_path)
    except Exception as e:
        logger.warning(f"nao carregou o tipo de carga dos embarcadores ({e}); valor sai como carga seca")
        return {}


def _nomes_remetentes(db_path) -> dict:
    try:
        import preferencias_notificacao
        return {k: v.get("nome") for k, v in
                preferencias_notificacao.carregar_embarcadores("agendamento", db_path=db_path).items()}
    except Exception as e:
        logger.warning(f"nao carregou o nome dos remetentes ({e}); dedicado sai sem o nome")
        return {}


def marcar_fora_dia_fixo(servicos: list[dict], config: dict, hoje: date | None = None,
                         db_path=None) -> list[dict]:
    """Marca como dedicado cada pedido detectado. Dedicado ativo (qualquer
    código do serviço) não é remarcado. Devolve os marcados nesta rodada:
    [{"servico", "regra", "data", "valor", "valor_pendente"}]."""
    hoje = hoje or date.today()
    db = db_path or pedidos_dedicados.DB_PATH
    tipos = carregar_tipos_carga(db)
    nomes = _nomes_remetentes(db)
    marcados: list[dict] = []
    conn_reg = registro_dia_fixo.conectar(db)
    conn_ded = pedidos_dedicados.conectar(db)
    try:
        ativos = pedidos_dedicados.ativos_por_codigo(conn_ded)
        for s in servicos:
            codigos = pedidos_dedicados.codigos_do_servico(s)
            if not codigos or any(c in ativos for c in codigos):
                continue
            achado = detectar(s, hoje, conn_reg)
            if not achado:
                continue
            regra, data = achado
            valor = calcular_valor(s, config, tipos)
            por = POR if valor is not None else POR_VALOR_PENDENTE
            pedidos_dedicados.marcar(conn_ded, [{
                "codigo_pedido": codigos[0], "service_id": s.get("id"), "sender_id": s.get("sender_id"),
                "remetente_nome": nomes.get(s.get("sender_id")),
            }], valor or 0.0, por)
            ativos[codigos[0]] = {"marcado_por": por}
            logger.info(f"  {s.get('code')}: data {data:%d/%m} fora dos dias de "
                        f"{regra.get('regiao') or regra['nome']} -- marcado como dedicado "
                        f"({'valor pendente' if valor is None else f'R$ {valor:.2f}'}).")
            marcados.append({"servico": s, "regra": regra, "data": data, "valor": valor or 0.0,
                             "valor_pendente": valor is None})
    finally:
        conn_reg.close()
        conn_ded.close()
    return marcados


def pendentes_de_aviso(servicos: list[dict], db_path=None) -> list[dict]:
    """Pedidos da lista marcados por ESTA regra e ainda não avisados
    (inclui os de rodadas anteriores cujo aviso falhou)."""
    db = db_path or pedidos_dedicados.DB_PATH
    conn_reg = registro_dia_fixo.conectar(db)
    conn_ded = pedidos_dedicados.conectar(db)
    try:
        ativos = pedidos_dedicados.ativos_por_codigo(conn_ded)
        itens = []
        for s in servicos:
            linha = next((ativos[c] for c in pedidos_dedicados.codigos_do_servico(s) if c in ativos), None)
            por = str((linha or {}).get("marcado_por") or "")
            if not por.startswith(POR) or registro_dia_fixo.ja_avisado(conn_reg, s):
                continue
            regra, data = regra_dia_fixo_do_servico(s), data_agendada(s)
            if not regra or not data:
                continue
            itens.append({"servico": s, "regra": regra, "data": data, "valor": float(linha["valor"] or 0.0),
                          "valor_pendente": por == POR_VALOR_PENDENTE})
        return itens
    finally:
        conn_reg.close()
        conn_ded.close()
```

- [ ] **Step 5: Rodar e ver passar**

```bash
py -3.11 -m unittest roteirizacao.test_fora_dia_fixo -v 2>&1 | tail -3
py -3.11 -m unittest test_pedidos_dedicados -v 2>&1 | tail -3
```

Expected: `OK` nos dois.

- [ ] **Step 6: Commit**

```bash
git add pedidos_dedicados.py roteirizacao/fora_dia_fixo.py roteirizacao/test_fora_dia_fixo.py
git commit -m "Dia fixo: data do embarcador fora do dia de visita vira dedicado com valor da calculadora

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4A: Reagendamento pela equipe fora do dia de visita (pergunta "Marcar como dedicado?")

Spec 5.4. Fica depois da Task 4 (e não colada na 3A) porque usa `fora_dia_fixo.calcular_valor` e `carregar_tipos_carga`, criadas na Task 4. Vale só para "Agendar / reagendar" (um pedido) e "Agendar" do lote; Roteirizar e adicionar à rota continuam só com o aviso "Americana: só Quartas".

**Files:**
- Modify: `painel_agentes/planejamento_rotas.py` (import de `regioes_dia_fixo` + função nova `checar_reagendamento_dia_fixo`)
- Modify: `painel_agentes/painel_agentes.py` (import + rota `POST /api/planejamento/checar-dia-fixo`)
- Modify: `painel_agentes/templates/planejamento_rotas.html` (modal novo + fluxo do reagendar + modal de dedicado pré-preenchido)
- Test: `painel_agentes/test_reagendar_fora_dia_fixo.py`

**Interfaces:**
- Consumes: `fora_dia_fixo.calcular_valor(servico, config, tipos_carga) -> float | None`, `fora_dia_fixo.carregar_tipos_carga(db_path) -> dict` (Task 4); `regioes_dia_fixo.regra_dia_fixo_do_servico`, `data_valida_na_regiao`, `descricao_dias` (Task 1); `pedidos_dedicados.codigos_do_servico`; `VuuptClient.buscar_servico_por_id(service_id) -> dict | None`; `_carregar_config`, `rascunhos_rota.DB_PATH` (já existem em `planejamento_rotas.py`); decoradores `requer_auth(niveis=("total", "operador"))` e `exige_mesma_origem` (mesma permissão das rotas de reagendar); no template: `postJSON`, `escaparHtml`, `abrirModalDedicado(serviceIds)`, `atualizarPreviaDedicado()`, `dedicadoPendente`, `reagendarPendente`; a gravação `EQUIPE` da Task 3A (o reagendamento continua passando por `reagendar_pedido`/`reagendar_pedidos`).
- Produces:
  - `planejamento_rotas.checar_reagendamento_dia_fixo(service_ids: list[int], data: str) -> dict`: `{"ok": True, "fora_do_dia": [{"service_id", "codigo", "regiao", "dias", "valor"}]}` (`valor` float ou `None`) ou `{"ok": False, "erro"}` para data inválida
  - rota `POST /api/planejamento/checar-dia-fixo` com corpo `{"service_ids": [...], "data": "AAAA-MM-DD"}`: 200 com o dict acima, 400 com `{"erro"}`, 401 sem sessão, 403 para nível sem permissão ou sem Origin
  - JS: `checarForaDiaFixo(serviceIds, data)`, `perguntarForaDiaFixo(fora, botao)`, `salvarReagendamento(pendente, data, horaInicio, horaFim)`, `abrirModalDedicadoForaDia(fora)`, `confirmarReagendamento(botao, marcarDedicado = null)`

- [ ] **Step 1: Escrever os testes**

Criar `painel_agentes/test_reagendar_fora_dia_fixo.py`:

```python
# -*- coding: utf-8 -*-
"""
Reagendamento pela equipe fora do dia de visita (Hugo, 03/10 -- spec dias
fixos v2, 5.4): a tela pergunta "Marcar como dedicado?" antes de salvar.
Testa a função que acha os pedidos fora do dia (com o valor da calculadora)
e a rota /api/planejamento/checar-dia-fixo (permissão igual à do reagendar).
Rodar (da raiz): py -3.11 -m unittest painel_agentes.test_reagendar_fora_dia_fixo -v
"""
import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import planejamento_rotas  # noqa: E402
import fora_dia_fixo  # noqa: E402  (roteirizacao/ entra no sys.path pelo planejamento_rotas)

# "painel_agentes" e o nome da pasta: o modulo do app precisa ser carregado pelo caminho
_spec = importlib.util.spec_from_file_location("painel_agentes_app", _AQUI / "painel_agentes.py")
painel_agentes = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = painel_agentes
_spec.loader.exec_module(painel_agentes)

CAMPINAS = "Rua Barão de Jaguara 900, Centro, Campinas - SP, 13015-001, Brasil"   # só quarta
SAO_PAULO = "Rua Augusta 100, Consolação, São Paulo - SP, 01304-000, Brasil"
QUINTA, QUARTA = "2026-10-08", "2026-10-07"
SERVICOS = {
    1: {"id": 1, "code": "#PS-1001", "address": CAMPINAS, "sender_id": 7, "dimension_3": 5,
        "latitude": -22.905, "longitude": -47.060},
    2: {"id": 2, "code": "#PS-1002", "address": SAO_PAULO, "sender_id": 7, "dimension_3": 5,
        "latitude": -23.55, "longitude": -46.65},
}
CONFIG_PAINEL = {
    "usuario": "u_total", "senha": "s_total",
    "usuario_operador": "u_op", "senha_operador": "s_op",
    "usuario_leitura": "u_le", "senha_leitura": "s_le",
}


class ChecarReagendamento(unittest.TestCase):
    def setUp(self):
        for alvo, obj, kw in (("_carregar_config", planejamento_rotas, {"return_value": {"vuupt_api": {"token": "t"}}}),
                              ("carregar_tipos_carga", fora_dia_fixo, {"return_value": {}}),
                              ("calcular_valor", fora_dia_fixo, {"return_value": 784.09})):
            p = mock.patch.object(obj, alvo, **kw)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(planejamento_rotas, "VuuptClient")
        self.vuupt = p.start().return_value
        self.addCleanup(p.stop)
        self.vuupt.buscar_servico_por_id.side_effect = lambda sid: SERVICOS.get(sid)

    def test_devolve_so_os_fora_do_dia_com_valor(self):
        r = planejamento_rotas.checar_reagendamento_dia_fixo([1, 2], QUINTA)
        self.assertEqual(r, {"ok": True, "fora_do_dia": [
            {"service_id": 1, "codigo": "PS-1001", "regiao": "Campinas", "dias": "Quartas", "valor": 784.09}]})

    def test_data_de_visita_nao_devolve_nada(self):
        self.assertEqual(planejamento_rotas.checar_reagendamento_dia_fixo([1, 2], QUARTA)["fora_do_dia"], [])

    def test_falha_da_calculadora_devolve_valor_nulo(self):
        with mock.patch.object(fora_dia_fixo, "calcular_valor", return_value=None):
            self.assertIsNone(planejamento_rotas.checar_reagendamento_dia_fixo([1], QUINTA)["fora_do_dia"][0]["valor"])
        with mock.patch.object(fora_dia_fixo, "calcular_valor", side_effect=RuntimeError("tabela")):
            self.assertIsNone(planejamento_rotas.checar_reagendamento_dia_fixo([1], QUINTA)["fora_do_dia"][0]["valor"])

    def test_pedido_que_nao_carrega_fica_de_fora(self):
        self.vuupt.buscar_servico_por_id.side_effect = lambda sid: (_ for _ in ()).throw(RuntimeError("timeout")) \
            if sid == 1 else SERVICOS.get(sid)
        self.assertEqual(planejamento_rotas.checar_reagendamento_dia_fixo([1, 2, 99], QUINTA)["fora_do_dia"], [])

    def test_data_invalida(self):
        r = planejamento_rotas.checar_reagendamento_dia_fixo([1], "08/10")
        self.assertFalse(r["ok"])
        self.vuupt.buscar_servico_por_id.assert_not_called()


class RotaChecarDiaFixo(unittest.TestCase):
    URL = "/api/planejamento/checar-dia-fixo"
    ORIGEM = {"Origin": "http://localhost"}

    def setUp(self):
        config = {**painel_agentes._carregar_config(), "painel_agentes": CONFIG_PAINEL}
        p = mock.patch.object(painel_agentes, "_carregar_config", return_value=config)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(painel_agentes, "checar_reagendamento_dia_fixo",
                              return_value={"ok": True, "fora_do_dia": [{"service_id": 1, "valor": None}]})
        self.checar = p.start()
        self.addCleanup(p.stop)
        painel_agentes.app.config["TESTING"] = True
        self.cliente = painel_agentes.app.test_client()

    def _logar(self, nivel):
        with self.cliente.session_transaction() as sess:
            sess["nivel_acesso"] = nivel
            sess["usuario"] = "teste"

    def _post(self, corpo=None, headers=ORIGEM):
        return self.cliente.post(self.URL, json=corpo or {"service_ids": [1, "2"], "data": QUINTA}, headers=headers)

    def test_operador_e_total_recebem_a_lista(self):
        for nivel in ("operador", "total"):
            self._logar(nivel)
            r = self._post()
            self.assertEqual(r.status_code, 200, nivel)
            self.assertEqual(r.get_json()["fora_do_dia"], [{"service_id": 1, "valor": None}])
        self.checar.assert_called_with([1, 2], QUINTA)

    def test_leitura_nao_pode(self):
        self._logar("leitura")
        self.assertEqual(self._post().status_code, 403)
        self.checar.assert_not_called()

    def test_sem_sessao_401(self):
        self.assertEqual(self._post().status_code, 401)

    def test_sem_origem_bloqueia(self):
        self._logar("operador")
        self.assertEqual(self._post(headers={}).status_code, 403)

    def test_erro_de_entrada_400(self):
        self._logar("operador")
        self.assertEqual(self._post({"service_ids": ["x"], "data": QUINTA}).status_code, 400)
        self.checar.return_value = {"ok": False, "erro": "Data inválida: 08/10"}
        self.assertEqual(self._post({"service_ids": [1], "data": "08/10"}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `py -3.11 -m unittest painel_agentes.test_reagendar_fora_dia_fixo -v`
Expected: ERROR (`AttributeError: ... 'checar_reagendamento_dia_fixo'`).

- [ ] **Step 3: Função em `painel_agentes/planejamento_rotas.py`**

3a. Trocar a linha de import:

```python
from regioes_dia_fixo import DIAS_NOMES, extrair_cidade, nomes_dias, regiao_da_cidade, regra_dia_fixo_do_servico
```

por

```python
from regioes_dia_fixo import (DIAS_NOMES, data_valida_na_regiao, descricao_dias, extrair_cidade, nomes_dias,
                              regiao_da_cidade, regra_dia_fixo_do_servico)
```

3b. Logo depois de `reagendar_pedidos` (antes de `def _gravar_endereco_pedido`), acrescentar:

```python
def checar_reagendamento_dia_fixo(service_ids: list[int], data: str) -> dict:
    """
    Reagendamento pela equipe fora do dia de visita (Hugo, 03/10 -- spec
    dias fixos v2, 5.4): antes de salvar, a tela pergunta se o pedido vira
    dedicado. Devolve {"ok": True, "fora_do_dia": [{service_id, codigo,
    regiao, dias, valor}]} só com os pedidos de região de dia fixo em que
    `data` (AAAA-MM-DD) não é dia de visita. `valor` vem da calculadora de
    frete dedicado (roteirizacao/fora_dia_fixo.calcular_valor, km em linha
    reta) ou é None quando não dá pra calcular -- o modal de dedicado abre
    vazio pra digitar. Pedido que não carrega da Vuupt fica de fora (nunca
    trava o reagendamento). Só lê: nada é gravado aqui.
    """
    try:
        alvo = date.fromisoformat(str(data))
    except ValueError:
        return {"ok": False, "erro": f"Data inválida: {data}"}
    if not service_ids:
        return {"ok": True, "fora_do_dia": []}
    import fora_dia_fixo
    import pedidos_dedicados
    config = _carregar_config()
    vuupt = VuuptClient(config.get("vuupt_api", {}).get("token", ""))
    tipos = fora_dia_fixo.carregar_tipos_carga(rascunhos_rota.DB_PATH)
    fora = []
    for sid in service_ids:
        try:
            servico = vuupt.buscar_servico_por_id(int(sid))
        except Exception as e:
            logger.warning(f"Checagem de dia fixo: serviço {sid} não carregou ({e}); segue sem a pergunta.")
            continue
        if not servico:
            continue
        regra = regra_dia_fixo_do_servico(servico)
        if not regra or data_valida_na_regiao(regra, alvo):
            continue
        try:
            valor = fora_dia_fixo.calcular_valor(servico, config, tipos)
        except Exception as e:
            logger.warning(f"Checagem de dia fixo: calculadora falhou no serviço {sid} ({e}).")
            valor = None
        codigos = pedidos_dedicados.codigos_do_servico(servico)
        fora.append({"service_id": int(sid), "codigo": codigos[0] if codigos else "",
                     "regiao": regra.get("regiao") or regra["nome"], "dias": descricao_dias(regra),
                     "valor": valor})
    return {"ok": True, "fora_do_dia": fora}
```

(`date`, `VuuptClient`, `rascunhos_rota` e `logger` já existem no módulo; conferir com `grep -n "^from datetime\|^import rascunhos_rota\|VuuptClient" painel_agentes/planejamento_rotas.py | head`.)

- [ ] **Step 4: Rota em `painel_agentes/painel_agentes.py`**

4a. No `from planejamento_rotas import (...)`, trocar a linha `    reagendar_pedidos, editar_endereco_pedido, editar_endereco_pedidos,` por:

```python
    reagendar_pedidos, checar_reagendamento_dia_fixo, editar_endereco_pedido, editar_endereco_pedidos,
```

4b. Logo depois da função `api_reagendar_pedidos` (antes de `@app.route("/api/planejamento/editar-endereco"`), acrescentar:

```python
@app.route("/api/planejamento/checar-dia-fixo", methods=["POST"])
@requer_auth(niveis=("total", "operador"))
@exige_mesma_origem
def api_checar_dia_fixo():
    """Antes de reagendar (um ou lote): quais pedidos ficam fora do dia de
    visita da região, com o valor da calculadora de frete dedicado (Hugo,
    03/10 -- spec dias fixos v2, 5.4). Mesma permissão do reagendar. Só lê."""
    body = request.get_json(force=True, silent=True) or {}
    try:
        service_ids = [int(s) for s in body.get("service_ids") or []]
    except (TypeError, ValueError) as e:
        return jsonify({"erro": str(e)}), 400
    try:
        resultado = checar_reagendamento_dia_fixo(service_ids, str(body.get("data") or ""))
    except Exception as e:
        logging.getLogger(__name__).exception("Falha ao checar dia fixo do reagendamento")
        return jsonify({"erro": str(e)}), 500
    if not resultado["ok"]:
        return jsonify({"erro": resultado["erro"]}), 400
    return jsonify(resultado)
```

- [ ] **Step 5: Rodar e ver passar**

```bash
py -3.11 -m unittest painel_agentes.test_reagendar_fora_dia_fixo -v 2>&1 | tail -3
```

Expected: `OK`, 10 testes.

- [ ] **Step 6: Modal novo no template**

Em `painel_agentes/templates/planejamento_rotas.html`, logo depois do `</div>` que fecha `<div class="modal-overlay-reagendar" id="modal-reagendar">`, inserir:

```html
{# Reagendamento fora do dia de visita da região (Hugo, 03/10 -- dias fixos v2,
   spec 5.4): pergunta antes de salvar. Sim = salva a data e abre o modal de
   dedicado com o valor da calculadora; Não = salva normal (fica como EQUIPE). #}
<div class="modal-overlay-reagendar" id="modal-fora-dia-fixo">
  <div class="modal-reagendar" role="dialog" aria-modal="true" aria-labelledby="titulo-modal-fora-dia-fixo">
    <h2 id="titulo-modal-fora-dia-fixo">Fora do dia de visita da região</h2>
    <p class="subtitulo" id="fora-dia-fixo-texto"></p>
    <ul id="fora-dia-fixo-lista" style="margin:0 0 10px; padding-left:18px; font-size:12.5px; max-height:160px; overflow:auto;"></ul>
    <div class="acoes">
      <button type="button" class="cancelar" id="botao-fora-dia-fixo-nao">Não, só reagendar</button>
      <button type="button" class="confirmar" id="botao-fora-dia-fixo-sim">Sim, marcar como dedicado</button>
    </div>
  </div>
</div>
```

- [ ] **Step 7: Fluxo do reagendar no JS**

7a. Substituir a função `async function confirmarReagendamento(botao) { ... }` inteira (do `async function confirmarReagendamento(botao) {` até a `}` que a fecha, logo antes do comentário `// pedido (ou lote, ver abrirModalEditarEnderecoLote) tendo o endereço`) por:

```js
  // Reagendamento fora do dia de visita (Hugo, 03/10 -- dias fixos v2, spec
  // 5.4): antes de salvar, pergunta se os pedidos fora do dia viram dedicado.
  // Falha na checagem não trava o reagendamento (segue sem a pergunta).
  let foraDiaFixoPendente = null;  // { fora: [{service_id, codigo, regiao, dias, valor}], botao }

  async function checarForaDiaFixo(serviceIds, data) {
    try {
      const resp = await postJSON("/api/planejamento/checar-dia-fixo", { service_ids: serviceIds, data });
      return resp.fora_do_dia || [];
    } catch (e) {
      console.warn("Checagem de dia fixo falhou; reagendando sem a pergunta.", e);
      return [];
    }
  }

  function perguntarForaDiaFixo(fora, botao) {
    const texto = fora.length === 1
      ? `Este pedido é de ${fora[0].regiao}, com visita só às ${fora[0].dias}. Marcar como dedicado?`
      : `Estes ${fora.length} pedidos ficam fora do dia de visita da região. Marcar como dedicado? Os demais selecionados são reagendados normalmente.`;
    document.getElementById("fora-dia-fixo-texto").textContent = texto;
    document.getElementById("fora-dia-fixo-lista").innerHTML = fora.length > 1
      ? fora.map(f => `<li>${escaparHtml(f.codigo || "#" + f.service_id)} — ${escaparHtml(f.regiao)}: só ${escaparHtml(f.dias)}</li>`).join("")
      : "";
    foraDiaFixoPendente = { fora, botao };
    document.getElementById("modal-fora-dia-fixo").classList.add("aberto");
  }

  async function salvarReagendamento(pendente, data, horaInicio, horaFim) {
    if (pendente.itens) {
      const resp = await postJSON("/api/planejamento/reagendar-pedidos", {
        itens: pendente.itens, data, hora_inicio: horaInicio, hora_fim: horaFim,
      });
      return resp.falhas || [];
    }
    await postJSON("/api/planejamento/reagendar-pedido", {
      service_id: pendente.serviceId, data, hora_inicio: horaInicio, hora_fim: horaFim,
    });
    return [];
  }

  // marcarDedicado: null = ainda não perguntou; [] = "Não"; lista = "Sim" (pedidos fora do dia)
  async function confirmarReagendamento(botao, marcarDedicado = null) {
    const pendente = reagendarPendente;
    if (!pendente) return;
    const data = document.getElementById("reagendar-data").value;
    const horaInicio = document.getElementById("reagendar-hora-inicio").value;
    const horaFim = document.getElementById("reagendar-hora-fim").value;
    if (!data || !horaInicio || !horaFim) { alert("Preencha data e a janela de horário."); return; }
    const original = botao.textContent;
    botao.disabled = true;
    botao.textContent = "Salvando...";
    try {
      if (marcarDedicado === null) {
        const ids = pendente.itens ? pendente.itens.map(it => it.service_id) : [pendente.serviceId];
        const fora = await checarForaDiaFixo(ids, data);
        if (fora.length) {
          botao.disabled = false;
          botao.textContent = original;
          perguntarForaDiaFixo(fora, botao);
          return;
        }
      }
      const falhas = await salvarReagendamento(pendente, data, horaInicio, horaFim);
      document.getElementById("modal-reagendar").classList.remove("aberto");
      if (falhas.length) {
        alert(`Agendamento aplicado, mas ${falhas.length} de ${pendente.itens.length} pedido(s) falharam:\n` +
          falhas.map(f => `#${f.service_id}: ${f.erro}`).join("\n"));
      }
      if (marcarDedicado && marcarDedicado.length) {
        // só os que a Vuupt aceitou viram dedicado
        const falhou = new Set(falhas.map(f => f.service_id));
        const aceitos = marcarDedicado.filter(f => !falhou.has(f.service_id));
        if (aceitos.length) { abrirModalDedicadoForaDia(aceitos); return; }
      }
      // recarrega -- o novo agendado_para pode mudar a seção/urgência
      // do card (mesmo padrão de cancelarPedido)
      location.reload();
    } catch (e) {
      alert(`Falha ao agendar: ${e.message}`);
      botao.disabled = false;
      botao.textContent = original;
    }
  }
```

7b. Logo depois de `let dedicadoPendente = null;  // { itens: [...] }`, acrescentar:

```js
  // reagendamento fora do dia (03/10): a data já foi salva quando o modal de
  // dedicado abre por ele -- fechar sem marcar precisa recarregar a tela.
  let recarregarAoFecharDedicado = false;
```

7c. Logo depois da função `abrirModalDedicado`, acrescentar:

```js
  // Sim no "Fora do dia de visita" (03/10): abre o modal de dedicado com os
  // pedidos fora do dia e o valor da calculadora já somado (editável). Valor
  // que não deu pra calcular deixa o campo vazio pra digitar.
  function abrirModalDedicadoForaDia(fora) {
    dedicadoPendente = null;
    abrirModalDedicado(fora.map(f => f.service_id));
    if (!dedicadoPendente) { location.reload(); return; }
    const valores = fora.map(f => f.valor);
    if (valores.every(v => typeof v === "number" && v > 0)) {
      document.getElementById("dedicado-valor").value = valores.reduce((a, b) => a + b, 0).toFixed(2).replace(".", ",");
    }
    atualizarPreviaDedicado();
    recarregarAoFecharDedicado = true;
  }
```

7d. Nos listeners do fim da página, trocar

```js
    document.getElementById("botao-cancelar-dedicado").addEventListener("click", () => {
      document.getElementById("modal-dedicado").classList.remove("aberto");
      dedicadoPendente = null;
    });
```

por

```js
    document.getElementById("botao-cancelar-dedicado").addEventListener("click", () => {
      document.getElementById("modal-dedicado").classList.remove("aberto");
      dedicadoPendente = null;
      if (recarregarAoFecharDedicado) location.reload();  // a data já foi salva (reagendamento fora do dia)
    });
    document.getElementById("botao-fora-dia-fixo-nao").addEventListener("click", () => {
      const p = foraDiaFixoPendente;
      foraDiaFixoPendente = null;
      document.getElementById("modal-fora-dia-fixo").classList.remove("aberto");
      if (p) confirmarReagendamento(p.botao, []);
    });
    document.getElementById("botao-fora-dia-fixo-sim").addEventListener("click", () => {
      const p = foraDiaFixoPendente;
      foraDiaFixoPendente = null;
      document.getElementById("modal-fora-dia-fixo").classList.remove("aberto");
      if (p) confirmarReagendamento(p.botao, p.fora);
    });
```

O listener existente `(e) => confirmarReagendamento(e.target)` não muda: sem o segundo argumento, `marcarDedicado` vale `null` e a checagem roda.

- [ ] **Step 8: Rodar e conferir o template**

```bash
py -3.11 -m unittest discover -s painel_agentes -p "test_*.py" 2>&1 | tail -3
grep -c "modal-fora-dia-fixo\|checarForaDiaFixo\|abrirModalDedicadoForaDia\|recarregarAoFecharDedicado" painel_agentes/templates/planejamento_rotas.html
```

Expected: `OK`; o grep conta pelo menos 9 ocorrências.

- [ ] **Step 9: Conferir na tela (porta de teste, nunca a 8070)**

```bash
cd painel_agentes && py -3.11 -c "import painel_agentes; painel_agentes.app.run(host='127.0.0.1', port=8099)"
```

Logar como operador, abrir o Planejamento de uma data de hoje em diante, "Agendar / reagendar" num pedido de Campinas e escolher uma quinta. Esperado: abre "Fora do dia de visita da região" com "Este pedido é de Campinas, com visita só às Quartas. Marcar como dedicado?"; "Não" salva e recarrega; "Sim" salva e abre "Marcar como dedicado" com o valor preenchido. ATENÇÃO: o local fala com a Vuupt de verdade. Só fazer com um pedido de teste combinado com o Hugo; sem isso, parar no popup (fechar o servidor antes de clicar Sim/Não) e registrar. Encerrar o servidor de teste (Ctrl+C) sem tocar em processos que já existiam.

- [ ] **Step 10: Commit**

```bash
git add painel_agentes/planejamento_rotas.py painel_agentes/painel_agentes.py painel_agentes/templates/planejamento_rotas.html painel_agentes/test_reagendar_fora_dia_fixo.py
git commit -m "Planejamento: reagendar fora do dia de visita pergunta se marca dedicado, com valor da calculadora

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: WhatsApp ao embarcador (texto e envio)

**Files:**
- Modify: `notificar_whatsapp.py`
- Test: `test_whatsapp_fora_dia_fixo.py`

**Interfaces:**
- Consumes: `MAX_MENSAGEM`, `ORIGEM_CLIENTE`, `clientes_ligado`, `_cfg`, `_cfg_clientes`, `despachar`, `integracao_openwa.numero_existe` (já existem).
- Produces:
  - `texto_fora_dia_fixo(codigo: str, data: date, regiao: str, dias: str, valor: float | None) -> str` (até 200 caracteres; `valor=None` = "valor a confirmar")
  - `avisar_cliente_fora_dia_fixo(codigo: str, telefone: str, texto: str, config: dict, modo_teste: bool = False, **kw) -> str` (`desligado | modo_teste | indeterminado | numero_sem_whatsapp | nao_enviado | enviado | falhou`)

- [ ] **Step 1: Escrever os testes**

Criar `test_whatsapp_fora_dia_fixo.py` (raiz):

```python
# -*- coding: utf-8 -*-
"""
WhatsApp ao embarcador: data fora do dia de visita da região (dias fixos v2,
Hugo 03/10). Rodar (da raiz): py -3.11 -m unittest test_whatsapp_fora_dia_fixo -v
"""
import sqlite3
import sys
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

import notificar_whatsapp as nw

DATA = date(2026, 10, 8)
CONFIG = {"whatsapp_notificacoes": {"ativo": True, "base_url": "http://x", "api_key": "k", "sessao": "s",
                                    "grupo_id": "g@g.us", "intervalo_min_seg": 0,
                                    "clientes": {"ativo": True, "teto_diario": 30, "forcar_destino": ""}}}


class TestTexto(unittest.TestCase):
    def test_texto_completo(self):
        t = nw.texto_fora_dia_fixo("PS-40316", DATA, "Campinas", "Quartas", 784.09)
        self.assertLessEqual(len(t), nw.MAX_MENSAGEM)
        for trecho in ("PS-40316", "08/10", "Campinas", "Quartas", "envio dedicado", "R$ 784,09", "portal"):
            self.assertIn(trecho, t)

    def test_texto_longo_encurta_e_mantem_o_essencial(self):
        t = nw.texto_fora_dia_fixo("#PS-40316-R12", DATA, "Vale do Paraíba", "Segundas, Quartas e Sextas", 12345.67)
        self.assertLessEqual(len(t), nw.MAX_MENSAGEM)
        for trecho in ("PS-40316-R12", "08/10", "envio dedicado", "R$ 12.345,67"):
            self.assertIn(trecho, t)
        self.assertNotIn("#", t)

    def test_valor_pendente(self):
        self.assertIn("valor a confirmar", nw.texto_fora_dia_fixo("PS-1", DATA, "Campinas", "Quartas", None))


class TestEnvio(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        for alvo, valor in (("configurado", lambda cfg: True), ("numero_existe", lambda cfg, n: True)):
            p = mock.patch.object(nw.integracao_openwa, alvo, valor)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(nw.integracao_openwa, "enviar_texto", return_value=(True, "m1"))
        self.enviar = p.start()
        self.addCleanup(p.stop)

    def _avisar(self, config=CONFIG, **kw):
        return nw.avisar_cliente_fora_dia_fixo("PS-1", "5511988887777", "texto", config, conn=self.conn,
                                               agora=datetime(2026, 10, 6, 18, 0), dormir=lambda s: None, **kw)

    def test_desligado_sem_canal_de_clientes(self):
        config = {"whatsapp_notificacoes": {**CONFIG["whatsapp_notificacoes"], "clientes": {"ativo": False}}}
        self.assertEqual(self._avisar(config), "desligado")
        self.enviar.assert_not_called()

    def test_envia_pro_numero_do_embarcador(self):
        self.assertEqual(self._avisar(), "enviado")
        self.assertEqual(self.enviar.call_args[0][1:], ("5511988887777@c.us", "texto"))
        tipo = self.conn.execute("SELECT origem, tipo, assinatura FROM notificacoes_whatsapp").fetchone()
        self.assertEqual(tipo, (nw.ORIGEM_CLIENTE, "fora_dia_fixo", "fora_dia_fixo:PS-1"))

    def test_forcar_destino_desvia_e_mostra_o_destino_real(self):
        cfg = {"whatsapp_notificacoes": {**CONFIG["whatsapp_notificacoes"],
                                         "clientes": {"ativo": True, "forcar_destino": "5511999990000"}}}
        self.assertEqual(self._avisar(cfg), "enviado")
        destino, texto = self.enviar.call_args[0][1:]
        self.assertEqual(destino, "5511999990000@c.us")
        self.assertTrue(texto.startswith("[teste → +5511988887777]"))

    def test_teto_diario_dos_clientes(self):
        cfg = {"whatsapp_notificacoes": {**CONFIG["whatsapp_notificacoes"],
                                         "clientes": {"ativo": True, "teto_diario": 0, "forcar_destino": ""}}}
        self.assertEqual(self._avisar(cfg), "nao_enviado")
        self.enviar.assert_not_called()

    def test_numero_sem_whatsapp(self):
        with mock.patch.object(nw.integracao_openwa, "numero_existe", lambda cfg, n: False):
            self.assertEqual(self._avisar(), "numero_sem_whatsapp")
        self.enviar.assert_not_called()


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `py -3.11 -m unittest test_whatsapp_fora_dia_fixo -v`
Expected: `AttributeError: module 'notificar_whatsapp' has no attribute 'texto_fora_dia_fixo'`.

- [ ] **Step 3: Implementar em `notificar_whatsapp.py`**

3a. No docstring do topo, depois do bloco "Origem PARA FORA da empresa ... avisar_cliente_sem_resposta", acrescentar:

```
Mesma origem de cliente (Hugo, 03/10/2026, dias fixos v2): pedido com data
fora do dia de visita da regiao virou envio dedicado. Um aviso por pedido
(quem garante e roteirizacao/registro_dia_fixo.py), mesmo teto e mesmo
forcar_destino dos clientes.
    roteirizacao/notificar_fora_dia_fixo.py -> avisar_cliente_fora_dia_fixo
```

3b. Logo depois de `texto_cliente_sem_resposta`, acrescentar:

```python
def texto_fora_dia_fixo(codigo: str, data, regiao: str, dias: str, valor: float | None) -> str:
    """Pro embarcador, ate MAX_MENSAGEM. Encurta tirando primeiro a regiao e
    os dias, depois o valor -- codigo, data e "envio dedicado" ficam sempre."""
    codigo = str(codigo or "").lstrip("#")
    quando = data.strftime("%d/%m")
    if valor is None:
        preco = "valor a confirmar"
    else:
        preco = "R$ " + f"{float(valor):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    fim = "Para outra data, responda pelo portal."
    opcoes = [
        f"Fresh Log: o pedido {codigo} está para {quando}, fora dos dias de visita da região {regiao} ({dias}). "
        f"Vai como envio dedicado, {preco}. {fim}",
        f"Fresh Log: o pedido {codigo} está para {quando}, fora dos dias de visita da região. "
        f"Vai como envio dedicado, {preco}. {fim}",
        f"Fresh Log: o pedido {codigo} está para {quando}, fora dos dias de visita da região. "
        f"Vai como envio dedicado. {fim}",
    ]
    return next((t for t in opcoes if len(t) <= MAX_MENSAGEM), opcoes[-1][:MAX_MENSAGEM])
```

3c. Logo depois de `avisar_cliente_sem_resposta`, acrescentar:

```python
def avisar_cliente_fora_dia_fixo(codigo: str, telefone: str, texto: str, config: dict,
                                 modo_teste: bool = False, **kw) -> str:
    """Aviso direto ao embarcador (roteirizacao/notificar_fora_dia_fixo.py):
    data escolhida fora do dia de visita virou envio dedicado. Sem janela de
    repeticao (origem de cliente): quem chama garante um aviso por pedido.
    Com clientes.forcar_destino a mensagem vai pra esse numero com o destino
    real na primeira linha."""
    try:
        telefone = re.sub(r"\D", "", str(telefone or ""))
        if not clientes_ligado(config) or not telefone:
            return "desligado"
        destino = re.sub(r"\D", "", str(_cfg_clientes(config).get("forcar_destino") or ""))
        if destino:
            texto = f"[teste → +{telefone}]\n{texto}"
        else:
            destino = telefone
        if not modo_teste:
            existe = integracao_openwa.numero_existe(_cfg(config), destino)
            if existe is None:
                logger.warning(f"WhatsApp nao enviado (fora do dia fixo {codigo}): gateway nao confirmou o numero.")
                return "indeterminado"
            if not existe:
                logger.info(f"WhatsApp nao enviado (fora do dia fixo {codigo}): numero sem WhatsApp.")
                return "numero_sem_whatsapp"
        return despachar(config, ORIGEM_CLIENTE, "fora_dia_fixo", texto, f"fora_dia_fixo:{codigo}",
                         modo_teste=modo_teste, grupo_id=f"{destino}@c.us", **kw)
    except Exception as exc:
        logger.warning(f"Falha na notificacao por WhatsApp (nao afeta a rotina): {exc}")
        return "falhou"
```

- [ ] **Step 4: Rodar e ver passar**

```bash
py -3.11 -m unittest test_whatsapp_fora_dia_fixo test_notificar_whatsapp -v 2>&1 | tail -3
```

Expected: `OK`.

- [ ] **Step 5: Commit**

```bash
git add notificar_whatsapp.py test_whatsapp_fora_dia_fixo.py
git commit -m "WhatsApp: aviso ao embarcador de data fora do dia de visita, ate 200 caracteres

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Aviso ao embarcador e ponto de chamada `tratar_fora_dia_fixo`

**Files:**
- Create: `roteirizacao/notificar_fora_dia_fixo.py`
- Modify: `roteirizacao/fora_dia_fixo.py`
- Test: `roteirizacao/test_notificar_fora_dia_fixo.py`

**Interfaces:**
- Consumes: itens de `fora_dia_fixo.pendentes_de_aviso` (Task 4); `registro_dia_fixo.conectar`, `registrar_aviso`, `DB_PATH` (Task 3); `regioes_dia_fixo.descricao_dias` (Task 1); `notificar_whatsapp.texto_fora_dia_fixo`, `avisar_cliente_fora_dia_fixo` (Task 5); `email_utils.envelope_html`, `enviar_email(destinos, assunto, corpo, config_email) -> bool`, `notificacoes_automaticas_ativas(config) -> bool`, cores `COR_*`; `preferencias_notificacao.carregar_embarcadores("agendamento", db_path=...) -> {sender_id: {"nome", "emails", "cnpj", "desligado"}}`, `whatsapp_do_embarcador(conn, cnpj) -> str | None`.
- Produces:
  - `notificar_fora_dia_fixo.EMAIL_TESTE`, `forcar_destino_do_config(config) -> str`
  - `notificar_fora_dia_fixo.avisar(itens: list[dict], config: dict, db_path=None, embarcadores: dict | None = None) -> dict` (chaves `emails, falhas, whatsapp, registrados, desligado`)
  - `fora_dia_fixo.tratar_fora_dia_fixo(servicos, config, modo_teste=False, hoje=None, db_path=None) -> dict` (nunca levanta)

- [ ] **Step 1: Escrever os testes**

Criar `roteirizacao/test_notificar_fora_dia_fixo.py`:

```python
# -*- coding: utf-8 -*-
"""
Aviso ao embarcador de data fora do dia de visita (dias fixos v2, Hugo
03/10) e o ponto de chamada tratar_fora_dia_fixo.
Rodar (da raiz): py -3.11 -m unittest roteirizacao.test_notificar_fora_dia_fixo -v
"""
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import fora_dia_fixo as fdf
import notificar_fora_dia_fixo as nfdf
import pedidos_dedicados
import registro_dia_fixo as reg
from regioes_dia_fixo import regra_dia_fixo_do_servico

CAMPINAS = "Rua Barão de Jaguara 900, Centro, Campinas - SP, 13015-001, Brasil"
HOJE = date(2026, 10, 6)
CONFIG = {"notificacoes_automaticas": {"ativo": True}, "email": {}}
EMBS = {7: {"nome": "EMB TESTE", "emails": ["cliente@emb.com"], "cnpj": "12345678000195", "desligado": False}}


def _servico(i=1, data="2026-10-08"):
    return {"id": i, "code": f"#PS-{1000 + i}", "address": CAMPINAS, "sender_id": 7, "dimension_3": 5,
            "latitude": -22.905, "longitude": -47.060, "created_at": "2026-10-06 15:00:00",
            "title": f"Cliente {i}", "scheduled_start": f"{data}T08:00:00-03:00"}


def _item(i=1, valor=784.09, pendente=False):
    s = _servico(i)
    return {"servico": s, "regra": regra_dia_fixo_do_servico(s), "data": date(2026, 10, 8),
            "valor": valor, "valor_pendente": pendente}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "t.db"
        p = mock.patch.object(nfdf, "enviar_email", return_value=True)
        self.email = p.start()
        self.addCleanup(p.stop)

    def _avisado(self, i=1):
        conn = reg.conectar(self.db)
        try:
            return reg.ja_avisado(conn, _servico(i))
        finally:
            conn.close()

    def _canais(self, i=1):
        conn = reg.conectar(self.db)
        try:
            return conn.execute("SELECT canais FROM avisos_fora_dia_fixo WHERE codigo = ?", (f"PS-{1000 + i}",)).fetchone()[0]
        finally:
            conn.close()


class TestAvisar(Base):
    def test_um_email_por_embarcador_vai_pro_hugo_no_piloto_e_registra(self):
        r = nfdf.avisar([_item(1), _item(2)], CONFIG, db_path=self.db, embarcadores=EMBS)
        self.assertEqual((r["emails"], r["registrados"]), (1, 2))
        self.assertEqual(self.email.call_count, 1)
        self.assertEqual(self.email.call_args[0][0], [nfdf.EMAIL_TESTE])
        self.assertIn("iria para cliente@emb.com", self.email.call_args[0][2])
        self.assertTrue(self._avisado(1) and self._avisado(2))
        self.assertEqual(self._canais(1), "email")

    def test_forcar_destino_vazio_vai_pro_embarcador(self):
        config = {**CONFIG, "fora_dia_fixo": {"forcar_destino": ""}}
        nfdf.avisar([_item()], config, db_path=self.db, embarcadores=EMBS)
        self.assertEqual(self.email.call_args[0][0], ["cliente@emb.com"])
        self.assertNotIn("iria para", self.email.call_args[0][2])

    def test_corpo_tem_data_dias_regiao_e_valor(self):
        nfdf.avisar([_item()], CONFIG, db_path=self.db, embarcadores=EMBS)
        corpo = self.email.call_args[0][2]
        for trecho in ("#PS-1001", "08/10/2026", "Campinas", "Quartas", "R$ 784,09", "envio dedicado"):
            self.assertIn(trecho, corpo)

    def test_valor_pendente_aparece_a_confirmar(self):
        nfdf.avisar([_item(valor=0.0, pendente=True)], CONFIG, db_path=self.db, embarcadores=EMBS)
        self.assertIn("a confirmar", self.email.call_args[0][2])

    def test_falha_de_email_nao_registra(self):
        self.email.return_value = False
        r = nfdf.avisar([_item()], CONFIG, db_path=self.db, embarcadores=EMBS)
        self.assertEqual((r["falhas"], r["registrados"]), (1, 0))
        self.assertFalse(self._avisado())

    def test_notificacoes_desligadas_nao_envia_nem_registra(self):
        config = {"notificacoes_automaticas": {"ativo": False}, "email": {}}
        r = nfdf.avisar([_item()], config, db_path=self.db, embarcadores=EMBS)
        self.assertEqual(r["desligado"], 1)
        self.email.assert_not_called()
        self.assertFalse(self._avisado())

    def test_embarcador_sem_email_registra_nenhum(self):
        nfdf.avisar([_item()], CONFIG, db_path=self.db, embarcadores={})
        self.email.assert_not_called()
        self.assertEqual(self._canais(), "nenhum")

    def test_embarcador_que_desligou_nao_recebe_email(self):
        embs = {7: {**EMBS[7], "desligado": True}}
        nfdf.avisar([_item()], CONFIG, db_path=self.db, embarcadores=embs)
        self.email.assert_not_called()

    def test_whatsapp_quando_tem_numero(self):
        with mock.patch.object(nfdf.preferencias_notificacao, "whatsapp_do_embarcador", return_value="5511988887777"), \
             mock.patch.object(nfdf.notificar_whatsapp, "avisar_cliente_fora_dia_fixo", return_value="enviado") as wpp:
            r = nfdf.avisar([_item()], CONFIG, db_path=self.db, embarcadores=EMBS)
        codigo, telefone, texto, _config = wpp.call_args[0]
        self.assertEqual((codigo, telefone), ("PS-1001", "5511988887777"))
        self.assertLessEqual(len(texto), 200)
        self.assertEqual(r["whatsapp"], 1)
        self.assertEqual(self._canais(), "email,whatsapp")

    def test_email_falhou_mas_whatsapp_saiu_registra(self):
        self.email.return_value = False
        with mock.patch.object(nfdf.preferencias_notificacao, "whatsapp_do_embarcador", return_value="5511988887777"), \
             mock.patch.object(nfdf.notificar_whatsapp, "avisar_cliente_fora_dia_fixo", return_value="enviado"):
            nfdf.avisar([_item()], CONFIG, db_path=self.db, embarcadores=EMBS)
        self.assertEqual(self._canais(), "whatsapp")


class TestTratar(Base):
    def setUp(self):
        super().setUp()
        for alvo, obj, valor in (("DETECCAO_A_PARTIR_DE", fdf, date(2026, 10, 6)),
                                 ("calcular_valor", fdf, lambda s, c, t: 784.09)):
            p = mock.patch.object(obj, alvo, valor)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(nfdf.preferencias_notificacao, "carregar_embarcadores", return_value=EMBS)
        p.start()
        self.addCleanup(p.stop)

    def _tratar(self, **kw):
        return fdf.tratar_fora_dia_fixo([_servico()], CONFIG, hoje=HOJE, db_path=self.db, **kw)

    def test_tratar_duas_vezes_marca_e_avisa_uma_vez(self):
        self.assertEqual(self._tratar()["marcados"], 1)
        self.assertEqual(self._tratar()["marcados"], 0)
        self._tratar()
        self.assertEqual(self.email.call_count, 1)
        conn = pedidos_dedicados.conectar(self.db)
        try:
            n = conn.execute("SELECT COUNT(*) FROM pedidos_dedicados").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(n, 1)

    def test_aviso_que_falhou_sai_na_rodada_seguinte(self):
        self.email.return_value = False
        self._tratar()
        self.assertFalse(self._avisado())
        self.email.return_value = True
        self._tratar()
        self.assertTrue(self._avisado())
        self._tratar()
        self.assertEqual(self.email.call_count, 2)

    def test_modo_teste_nao_marca_nem_avisa(self):
        r = self._tratar(modo_teste=True)
        self.assertEqual((r["marcados"], r["candidatos"]), (0, 1))
        self.email.assert_not_called()
        conn = pedidos_dedicados.conectar(self.db)
        try:
            self.assertEqual(pedidos_dedicados.ativos_por_codigo(conn), {})
        finally:
            conn.close()

    def test_nunca_levanta(self):
        with mock.patch.object(fdf, "marcar_fora_dia_fixo", side_effect=RuntimeError("banco travado")):
            self.assertIn("erro", self._tratar())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `py -3.11 -m unittest roteirizacao.test_notificar_fora_dia_fixo -v`
Expected: `ModuleNotFoundError: No module named 'notificar_fora_dia_fixo'`.

- [ ] **Step 3: Criar `roteirizacao/notificar_fora_dia_fixo.py`**

```python
# -*- coding: utf-8 -*-
"""
notificar_fora_dia_fixo.py

Aviso ao embarcador (Hugo, 03/10/2026 -- spec dias fixos v2, seção 5.2):
pedido com data escolhida fora do dia de visita da região virou envio
dedicado (fora_dia_fixo.py). Um aviso por pedido (registro_dia_fixo.
avisos_fora_dia_fixo), por e-mail (um por embarcador por rodada) e por
WhatsApp (notificar_whatsapp.avisar_cliente_fora_dia_fixo).

Chaves do config.yaml:
  notificacoes_automaticas.ativo  desligada -> nada sai, nada é registrado
  fora_dia_fixo.forcar_destino    ausente = hugo@ (piloto); "" = envio real
  whatsapp_notificacoes.clientes  ativo / forcar_destino / teto_diario
Destinatário do e-mail: preferências do portal, tipo "agendamento" (o mesmo
dos avisos de dia fixo; quem desligou não recebe e-mail).

Registro: grava quando pelo menos um canal saiu, ou quando o embarcador não
tem canal nenhum ("nenhum"). E-mail que falhou sem WhatsApp enviado não
grava: a próxima rodada tenta de novo.
"""
import html
import logging
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from email_utils import (  # noqa: E402
    envelope_html, enviar_email, notificacoes_automaticas_ativas,
    COR_ACENTO, COR_BORDA, COR_FUNDO, COR_PRIMARIA, COR_TEXTO,
)
from regioes_dia_fixo import descricao_dias  # noqa: E402
import notificar_whatsapp  # noqa: E402
import pedidos_dedicados  # noqa: E402
import preferencias_notificacao  # noqa: E402
import registro_dia_fixo  # noqa: E402

logger = logging.getLogger(__name__)

EMAIL_TESTE = "hugo@freshlogbr.com"


def forcar_destino_do_config(config: dict) -> str:
    """Ausente = hugo@ (piloto). Envio real exige forcar_destino: "" explícito."""
    secao = (config or {}).get("fora_dia_fixo") or {}
    if "forcar_destino" not in secao:
        return EMAIL_TESTE
    return str(secao.get("forcar_destino") or "").strip()


def _brl(valor) -> str:
    return "R$ " + f"{float(valor):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _codigo(servico: dict) -> str:
    codigos = pedidos_dedicados.codigos_do_servico(servico)
    return codigos[0] if codigos else str(servico.get("code") or "").lstrip("#")


def _regiao(regra: dict) -> str:
    return regra.get("regiao") or regra["nome"]


def _montar_conteudo(nome: str, itens: list[dict], iria_para: list[str] | None) -> str:
    faixa = ""
    if iria_para:
        faixa = (f'<p style="margin:0 0 16px 0;padding:8px 12px;background:{COR_FUNDO};border:1px dashed {COR_BORDA};'
                 f'font-size:12px;color:{COR_TEXTO};">PILOTO: este e-mail iria para '
                 f'{html.escape(", ".join(iria_para))}.</p>')
    celula = f"padding:8px 14px;border-bottom:1px solid {COR_BORDA};"
    linhas = "".join(f"""
    <tr>
      <td style="{celula}">{html.escape('#' + _codigo(i['servico']))}</td>
      <td style="{celula}">{html.escape((i['servico'].get('title') or '')[:50])}</td>
      <td style="{celula}"><strong>{i['data'].strftime('%d/%m/%Y')}</strong></td>
      <td style="{celula}">{html.escape(_regiao(i['regra']))} ({html.escape(descricao_dias(i['regra']))})</td>
      <td style="{celula}text-align:right;">{'a confirmar' if i['valor_pendente'] else _brl(i['valor'])}</td>
    </tr>""" for i in itens)
    return f"""
{faixa}
<p style="margin:0 0 4px 0;font-size:12px;font-weight:800;color:{COR_ACENTO};letter-spacing:0.5px;">
  DATA FORA DO DIA DE VISITA DA REGIÃO
</p>
<p style="margin:0 0 16px 0;font-size:20px;font-weight:800;color:{COR_PRIMARIA};">
  Pedidos tratados como envio dedicado
</p>
<p style="margin:0 0 20px 0;font-size:14px;color:{COR_TEXTO};line-height:1.6;">
  Olá, {html.escape(nome or '')}.<br>
  Os pedidos abaixo têm destino em regiões atendidas em <strong>dias fixos</strong>, e a data
  escolhida não é um dia de visita da região. Para cumprir a data, eles serão tratados como
  <strong>envio dedicado</strong>, com o valor estimado pela tabela de frete dedicado da Fresh Log.
</p>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"
      style="border:1px solid {COR_BORDA};border-radius:8px;overflow:hidden;">
<thead><tr style="background:{COR_FUNDO};">
<th style="padding:8px 14px;text-align:left;font-size:11px;color:{COR_PRIMARIA};">Pedido</th>
<th style="padding:8px 14px;text-align:left;font-size:11px;color:{COR_PRIMARIA};">Descrição</th>
<th style="padding:8px 14px;text-align:left;font-size:11px;color:{COR_PRIMARIA};">Data escolhida</th>
<th style="padding:8px 14px;text-align:left;font-size:11px;color:{COR_PRIMARIA};">Região (dias de visita)</th>
<th style="padding:8px 14px;text-align:right;font-size:11px;color:{COR_PRIMARIA};">Valor estimado</th>
</tr></thead><tbody>{linhas}</tbody></table>
<p style="margin:20px 0 0 0;font-size:14px;color:{COR_TEXTO};line-height:1.6;">
  Se preferir uma data dentro dos dias de visita da região, responda este e-mail ou fale com a
  equipe pelo portal do cliente.<br><br>
  Atenciosamente,<br><strong>Freshlog Logística</strong>
</p>
"""


def _texto_whatsapp(item: dict) -> str:
    return notificar_whatsapp.texto_fora_dia_fixo(
        _codigo(item["servico"]), item["data"], _regiao(item["regra"]), descricao_dias(item["regra"]),
        None if item["valor_pendente"] else item["valor"])


def avisar(itens: list[dict], config: dict, db_path=None, embarcadores: dict | None = None) -> dict:
    """Avisa os embarcadores dos itens (formato de fora_dia_fixo.pendentes_de_aviso)
    e registra o aviso. `embarcadores` só pra teste."""
    resultado = {"emails": 0, "falhas": 0, "whatsapp": 0, "registrados": 0, "desligado": 0}
    if not itens:
        return resultado
    if not notificacoes_automaticas_ativas(config):
        logger.info(f"{len(itens)} aviso(s) de data fora do dia fixo não enviados: notificações automáticas desligadas.")
        resultado["desligado"] = len(itens)
        return resultado
    db = db_path or registro_dia_fixo.DB_PATH
    if embarcadores is None:
        try:
            embarcadores = preferencias_notificacao.carregar_embarcadores("agendamento", db_path=db)
        except Exception as e:
            logger.warning(f"nao carregou os embarcadores ({e}); avisos sem e-mail")
            embarcadores = {}
    forcar = forcar_destino_do_config(config)
    grupos: dict = defaultdict(list)
    for item in itens:
        grupos[item["servico"].get("sender_id")].append(item)

    conn_reg = registro_dia_fixo.conectar(db)
    conn_pref = sqlite3.connect(str(db), timeout=10)
    try:
        for sender_id, grupo in grupos.items():
            emb = embarcadores.get(sender_id) or {}
            email_ok = email_falhou = False
            if emb.get("emails") and not emb.get("desligado"):
                destinos = [forcar] if forcar else emb["emails"]
                corpo = envelope_html(_montar_conteudo(emb.get("nome", ""), grupo, emb["emails"] if forcar else None),
                                      rodape="Mensagem automática — Agente Stokki Eventos.")
                assunto = f"[Freshlog] Data fora do dia de visita da região — {len(grupo)} pedido(s) como envio dedicado"
                if enviar_email(destinos, assunto, corpo, (config or {}).get("email", {})):
                    email_ok = True
                    resultado["emails"] += 1
                else:
                    email_falhou = True
                    resultado["falhas"] += 1
            telefone = None
            if emb.get("cnpj"):
                try:
                    telefone = preferencias_notificacao.whatsapp_do_embarcador(conn_pref, emb["cnpj"])
                except Exception as e:
                    logger.warning(f"nao leu o WhatsApp do embarcador {sender_id} ({e})")
            for item in grupo:
                canais = ["email"] if email_ok else []
                if telefone and notificar_whatsapp.avisar_cliente_fora_dia_fixo(
                        _codigo(item["servico"]), telefone, _texto_whatsapp(item), config) == "enviado":
                    canais.append("whatsapp")
                    resultado["whatsapp"] += 1
                if email_falhou and not canais:
                    continue  # tenta de novo na próxima rodada
                registro_dia_fixo.registrar_aviso(conn_reg, item["servico"], item["data"], canais)
                resultado["registrados"] += 1
    finally:
        conn_reg.close()
        conn_pref.close()
    return resultado
```

- [ ] **Step 4: Acrescentar `tratar_fora_dia_fixo` ao fim de `roteirizacao/fora_dia_fixo.py`**

```python
def tratar_fora_dia_fixo(servicos: list[dict], config: dict, modo_teste: bool = False,
                         hoje: date | None = None, db_path=None) -> dict:
    """Ponto de chamada do criar_rotas_diarias / incrementar_rotas: marca os
    pedidos novos e avisa os pendentes (inclusive de rodadas anteriores).
    Em modo teste só loga os candidatos. Nunca levanta."""
    try:
        hoje = hoje or date.today()
        if modo_teste:
            conn = registro_dia_fixo.conectar(db_path or registro_dia_fixo.DB_PATH)
            try:
                achados = [(s, *a) for s in servicos if (a := detectar(s, hoje, conn))]
            finally:
                conn.close()
            for s, regra, data in achados:
                logger.info(f"[MODO TESTE] {s.get('code')}: data {data:%d/%m} fora dos dias de "
                            f"{regra.get('regiao') or regra['nome']} -- seria marcado como dedicado (não marcado).")
            return {"marcados": 0, "candidatos": len(achados)}
        marcados = marcar_fora_dia_fixo(servicos, config, hoje=hoje, db_path=db_path)
        import notificar_fora_dia_fixo
        avisos = notificar_fora_dia_fixo.avisar(pendentes_de_aviso(servicos, db_path=db_path), config,
                                                db_path=db_path)
        if marcados or avisos.get("registrados") or avisos.get("falhas"):
            logger.info(f"Fora do dia fixo: {len(marcados)} marcado(s) como dedicado; avisos {avisos}.")
        return {"marcados": len(marcados), "avisos": avisos}
    except Exception as e:
        logger.error(f"Falha na regra de data fora do dia fixo (não afeta a roteirização): {e}")
        return {"erro": str(e)}
```

- [ ] **Step 5: Rodar e ver passar**

```bash
py -3.11 -m unittest roteirizacao.test_notificar_fora_dia_fixo roteirizacao.test_fora_dia_fixo -v 2>&1 | tail -3
```

Expected: `OK`.

- [ ] **Step 6: Commit**

```bash
git add roteirizacao/notificar_fora_dia_fixo.py roteirizacao/fora_dia_fixo.py roteirizacao/test_notificar_fora_dia_fixo.py
git commit -m "Dia fixo: aviso unico ao embarcador (e-mail e WhatsApp) do pedido que virou dedicado

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Chamada no job das 18h e no incremento

**Files:**
- Modify: `roteirizacao/criar_rotas_diarias.py` (só um bloco no `main`)
- Modify: `roteirizacao/incrementar_rotas.py` (só um bloco no `main`)
- Test: `roteirizacao/test_fora_dia_fixo_integracao.py`

**Interfaces:**
- Consumes: `fora_dia_fixo.tratar_fora_dia_fixo(servicos, config, modo_teste=...)` (Task 6).
- Produces: nada novo.

- [ ] **Step 1: Escrever o teste**

Criar `roteirizacao/test_fora_dia_fixo_integracao.py`:

```python
# -*- coding: utf-8 -*-
"""
A regra de data fora do dia fixo roda depois do dia fixo e antes de separar
os dedicados, no job das 18h e no incremento (dias fixos v2, Hugo 03/10).
Rodar (da raiz): py -3.11 -m unittest roteirizacao.test_fora_dia_fixo_integracao -v
"""
import inspect
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import criar_rotas_diarias as crd
import incrementar_rotas as inc


class OrdemDasChamadas(unittest.TestCase):
    def _confere(self, fonte: str):
        dia_fixo = fonte.index("aplicar_regioes_dia_fixo(")
        fora = fonte.index("tratar_fora_dia_fixo(")
        dedicados = fonte.index("separar_dedicados(")
        self.assertLess(dia_fixo, fora)
        self.assertLess(fora, dedicados)
        self.assertIn("modo_teste=modo_teste", fonte[fora:dedicados])

    def test_criar_rotas_diarias(self):
        self._confere(inspect.getsource(crd.main))

    def test_incrementar_rotas(self):
        self._confere(inspect.getsource(inc.main))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `py -3.11 -m unittest roteirizacao.test_fora_dia_fixo_integracao -v`
Expected: FAIL com `ValueError: substring not found` (ainda não há `tratar_fora_dia_fixo(`).

- [ ] **Step 3: Inserir o bloco em `roteirizacao/criar_rotas_diarias.py`**

Imediatamente ANTES da linha `        # Dedicado (Hugo, 23/09): transporte cotado à parte -- sai ANTES da` (dentro do `main`), inserir:

```python
        # Fora do dia fixo (Hugo, 03/10): data do embarcador fora do dia de
        # visita da região vira dedicado (sai da rota logo abaixo, em
        # separar_dedicados) e o embarcador é avisado uma vez.
        try:
            from fora_dia_fixo import tratar_fora_dia_fixo
            tratar_fora_dia_fixo(servicos_brutos, config, modo_teste=modo_teste)
        except Exception as e:
            logger.error(f"Falha na regra de data fora do dia fixo (não afeta a criação de rotas): {e}")

```

Nada mais muda neste arquivo.

- [ ] **Step 4: Inserir o bloco em `roteirizacao/incrementar_rotas.py`**

Imediatamente ANTES da linha `        # Dedicado (Hugo, 23/09): sai antes da checagem de área não atendida` (dentro do `main`), inserir:

```python
        # Fora do dia fixo (Hugo, 03/10): mesma regra do criar_rotas_diarias --
        # data do embarcador fora do dia de visita vira dedicado e é avisada.
        try:
            from fora_dia_fixo import tratar_fora_dia_fixo
            tratar_fora_dia_fixo(servicos, config, modo_teste=modo_teste)
        except Exception as e:
            logger.error(f"Falha na regra de data fora do dia fixo (não afeta o incremento): {e}")

```

- [ ] **Step 5: Rodar e ver passar**

```bash
py -3.11 -m unittest roteirizacao.test_fora_dia_fixo_integracao -v 2>&1 | tail -3
py -3.11 -m unittest discover -s roteirizacao -p "test_*.py" 2>&1 | tail -3
git diff --stat HEAD -- roteirizacao/criar_rotas_diarias.py roteirizacao/incrementar_rotas.py
```

Expected: `OK` nos dois; o diff mostra só `+9` linhas em cada arquivo.

- [ ] **Step 6: Commit**

```bash
git add roteirizacao/criar_rotas_diarias.py roteirizacao/incrementar_rotas.py roteirizacao/test_fora_dia_fixo_integracao.py
git commit -m "Roteirizacao: job das 18h e incremento aplicam a regra de data fora do dia fixo

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Planejamento (aviso com frequência e chip com motivo)

**Files:**
- Modify: `roteirizacao/dedicados.py`
- Modify: `roteirizacao/test_dedicado_fora_rota.py`
- Modify: `painel_agentes/planejamento_rotas.py`
- Modify: `painel_agentes/templates/planejamento_rotas.html`
- Modify: `painel_agentes/test_aviso_dia_fixo.py`

**Interfaces:**
- Consumes: `pedidos_dedicados.POR_FORA_DIA_FIXO` (Task 4); `regioes_dia_fixo.data_valida_na_regiao`, `descricao_dias`, `proxima_data_valida` (Task 1; já importadas em `planejamento_rotas.py` pela Task 4A).
- Produces: `dedicados.MOTIVO_FORA_DIA_FIXO = "fora do dia fixo"`; `dedicados_por_servico` devolve `{"valor", "valor_total", "n_grupo"}` e, só para marcação desta regra, `"motivo": "fora do dia fixo"`.

- [ ] **Step 1: Acrescentar os testes**

Em `roteirizacao/test_dedicado_fora_rota.py`, dentro da classe `DedicadosPorServico`, acrescentar:

```python
    def test_motivo_so_na_marcacao_fora_do_dia_fixo(self):
        ativos = {"PS-1": {"valor": 784.09, "valor_total_grupo": 784.09, "grupo_id": "a",
                           "marcado_por": "automatico: fora do dia fixo"},
                  "PS-2": {"valor": 0.0, "valor_total_grupo": 0.0, "grupo_id": "b",
                           "marcado_por": "automatico: fora do dia fixo (valor pendente)"},
                  "PS-3": {"valor": 10.0, "valor_total_grupo": 10.0, "grupo_id": "c", "marcado_por": "hugo"}}
        with mock.patch.object(dedicados, "carregar_dedicados", return_value=ativos):
            m = dedicados.dedicados_por_servico([{"id": 1, "code": "PS-1"}, {"id": 2, "code": "PS-2"},
                                                 {"id": 3, "code": "PS-3"}])
        self.assertEqual(m[1]["motivo"], "fora do dia fixo")
        self.assertEqual(m[2]["motivo"], "fora do dia fixo")
        self.assertNotIn("motivo", m[3])
```

Em `painel_agentes/test_aviso_dia_fixo.py`, trocar `from datetime import date` por `from datetime import date, timedelta`, acrescentar depois de `import planejamento_rotas` a linha `import regioes_dia_fixo`, acrescentar as constantes

```python
ABCD = {"address": "Rua das Figueiras 100, Jardim, Santo André - SP, 09080-300, Brasil"}
SOROCABA = {"address": "Rua XV de Novembro 10, Centro, Sorocaba - SP, 18010-080, Brasil"}
```

e, dentro da classe `AvisoDiaFixoTestCase`, os testes:

```python
    def test_abcd_agora_segunda_e_quinta(self):
        self.assertEqual(planejamento_rotas._aviso_dia_fixo(ABCD, QUARTA), "Santo André: só Segundas e Quintas")
        self.assertIsNone(planejamento_rotas._aviso_dia_fixo(ABCD, QUINTA))

    def test_sorocaba_quinzenal_avisa_na_semana_sem_visita(self):
        regra = regioes_dia_fixo.regra_dia_fixo_do_servico(SOROCABA)
        visita = regioes_dia_fixo.proxima_data_valida(regra, date(2026, 10, 3))
        self.assertIsNone(planejamento_rotas._aviso_dia_fixo(SOROCABA, visita))
        self.assertEqual(planejamento_rotas._aviso_dia_fixo(SOROCABA, visita + timedelta(days=7)),
                         "Sorocaba: só Terças (quinzenal)")
```

- [ ] **Step 2: Rodar e ver falhar**

```bash
py -3.11 -m unittest roteirizacao.test_dedicado_fora_rota -v 2>&1 | tail -3
py -3.11 -m unittest painel_agentes.test_aviso_dia_fixo -v 2>&1 | tail -3
```

Expected: FAIL (`KeyError: 'motivo'`; aviso da Sorocaba sai `None` na semana sem visita; ABCD ainda passa pelo dia da semana novo, mas a Sorocaba falha).

- [ ] **Step 3: Implementar em `roteirizacao/dedicados.py`**

Depois de `logger = logging.getLogger(__name__)`, acrescentar:

```python
# Chip do pool (Hugo, 03/10): marcacao automatica de data fora do dia fixo
# (roteirizacao/fora_dia_fixo.py) mostra o motivo.
MOTIVO_FORA_DIA_FIXO = "fora do dia fixo"
```

Em `dedicados_por_servico`, trocar o corpo do `if c in ativos:` por:

```python
            if c in ativos:
                d = ativos[c]
                mapa[s["id"]] = {"valor": d["valor"], "valor_total": d["valor_total_grupo"], "n_grupo": n_por_grupo[d["grupo_id"]]}
                if str(d.get("marcado_por") or "").startswith(pedidos_dedicados.POR_FORA_DIA_FIXO):
                    mapa[s["id"]]["motivo"] = MOTIVO_FORA_DIA_FIXO
                break
```

E na docstring dela trocar `{service_id: {valor, valor_total, n_grupo}}` por `{service_id: {valor, valor_total, n_grupo[, motivo]}}`.

- [ ] **Step 4: Implementar em `painel_agentes/planejamento_rotas.py`**

O import de `data_valida_na_regiao` e `descricao_dias` já entrou na Task 4A (conferir com `grep -n "from regioes_dia_fixo import" painel_agentes/planejamento_rotas.py`).

Em `_aviso_dia_fixo`, trocar as três últimas linhas por:

```python
    regra = regra_dia_fixo_do_servico(servico)
    if not regra or data_valida_na_regiao(regra, data_alvo):
        return None
    return f"{regra['nome']}: só {descricao_dias(regra)}"
```

E acrescentar à docstring dela: `03/10 (dias fixos v2): respeita a frequência -- Sorocaba quinzenal avisa na semana sem visita ("Sorocaba: só Terças (quinzenal)").`

Se `nomes_dias` deixar de ser usado em `planejamento_rotas.py` depois desta troca, tirar só `nomes_dias` do import (`grep -n "nomes_dias" painel_agentes/planejamento_rotas.py` decide).

- [ ] **Step 5: Chip no template**

Em `painel_agentes/templates/planejamento_rotas.html`, na função `badgesPedidoHtml`, trocar

```js
      partes.push(`<span class="badge-dedicado"${titulo}>🚚 Dedicado · R$ ${brl(p.dedicado.valor)}</span>`);
```

por

```js
      // motivo (Hugo, 03/10): marcado sozinho por data fora do dia fixo da região
      const motivo = p.dedicado.motivo ? ` · ${p.dedicado.motivo}` : "";
      partes.push(`<span class="badge-dedicado"${titulo}>🚚 Dedicado · R$ ${brl(p.dedicado.valor)}${motivo}</span>`);
```

- [ ] **Step 6: Rodar e ver passar**

```bash
py -3.11 -m unittest roteirizacao.test_dedicado_fora_rota -v 2>&1 | tail -3
py -3.11 -m unittest discover -s painel_agentes -p "test_*.py" 2>&1 | tail -3
grep -n "p.dedicado.motivo" painel_agentes/templates/planejamento_rotas.html
```

Expected: `OK` nos dois; o grep mostra a linha nova.

- [ ] **Step 7: Commit**

```bash
git add roteirizacao/dedicados.py roteirizacao/test_dedicado_fora_rota.py painel_agentes/planejamento_rotas.py painel_agentes/templates/planejamento_rotas.html painel_agentes/test_aviso_dia_fixo.py
git commit -m "Planejamento: aviso de dia fixo respeita a quinzena e chip do dedicado mostra fora do dia fixo

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9: Portal (chip com motivo e aviso no envio)

**Files:**
- Modify: `portal_cliente/envio_pedidos.py`
- Modify: `portal_cliente/app.py` (`api_envios_confirmar`)
- Modify: `portal_cliente/templates/_envios.html`
- Modify: `portal_cliente/test_bloqueio_area.py`
- Test: `portal_cliente/test_fora_dia_fixo_envio.py`

**Interfaces:**
- Consumes: `pedidos_dedicados.POR_FORA_DIA_FIXO`, `normalizar_codigo` (Task 4); `regioes_dia_fixo.regra_dia_fixo_do_servico`, `data_valida_na_regiao`, `descricao_dias` (Task 1); `_caminho_temporario`, `ler_nfe`, `rotulo_envio` (já existem).
- Produces:
  - `envio_pedidos.MOTIVO_DEDICADO_FORA_DIA_FIXO = "data fora do dia de visita da região"`
  - `listar_envios(...)`: `l["dedicado"]` = `{"valor": X}` ou `{"valor": X, "motivo": "..."}`, casando por `envio_id` ou por `codigo_pedido`
  - `envio_pedidos.avisos_fora_dia_fixo(itens: list[dict]) -> list[dict]` (`{"token", "rotulo", "destinatario_nome", "data_br", "regiao", "dias"}`)
  - `/api/envios/confirmar` responde **409** `{"erro", "fora_dia_fixo": [...]}` quando há data fora do dia e o item não traz `aceita_fora_dia_fixo: true`

- [ ] **Step 1: Escrever os testes**

Criar `portal_cliente/test_fora_dia_fixo_envio.py`:

```python
# -*- coding: utf-8 -*-
"""
Portal: aviso no envio quando a data escolhida está fora do dia de visita
da região, e chip "Envio dedicado" com o motivo (dias fixos v2, Hugo 03/10).
    py -3.11 -m unittest portal_cliente.test_fora_dia_fixo_envio -v
"""
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import envio_pedidos as ep  # noqa: E402

HOJE = date.today()
QUARTA = HOJE + timedelta(days=(2 - HOJE.weekday()) % 7 or 7)   # próxima quarta (nunca hoje)
QUINTA = QUARTA + timedelta(days=1)


def _pedido(cidade="Campinas", endereco="Rua Barão de Jaguara, 900"):
    return {"origem": ep.ORIGEM_PLANILHA, "referencia": "PED-1", "numero_nf": "", "destinatario_nome": "Mercado X",
            "destinatario_endereco": endereco, "destinatario_municipio": cidade, "destinatario_uf": "SP",
            "destinatario_cep": "13015001"}


class AvisoNoEnvio(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(ep, "PASTA_TEMP", Path(self.tmp.name) / "_temporarios")
        p.start()
        self.addCleanup(p.stop)

    def _item(self, data, **kw):
        token = ep.guardar_temporario_pedido(kw.pop("pedido", _pedido()), "tokenplanilha0001")
        return {"token": token, "requer_agendamento": True, "agendamento_data": data.isoformat(), **kw}

    def test_data_fora_do_dia_avisa(self):
        avisos = ep.avisos_fora_dia_fixo([self._item(QUINTA)])
        self.assertEqual(len(avisos), 1)
        a = avisos[0]
        self.assertEqual((a["regiao"], a["dias"], a["data_br"], a["rotulo"], a["destinatario_nome"]),
                         ("Campinas", "Quartas", QUINTA.strftime("%d/%m/%Y"), "Pedido PED-1", "Mercado X"))

    def test_data_no_dia_de_visita_nao_avisa(self):
        self.assertEqual(ep.avisos_fora_dia_fixo([self._item(QUARTA)]), [])

    def test_confirmado_assim_mesmo_nao_avisa_de_novo(self):
        self.assertEqual(ep.avisos_fora_dia_fixo([self._item(QUINTA, aceita_fora_dia_fixo=True)]), [])

    def test_agendamento_pendente_ou_sem_data_nao_avisa(self):
        item = self._item(QUINTA, agendamento_pendente=True)
        self.assertEqual(ep.avisos_fora_dia_fixo([item]), [])
        sem = self._item(QUINTA)
        sem["agendamento_data"] = ""
        self.assertEqual(ep.avisos_fora_dia_fixo([sem]), [])

    def test_data_invalida_ou_passada_nao_avisa(self):
        ruim = self._item(QUINTA)
        ruim["agendamento_data"] = "31/02"
        self.assertEqual(ep.avisos_fora_dia_fixo([ruim, self._item(HOJE - timedelta(days=1))]), [])

    def test_destino_sem_regiao_nao_avisa(self):
        self.assertEqual(ep.avisos_fora_dia_fixo([self._item(QUINTA, pedido=_pedido(cidade="São Paulo"))]), [])

    def test_sem_logradouro_reconhece_pela_cidade(self):
        self.assertEqual(len(ep.avisos_fora_dia_fixo([self._item(QUINTA, pedido=_pedido(endereco=""))])), 1)

    def test_token_expirado_levanta_erro_de_envio(self):
        with self.assertRaises(ep.ErroEnvio):
            ep.avisos_fora_dia_fixo([{"token": "naoexiste000000", "agendamento_data": QUINTA.isoformat()}])


class ChipNoPortal(unittest.TestCase):
    def test_dedicado_por_codigo_com_motivo(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""CREATE TABLE portal_envios (id INTEGER PRIMARY KEY, cnpj_embarcador TEXT, status TEXT, criado_em TEXT,
            criado_stokki_em, emitida_em, destinatario_doc, destinatario_endereco, destinatario_bairro, destinatario_municipio,
            destinatario_uf, agendamento_data, numero_nf, referencia, data_expedicao, xml_path, origem, agendamento_pendente,
            bloqueio_motivo, bloqueio_chamado_id, codigo_pedido);
            CREATE TABLE portal_solicitacoes (id INTEGER PRIMARY KEY, envio_id, cnpj_embarcador, tipo, detalhes, status, solicitado_por, criado_em, concluido_em, resposta);
            CREATE TABLE pedidos_dedicados (id INTEGER PRIMARY KEY, codigo_pedido TEXT, envio_id INTEGER, valor REAL,
                marcado_por TEXT, removido_em TEXT);
            INSERT INTO portal_envios (id, cnpj_embarcador, status, criado_em, numero_nf, xml_path, origem, agendamento_pendente, codigo_pedido)
              VALUES (1, '1', 'CRIADO', '2099-01-01 00:00:00', '1', 'a.xml', 'xml', 0, 'PS-77'),
                     (2, '1', 'NA_FILA', '2099-01-01 00:00:00', '2', 'b.xml', 'xml', 0, NULL);
            INSERT INTO pedidos_dedicados (codigo_pedido, envio_id, valor, marcado_por, removido_em) VALUES
              ('PS-77', NULL, 784.09, 'automatico: fora do dia fixo', NULL),
              (NULL, 2, 10.0, 'hugo', NULL);""")
        lista = {l["id"]: l for l in ep.listar_envios(conn, "1")}
        self.assertEqual(lista[1]["dedicado"], {"valor": 784.09, "motivo": "data fora do dia de visita da região"})
        self.assertEqual(lista[2]["dedicado"], {"valor": 10.0})


if __name__ == "__main__":
    unittest.main()
```

Em `portal_cliente/test_bloqueio_area.py`, no `test_listar_envios_com_dedicado`, trocar as duas linhas da tabela de dedicados:

```python
            CREATE TABLE pedidos_dedicados (id INTEGER PRIMARY KEY, envio_id INTEGER, valor REAL, removido_em TEXT);
```
e
```python
            INSERT INTO pedidos_dedicados VALUES (1, 1, 33.34, NULL), (2, 2, 5, '2026-01-01');""")
```

por (esquema igual ao de `pedidos_dedicados.conectar`, que é o que a produção tem):

```python
            CREATE TABLE pedidos_dedicados (id INTEGER PRIMARY KEY, codigo_pedido TEXT, envio_id INTEGER, valor REAL,
                marcado_por TEXT, removido_em TEXT);
```
e
```python
            INSERT INTO pedidos_dedicados (id, envio_id, valor, removido_em) VALUES (1, 1, 33.34, NULL), (2, 2, 5, '2026-01-01');""")
```

(O resultado esperado do teste, `{"valor": 33.34}`, não muda.)

- [ ] **Step 2: Rodar e ver falhar**

Run: `py -3.11 -m unittest portal_cliente.test_fora_dia_fixo_envio portal_cliente.test_bloqueio_area -v`
Expected: ERROR (`AttributeError: module 'envio_pedidos' has no attribute 'avisos_fora_dia_fixo'`) e FAIL no chip por código.

- [ ] **Step 3: Implementar em `portal_cliente/envio_pedidos.py`**

3a. Depois de `class ErroEnvio(Exception): pass`, acrescentar:

```python
# Dedicado por data fora do dia fixo (Hugo, 03/10/2026 -- spec dias fixos v2):
# texto do detalhe do chip "Envio dedicado".
MOTIVO_DEDICADO_FORA_DIA_FIXO = "data fora do dia de visita da região"
```

3b. No fim de `listar_envios`, trocar o bloco desde `# Marca de envio dedicado (Hugo, 23/09)` até o `return linhas` por:

```python
    # Marca de envio dedicado (Hugo, 23/09): pedidos_dedicados mora no mesmo
    # dados.db; a checagem em sqlite_master cobre banco sem a tabela ainda.
    # 03/10: casa também pelo código do pedido (a marcação automática de data
    # fora do dia fixo não conhece o envio_id) e traz o motivo.
    import pedidos_dedicados
    por_envio: dict[int, dict] = {}
    por_codigo: dict[str, dict] = {}
    if conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'pedidos_dedicados'").fetchone():
        for r in conn.execute("SELECT envio_id, codigo_pedido, valor, marcado_por FROM pedidos_dedicados "
                              "WHERE removido_em IS NULL"):
            info = {"valor": r["valor"]}
            if str(r["marcado_por"] or "").startswith(pedidos_dedicados.POR_FORA_DIA_FIXO):
                info["motivo"] = MOTIVO_DEDICADO_FORA_DIA_FIXO
            if r["envio_id"] is not None:
                por_envio[r["envio_id"]] = info
            if r["codigo_pedido"]:
                por_codigo[r["codigo_pedido"]] = info
    for l in linhas:
        codigo = pedidos_dedicados.normalizar_codigo(l.get("codigo_pedido")) if l.get("codigo_pedido") else None
        l["dedicado"] = por_envio.get(l["id"]) or (por_codigo.get(codigo) if codigo else None)
    return linhas
```

3c. Logo depois de `classificar_area_envio`, acrescentar:

```python
def _regioes_dia_fixo():
    """roteirizacao/regioes_dia_fixo.py (caminho pelo próprio arquivo: os
    testes trocam _RAIZ por uma pasta temporária)."""
    pasta = str(Path(__file__).resolve().parent.parent / "roteirizacao")
    if pasta not in sys.path:
        sys.path.insert(0, pasta)
    import regioes_dia_fixo
    return regioes_dia_fixo


def _endereco_sintetico(nfe: dict) -> str:
    """Endereço no formato da Vuupt ('..., Cidade - UF, CEP, Brasil'), que é o
    que regioes_dia_fixo.extrair_cidade lê."""
    cidade_uf = f"{nfe.get('destinatario_municipio') or ''} - {nfe.get('destinatario_uf') or ''}".strip(" -")
    partes = [nfe.get("destinatario_endereco") or "S/N", cidade_uf, nfe.get("destinatario_cep")]
    return ", ".join(p for p in partes if p) + ", Brasil"


def avisos_fora_dia_fixo(itens: list[dict]) -> list[dict]:
    """Itens do confirmar (mesmo formato de confirmar_envios) com data de
    agendamento escolhida fora do dia de visita da região do destino (Hugo,
    03/10 -- spec dias fixos v2, 5.2). Fica de fora: sem data, agendamento
    pendente, data inválida ou passada, e quem já confirmou assim mesmo
    (aceita_fora_dia_fixo). Relê o temporário, como confirmar_envios."""
    rdf = _regioes_dia_fixo()
    hoje = date.today()
    avisos = []
    for item in itens:
        if item.get("aceita_fora_dia_fixo") or item.get("agendamento_pendente") or not item.get("agendamento_data"):
            continue
        try:
            data = date.fromisoformat(str(item.get("agendamento_data")))
        except ValueError:
            continue
        if data < hoje:
            continue
        caminho = _caminho_temporario(item.get("token", ""))
        conteudo = caminho.read_bytes()
        nfe = json.loads(conteudo.decode("utf-8")) if caminho.suffix == ".json" else ler_nfe(conteudo)
        regra = rdf.regra_dia_fixo_do_servico({"address": _endereco_sintetico(nfe)})
        if not regra or rdf.data_valida_na_regiao(regra, data):
            continue
        avisos.append({"token": item.get("token"), "rotulo": rotulo_envio(nfe),
                       "destinatario_nome": nfe.get("destinatario_nome") or "",
                       "data_br": data.strftime("%d/%m/%Y"), "regiao": regra.get("regiao") or regra["nome"],
                       "dias": rdf.descricao_dias(regra)})
    return avisos
```

- [ ] **Step 4: Rota em `portal_cliente/app.py`**

Em `api_envios_confirmar`, logo depois de `conn = envios.conectar()` e `try:`, ANTES de `cfg = envios.config_stokki_cliente(...)`, inserir:

```python
        # Data fora do dia de visita da região (Hugo, 03/10): pergunta antes de
        # gravar; o embarcador troca a data ou confirma assim mesmo
        # (aceita_fora_dia_fixo no item) e o pedido vira envio dedicado.
        fora = envios.avisos_fora_dia_fixo(itens)
        if fora:
            return jsonify({"erro": "Há pedidos com data fora do dia de visita da região.", "fora_dia_fixo": fora}), 409
```

- [ ] **Step 5: Tela em `portal_cliente/templates/_envios.html`**

5a. Na linha da tabela de envios que monta o chip, trocar

```js
${x.dedicado ? `<div><span class="chip dedicado" title="Transporte dedicado combinado com a Fresh Log">Envio dedicado</span></div>` : ''}
```

por

```js
${x.dedicado ? `<div><span class="chip dedicado" title="Transporte dedicado combinado com a Fresh Log">Envio dedicado</span></div>${x.dedicado.motivo ? `<div class="bloq-txt">${esc(x.dedicado.motivo)}</div>` : ''}` : ''}
```

5b. Trocar a assinatura `async function confirmar() {` por `async function confirmar(aceitos = new Set()) {`.

5c. Dentro de `confirmar`, trocar

```js
      const item = { token: it.token, horario_inicio: h.ini, horario_fim: h.fim, requer_agendamento: h.ag };
```

por

```js
      const item = { token: it.token, horario_inicio: h.ini, horario_fim: h.fim, requer_agendamento: h.ag };
      if (aceitos.has(it.token)) item.aceita_fora_dia_fixo = true;
```

5d. Dentro de `confirmar`, logo depois de `const j = await r.json();`, inserir:

```js
      if (r.status === 409 && j.fora_dia_fixo) {
        mensagem('', '');
        $('btn-confirmar').disabled = false;
        perguntarForaDiaFixo(j.fora_dia_fixo, aceitos);
        return;
      }
```

5e. Logo depois do fim da função `confirmar`, acrescentar:

```js
  // Data fora do dia de visita da região (Hugo, 03/10): o embarcador troca a
  // data na prévia ou confirma assim mesmo (vira envio dedicado, com valor).
  function perguntarForaDiaFixo(lista, aceitos) {
    modal(`<h3>Data fora do dia de visita da região</h3>
      <p>Atendemos estas regiões só em alguns dias da semana. Se a data for mantida, o pedido vai como <b>envio dedicado</b>, com valor calculado pela tabela de frete dedicado da Fresh Log.</p>
      <ul style="margin:0 0 12px;padding-left:18px;font-size:13px;">${lista.map(a => `<li><b>${esc(a.rotulo)}</b> · ${esc(a.destinatario_nome)} · ${esc(a.data_br)} — ${esc(a.regiao)}: ${esc(a.dias)}</li>`).join('')}</ul>
      <div class="pe"><button type="button" class="botao-linha" data-fechar>Trocar a data</button><button type="button" class="botao" id="m-ok">Confirmar assim mesmo</button></div>`);
    $('m-ok').addEventListener('click', () => {
      lista.forEach(a => aceitos.add(a.token));
      fecharModal();
      confirmar(aceitos);
    });
  }
```

5f. Trocar `$('btn-confirmar').addEventListener('click', confirmar);` por:

```js
  $('btn-confirmar').addEventListener('click', () => confirmar());
```

- [ ] **Step 6: Rodar e ver passar**

```bash
py -3.11 -m unittest portal_cliente.test_fora_dia_fixo_envio portal_cliente.test_bloqueio_area -v 2>&1 | tail -3
py -3.11 -m unittest discover -s portal_cliente -p "test_*.py" 2>&1 | tail -3
py -3.11 -m py_compile portal_cliente/app.py portal_cliente/envio_pedidos.py
```

Expected: `OK` nos dois primeiros (se o discover do portal já falhava na Task 0 por testes em estilo pytest, comparar com a contagem anotada lá: nenhuma falha nova); o `py_compile` sem saída.

- [ ] **Step 7: Conferir a tela no navegador (porta de teste, nunca a 8074 de produção nem a 8070 do painel)**

```bash
cd portal_cliente && py -3.11 -c "import app; app.app.run(host='127.0.0.1', port=8098)"
```

Logar com o cliente teste (CNPJ `00.000.000/0001-91`, PIN `123456`), aba Envios, subir uma planilha do modelo com cidade `Campinas`, marcar "só recebe com agendamento", escolher uma quinta e confirmar. Esperado: abre o quadro "Data fora do dia de visita da região" com a linha do pedido; "Trocar a data" fecha e mantém a prévia; "Confirmar assim mesmo" grava. Encerrar o servidor de teste (Ctrl+C) sem tocar em processos que já existiam. Se o login local não funcionar, registrar e seguir: a regra está coberta pelos testes do Step 6.

- [ ] **Step 8: Commit**

```bash
git add portal_cliente/envio_pedidos.py portal_cliente/app.py portal_cliente/templates/_envios.html portal_cliente/test_fora_dia_fixo_envio.py portal_cliente/test_bloqueio_area.py
git commit -m "Portal: aviso no envio de data fora do dia de visita e chip do dedicado com o motivo

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 10: Vigia (prazo de 7/15 dias no pool)

**Files:**
- Modify: `vigia/regras.py`
- Modify: `vigia/vigiar.py`
- Modify: `vigia/test_regras.py`
- Modify: `vigia/test_vigiar.py`

**Interfaces:**
- Consumes: `regioes_dia_fixo.regra_dia_fixo_do_servico`, `NIVEL_SEMANAL`, `NIVEL_QUINZENAL` (Task 1); coluna `endereco` de `nucleo_pedidos` (já existe em produção, `nucleo/banco.py`).
- Produces: `regras.prazo(estado, desde, *, data_rascunho=None, prazo_segurado=None, prazo_dias_regiao: int | None = None)`; `vigiar._prazo_dias_regiao(endereco) -> int | None`; fato `prazo_dias_regiao`.

- [ ] **Step 1: Acrescentar os testes**

Em `vigia/test_regras.py`, dentro de `TestPrazos`, acrescentar:

```python
    def test_no_pool_de_regiao_semanal_ou_quinzenal_usa_dias_corridos(self):
        desde = datetime(2026, 9, 29, 8, 0)
        self.assertEqual(r.prazo(r.NO_POOL, desde, prazo_dias_regiao=7), datetime(2026, 10, 6, 8, 0))
        self.assertEqual(r.prazo(r.NO_POOL, desde, prazo_dias_regiao=15), datetime(2026, 10, 14, 8, 0))
        self.assertEqual(r.prazo(r.NO_POOL, desde, prazo_dias_regiao=None), datetime(2026, 9, 30, 8, 0))

    def test_segurado_vence_o_prazo_da_regiao(self):
        desde = datetime(2026, 9, 29, 8, 0)
        self.assertEqual(r.prazo(r.NO_POOL, desde, prazo_segurado=date(2026, 10, 1), prazo_dias_regiao=7),
                         r.prazo(r.NO_POOL, desde, prazo_segurado=date(2026, 10, 1)))

    def test_prazo_da_regiao_so_vale_no_pool(self):
        desde = datetime(2026, 9, 29, 8, 0)
        self.assertEqual(r.prazo(r.SEM_SERVICO, desde, prazo_dias_regiao=7), datetime(2026, 9, 29, 12, 0))
```

Em `vigia/test_vigiar.py`, no `ESQUEMA`, trocar

```
    criado_em TEXT, remetente_nome TEXT, destinatario_nome TEXT, fluxo TEXT, excluido_em TEXT,
    reentrega_de_service_id INTEGER);
```

por

```
    criado_em TEXT, remetente_nome TEXT, destinatario_nome TEXT, fluxo TEXT, excluido_em TEXT,
    reentrega_de_service_id INTEGER, endereco TEXT);
```

e acrescentar em `TestRodada`:

```python
    def test_pool_de_regiao_semanal_tem_prazo_de_7_dias(self):
        self.conn.execute(
            "INSERT INTO nucleo_pedidos (codigo, vuupt_service_id, status, criado_em_provedor, fluxo, endereco) "
            "VALUES ('PS-10', 10, 'ABERTO', '2026-09-25 09:00:00', 'ENTREGA', "
            "'Rua Barão de Jaguara 900, Centro, Campinas - SP, 13015-001, Brasil')")
        self.conn.commit()
        vigiar.rodar(self.conn, agora=AGORA)
        e = self._estados()
        self.assertEqual(e["PS-10"]["estado"], "NO_POOL")
        self.assertEqual(e["PS-10"]["vence_em"], "2026-10-02 09:00:00")
        self.assertEqual(e["PS-10"]["vencido"], 0)
        self.assertEqual(e["PS-1"]["vencido"], 1)   # Grande SP / sem endereço: 1 dia útil

    def test_prazo_dias_regiao(self):
        self.assertEqual(vigiar._prazo_dias_regiao("Rua A 1, Centro, Campinas - SP, 13000-000, Brasil"), 7)
        self.assertEqual(vigiar._prazo_dias_regiao("Rua A 1, Centro, Sorocaba - SP, 18000-000, Brasil"), 15)
        self.assertIsNone(vigiar._prazo_dias_regiao("Rua A 1, Centro, Santo André - SP, 09000-000, Brasil"))
        self.assertIsNone(vigiar._prazo_dias_regiao(None))
```

Se o formato de `vence_em` em `vigia/banco.py` (`banco.FMT`) não for `"%Y-%m-%d %H:%M:%S"`, ajustar a string esperada pelo `FMT` real (conferir com `grep -n "^FMT" vigia/banco.py`).

- [ ] **Step 2: Rodar e ver falhar**

Run: `py -3.11 -m unittest vigia.test_regras vigia.test_vigiar -v`
Expected: `TypeError: prazo() got an unexpected keyword argument 'prazo_dias_regiao'` e `AttributeError: ... '_prazo_dias_regiao'`.

- [ ] **Step 3: Implementar em `vigia/regras.py`**

No docstring do topo, logo abaixo da linha do segurado, acrescentar:

```
                       (região semanal/quinzenal sem agendamento) 7 / 15 dias corridos (Hugo, 03/10)
```

Trocar a assinatura e o ramo `NO_POOL` de `prazo`:

```python
def prazo(estado: str, desde: datetime, *, data_rascunho: date | None = None,
          prazo_segurado: date | None = None, prazo_dias_regiao: int | None = None) -> datetime | None:
    """Quando o prazo do estado vence (None = estado sem prazo).
    `prazo_segurado`: prazo final de entrega do pedido segurado de
    propósito pela regra de rota fraca (roteirizacao/rotas_fracas.py).
    `prazo_dias_regiao`: dias corridos do nível da região de dia fixo
    (semanal 7, quinzenal 15 -- roteirizacao/regioes_dia_fixo.py), só pra
    pedido sem agendamento."""
    if estado == SEM_SERVICO:
        return desde + timedelta(hours=HORAS_SEM_SERVICO)
    if estado == NO_POOL:
        if prazo_segurado:
            return datetime.combine(ultimo_dia_util_antes(prazo_segurado), HORA_LIMITE_RASCUNHO)
        if prazo_dias_regiao:
            return desde + timedelta(days=prazo_dias_regiao)
        return somar_dias_uteis(desde, DIAS_UTEIS_NO_POOL)
```

(O resto da função fica igual.)

- [ ] **Step 4: Implementar em `vigia/vigiar.py`**

4a. Logo depois de `from vigia import banco, regras  # noqa: E402`, acrescentar:

```python
from roteirizacao.regioes_dia_fixo import NIVEL_QUINZENAL, NIVEL_SEMANAL, regra_dia_fixo_do_servico  # noqa: E402
```

4b. Antes de `def coletar_fatos`, acrescentar:

```python
def _prazo_dias_regiao(endereco) -> int | None:
    """Dias corridos do prazo de pedido de região semanal/quinzenal (Hugo,
    03/10); None pra Grande SP, região interna ou endereço sem região."""
    if not endereco:
        return None
    try:
        regra = regra_dia_fixo_do_servico({"address": endereco})
    except Exception:
        return None
    if regra and regra.get("nivel") in (NIVEL_SEMANAL, NIVEL_QUINZENAL):
        return regra["prazo_dias"]
    return None
```

4c. Em `coletar_fatos`, no SELECT de `nucleo_pedidos`, trocar `destinatario_nome, fluxo, excluido_em, reentrega_de_service_id` por `destinatario_nome, fluxo, excluido_em, reentrega_de_service_id, endereco`.

4d. No dict `f`, logo depois da linha `"prazo_segurado": ...`, acrescentar:

```python
            "prazo_dias_regiao": None if s["agendamento_inicio"] else _prazo_dias_regiao(s["endereco"]),
```

4e. Em `rodar`, trocar

```python
        vence = regras.prazo(estado, desde, data_rascunho=f.get("rascunho_data"),
                             prazo_segurado=f.get("prazo_segurado"))
```

por

```python
        vence = regras.prazo(estado, desde, data_rascunho=f.get("rascunho_data"),
                             prazo_segurado=f.get("prazo_segurado"),
                             prazo_dias_regiao=f.get("prazo_dias_regiao"))
```

- [ ] **Step 5: Rodar e ver passar**

```bash
py -3.11 -m unittest vigia.test_regras vigia.test_vigiar -v 2>&1 | tail -3
```

Expected: `OK`.

- [ ] **Step 6: Commit**

```bash
git add vigia/regras.py vigia/vigiar.py vigia/test_regras.py vigia/test_vigiar.py
git commit -m "Vigia: pedido de regiao semanal/quinzenal no pool tem prazo de 7/15 dias corridos

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 11: Informativo em PDF aos embarcadores

**Files:**
- Create: `gerar_informativo_regioes.py`
- Test: `test_gerar_informativo_regioes.py`

**Interfaces:**
- Consumes: `regioes_dia_fixo.REGIOES`, `ENDERECOS_DIA_FIXO`, `nomes_dias`, `nivel_da_regra`, `PRAZO_POR_NIVEL`, `FREQUENCIA_SEMANAL`, `FREQUENCIA_QUINZENAL` (Task 1); de `portal_cliente/cotacao_pdf.py`: `A4`, `DPI`, `MARGEM`, `NAVY`, `TEAL`, `CINZA_ZEBRA`, `CINZA_LINHA`, `CINZA_TXT`, `PRETO`, `_fonte`, `_logo`, `_quebrar`.
- Produces: `nome_bonito(cidade: str) -> str`; `linhas_informativo() -> list[dict]` (chaves `regiao, cidades, dias, frequencia, prazo`); `gerar_pdf(linhas: list[dict], caminho: Path) -> Path`; CLI `py -3.11 gerar_informativo_regioes.py [--saida X.pdf]`.

- [ ] **Step 1: Escrever os testes**

Criar `test_gerar_informativo_regioes.py` (raiz):

```python
# -*- coding: utf-8 -*-
"""
Informativo de regiões aos embarcadores (dias fixos v2, Hugo 03/10).
Rodar (da raiz): py -3.11 -m unittest test_gerar_informativo_regioes -v
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import gerar_informativo_regioes as gir
import regioes_dia_fixo as rdf


class TestConteudo(unittest.TestCase):
    def test_tem_todas_as_regioes_e_pontos(self):
        nomes = [l["regiao"] for l in gir.linhas_informativo()]
        for r in rdf.REGIOES + rdf.ENDERECOS_DIA_FIXO:
            self.assertIn(r["nome"], nomes)

    def test_dias_frequencia_e_prazo(self):
        linhas = {l["regiao"]: l for l in gir.linhas_informativo()}
        self.assertEqual((linhas["ABCD"]["dias"], linhas["ABCD"]["frequencia"], linhas["ABCD"]["prazo"]),
                         ("Segundas e Quintas", "2x por semana", "até 3 dias úteis"))
        self.assertEqual((linhas["Campinas"]["frequencia"], linhas["Campinas"]["prazo"]),
                         ("Semanal", "até 7 dias corridos"))
        self.assertEqual((linhas["Sorocaba"]["frequencia"], linhas["Sorocaba"]["prazo"]),
                         ("Quinzenal", "até 15 dias corridos"))
        self.assertIn("a cada 15 dias", linhas["Sorocaba"]["dias"])
        self.assertIn("São José dos Campos", linhas["Vale do Paraíba"]["cidades"])

    def test_nome_bonito(self):
        self.assertEqual(gir.nome_bonito("SAO JOSE DOS CAMPOS"), "São José dos Campos")
        self.assertEqual(gir.nome_bonito("SANTANA DO PARNAIBA"), "Santana do Parnaíba")
        self.assertEqual(gir.nome_bonito("JACAREI"), "Jacareí")


class TestPdf(unittest.TestCase):
    def test_gera_pdf(self):
        with tempfile.TemporaryDirectory() as tmp:
            caminho = gir.gerar_pdf(gir.linhas_informativo(), Path(tmp) / "x" / "informativo.pdf")
            self.assertTrue(caminho.read_bytes().startswith(b"%PDF"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `py -3.11 -m unittest test_gerar_informativo_regioes -v`
Expected: `ModuleNotFoundError: No module named 'gerar_informativo_regioes'` (o `import regioes_dia_fixo` depende do `sys.path` que o módulo novo monta).

- [ ] **Step 3: Criar `gerar_informativo_regioes.py`**

```python
# -*- coding: utf-8 -*-
"""
gerar_informativo_regioes.py

Informativo aos embarcadores (Hugo, 03/10/2026 -- spec dias fixos v2,
seção 7): regiões, cidades, dias de visita, frequência e prazo, gerado da
configuração de roteirizacao/regioes_dia_fixo.py (pra não desatualizar).
PDF desenhado com Pillow, mesmo padrão de portal_cliente/cotacao_pdf.py
(reportlab não está no projeto). Nada é enviado: o envio é do Hugo.

    py -3.11 gerar_informativo_regioes.py
        -> dados/informativos/informativo_regioes_AAAA-MM-DD.pdf
    py -3.11 gerar_informativo_regioes.py --saida caminho.pdf
"""
import argparse
import sys
from datetime import date, datetime
from pathlib import Path

from PIL import Image, ImageDraw

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

_RAIZ = Path(__file__).parent
for _p in (_RAIZ, _RAIZ / "roteirizacao", _RAIZ / "portal_cliente"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import regioes_dia_fixo as rdf  # noqa: E402
from cotacao_pdf import (  # noqa: E402
    A4, CINZA_LINHA, CINZA_TXT, CINZA_ZEBRA, DPI, MARGEM, NAVY, PRETO, TEAL, _fonte, _logo, _quebrar,
)

PASTA_SAIDA = _RAIZ / "dados" / "informativos"
CONTATO = "entregas@freshlogbr.com"

# Acento e preposição pros nomes de REGIOES (gravados sem acento, em maiúsculas).
_PALAVRAS = {"SAO": "São", "JOSE": "José", "JACAREI": "Jacareí", "CUBATAO": "Cubatão", "GUARUJA": "Guarujá",
             "JUNDIAI": "Jundiaí", "CABREUVA": "Cabreúva", "HORTOLANDIA": "Hortolândia", "SUMARE": "Sumaré",
             "PARNAIBA": "Parnaíba", "ANDRE": "André", "MAUA": "Mauá", "RIBEIRAO": "Ribeirão"}
_MINUSCULAS = {"DA", "DAS", "DE", "DO", "DOS"}

TEXTOS = [
    ("Como funciona",
     "Fora da Grande São Paulo, cada região é visitada em dias fixos da semana. Pedido sem data de entrega é "
     "agendado automaticamente para o próximo dia de visita da região."),
    ("Data escolhida pelo embarcador",
     "Se o pedido chega com data de entrega fora dos dias de visita da região, a data é respeitada e o pedido "
     "é tratado como envio dedicado, com valor calculado pela tabela de frete dedicado da Fresh Log. Você "
     "recebe um aviso por e-mail (e por WhatsApp, se cadastrado no portal) e pode pedir outra data dentro dos "
     "dias de visita. No envio pelo portal, o aviso aparece antes de confirmar."),
    ("Prazo de entrega",
     "Até 3 dias úteis na Grande São Paulo e nas regiões internas; até 7 dias corridos nas regiões de visita "
     "semanal; até 15 dias corridos na região de visita quinzenal."),
    ("Contato", f"Dúvidas e pedidos de data: {CONTATO} ou pelo chat do portal do cliente."),
]


def nome_bonito(cidade: str) -> str:
    palavras = []
    for p in str(cidade or "").split():
        if p in _MINUSCULAS and palavras:
            palavras.append(p.lower())
        else:
            palavras.append(_PALAVRAS.get(p, p.capitalize()))
    return " ".join(palavras)


def _frequencia(cfg: dict) -> str:
    if cfg.get("frequencia") == rdf.FREQUENCIA_QUINZENAL:
        return "Quinzenal"
    n = len(cfg["dias"])
    return "Semanal" if n == 1 else f"{n}x por semana"


def _prazo(cfg: dict, externa: bool) -> str:
    nivel = rdf.nivel_da_regra({"frequencia": cfg.get("frequencia", rdf.FREQUENCIA_SEMANAL), "externa": externa})
    dias, uteis = rdf.PRAZO_POR_NIVEL[nivel]
    return f"até {dias} dias {'úteis' if uteis else 'corridos'}"


def _dias(cfg: dict) -> str:
    texto = rdf.nomes_dias(cfg["dias"])
    if cfg.get("frequencia") == rdf.FREQUENCIA_QUINZENAL and cfg.get("ancora"):
        texto += f" (a cada 15 dias, a partir de {date.fromisoformat(cfg['ancora']):%d/%m/%Y})"
    return texto


def linhas_informativo() -> list[dict]:
    linhas = []
    for r in rdf.REGIOES:
        linhas.append({"regiao": r["nome"], "cidades": ", ".join(nome_bonito(c) for c in r["cidades"]),
                       "dias": _dias(r), "frequencia": _frequencia(r), "prazo": _prazo(r, r.get("externa", False))})
    for e in rdf.ENDERECOS_DIA_FIXO:
        linhas.append({"regiao": e["nome"], "cidades": f"Ponto de entrega: {nome_bonito(e['padroes'][0])}",
                       "dias": _dias(e), "frequencia": _frequencia(e), "prazo": _prazo(e, False)})
    return linhas


# Colunas da tabela: (chave, título, largura em px a 150 dpi).
_COLUNAS = [("regiao", "Região", 200), ("cidades", "Cidades", 380), ("dias", "Dias de visita", 220),
            ("frequencia", "Frequência", 120), ("prazo", "Prazo", 120)]


def _nova_pagina() -> tuple[Image.Image, ImageDraw.ImageDraw, int]:
    img = Image.new("RGB", A4, "white")
    draw = ImageDraw.Draw(img)
    logo = _logo()
    if logo is not None:
        img.paste(logo, (MARGEM, 78), logo)
    draw.text((img.width - MARGEM, 105), "REGIÕES E DIAS DE VISITA", font=_fonte(40, True), fill=NAVY, anchor="rm")
    draw.text((img.width - MARGEM, 160), f"Informativo aos embarcadores · {date.today():%d/%m/%Y}",
              font=_fonte(22), fill=CINZA_TXT, anchor="rm")
    draw.rectangle([(MARGEM, 205), (img.width - MARGEM, 212)], fill=TEAL)
    return img, draw, 250


def gerar_pdf(linhas: list[dict], caminho: Path) -> Path:
    paginas = []
    img, draw, y = _nova_pagina()
    fonte, negrito = _fonte(19), _fonte(19, True)
    altura_util = A4[1] - 140

    def cabecalho_tabela(draw, y):
        x = MARGEM
        draw.rectangle([(MARGEM, y), (A4[0] - MARGEM, y + 34)], fill=NAVY)
        for _chave, titulo, largura in _COLUNAS:
            draw.text((x + 8, y + 7), titulo, font=negrito, fill="white")
            x += largura
        return y + 40

    y = cabecalho_tabela(draw, y)
    for i, linha in enumerate(linhas):
        quebras = {chave: _quebrar(draw, linha[chave], fonte, largura - 16) for chave, _t, largura in _COLUNAS}
        altura = 26 * max(len(v) for v in quebras.values()) + 12
        if y + altura > altura_util:
            paginas.append(img)
            img, draw, y = _nova_pagina()
            y = cabecalho_tabela(draw, y)
        if i % 2:
            draw.rectangle([(MARGEM, y), (A4[0] - MARGEM, y + altura)], fill=CINZA_ZEBRA)
        x = MARGEM
        for chave, _t, largura in _COLUNAS:
            for n, texto in enumerate(quebras[chave]):
                draw.text((x + 8, y + 6 + 26 * n), texto, font=negrito if chave == "regiao" else fonte, fill=PRETO)
            x += largura
        y += altura
        draw.line([(MARGEM, y), (A4[0] - MARGEM, y)], fill=CINZA_LINHA, width=1)

    y += 30
    for titulo, texto in TEXTOS:
        corpo = _quebrar(draw, texto, _fonte(21), A4[0] - 2 * MARGEM)
        if y + 40 + 30 * len(corpo) > altura_util:
            paginas.append(img)
            img, draw, y = _nova_pagina()
        draw.text((MARGEM, y), titulo, font=_fonte(24, True), fill=NAVY)
        y += 38
        for t in corpo:
            draw.text((MARGEM, y), t, font=_fonte(21), fill=PRETO)
            y += 30
        y += 16
    paginas.append(img)

    for n, pg in enumerate(paginas, start=1):
        ImageDraw.Draw(pg).text((A4[0] // 2, A4[1] - 60),
                                f"Fresh Log · gerado em {datetime.now():%d/%m/%Y %H:%M} · página {n}/{len(paginas)}",
                                font=_fonte(18), fill=CINZA_TXT, anchor="mm")
    caminho = Path(caminho)
    caminho.parent.mkdir(parents=True, exist_ok=True)
    paginas[0].save(caminho, format="PDF", save_all=True, append_images=paginas[1:], resolution=DPI)
    return caminho


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Gera o informativo de regiões e dias de visita (PDF)")
    parser.add_argument("--saida", default=None, help="caminho do PDF (padrão: dados/informativos/)")
    args = parser.parse_args(argv)
    destino = Path(args.saida) if args.saida else PASTA_SAIDA / f"informativo_regioes_{date.today():%Y-%m-%d}.pdf"
    print(f"Informativo gravado em {gerar_pdf(linhas_informativo(), destino)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Rodar e ver passar**

```bash
py -3.11 -m unittest test_gerar_informativo_regioes -v 2>&1 | tail -3
py -3.11 gerar_informativo_regioes.py
```

Expected: `OK`; a segunda linha imprime `Informativo gravado em ...dados\informativos\informativo_regioes_AAAA-MM-DD.pdf`.

- [ ] **Step 5: Olhar o PDF**

Abrir o PDF gerado com a ferramenta Read (ela mostra páginas de PDF) e conferir: 1 a 2 páginas, tabela com as 7 regiões e os 4 pontos de entrega, acentos certos, os quatro textos e o contato. Se o texto passar da borda ou se sobrepor, ajustar larguras em `_COLUNAS` (soma tem que ser `A4[0] - 2 * MARGEM` = 1040) e gerar de novo. O PDF fica em `dados/` (fora do git): não commitar.

- [ ] **Step 6: Commit**

```bash
git add gerar_informativo_regioes.py test_gerar_informativo_regioes.py
git commit -m "Informativo: PDF de regioes, dias de visita, frequencia e prazo gerado da configuracao

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 12: Replay com a configuração nova e mapa do sistema

**Files:**
- Modify: `roteirizacao/replay_rotas.py`
- Modify: `roteirizacao/test_replay_rotas.py`
- Modify: `MAPA_DO_SISTEMA.txt`

**Interfaces:**
- Consumes: `regioes_dia_fixo.regra_dia_fixo_do_servico`, `data_valida_na_regiao`, `proxima_data_valida`, constantes de dia (Task 1); `servicos_do_dia`, `rodar_dia` (já existem).
- Produces: `replay_rotas.DIAS_ANTIGOS`, `redistribuir_dias_fixos_v2(servicos_por_dia: dict[date, list[dict]]) -> tuple[dict[date, list[dict]], int]`; opção `--dias-fixos-v2`.

- [ ] **Step 1: Acrescentar o teste**

Em `roteirizacao/test_replay_rotas.py`, acrescentar antes do `if __name__`:

```python
class DiasFixosV2TestCase(unittest.TestCase):
    ABCD = "Rua A 1, Centro, Santo André - SP, 09000-000, Brasil"
    TRANSFRIOS = "Estrada Francisco Hengles, 591, Potuvera, Itapecerica da Serra - SP, 06885-160, Brasil"
    CAMPINAS = "Rua B 2, Centro, Campinas - SP, 13000-000, Brasil"
    SP = "Rua C 3, Centro, Sao Paulo - SP, 01000-000, Brasil"

    def test_move_so_quem_caiu_no_dia_pela_regra_antiga(self):
        seg, ter, qua, qui, sex = (date(2026, 9, 28), date(2026, 9, 29), date(2026, 9, 30),
                                   date(2026, 10, 1), date(2026, 10, 2))
        s = lambda i, endereco: {"id": i, "address": endereco}  # noqa: E731
        mapa = {seg: [s(1, self.ABCD), s(6, self.TRANSFRIOS)],
                qua: [s(2, self.ABCD), s(3, self.CAMPINAS), s(4, self.SP)],
                qui: [s(5, self.CAMPINAS)],
                sex: [s(7, self.ABCD)]}
        novo, movidos = rr.redistribuir_dias_fixos_v2(mapa)
        ids = lambda d: sorted(x["id"] for x in novo.get(d, []))  # noqa: E731
        self.assertEqual(movidos, 3)
        self.assertEqual(ids(seg), [1])
        self.assertEqual(ids(ter), [6])              # Transfrios segunda -> terça
        self.assertEqual(ids(qua), [3, 4])
        self.assertEqual(ids(qui), [2, 5])           # ABCD quarta -> quinta; Campinas quinta (embarcador) fica
        self.assertEqual(ids(sex), [])
        self.assertEqual(ids(date(2026, 10, 5)), [7])  # ABCD sexta -> segunda
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `py -3.11 -m unittest roteirizacao.test_replay_rotas -v`
Expected: `AttributeError: module 'replay_rotas' has no attribute 'redistribuir_dias_fixos_v2'`.

- [ ] **Step 3: Implementar em `roteirizacao/replay_rotas.py`**

3a. Depois de `import rotas_fracas`, acrescentar `import regioes_dia_fixo as rdf`.

3b. Depois de `ARQUIVO_RESULTADO = ...`, acrescentar:

```python
# Dias de visita ANTES de 03/10 (dias fixos v2), pra --dias-fixos-v2 saber
# quem so caiu naquele dia por causa da regra antiga. Regiao fora daqui nao
# mudou de dia (Sorocaba mudou so de frequencia: semana impar sai).
DIAS_ANTIGOS = {"ABCD": [rdf.SEGUNDA, rdf.QUARTA, rdf.SEXTA], "Transfrios": [rdf.SEGUNDA, rdf.QUARTA]}


def _valido_na_regra_antiga(regra: dict, dia: date) -> bool:
    return dia.weekday() in DIAS_ANTIGOS.get(regra.get("regiao") or regra["nome"], regra["dias"])


def redistribuir_dias_fixos_v2(servicos_por_dia: dict[date, list[dict]]) -> tuple[dict[date, list[dict]], int]:
    """Move pro proximo dia de visita da regra NOVA o pedido que caiu num dia
    valido da regra antiga e invalido da nova. Pedido com data do embarcador
    fora do dia (invalido nas duas) fica onde esta: o replay nao simula a
    marcacao como dedicado. Devolve (novo mapa, quantos mudaram de dia)."""
    novo: dict[date, list[dict]] = {d: [] for d in servicos_por_dia}
    movidos = 0
    for dia in sorted(servicos_por_dia):
        for s in servicos_por_dia[dia]:
            regra = rdf.regra_dia_fixo_do_servico(s)
            if regra and _valido_na_regra_antiga(regra, dia) and not rdf.data_valida_na_regiao(regra, dia):
                novo.setdefault(rdf.proxima_data_valida(regra, dia), []).append(s)
                movidos += 1
            else:
                novo[dia].append(s)
    return novo, movidos
```

3c. Em `main`, depois de `parser.add_argument("--modelo", ...)`, acrescentar:

```python
    parser.add_argument("--dias-fixos-v2", action="store_true",
                        help="move pro proximo dia de visita o pedido que so caiu no dia pela regra antiga")
```

3d. No `cabecalho`, antes do trecho do `modelo`, acrescentar ao f-string:

```python
                 f" | dias_fixos_v2={'on' if args.dias_fixos_v2 else 'off'}"
```

3e. Trocar o laço `dia = date.fromisoformat(args.de) ... while dia <= fim: ...` inteiro (até o `dia += timedelta(days=1)` final dele) por:

```python
    dias = []
    dia = date.fromisoformat(args.de)
    fim = date.fromisoformat(args.ate)
    while dia <= fim:
        dias.append(dia)
        dia += timedelta(days=1)
    lidos = {d: servicos_do_dia(conn, d.isoformat(), mapa_tipos) for d in dias}
    servicos_por_dia = {d: servicos for d, (servicos, _rotas) in lidos.items()}
    if args.dias_fixos_v2:
        servicos_por_dia, movidos = redistribuir_dias_fixos_v2(servicos_por_dia)
        depois = sum(len(v) for d, v in servicos_por_dia.items() if d > fim)
        linhas.append(f"dias_fixos_v2: {movidos} pedido(s) mudaram de dia; {depois} caíram depois de {fim} "
                      f"(fora da medição)")
    for dia in dias:
        servicos, rotas = servicos_por_dia.get(dia, []), lidos[dia][1]
        # Aviso: dias com menos de 2 pedidos sao pulados sem imprimir nada (ver limitacao #3 no docstring).
        if len(servicos) >= 2 and rotas:
            try:
                env, novo = rodar_dia(servicos, rotas, coords_base, dia, args.modelo)
            except Exception as e:
                linhas.append(f"{dia}: ERRO {e}")
                continue
            _somar(total_env, env)
            _somar(total_novo, novo)
            linhas.append(mp.formatar_metricas(env, f"{dia} enviado"))
            linhas.append(mp.formatar_metricas(novo, f"{dia} novo   "))
```

3f. Na docstring do módulo, em "COMO USAR", acrescentar a linha:

```
    py -3.11 roteirizacao/replay_rotas.py --de ... --ate ... --dias-fixos-v2
```

e em "LIMITACOES CONHECIDAS", o item:

```
  5. DIAS FIXOS V2 SEM DEDICADO: --dias-fixos-v2 so muda de dia quem caiu
     no dia pela regra antiga (ABCD qua/sex, Transfrios seg/qua, Sorocaba
     em semana impar). Pedido com data do embarcador fora do dia continua
     no dia dele; na operacao ele vira dedicado e sai da rota.
```

- [ ] **Step 4: Rodar e ver passar**

```bash
py -3.11 -m unittest roteirizacao.test_replay_rotas roteirizacao.test_metricas_plano -v 2>&1 | tail -3
```

Expected: `OK`.

- [ ] **Step 5: Rodar o replay de 31 dias, com e sem a configuração nova**

Precisa de `dados/dados_replay.db` (gitignored; fica no working tree principal). Conferir a data da cópia:

```bash
ls -l /c/agente_stokki_eventos/dados/dados_replay.db
```

Se a cópia for anterior a 02/10/2026, pedir ao Hugo autorização para copiar de novo da VPS (comando na docstring de `replay_rotas.py`; é só leitura lá). Com a cópia em dia:

```bash
py -3.11 roteirizacao/replay_rotas.py --de 2026-09-01 --ate 2026-10-01 --banco /c/agente_stokki_eventos/dados/dados_replay.db 2>&1 | tail -3
py -3.11 roteirizacao/replay_rotas.py --de 2026-09-01 --ate 2026-10-01 --banco /c/agente_stokki_eventos/dados/dados_replay.db --dias-fixos-v2 2>&1 | grep -E "dias_fixos_v2:|TOTAL"
```

Anotar, das linhas `TOTAL novo`, os dois lados: rotas, rotas fracas, km e rotas acima do teto, e a linha `dias_fixos_v2:` (quantos mudaram de dia). Atenção: a primeira rodada (sem a opção) já usa a regra nova no planejamento, mas os pedidos ficam no dia em que saíram; a comparação que interessa é rotas e rotas fracas entre as duas linhas `TOTAL novo`. Critério: rotas acima do teto continua **0**. Se passar de 0 com `--dias-fixos-v2` (ABCD em 2 dias concentra ~14 pedidos por dia, risco 3 da spec), levar o número ao Hugo antes do deploy. Os números vão no relatório final (Task 13), não em arquivo commitado.

- [ ] **Step 6: Atualizar o `MAPA_DO_SISTEMA.txt`**

Achar as linhas vizinhas para copiar o formato:

Run: `grep -n "Dia fixo por região\|regioes_dia_fixo.py \|pedidos_segurados.py \|pedidos_dedicados.py \|pedidos_segurados         roteirizacao\|ARMADILHAS CONHECIDAS" MAPA_DO_SISTEMA.txt`

Acrescentar, no mesmo formato das linhas encontradas:

- no índice, trocar `Dia fixo por região ....................... roteirizacao/regioes_dia_fixo.py` por `Dia fixo por região ....................... roteirizacao/regioes_dia_fixo.py, fora_dia_fixo.py` e acrescentar a linha `Informativo de regiões (PDF) .............. gerar_informativo_regioes.py`;
- na seção `roteirizacao/`, junto de `regioes_dia_fixo.py`, trocar a descrição para `Regiões com dia fixo: dias, frequência (Sorocaba quinzenal), prazo por nível, cidade truncada.` e acrescentar:
  - `fora_dia_fixo.py` — "Data do embarcador fora do dia de visita: marca dedicado (valor da calculadora, km em linha reta) e chama o aviso. Roda no job das 18h e no incremento."
  - `notificar_fora_dia_fixo.py` — "Aviso único ao embarcador (e-mail + WhatsApp). fora_dia_fixo.forcar_destino ausente = hugo@."
  - `registro_dia_fixo.py` — "Tabelas agendamentos_origem (origem da data gravada pelo sistema: DIA_FIXO ou EQUIPE) e avisos_fora_dia_fixo."
- na seção `painel_agentes/`, junto de `planejamento_rotas.py`: "Reagendar (um ou lote) registra a data como EQUIPE em agendamentos_origem (não vira dedicado sozinho); data fora do dia de visita abre a pergunta 'Marcar como dedicado?' (POST /api/planejamento/checar-dia-fixo, valor da calculadora)."
- na lista de arquivos da raiz, `gerar_informativo_regioes.py` — "PDF do informativo de regiões (dados/informativos/). Envio é manual, do Hugo."
- na lista de tabelas, acrescentar `agendamentos_origem, avisos_fora_dia_fixo` ao grupo de `roteirizacao/` (ao lado de `pedidos_segurados`);
- em `17. ARMADILHAS CONHECIDAS`: "Data só vira dedicado quando é do cliente: reagendar pelo Planejamento grava EQUIPE em agendamentos_origem e fica protegido; data trocada direto na tela da Vuupt não passa pelo sistema, conta como do cliente e vira dedicado se cair fora do dia de visita. Pedido criado antes de fora_dia_fixo.DETECCAO_A_PARTIR_DE não é marcado."

- [ ] **Step 7: Commit**

```bash
git add roteirizacao/replay_rotas.py roteirizacao/test_replay_rotas.py MAPA_DO_SISTEMA.txt
git commit -m "Replay: opcao --dias-fixos-v2 mede a configuracao nova; mapa do sistema atualizado

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 13: Conferência final

**Files:** nenhum arquivo novo.

- [ ] **Step 1: Rodar tudo de novo**

```bash
py -3.11 -m unittest discover -s roteirizacao -p "test_*.py" 2>&1 | tail -3
py -3.11 -m unittest discover -s painel_agentes -p "test_*.py" 2>&1 | tail -3
py -3.11 -m unittest vigia.test_regras vigia.test_vigiar test_notificar_whatsapp test_whatsapp_fora_dia_fixo test_pedidos_dedicados test_gerar_informativo_regioes portal_cliente.test_bloqueio_area portal_cliente.test_fora_dia_fixo_envio 2>&1 | tail -3
```

Expected: `OK` nos três, com a contagem da Task 0 mais os testes novos: em `roteirizacao`, +18 (regiões, Tasks 1-2) +12 (registro, Task 3) +22 (fora do dia fixo, Task 4) +14 (aviso/tratar, Task 6) +2 (integração) +1 (dedicados) +1 (replay); em `painel_agentes`, +4 (equipe, Task 3A) +10 (reagendar fora do dia, Task 4A) +2 (aviso de dia fixo); no terceiro comando, +8 do WhatsApp, +3 do vigia (regras) +2 (vigiar), +4 do informativo e +9 do portal. Se a contagem divergir, conferir qual arquivo de teste ficou de fora antes de seguir.

- [ ] **Step 2: Compilar tudo que foi editado**

```bash
py -3.11 -m py_compile painel_agentes/painel_agentes.py roteirizacao/regioes_dia_fixo.py roteirizacao/registro_dia_fixo.py roteirizacao/fora_dia_fixo.py roteirizacao/notificar_fora_dia_fixo.py roteirizacao/criar_rotas_diarias.py roteirizacao/incrementar_rotas.py roteirizacao/dedicados.py roteirizacao/replay_rotas.py pedidos_dedicados.py notificar_whatsapp.py painel_agentes/planejamento_rotas.py portal_cliente/envio_pedidos.py portal_cliente/app.py vigia/regras.py vigia/vigiar.py gerar_informativo_regioes.py
```

Expected: sem saída.

- [ ] **Step 3: Modo teste do job das 18h (sem escrever na Vuupt)**

```bash
py -3.11 roteirizacao/criar_rotas_diarias.py --modo-teste 2>&1 | grep -i "fora do dia\|MODO TESTE.*dedicado\|Falha na regra" | head -20
```

Expected: nenhuma linha `Falha na regra de data fora do dia fixo`. Pode haver linhas `[MODO TESTE] ... seria marcado como dedicado` (o banco local está congelado desde 17/08, então provavelmente não há candidatos: tudo foi criado antes do corte). Se o script local não rodar por falta de credencial/sessão, registrar e seguir: a integração está coberta pela Task 7.

- [ ] **Step 4: Conferir o que o ramo mexeu**

Run: `git diff --stat origin/master...dias-fixos-v2`
Expected: só os arquivos do mapa de arquivos deste plano, mais a spec e o plano. Em `roteirizacao/criar_rotas_diarias.py`, só `+9`.

- [ ] **Step 5: Entregar ao Hugo**

Relatar: contagem de testes, números do replay (com e sem `--dias-fixos-v2`: rotas, rotas fracas, km, acima do teto, quantos mudaram de dia), caminho do PDF gerado, e as "Decisões do plano" para ele confirmar. Não fazer merge, push nem deploy sem pedido.

Pontos para o deploy (quando o Hugo pedir):
- No commit do deploy, ajustar `fora_dia_fixo.DETECCAO_A_PARTIR_DE` para o dia do deploy e, se o deploy cair em 13/10/2026 ou depois, a `"ancora"` da Sorocaba em `regioes_dia_fixo.REGIOES` para a terça seguinte ao deploy (e conferir que o teste da Task 3 continua passando).
- `config.yaml` da VPS: a seção `fora_dia_fixo` pode ficar ausente (avisos vão pra hugo@). O WhatsApp ao cliente só sai com `whatsapp_notificacoes.clientes.ativo: true`; com `clientes.forcar_destino` preenchido, vai pro número do Hugo. Ligar o envio real é decisão do Hugo.
- Reiniciar `painel-agentes` (importa `planejamento_rotas` e `dedicados`) e `portal-cliente` (importa `envio_pedidos` e `app`). Os jobs de lote (criar rotas, incremento, vigia) pegam o código novo sozinhos.
- As tabelas `agendamentos_origem` e `avisos_fora_dia_fixo` são criadas na primeira execução. Rodar a primeira vez como `www-data`, nunca como root.
- Prova real depois do deploy: `sudo -u www-data venv/bin/python -c "import sys; sys.path[:0]=['roteirizacao']; import regioes_dia_fixo as r; print(r.regra_dia_fixo_do_servico({'address': 'Rua A 1, Centro, Santo André - SP, 09000-000, Brasil'})['dias'])"` deve imprimir `[0, 3]`; e gerar o PDF na VPS ou local (`py -3.11 gerar_informativo_regioes.py`) e entregar ao Hugo.
