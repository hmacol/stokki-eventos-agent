# Notificações internas por WhatsApp (OpenWA) — Plano de implementação

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Quatro notificações internas que hoje saem só por e-mail passam a sair também num grupo de WhatsApp, pelo gateway OpenWA, desligadas por padrão.

**Architecture:** Um módulo de transporte (`integracao_openwa.py`) fala com a API REST do gateway. Uma camada (`notificar_whatsapp.py`) monta o texto curto, aplica as regras de volume e registra cada tentativa em `dados.db`. Três rotinas existentes ganham uma chamada a mais, sempre depois do e-mail e dentro de `try/except`.

**Tech Stack:** Python 3.11, `requests`, `sqlite3`, `unittest` + `unittest.mock`. Gateway OpenWA (github.com/rmyndharis/OpenWA) em Docker na VPS, motor `whatsapp-web.js`.

**Spec:** `docs/superpowers/specs/2026-09-28-notificacoes-whatsapp-openwa-design.md`

## Global Constraints

- Idioma: português em código, comentários, commits e mensagens.
- Python local: sempre `py -3.11`. Na VPS: `/opt/stokki-eventos/venv/bin/python`, como `www-data`.
- Testes com `unittest` (não há pytest). Nenhum teste toca rede nem o `dados/dados.db` real.
- `config.yaml` nunca é commitado, sobrescrito nem tem valores colados no chat ou em log.
- Nada em `atendimento/`, `integracao_evolution.py` ou na Evolution API é alterado.
- WhatsApp é adicional: nenhuma falha dele altera o resultado do e-mail ou da rotina.
- Desligado por padrão: seção `whatsapp_notificacoes` ausente ou `ativo: false` não envia e não abre o banco.
- Sem reenvio automático.
- Mensagens só com contagens e nomes de rotina. Sem nome de cliente, endereço, número de NF ou log.
- Padrões: `teto_diario: 20`, `intervalo_min_seg: 20`, `janela_repeticao_min: 120`, `falhas_para_alerta: 3`.
- Lista inicial `sempre_avisar`: `cancelar_rotas_sem_motorista`, `criar_rotas_diarias`, `pipeline`.
- MCP desligado no gateway. Porta 2785 só em 127.0.0.1.
- Trabalho em paralelo: antes de editar e antes de commitar, `git status --short` e `git log --oneline -5`. Só `git add` dos arquivos da tarefa.
- Commit local por tarefa só com autorização do Hugo. Push e deploy só quando ele pedir.

## Review Focus

1. **Detalhe de erro com formatação ou quebras de linha** (`*`, `_`, `~`, crase, `\n`, traceback longo): a mensagem deve sair em linhas únicas, sem formatação quebrada, com corte em 200 caracteres. Teste na Tarefa 2.
2. **`resumo_etapas` vazio, `None`, ou etapa com `info` igual a `None`**: deve gerar texto ou não enviar, nunca exceção. Teste na Tarefa 2.
3. **Banco indisponível** (`database is locked`, arquivo sem permissão): `despachar` devolve `"falhou"` e a rotina segue. Teste na Tarefa 3.
4. **Gateway responde 200 com corpo que não é JSON, ou sem `messageId`**: conta como enviado, com id `None`. HTTP 401/404/500 conta como falha. Teste na Tarefa 1.
5. **Configuração malformada** (`teto_diario: "vinte"`, `sempre_avisar` ausente ou `null`, `grupo_id` vazio): cai no padrão ou em "desligado", nunca exceção. Testes nas Tarefas 3 e 4.

## Estrutura de arquivos

| Arquivo | Ação | Responsabilidade |
|---|---|---|
| `infra/openwa/docker-compose.override.yml` | criar | Prende a porta do gateway em 127.0.0.1 e fixa as variáveis de ambiente |
| `infra/openwa/LEIAME.md` | criar | Como subir, parear e descobrir o grupo |
| `integracao_openwa.py` | criar | Transporte HTTP |
| `test_integracao_openwa.py` | criar | Testes do transporte |
| `notificar_whatsapp.py` | criar | Textos, regras, registro, alerta de canal parado |
| `test_notificar_whatsapp.py` | criar | Testes da camada |
| `notificar_execucao_agente.py` | modificar | Chamada em `notificar_execucao()` |
| `alertar_falha_job.py` | modificar | Chamada em `main()` |
| `verificar_entregues_nao_expedidos.py` | modificar | Chamada em `main()` |
| `MAPA_DO_SISTEMA.txt` | modificar | Arquivos, porta, tabela e configuração novos |

---

### Task 0: Prova do gateway (manual, com o Hugo presente)

Nenhum código de produção é escrito antes desta tarefa passar. Ela confirma os pontos da seção 12 da spec.

**Files:**
- Create: `infra/openwa/docker-compose.override.yml`
- Create: `infra/openwa/LEIAME.md`

**Interfaces:**
- Produces: gateway respondendo em `http://127.0.0.1:2785/api` na VPS; um `sessionId` (UUID) em estado `ready`; um `grupo_id` no formato `<numero>@g.us`; uma chave de API de papel `operator`. Esses três valores vão para o `config.yaml` da VPS, nunca para o git.

- [ ] **Step 1: Conferir que nada disso já existe na VPS**

```bash
ssh -i ~/.ssh/atendimento_vps root@187.127.52.197 "ls /opt | grep -i openwa; docker ps --format '{{.Names}}\t{{.Image}}\t{{.Ports}}'; ss -ltnp | grep -E ':2785|:3000' ; free -m | head -2"
```

Esperado: nenhuma pasta `openwa`, nada escutando em 2785, mais de 3000 MB em `available`.

- [ ] **Step 2: Clonar o gateway e descobrir o nome do serviço**

```bash
ssh -i ~/.ssh/atendimento_vps root@187.127.52.197 "git clone https://github.com/rmyndharis/OpenWA.git /opt/openwa && cd /opt/openwa && git log --oneline -1 && docker compose -f docker-compose.dev.yml config --services && grep -n -A3 'ports:' docker-compose.dev.yml"
```

Anotar: o SHA do commit clonado, o nome do serviço principal e o mapeamento de portas original.

- [ ] **Step 3: Escrever o override**

Criar `infra/openwa/docker-compose.override.yml`. Se o nome do serviço anotado no Step 2 não for `openwa`, trocar a chave `openwa:` pelo nome real.

```yaml
# Override do gateway OpenWA usado pelas notificacoes internas por WhatsApp.
# Copiado para /opt/openwa/ na VPS. Nao tem segredo: a chave mestra nasce
# no primeiro boot e fica so no volume do container.
services:
  openwa:
    restart: unless-stopped
    ports: !override
      - "127.0.0.1:2785:2785"
    environment:
      ENGINE_TYPE: whatsapp-web.js
      DATABASE_TYPE: sqlite
      AUTO_START_SESSIONS: "true"
      SEND_PACING_ENABLED: "true"
```

`MCP_ENABLED` fica de fora de propósito (desligado é o padrão).

- [ ] **Step 4: Subir e provar que a porta está só em 127.0.0.1**

```bash
scp -i ~/.ssh/atendimento_vps infra/openwa/docker-compose.override.yml root@187.127.52.197:/opt/openwa/docker-compose.override.yml
ssh -i ~/.ssh/atendimento_vps root@187.127.52.197 "cd /opt/openwa && docker compose -f docker-compose.dev.yml -f docker-compose.override.yml up -d && sleep 20 && ss -ltnp | grep 2785 && docker stats --no-stream --format '{{.Name}}\t{{.MemUsage}}'"
```

Esperado: a linha do `ss` mostra `127.0.0.1:2785`. Se mostrar `0.0.0.0:2785` ou `*:2785`, derrubar com `docker compose ... down` e corrigir o override antes de seguir.

- [ ] **Step 5: Obter a chave mestra e criar a chave de operador**

```bash
ssh -i ~/.ssh/atendimento_vps root@187.127.52.197 "cd /opt/openwa && docker compose -f docker-compose.dev.yml -f docker-compose.override.yml logs --no-color | grep -i -m5 'key'"
```

A chave aparece só no terminal do Hugo. Não colar no chat, não gravar em arquivo do repo.

O Hugo abre o painel por túnel, na máquina dele:

```bash
ssh -i ~/.ssh/atendimento_vps -L 2785:127.0.0.1:2785 root@187.127.52.197
```

e acessa `http://localhost:2785`. No painel: criar uma sessão chamada `notificacoes`, criar uma chave de API de papel `operator` restrita a essa sessão.

- [ ] **Step 6: Parear o número**

No painel, iniciar a sessão `notificacoes` e ler o QR com o WhatsApp do celular (Aparelhos conectados > Conectar um aparelho). Parear logo depois de gerar: o QR expira.

Conferir pela API, na VPS, com a chave de operador numa variável de ambiente da sessão SSH (não no histórico):

```bash
read -s API_KEY; export API_KEY
curl -s http://127.0.0.1:2785/api/sessions -H "X-API-Key: $API_KEY"
```

Esperado: a sessão `notificacoes` com `status: "ready"` e o telefone conectado. Anotar o `id` (UUID).

- [ ] **Step 7: Descobrir o identificador do grupo**

O Hugo cria (ou escolhe) o grupo interno no celular. Depois:

```bash
export SESSION_ID=<uuid do Step 6>
curl -s "http://127.0.0.1:2785/api/sessions/$SESSION_ID/groups" -H "X-API-Key: $API_KEY"
```

Esperado: um array com objetos `{"id": "...@g.us", "name": "..."}`. Anotar o `id` do grupo escolhido.

- [ ] **Step 8: Enviar a mensagem de prova**

```bash
export GRUPO_ID=<id do Step 7>
curl -s -X POST "http://127.0.0.1:2785/api/sessions/$SESSION_ID/messages/send-text" \
  -H "Content-Type: application/json" -H "X-API-Key: $API_KEY" \
  -d "{\"chatId\": \"$GRUPO_ID\", \"text\": \"Prova do gateway de notificacoes. Pode ignorar.\"}"
```

Esperado: resposta com `messageId` e `timestamp`, e a mensagem visível no grupo. Anotar o formato exato da resposta.

- [ ] **Step 9: Provar que a sessão sobrevive a reinício**

```bash
ssh -i ~/.ssh/atendimento_vps root@187.127.52.197 "cd /opt/openwa && docker compose -f docker-compose.dev.yml -f docker-compose.override.yml restart && sleep 60"
curl -s "http://127.0.0.1:2785/api/sessions/$SESSION_ID" -H "X-API-Key: $API_KEY"
```

Esperado: `status: "ready"` sem novo QR.

- [ ] **Step 10: Escrever o LEIAME e decidir**

Criar `infra/openwa/LEIAME.md` com: SHA clonado, nome real do serviço, comandos dos Steps 4 a 9 (sem valores de chave), memória observada e formato da resposta do envio.

**Portão de decisão.** Se algum dos Steps 6, 8 ou 9 falhar, parar aqui e relatar ao Hugo. Se a rota, o cabeçalho ou o corpo reais divergirem do que a Tarefa 1 assume, corrigir a Tarefa 1 antes de começá-la.

- [ ] **Step 11: Commit (com autorização do Hugo)**

```bash
git add infra/openwa/docker-compose.override.yml infra/openwa/LEIAME.md
git commit -m "Infra: gateway OpenWA para notificacoes internas por WhatsApp (so 127.0.0.1)"
```

---

### Task 1: Transporte `integracao_openwa.py`

**Files:**
- Create: `integracao_openwa.py`
- Test: `test_integracao_openwa.py`

**Interfaces:**
- Consumes: formato de rota e resposta confirmado na Tarefa 0.
- Produces:
  - `configurado(cfg: dict | None) -> bool`
  - `enviar_texto(cfg: dict, chat_id: str, texto: str) -> tuple[bool, str | None]`
  - `cfg` é a seção `whatsapp_notificacoes` do `config.yaml`, com as chaves `base_url`, `api_key`, `sessao`.

- [ ] **Step 1: Escrever os testes que falham**

Criar `test_integracao_openwa.py`:

```python
"""Testes do transporte do WhatsApp das notificacoes (integracao_openwa.py).

Rodar: py -3.11 -m unittest test_integracao_openwa
"""

import unittest
from unittest.mock import MagicMock, patch

import requests

import integracao_openwa as openwa

CFG = {"base_url": "http://127.0.0.1:2785/api/", "api_key": "chave", "sessao": "abc-123"}


def _resposta(status=200, corpo=None, json_invalido=False):
    resp = MagicMock()
    resp.status_code = status
    if status >= 400:
        resp.raise_for_status.side_effect = requests.HTTPError(f"{status}")
    if json_invalido:
        resp.json.side_effect = ValueError("nao e json")
    else:
        resp.json.return_value = corpo
    return resp


class TestConfigurado(unittest.TestCase):
    def test_completo(self):
        self.assertTrue(openwa.configurado(CFG))

    def test_incompleto_ou_vazio(self):
        self.assertFalse(openwa.configurado(None))
        self.assertFalse(openwa.configurado({}))
        self.assertFalse(openwa.configurado(dict(CFG, api_key="")))
        self.assertFalse(openwa.configurado({"base_url": "x", "api_key": "y"}))


class TestEnviarTexto(unittest.TestCase):
    def test_sucesso_monta_rota_cabecalho_e_corpo(self):
        with patch.object(openwa.requests, "post",
                          return_value=_resposta(corpo={"messageId": "m1", "timestamp": 1})) as post:
            self.assertEqual(openwa.enviar_texto(CFG, "1@g.us", "oi"), (True, "m1"))
        args, kwargs = post.call_args
        self.assertEqual(args[0], "http://127.0.0.1:2785/api/sessions/abc-123/messages/send-text")
        self.assertEqual(kwargs["json"], {"chatId": "1@g.us", "text": "oi"})
        self.assertEqual(kwargs["headers"], {"X-API-Key": "chave"})
        self.assertEqual(kwargs["timeout"], 15)

    def test_sem_config_destino_ou_texto_nao_chama_a_rede(self):
        with patch.object(openwa.requests, "post") as post:
            self.assertEqual(openwa.enviar_texto({}, "1@g.us", "oi"), (False, None))
            self.assertEqual(openwa.enviar_texto(CFG, "", "oi"), (False, None))
            self.assertEqual(openwa.enviar_texto(CFG, "1@g.us", ""), (False, None))
        post.assert_not_called()

    def test_erro_http_vira_falha(self):
        for status in (401, 404, 500):
            with patch.object(openwa.requests, "post", return_value=_resposta(status=status)):
                self.assertEqual(openwa.enviar_texto(CFG, "1@g.us", "oi"), (False, None))

    def test_erro_de_rede_vira_falha(self):
        with patch.object(openwa.requests, "post", side_effect=requests.ConnectionError("fora do ar")):
            self.assertEqual(openwa.enviar_texto(CFG, "1@g.us", "oi"), (False, None))

    def test_200_sem_json_conta_como_enviado(self):
        with patch.object(openwa.requests, "post", return_value=_resposta(json_invalido=True)):
            self.assertEqual(openwa.enviar_texto(CFG, "1@g.us", "oi"), (True, None))

    def test_200_sem_message_id_ou_com_lista(self):
        with patch.object(openwa.requests, "post", return_value=_resposta(corpo={})):
            self.assertEqual(openwa.enviar_texto(CFG, "1@g.us", "oi"), (True, None))
        with patch.object(openwa.requests, "post", return_value=_resposta(corpo=[1, 2])):
            self.assertEqual(openwa.enviar_texto(CFG, "1@g.us", "oi"), (True, None))

    def test_chave_nao_aparece_no_log(self):
        with patch.object(openwa.requests, "post", side_effect=requests.ConnectionError("x")), \
                self.assertLogs(openwa.logger, level="WARNING") as logs:
            openwa.enviar_texto(CFG, "1@g.us", "oi")
        self.assertNotIn("chave", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `py -3.11 -m unittest test_integracao_openwa -v`
Expected: `ModuleNotFoundError: No module named 'integracao_openwa'`

- [ ] **Step 3: Implementar**

Criar `integracao_openwa.py`:

```python
# -*- coding: utf-8 -*-
"""
integracao_openwa.py

Envio de WhatsApp pelo gateway OpenWA (github.com/rmyndharis/OpenWA,
auto-hospedado, nao-oficial) -- usado SO pelas notificacoes internas
(notificar_whatsapp.py). Nao tem relacao com a central de atendimento,
que usa a Evolution API (integracao_evolution.py).

O numero pareado e o do proprio Hugo (decisao dele, 28/09/2026, risco de
banimento aceito). Por isso o volume e controlado em notificar_whatsapp.py
e aqui nao existe reenvio.

config.yaml (nao versionado):
    whatsapp_notificacoes:
        base_url: "http://127.0.0.1:2785/api"   # gateway nunca e exposto pelo Caddy
        api_key: "..."                          # chave de papel operator
        sessao: "..."                           # id (UUID) da sessao no gateway
"""
import logging

import requests

logger = logging.getLogger(__name__)

_TIMEOUT = 15


def configurado(cfg: dict | None) -> bool:
    if not cfg:
        return False
    return all(cfg.get(chave) for chave in ("base_url", "api_key", "sessao"))


def enviar_texto(cfg: dict, chat_id: str, texto: str) -> tuple[bool, str | None]:
    """POST /sessions/{sessao}/messages/send-text. `chat_id` no formato do
    WhatsApp ('<numero>@g.us' pra grupo, '<telefone>@c.us' pra pessoa).
    Nunca levanta excecao -- qualquer falha vira (False, None). Retorna
    (sucesso, id da mensagem no gateway)."""
    if not configurado(cfg) or not chat_id or not texto:
        return False, None
    url = f"{cfg['base_url'].rstrip('/')}/sessions/{cfg['sessao']}/messages/send-text"
    try:
        resp = requests.post(url, json={"chatId": chat_id, "text": texto},
                             headers={"X-API-Key": cfg["api_key"]}, timeout=_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.warning(f"Falha ao enviar WhatsApp pelo OpenWA: {exc}")
        return False, None
    try:
        dados = resp.json()
    except ValueError:
        dados = None
    return True, dados.get("messageId") if isinstance(dados, dict) else None
```

- [ ] **Step 4: Rodar e ver passar**

Run: `py -3.11 -m unittest test_integracao_openwa -v`
Expected: 9 testes, `OK`

- [ ] **Step 5: Commit (com autorização do Hugo)**

```bash
git add integracao_openwa.py test_integracao_openwa.py
git commit -m "WhatsApp: transporte do OpenWA para as notificacoes internas"
```

---

### Task 2: Textos das mensagens

**Files:**
- Create: `notificar_whatsapp.py`
- Test: `test_notificar_whatsapp.py`

**Interfaces:**
- Consumes: de `notificar_execucao_agente`: `titulo_da_rotina(resumo_etapas, titulo)`, `etapas_com_erro(resumo_etapas) -> list[str]`, `resumir_execucao(resumo_etapas) -> str`, `formatar_duracao(segundos) -> str`.
- Produces:
  - `texto_execucao(resumo_etapas: dict, duracao_seg: float, titulo: str | None = None, agora: datetime | None = None) -> str`
  - `texto_falha_job(unidade: str, info: dict, agora: datetime | None = None) -> str`
  - `texto_nao_expedidos(n_alertas: int, n_rotas: int, n_retiradas: int) -> str`

`resumo_etapas` tem o formato `{"Nome da etapa": {"status": "ok" | "erro", "detalhe": "texto"}}`.

- [ ] **Step 1: Escrever os testes que falham**

Criar `test_notificar_whatsapp.py`:

```python
"""Testes das notificacoes internas por WhatsApp (notificar_whatsapp.py).

Rodar: py -3.11 -m unittest test_notificar_whatsapp
"""

import unittest
from datetime import datetime
from unittest.mock import patch

import notificar_whatsapp as nw

AGORA = datetime(2026, 9, 16, 22, 5)
UMA = {"Criação de rotas": {"status": "ok", "detalhe": "12 rotas criadas."}}
VARIAS_OK = {
    "Agendamento": {"status": "ok", "detalhe": "3 pedidos agendados"},
    "Impressão": {"status": "ok", "detalhe": "Nada a imprimir"},
    "Pipeline": {"status": "ok", "detalhe": "40 pedido(s)"},
}
VARIAS_ERRO = dict(VARIAS_OK, Pipeline={"status": "erro", "detalhe": "TimeoutError: <stokki>\nlinha 2"},
                   Retiradas={"status": "erro", "detalhe": "401"})


class TestTextoExecucao(unittest.TestCase):
    def test_sucesso(self):
        self.assertEqual(nw.texto_execucao(UMA, 42, agora=AGORA),
                         "✅ *Criação de rotas* · 16/09 22:05\nEtapa concluída sem erro (42 s)")

    def test_erro_lista_etapas_e_aponta_o_email(self):
        with patch("notificar_execucao_agente.sys.argv", ["desconhecido.py"]):
            texto = nw.texto_execucao(VARIAS_ERRO, 138, agora=AGORA)
        self.assertEqual(texto, "\n".join([
            "❌ *Agente Stokki Eventos* · 16/09 22:05",
            "2 de 4 etapas com erro: Pipeline, Retiradas",
            "Pipeline: TimeoutError: <stokki> linha 2",
            "Retiradas: 401",
            "Detalhes completos no e-mail.",
        ]))

    def test_detalhe_longo_e_cortado_em_200(self):
        etapas = {"Pipeline": {"status": "erro", "detalhe": "x" * 500}}
        linha = nw.texto_execucao(etapas, 1, agora=AGORA).split("\n")[2]
        self.assertEqual(len(linha), len("Pipeline: ") + 200)
        self.assertTrue(linha.endswith("…"))

    def test_detalhe_perde_formatacao_do_whatsapp(self):
        etapas = {"Pipeline": {"status": "erro", "detalhe": "erro *grave* em _campo_ ~x~ `y`"}}
        self.assertIn("Pipeline: erro grave em campo x y", nw.texto_execucao(etapas, 1, agora=AGORA))

    def test_no_maximo_tres_etapas_com_detalhe(self):
        etapas = {f"E{i}": {"status": "erro", "detalhe": f"d{i}"} for i in range(5)}
        with patch("notificar_execucao_agente.sys.argv", ["desconhecido.py"]):
            linhas = nw.texto_execucao(etapas, 1, agora=AGORA).split("\n")
        self.assertEqual(linhas[2:5], ["E0: d0", "E1: d1", "E2: d2"])
        self.assertEqual(len(linhas), 6)

    def test_etapa_com_erro_sem_detalhe_nao_gera_linha_vazia(self):
        etapas = {"Pipeline": {"status": "erro", "detalhe": ""}, "Outra": None}
        with patch("notificar_execucao_agente.sys.argv", ["desconhecido.py"]):
            linhas = nw.texto_execucao(etapas, 1, agora=AGORA).split("\n")
        self.assertEqual(linhas[1:], ["2 de 2 etapas com erro: Pipeline, Outra", "Detalhes completos no e-mail."])

    def test_resumo_vazio_ou_none_nao_quebra(self):
        with patch("notificar_execucao_agente.sys.argv", ["desconhecido.py"]):
            for vazio in ({}, None):
                self.assertEqual(nw.texto_execucao(vazio, 0, agora=AGORA),
                                 "✅ *Agente Stokki Eventos* · 16/09 22:05\nNenhuma etapa reportada (0 s)")


class TestTextoFalhaJob(unittest.TestCase):
    def test_formato(self):
        info = {"Result": "exit-code", "ExecMainStatus": "1"}
        self.assertEqual(nw.texto_falha_job("stokki-backup-gcs.service", info, agora=AGORA), "\n".join([
            "🚨 *Job da VPS falhou*",
            "stokki-backup-gcs.service",
            "Resultado: exit-code (código 1) · 16/09 22:05",
            "Log completo no e-mail.",
        ]))

    def test_info_vazio(self):
        self.assertIn("Resultado: ? (código ?)", nw.texto_falha_job("x.service", {}, agora=AGORA))
        self.assertIn("Resultado: ? (código ?)", nw.texto_falha_job("x.service", None, agora=AGORA))


class TestTextoNaoExpedidos(unittest.TestCase):
    def test_completo(self):
        self.assertEqual(nw.texto_nao_expedidos(4, 2, 1), "\n".join([
            "⚠️ *Checagem da expedição*",
            "4 pedidos entregues sem expedição na Stokki",
            "2 rotas sem terminar · 1 retirada parada há mais de 7 dias",
            "Lista completa no e-mail.",
        ]))

    def test_singular_e_plural(self):
        self.assertIn("1 pedido entregue sem expedição", nw.texto_nao_expedidos(1, 0, 0))
        self.assertIn("1 rota sem terminar · 3 retiradas paradas há mais de 7 dias",
                      nw.texto_nao_expedidos(0, 1, 3))

    def test_contagem_zero_e_omitida(self):
        self.assertEqual(nw.texto_nao_expedidos(0, 2, 0), "\n".join([
            "⚠️ *Checagem da expedição*",
            "2 rotas sem terminar",
            "Lista completa no e-mail.",
        ]))
        self.assertNotIn("rota", nw.texto_nao_expedidos(3, 0, 0))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `py -3.11 -m unittest test_notificar_whatsapp -v`
Expected: `ModuleNotFoundError: No module named 'notificar_whatsapp'`

- [ ] **Step 3: Implementar**

Criar `notificar_whatsapp.py`:

```python
# -*- coding: utf-8 -*-
"""
notificar_whatsapp.py

Aviso curto no grupo interno de WhatsApp para notificacoes que ja saem
por e-mail (pedido do Hugo, 28/09/2026). O e-mail continua sendo o
registro completo; aqui vai so o essencial: contagens e nome da rotina,
NUNCA nome de cliente, endereco, NF ou log.

Tres origens chamam este modulo, sempre DEPOIS do e-mail:
    notificar_execucao_agente.notificar_execucao  -> avisar_execucao
    alertar_falha_job.main                        -> avisar_falha_job
    verificar_entregues_nao_expedidos.main        -> avisar_nao_expedidos

O numero que envia e o do proprio Hugo, por um gateway nao-oficial
(integracao_openwa.py). Por isso: desligado por padrao, teto diario,
intervalo minimo, sem repeticao e SEM reenvio automatico (licao do erro
463 em 27/08: insistir piora).

Falha aqui NUNCA derruba a rotina que chamou.

config.yaml:
    whatsapp_notificacoes:
      ativo: false
      base_url: "http://127.0.0.1:2785/api"
      api_key: "..."
      sessao: "..."
      grupo_id: "...@g.us"
      sempre_avisar: [cancelar_rotas_sem_motorista, criar_rotas_diarias, pipeline]
      teto_diario: 20
      intervalo_min_seg: 20
      janela_repeticao_min: 120
      falhas_para_alerta: 3
"""
import logging
import re
from datetime import datetime

logger = logging.getLogger(__name__)

MAX_DETALHE = 200
MAX_ETAPAS_ERRO = 3


# --- Texto --------------------------------------------------------------------

def _uma_linha(texto, limite: int = MAX_DETALHE) -> str:
    """Uma linha so, sem os caracteres de formatacao do WhatsApp, cortada."""
    limpo = re.sub(r"[*_~`]", "", str(texto or ""))
    limpo = re.sub(r"\s+", " ", limpo).strip()
    return limpo if len(limpo) <= limite else limpo[:limite - 1].rstrip() + "…"


def _plural(n: int, singular: str, plural: str) -> str:
    return f"{n} {singular if n == 1 else plural}"


def texto_execucao(resumo_etapas: dict, duracao_seg: float, titulo: str | None = None,
                   agora: datetime | None = None) -> str:
    from notificar_execucao_agente import (etapas_com_erro, formatar_duracao, resumir_execucao,
                                           titulo_da_rotina)
    resumo_etapas = resumo_etapas or {}
    erros = etapas_com_erro(resumo_etapas)
    nome = _uma_linha(titulo_da_rotina(resumo_etapas, titulo), 80)
    quando = (agora or datetime.now()).strftime("%d/%m %H:%M")
    resumo = resumir_execucao(resumo_etapas).rstrip(".")
    if not erros:
        return f"✅ *{nome}* · {quando}\n{resumo} ({formatar_duracao(duracao_seg)})"
    linhas = [f"❌ *{nome}* · {quando}", resumo]
    for etapa in erros[:MAX_ETAPAS_ERRO]:
        detalhe = _uma_linha((resumo_etapas.get(etapa) or {}).get("detalhe"))
        if detalhe:
            linhas.append(f"{_uma_linha(etapa, 60)}: {detalhe}")
    linhas.append("Detalhes completos no e-mail.")
    return "\n".join(linhas)


def texto_falha_job(unidade: str, info: dict, agora: datetime | None = None) -> str:
    info = info or {}
    quando = (agora or datetime.now()).strftime("%d/%m %H:%M")
    return "\n".join([
        "🚨 *Job da VPS falhou*",
        _uma_linha(unidade, 80),
        f"Resultado: {_uma_linha(info.get('Result', '?'), 40)} "
        f"(código {_uma_linha(info.get('ExecMainStatus', '?'), 10)}) · {quando}",
        "Log completo no e-mail.",
    ])


def texto_nao_expedidos(n_alertas: int, n_rotas: int, n_retiradas: int) -> str:
    linhas = ["⚠️ *Checagem da expedição*"]
    if n_alertas:
        linhas.append(f"{_plural(n_alertas, 'pedido entregue', 'pedidos entregues')} sem expedição na Stokki")
    partes = []
    if n_rotas:
        partes.append(f"{_plural(n_rotas, 'rota', 'rotas')} sem terminar")
    if n_retiradas:
        partes.append(f"{_plural(n_retiradas, 'retirada parada', 'retiradas paradas')} há mais de 7 dias")
    if partes:
        linhas.append(" · ".join(partes))
    linhas.append("Lista completa no e-mail.")
    return "\n".join(linhas)
```

- [ ] **Step 4: Rodar e ver passar**

Run: `py -3.11 -m unittest test_notificar_whatsapp -v`
Expected: 12 testes, `OK`

- [ ] **Step 5: Commit (com autorização do Hugo)**

```bash
git add notificar_whatsapp.py test_notificar_whatsapp.py
git commit -m "WhatsApp: textos curtos das notificacoes internas"
```

---

### Task 3: Regras de envio, registro e alerta de canal parado

**Files:**
- Modify: `notificar_whatsapp.py`
- Test: `test_notificar_whatsapp.py`

**Interfaces:**
- Consumes: `integracao_openwa.configurado(cfg)`, `integracao_openwa.enviar_texto(cfg, chat_id, texto) -> tuple[bool, str | None]`; `email_utils.enviar_email(destinatarios, assunto, corpo_html, config_email)` e `email_utils.envelope_html(conteudo)`.
- Produces:
  - `despachar(config: dict, origem: str, tipo: str, texto: str, assinatura: str | None = None, modo_teste: bool = False, conn=None, agora: datetime | None = None, dormir=time.sleep) -> str`
  - Devolve uma de: `"desligado"`, `"modo_teste"`, `"nao_enviado"`, `"enviado"`, `"falhou"`. Nunca levanta.
  - `config` é o `config.yaml` inteiro. `tipo` é `"execucao"`, `"falha_job"` ou `"nao_expedidos"`.
  - `conn` e `dormir` existem para teste; em produção ficam no padrão.

- [ ] **Step 1: Escrever os testes que falham**

Acrescentar em `test_notificar_whatsapp.py`. No topo, junto dos imports:

```python
import sqlite3
from datetime import timedelta
from unittest.mock import MagicMock
```

Antes do `if __name__ == "__main__":`:

```python
def _config(**extra):
    wa = {"ativo": True, "base_url": "http://x/api", "api_key": "k", "sessao": "s",
          "grupo_id": "1@g.us", "sempre_avisar": ["criar_rotas_diarias"], "teto_diario": 3,
          "intervalo_min_seg": 20, "janela_repeticao_min": 120, "falhas_para_alerta": 3}
    wa.update(extra)
    return {"whatsapp_notificacoes": wa, "email": {"remetente": "a@b.com"}}


class _ComBanco(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.dormir = MagicMock()
        self.envio = patch.object(nw.integracao_openwa, "enviar_texto", return_value=(True, "m1"))
        self.enviar = self.envio.start()
        self.addCleanup(self.envio.stop)
        self.addCleanup(self.conn.close)

    def despachar(self, config=None, origem="rotina", assinatura=None, agora=AGORA, **kw):
        return nw.despachar(config or _config(), origem, "execucao", "texto", assinatura,
                            conn=self.conn, agora=agora, dormir=self.dormir, **kw)

    def linhas(self):
        return self.conn.execute(
            "SELECT origem, situacao, motivo, id_mensagem FROM notificacoes_whatsapp ORDER BY id").fetchall()


class TestDespacharChave(_ComBanco):
    def test_envia_e_registra(self):
        self.assertEqual(self.despachar(), "enviado")
        self.enviar.assert_called_once_with(_config()["whatsapp_notificacoes"], "1@g.us", "texto")
        self.assertEqual(self.linhas(), [("rotina", "enviado", None, "m1")])

    def test_desligado_nao_envia_nem_cria_tabela(self):
        for config in (_config(ativo=False), _config(grupo_id=""), _config(api_key=""), {}, None,
                       {"whatsapp_notificacoes": None}):
            self.assertEqual(nw.despachar(config, "r", "execucao", "t", conn=self.conn), "desligado")
        self.enviar.assert_not_called()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0], 0)

    def test_modo_teste_so_loga(self):
        with self.assertLogs(nw.logger, level="INFO") as logs:
            self.assertEqual(self.despachar(modo_teste=True), "modo_teste")
        self.assertIn("texto", "\n".join(logs.output))
        self.enviar.assert_not_called()


class TestDespacharVolume(_ComBanco):
    def test_repeticao_dentro_da_janela(self):
        self.assertEqual(self.despachar(assinatura="erro:Pipeline"), "enviado")
        depois = AGORA + timedelta(minutes=119)
        self.assertEqual(self.despachar(assinatura="erro:Pipeline", agora=depois), "nao_enviado")
        self.assertEqual(self.linhas()[-1][1:3], ("nao_enviado", "repetida dentro da janela"))

    def test_repeticao_fora_da_janela_ou_de_outra_origem_envia(self):
        self.despachar(assinatura="erro:Pipeline")
        self.assertEqual(self.despachar(assinatura="erro:Pipeline", origem="outra",
                                        agora=AGORA + timedelta(minutes=5)), "enviado")
        self.assertEqual(self.despachar(assinatura="erro:Pipeline",
                                        agora=AGORA + timedelta(minutes=121)), "enviado")

    def test_sem_assinatura_nao_tem_regra_de_repeticao(self):
        self.despachar()
        self.assertEqual(self.despachar(agora=AGORA + timedelta(minutes=5)), "enviado")

    def test_teto_diario(self):
        for i in range(3):
            self.assertEqual(self.despachar(agora=AGORA + timedelta(minutes=i)), "enviado")
        self.assertEqual(self.despachar(agora=AGORA + timedelta(minutes=10)), "nao_enviado")
        self.assertEqual(self.linhas()[-1][2], "teto diario atingido")
        amanha = AGORA + timedelta(days=1)
        self.assertEqual(self.despachar(agora=amanha), "enviado")

    def test_intervalo_minimo_espera_a_diferenca(self):
        self.despachar()
        self.dormir.assert_not_called()
        self.despachar(agora=AGORA + timedelta(seconds=5))
        self.dormir.assert_called_once_with(15.0)

    def test_intervalo_ja_cumprido_nao_espera(self):
        self.despachar()
        self.despachar(agora=AGORA + timedelta(seconds=25))
        self.dormir.assert_not_called()

    def test_numeros_malformados_caem_no_padrao(self):
        config = _config(teto_diario="vinte", intervalo_min_seg=None, janela_repeticao_min="x",
                         falhas_para_alerta=[])
        self.assertEqual(self.despachar(config=config), "enviado")


class TestDespacharFalhas(_ComBanco):
    def setUp(self):
        super().setUp()
        self.enviar.return_value = (False, None)
        self.email = patch("email_utils.enviar_email", return_value=True)
        self.enviar_email = self.email.start()
        self.addCleanup(self.email.stop)

    def falhar(self, n, inicio=0):
        for i in range(n):
            self.assertEqual(self.despachar(agora=AGORA + timedelta(minutes=inicio + i)), "falhou")

    def test_falha_registra_e_nao_reenvia(self):
        self.falhar(1)
        self.assertEqual(self.enviar.call_count, 1)
        self.assertEqual(self.linhas()[0][1], "falhou")

    def test_email_unico_na_terceira_falha_seguida(self):
        self.falhar(2)
        self.enviar_email.assert_not_called()
        self.falhar(1, inicio=2)
        self.enviar_email.assert_called_once()
        self.assertEqual(self.enviar_email.call_args[0][0], ["a@b.com"])
        self.falhar(3, inicio=3)
        self.enviar_email.assert_called_once()

    def test_sucesso_no_meio_zera_a_contagem(self):
        self.falhar(2)
        self.enviar.return_value = (True, "m")
        self.despachar(agora=AGORA + timedelta(minutes=5))
        self.enviar.return_value = (False, None)
        self.falhar(2, inicio=6)
        self.enviar_email.assert_not_called()
        self.falhar(1, inicio=8)
        self.enviar_email.assert_called_once()

    def test_falha_nao_conta_para_o_teto(self):
        self.falhar(5)
        self.enviar.return_value = (True, "m")
        self.assertEqual(self.despachar(agora=AGORA + timedelta(minutes=30)), "enviado")


class TestDespacharNuncaLevanta(unittest.TestCase):
    def test_banco_indisponivel(self):
        conn = MagicMock()
        conn.execute.side_effect = sqlite3.OperationalError("database is locked")
        with patch.object(nw.integracao_openwa, "enviar_texto") as enviar:
            self.assertEqual(nw.despachar(_config(), "r", "execucao", "t", conn=conn, agora=AGORA), "falhou")
        enviar.assert_not_called()

    def test_excecao_no_transporte(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        with patch.object(nw.integracao_openwa, "enviar_texto", side_effect=RuntimeError("bug")):
            self.assertEqual(nw.despachar(_config(), "r", "execucao", "t", conn=conn, agora=AGORA,
                                          dormir=MagicMock()), "falhou")
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `py -3.11 -m unittest test_notificar_whatsapp -v`
Expected: os 12 testes da Tarefa 2 passam; os novos falham com `AttributeError: module 'notificar_whatsapp' has no attribute 'integracao_openwa'`

- [ ] **Step 3: Implementar**

Em `notificar_whatsapp.py`, trocar o bloco de imports por:

```python
import logging
import re
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

import integracao_openwa

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).resolve().parent / "dados" / "dados.db"
MAX_DETALHE = 200
MAX_ETAPAS_ERRO = 3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS notificacoes_whatsapp (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    criado_em    TEXT NOT NULL,
    origem       TEXT NOT NULL,
    tipo         TEXT NOT NULL,
    assinatura   TEXT,
    situacao     TEXT NOT NULL,
    motivo       TEXT,
    id_mensagem  TEXT
)"""
```

E acrescentar ao final do arquivo:

```python
# --- Envio --------------------------------------------------------------------

def _cfg(config: dict | None) -> dict:
    return (config or {}).get("whatsapp_notificacoes") or {}


def _inteiro(cfg: dict, chave: str, padrao: int) -> int:
    try:
        return int(cfg.get(chave, padrao))
    except (TypeError, ValueError):
        return padrao


def _iso(quando: datetime) -> str:
    return quando.isoformat(timespec="seconds")


def _registrar(conn, agora, origem, tipo, assinatura, situacao, motivo=None, id_mensagem=None):
    conn.execute(
        "INSERT INTO notificacoes_whatsapp (criado_em, origem, tipo, assinatura, situacao, motivo, id_mensagem) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (_iso(agora), origem, tipo, assinatura, situacao, motivo, id_mensagem))
    conn.commit()


def _motivo_para_nao_enviar(conn, cfg: dict, origem: str, assinatura: str | None, agora: datetime) -> str | None:
    if assinatura:
        desde = _iso(agora - timedelta(minutes=_inteiro(cfg, "janela_repeticao_min", 120)))
        if conn.execute(
                "SELECT 1 FROM notificacoes_whatsapp WHERE origem = ? AND assinatura = ? "
                "AND situacao = 'enviado' AND criado_em >= ? LIMIT 1",
                (origem, assinatura, desde)).fetchone():
            return "repetida dentro da janela"
    inicio_do_dia = _iso(agora.replace(hour=0, minute=0, second=0, microsecond=0))
    enviadas = conn.execute(
        "SELECT COUNT(*) FROM notificacoes_whatsapp WHERE situacao = 'enviado' AND criado_em >= ?",
        (inicio_do_dia,)).fetchone()[0]
    if enviadas >= _inteiro(cfg, "teto_diario", 20):
        return "teto diario atingido"
    return None


def _esperar_intervalo(conn, cfg: dict, agora: datetime, dormir) -> None:
    ultima = conn.execute(
        "SELECT MAX(criado_em) FROM notificacoes_whatsapp WHERE situacao = 'enviado'").fetchone()[0]
    if not ultima:
        return
    intervalo = _inteiro(cfg, "intervalo_min_seg", 20)
    falta = intervalo - (agora - datetime.fromisoformat(ultima)).total_seconds()
    if 0 < falta <= intervalo:
        dormir(falta)


def _alertar_se_canal_parou(conn, cfg: dict, config: dict) -> None:
    """Um e-mail so, exatamente na N-esima falha seguida. A N+1 nao repete;
    um envio com sucesso zera a contagem."""
    limite = _inteiro(cfg, "falhas_para_alerta", 3)
    seguidas = 0
    for (situacao,) in conn.execute(
            "SELECT situacao FROM notificacoes_whatsapp WHERE situacao IN ('enviado', 'falhou') "
            "ORDER BY id DESC LIMIT ?", (limite + 1,)):
        if situacao != "falhou":
            break
        seguidas += 1
    if seguidas != limite:
        return
    destino = (config.get("notificacao_execucao") or {}).get("destinatario") \
        or (config.get("email") or {}).get("remetente")
    if not destino:
        return
    from email_utils import envelope_html, enviar_email
    corpo = (f"<h2 style='margin:0 0 12px;color:#EF4444'>WhatsApp das notificações parou</h2>"
             f"<p>{limite} envios seguidos falharam. O gateway pode estar fora do ar ou o número "
             f"desconectado. Os e-mails continuam saindo normalmente.</p>"
             f"<p>Conferir na VPS: <code>docker ps</code> e a tabela <code>notificacoes_whatsapp</code>.</p>")
    enviar_email([destino], "[ALERTA] WhatsApp das notificações parou", envelope_html(corpo),
                 config.get("email", {}))


def _despachar(config, origem, tipo, texto, assinatura, modo_teste, conn, agora, dormir) -> str:
    cfg = _cfg(config)
    if not cfg.get("ativo") or not integracao_openwa.configurado(cfg) or not cfg.get("grupo_id"):
        return "desligado"
    if modo_teste:
        logger.info(f"[MODO TESTE] WhatsApp nao enviado ({origem}). Texto:\n{texto}")
        return "modo_teste"
    agora = agora or datetime.now()
    fechar = conn is None
    if fechar:
        conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        conn.execute(_SCHEMA)
        motivo = _motivo_para_nao_enviar(conn, cfg, origem, assinatura, agora)
        if motivo:
            _registrar(conn, agora, origem, tipo, assinatura, "nao_enviado", motivo)
            logger.info(f"WhatsApp nao enviado ({origem}): {motivo}.")
            return "nao_enviado"
        _esperar_intervalo(conn, cfg, agora, dormir)
        ok, id_mensagem = integracao_openwa.enviar_texto(cfg, cfg["grupo_id"], texto)
        if ok:
            _registrar(conn, agora, origem, tipo, assinatura, "enviado", None, id_mensagem)
            logger.info(f"WhatsApp enviado ao grupo ({origem}).")
            return "enviado"
        _registrar(conn, agora, origem, tipo, assinatura, "falhou", "gateway fora do ar ou envio recusado")
        _alertar_se_canal_parou(conn, cfg, config)
        return "falhou"
    finally:
        if fechar:
            conn.close()


def despachar(config: dict, origem: str, tipo: str, texto: str, assinatura: str | None = None,
              modo_teste: bool = False, conn=None, agora: datetime | None = None,
              dormir=time.sleep) -> str:
    """Aplica as regras e envia. Devolve a situacao: desligado | modo_teste |
    nao_enviado | enviado | falhou. Nunca levanta excecao."""
    try:
        return _despachar(config, origem, tipo, texto, assinatura, modo_teste, conn, agora, dormir)
    except Exception as exc:
        logger.warning(f"Falha na notificacao por WhatsApp (nao afeta a rotina): {exc}")
        return "falhou"
```

- [ ] **Step 4: Rodar e ver passar**

Run: `py -3.11 -m unittest test_notificar_whatsapp -v`
Expected: 28 testes, `OK`

- [ ] **Step 5: Conferir que nada abriu o banco real**

Run: `git status --short dados/`
Expected: nenhuma linha nova (`dados/` é ignorado; o ponto é confirmar que os testes usaram `:memory:`). Conferir também:

Run: `py -3.11 -c "import sqlite3; c=sqlite3.connect('file:dados/dados.db?mode=ro', uri=True); print(c.execute(\"select count(*) from sqlite_master where name='notificacoes_whatsapp'\").fetchone())"`
Expected: `(0,)`

- [ ] **Step 6: Commit (com autorização do Hugo)**

```bash
git add notificar_whatsapp.py test_notificar_whatsapp.py
git commit -m "WhatsApp: regras de volume, registro e alerta de canal parado"
```

---

### Task 4: Ligar as três rotinas

**Files:**
- Modify: `notificar_whatsapp.py`
- Modify: `notificar_execucao_agente.py:174-180`
- Modify: `alertar_falha_job.py:162-165`
- Modify: `verificar_entregues_nao_expedidos.py:387-398`
- Test: `test_notificar_whatsapp.py`

**Interfaces:**
- Consumes: `despachar(...)` e as três funções `texto_*` das Tarefas 2 e 3.
- Produces:
  - `avisar_execucao(resumo_etapas: dict, duracao_seg: float, modo_teste: bool, config: dict, titulo: str | None = None, **kw) -> str`
  - `avisar_falha_job(unidade: str, info: dict, config: dict, **kw) -> str`
  - `avisar_nao_expedidos(n_alertas: int, n_rotas: int, n_retiradas: int, config: dict, **kw) -> str`
  - Além das situações de `despachar`, `avisar_execucao` pode devolver `"nao_relevante"`. `**kw` repassa `conn`, `agora` e `dormir` para teste. Nenhuma das três levanta.

- [ ] **Step 1: Escrever os testes que falham**

Acrescentar em `test_notificar_whatsapp.py`, antes do `if __name__ == "__main__":`:

```python
class TestAvisarExecucao(_ComBanco):
    def avisar(self, etapas, script, config=None, modo_teste=False):
        with patch.object(nw.sys, "argv", [script]), \
                patch("notificar_execucao_agente.sys.argv", [script]):
            return nw.avisar_execucao(etapas, 10, modo_teste, config or _config(),
                                      conn=self.conn, agora=AGORA, dormir=self.dormir)

    def test_sucesso_fora_da_lista_nao_envia_nem_registra(self):
        self.assertEqual(self.avisar(UMA, "/opt/x/expedir_pedidos.py"), "nao_relevante")
        self.enviar.assert_not_called()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0], 0)

    def test_sucesso_de_script_da_lista_envia(self):
        self.assertEqual(self.avisar(UMA, "/opt/x/roteirizacao/criar_rotas_diarias.py"), "enviado")
        self.assertEqual(self.linhas()[0][0], "criar_rotas_diarias")
        self.assertTrue(self.enviar.call_args[0][2].startswith("✅ *Criação de rotas*"))

    def test_erro_de_qualquer_script_envia_com_assinatura(self):
        self.assertEqual(self.avisar(VARIAS_ERRO, "expedir_pedidos.py"), "enviado")
        assinatura = self.conn.execute("SELECT assinatura FROM notificacoes_whatsapp").fetchone()[0]
        self.assertEqual(assinatura, "erro:Pipeline,Retiradas")

    def test_mesmo_erro_de_novo_nao_repete(self):
        self.avisar(VARIAS_ERRO, "expedir_pedidos.py")
        self.assertEqual(self.avisar(VARIAS_ERRO, "expedir_pedidos.py"), "nao_enviado")

    def test_desligado_vence_a_relevancia(self):
        self.assertEqual(self.avisar(VARIAS_ERRO, "x.py", config=_config(ativo=False)), "desligado")

    def test_modo_teste(self):
        self.assertEqual(self.avisar(VARIAS_ERRO, "x.py", modo_teste=True), "modo_teste")
        self.enviar.assert_not_called()

    def test_sempre_avisar_ausente_ou_none(self):
        for valor in (None, "", []):
            self.assertEqual(self.avisar(UMA, "criar_rotas_diarias.py",
                                         config=_config(sempre_avisar=valor)), "nao_relevante")

    def test_argv_vazio_e_resumo_none(self):
        with patch.object(nw.sys, "argv", [""]), patch("notificar_execucao_agente.sys.argv", [""]):
            self.assertEqual(nw.avisar_execucao(None, 0, False, _config(), conn=self.conn, agora=AGORA),
                             "nao_relevante")
            self.assertEqual(nw.avisar_execucao(VARIAS_ERRO, 0, False, _config(), conn=self.conn,
                                                agora=AGORA, dormir=self.dormir), "enviado")
        self.assertEqual(self.linhas()[0][0], "desconhecido")


class TestAvisarOutros(_ComBanco):
    def test_falha_job(self):
        situacao = nw.avisar_falha_job("stokki-backup-gcs.service", {"Result": "exit-code"}, _config(),
                                       conn=self.conn, agora=AGORA, dormir=self.dormir)
        self.assertEqual(situacao, "enviado")
        self.assertEqual(self.conn.execute("SELECT origem, tipo FROM notificacoes_whatsapp").fetchone(),
                         ("stokki-backup-gcs.service", "falha_job"))
        self.assertIn("Job da VPS falhou", self.enviar.call_args[0][2])

    def test_nao_expedidos(self):
        situacao = nw.avisar_nao_expedidos(4, 2, 1, _config(), conn=self.conn, agora=AGORA,
                                           dormir=self.dormir)
        self.assertEqual(situacao, "enviado")
        self.assertEqual(self.conn.execute("SELECT origem, tipo FROM notificacoes_whatsapp").fetchone(),
                         ("verificar_entregues_nao_expedidos", "nao_expedidos"))

    def test_nenhuma_levanta_com_entrada_ruim(self):
        with patch.object(nw, "despachar", side_effect=RuntimeError("bug")):
            self.assertEqual(nw.avisar_falha_job("u", None, _config()), "falhou")
            self.assertEqual(nw.avisar_nao_expedidos(1, 1, 1, _config()), "falhou")
            with patch.object(nw.sys, "argv", ["x.py"]):
                self.assertEqual(nw.avisar_execucao(VARIAS_ERRO, 1, False, _config()), "falhou")


class TestChamadaNoResumoDasRotinas(unittest.TestCase):
    def test_notificar_execucao_chama_o_whatsapp_depois_do_email(self):
        import notificar_execucao_agente as nea
        ordem = []
        with patch.object(nea, "enviar_email", side_effect=lambda *a, **k: ordem.append("email") or True), \
                patch.object(nw, "avisar_execucao", side_effect=lambda *a, **k: ordem.append("whatsapp")) as avisar:
            nea.notificar_execucao(UMA, 12.0, False, _config(), titulo="T")
        self.assertEqual(ordem, ["email", "whatsapp"])
        avisar.assert_called_once_with(UMA, 12.0, False, _config(), "T")

    def test_falha_no_whatsapp_nao_derruba_a_rotina(self):
        import notificar_execucao_agente as nea
        with patch.object(nea, "enviar_email", return_value=True), \
                patch.object(nw, "avisar_execucao", side_effect=RuntimeError("bug")):
            nea.notificar_execucao(UMA, 12.0, False, _config())

    def test_email_de_execucao_desligado_desliga_tudo(self):
        import notificar_execucao_agente as nea
        config = dict(_config(), notificacao_execucao={"ativo": False})
        with patch.object(nea, "enviar_email") as email, patch.object(nw, "avisar_execucao") as avisar:
            nea.notificar_execucao(UMA, 12.0, False, config)
        email.assert_not_called()
        avisar.assert_not_called()
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `py -3.11 -m unittest test_notificar_whatsapp -v`
Expected: os 28 anteriores passam; os novos falham com `AttributeError: module 'notificar_whatsapp' has no attribute 'sys'`

- [ ] **Step 3: Implementar as funções `avisar_*`**

Em `notificar_whatsapp.py`, acrescentar `import sys` no bloco de imports (depois de `import sqlite3`) e, ao final do arquivo:

```python
# --- Pontos de chamada -----------------------------------------------------------

def avisar_execucao(resumo_etapas: dict, duracao_seg: float, modo_teste: bool, config: dict,
                    titulo: str | None = None, **kw) -> str:
    """Resumo de rotina. So vai pro grupo se houve erro ou se o script
    esta em whatsapp_notificacoes.sempre_avisar."""
    try:
        if not _cfg(config).get("ativo"):
            return "desligado"
        from notificar_execucao_agente import etapas_com_erro
        origem = Path(sys.argv[0]).stem if sys.argv and sys.argv[0] else ""
        erros = etapas_com_erro(resumo_etapas or {})
        if not erros and origem not in (_cfg(config).get("sempre_avisar") or []):
            return "nao_relevante"
        assinatura = "erro:" + ",".join(erros) if erros else None
        texto = texto_execucao(resumo_etapas, duracao_seg, titulo, kw.get("agora"))
        return despachar(config, origem or "desconhecido", "execucao", texto, assinatura, modo_teste, **kw)
    except Exception as exc:
        logger.warning(f"Falha na notificacao por WhatsApp (nao afeta a rotina): {exc}")
        return "falhou"


def avisar_falha_job(unidade: str, info: dict, config: dict, **kw) -> str:
    """Sem regra de repeticao aqui: alertar_falha_job.py ja tem a janela
    anti-enxurrada e so chama quando o e-mail tambem saiu."""
    try:
        return despachar(config, unidade, "falha_job", texto_falha_job(unidade, info, kw.get("agora")), **kw)
    except Exception as exc:
        logger.warning(f"Falha na notificacao por WhatsApp (nao afeta a rotina): {exc}")
        return "falhou"


def avisar_nao_expedidos(n_alertas: int, n_rotas: int, n_retiradas: int, config: dict, **kw) -> str:
    try:
        return despachar(config, "verificar_entregues_nao_expedidos", "nao_expedidos",
                         texto_nao_expedidos(n_alertas, n_rotas, n_retiradas), **kw)
    except Exception as exc:
        logger.warning(f"Falha na notificacao por WhatsApp (nao afeta a rotina): {exc}")
        return "falhou"
```

- [ ] **Step 4: Ligar o resumo das rotinas**

Em `notificar_execucao_agente.py`, o final de `notificar_execucao()` hoje é:

```python
    try:
        assunto = montar_assunto(resumo_etapas, modo_teste, titulo)
        corpo = montar_html(resumo_etapas, duracao_total_seg, modo_teste, titulo)
        if enviar_email([destinatario], assunto, corpo, config_email):
            logger.info(f"Notificação de execução enviada para {destinatario}.")
    except Exception as e:
        logger.warning(f"Falha ao enviar notificação de execução (não afeta o resultado): {e}")
```

Acrescentar logo depois, no mesmo nível de indentação do `try`:

```python

    try:
        # import tardio: notificar_whatsapp importa funcoes deste modulo
        import notificar_whatsapp
        notificar_whatsapp.avisar_execucao(resumo_etapas, duracao_total_seg, modo_teste, config, titulo)
    except Exception as e:
        logger.warning(f"Falha no aviso por WhatsApp (não afeta o resultado): {e}")
```

O retorno antecipado quando `notificacao_execucao.ativo` é falso continua acima e desliga os dois canais. É intencional.

- [ ] **Step 5: Ligar a falha de job**

Em `alertar_falha_job.py`, o final de `main()` hoje é:

```python
    ok = enviar_email([destino], assunto, envelope_html(montar_corpo(args.unidade, info, log, suprimidos),
                                                        cor_acento=COR_ERRO), config.get("email", {}))
    return 0 if ok else 1
```

Trocar por:

```python
    ok = enviar_email([destino], assunto, envelope_html(montar_corpo(args.unidade, info, log, suprimidos),
                                                        cor_acento=COR_ERRO), config.get("email", {}))
    try:
        import notificar_whatsapp
        notificar_whatsapp.avisar_falha_job(args.unidade, info, config)
    except Exception as exc:
        logger.warning(f"Falha no aviso por WhatsApp (nao afeta o alerta): {exc}")
    return 0 if ok else 1
```

Atualizar a docstring do topo, acrescentando depois do parágrafo "Anti-enxurrada":

```python
28/09/2026: depois do e-mail, o mesmo alerta vai resumido pro grupo interno
de WhatsApp (notificar_whatsapp.py), se whatsapp_notificacoes.ativo.
```

- [ ] **Step 6: Ligar a checagem de não expedidos**

Em `verificar_entregues_nao_expedidos.py`, o bloco hoje é:

```python
    if (alertas or rotas or retiradas) and not modo_teste:
        config_email = config.get("email", {})
        destino = config_email.get("email_responsavel", "")
        if destino:
            n_ret = sum(g["qtd"] for g in retiradas)
            assunto = (f"[Freshlog] Expedicao: {len(alertas)} entregue(s) sem expedir, "
                       f"{len(rotas)} rota(s) sem terminar, {n_ret} retirada(s) antiga(s)")
            ok = enviar_email([destino], assunto,
                              montar_email(alertas, so_insucesso, len(stokki), rotas, retiradas), config_email)
            logger.info(f"E-mail {'enviado' if ok else 'FALHOU'} para {destino}.")
        else:
            logger.warning("email.email_responsavel nao configurado -- e-mail pulado.")
    return len(alertas)
```

Trocar por (muda só a posição de `n_ret` e acrescenta o bloco do WhatsApp):

```python
    if (alertas or rotas or retiradas) and not modo_teste:
        config_email = config.get("email", {})
        destino = config_email.get("email_responsavel", "")
        n_ret = sum(g["qtd"] for g in retiradas)
        if destino:
            assunto = (f"[Freshlog] Expedicao: {len(alertas)} entregue(s) sem expedir, "
                       f"{len(rotas)} rota(s) sem terminar, {n_ret} retirada(s) antiga(s)")
            ok = enviar_email([destino], assunto,
                              montar_email(alertas, so_insucesso, len(stokki), rotas, retiradas), config_email)
            logger.info(f"E-mail {'enviado' if ok else 'FALHOU'} para {destino}.")
        else:
            logger.warning("email.email_responsavel nao configurado -- e-mail pulado.")
        try:
            import notificar_whatsapp
            notificar_whatsapp.avisar_nao_expedidos(len(alertas), len(rotas), n_ret, config)
        except Exception as exc:
            logger.warning(f"Falha no aviso por WhatsApp (nao afeta a checagem): {exc}")
    return len(alertas)
```

- [ ] **Step 7: Rodar tudo**

Run: `py -3.11 -m unittest test_notificar_whatsapp test_integracao_openwa test_notificar_execucao_agente -v`
Expected: todos `OK`, incluindo os testes antigos de `test_notificar_execucao_agente`.

Run: `py -3.11 -m py_compile notificar_whatsapp.py integracao_openwa.py notificar_execucao_agente.py alertar_falha_job.py verificar_entregues_nao_expedidos.py`
Expected: sem saída.

- [ ] **Step 8: Provar localmente que desligado não faz nada**

O `config.yaml` local não tem a seção `whatsapp_notificacoes`.

Run: `py -3.11 -c "import yaml, notificar_whatsapp as nw; c = yaml.safe_load(open('config.yaml', encoding='utf-8')); print(nw.avisar_falha_job('teste.service', {}, c), nw.avisar_nao_expedidos(1, 1, 1, c))"`
Expected: `desligado desligado`

- [ ] **Step 9: Commit (com autorização do Hugo)**

```bash
git status --short
git add notificar_whatsapp.py test_notificar_whatsapp.py notificar_execucao_agente.py alertar_falha_job.py verificar_entregues_nao_expedidos.py
git commit -m "WhatsApp: falha de job, nao expedidos e resumo das rotinas avisam no grupo interno"
```

---

### Task 5: Mapa do sistema, deploy desligado e prova real

**Files:**
- Modify: `MAPA_DO_SISTEMA.txt`

**Interfaces:**
- Consumes: tudo das Tarefas 0 a 4; os três valores anotados na Tarefa 0 (`sessao`, `grupo_id`, chave de operador).
- Produces: funcionalidade em produção, desligada, com prova de envio real feita sob comando do Hugo.

- [ ] **Step 1: Atualizar o mapa**

Em `MAPA_DO_SISTEMA.txt`:

Na tabela de portas (perto da linha `Evolution API (docker) 8080`), acrescentar:

```
OpenWA (docker)        2785   (só local)                       /opt/openwa + infra/openwa/docker-compose.override.yml
```

Na seção "Notificações e e-mails" (perto de `integracao_evolution.py`), acrescentar:

```
integracao_openwa.py          Envio de WhatsApp pelo OpenWA (só notificações internas).
notificar_whatsapp.py         Aviso curto no grupo interno: falha de job, não
                              expedidos, resumo das rotinas (erro ou lista
                              sempre_avisar). Desligado por padrão.
```

Na lista de tabelas (perto de `notificacoes_entrega`), acrescentar:

```
  notificacoes_whatsapp     notificar_whatsapp.py (teto diário, repetição, falhas)
```

Na seção de armadilhas/convenções, acrescentar:

```
  WhatsApp das notificações (whatsapp_notificacoes.ativo) usa o número do
  Hugo por gateway não-oficial. Sem reenvio, teto de 20/dia. Não tem
  relação com atendimento/ nem com a Evolution. Ligar é decisão do Hugo.
```

- [ ] **Step 2: Commit e push (só quando o Hugo pedir)**

```bash
git status --short
git log --oneline -5
git add MAPA_DO_SISTEMA.txt
git commit -m "Mapa: gateway OpenWA e notificacoes internas por WhatsApp"
git push origin master
```

- [ ] **Step 3: Deploy**

Seguir a skill `deploy-vps`. Resumo:

```bash
ssh -i ~/.ssh/atendimento_vps root@187.127.52.197 "cd /opt/stokki-eventos && git status --short && git log --oneline -3"
ssh -i ~/.ssh/atendimento_vps root@187.127.52.197 "cd /opt/stokki-eventos && git pull --no-rebase && chown -R www-data:www-data /opt/stokki-eventos"
```

Nenhum serviço precisa de restart: os três pontos de chamada rodam em timers, que pegam a versão nova sozinhos.

- [ ] **Step 4: Provar em produção que desligado não faz nada**

```bash
ssh -i ~/.ssh/atendimento_vps root@187.127.52.197 "cd /opt/stokki-eventos && sudo -u www-data venv/bin/python -c \"import yaml, notificar_whatsapp as nw; c = yaml.safe_load(open('config.yaml', encoding='utf-8')); print(nw.avisar_falha_job('teste.service', {}, c))\""
```

Expected: `desligado`

- [ ] **Step 5: O Hugo preenche a configuração na VPS**

O Hugo edita `/opt/stokki-eventos/config.yaml` e acrescenta a seção abaixo, com os valores da Tarefa 0, ainda com `ativo: false`:

```yaml
whatsapp_notificacoes:
  ativo: false
  base_url: "http://127.0.0.1:2785/api"
  api_key: "<chave de operador>"
  sessao: "<uuid da sessao>"
  grupo_id: "<id do grupo>@g.us"
  sempre_avisar:
    - cancelar_rotas_sem_motorista
    - criar_rotas_diarias
    - pipeline
  teto_diario: 20
  intervalo_min_seg: 20
  janela_repeticao_min: 120
  falhas_para_alerta: 3
```

Conferir que o arquivo continua válido e de `www-data`, sem imprimir valores:

```bash
ssh -i ~/.ssh/atendimento_vps root@187.127.52.197 "cd /opt/stokki-eventos && ls -l config.yaml && sudo -u www-data venv/bin/python -c \"import yaml, integracao_openwa as o; c = yaml.safe_load(open('config.yaml', encoding='utf-8'))['whatsapp_notificacoes']; print('configurado:', o.configurado(c), '| grupo:', bool(c.get('grupo_id')), '| ativo:', c.get('ativo'))\""
```

Expected: `configurado: True | grupo: True | ativo: False`

- [ ] **Step 6: Prova real (quando o Hugo mandar ligar)**

O Hugo troca `ativo` para `true`. Depois:

```bash
ssh -i ~/.ssh/atendimento_vps root@187.127.52.197 "cd /opt/stokki-eventos && sudo -u www-data venv/bin/python alertar_falha_job.py stokki-backup-gcs.service --forcar"
ssh -i ~/.ssh/atendimento_vps root@187.127.52.197 "cd /opt/stokki-eventos && sudo -u www-data sqlite3 'file:dados/dados.db?mode=ro' 'select criado_em, origem, tipo, situacao, motivo from notificacoes_whatsapp order by id desc limit 5'"
```

Expected: mensagem "🚨 Job da VPS falhou" no grupo, e-mail de alerta na caixa do Hugo, e uma linha `stokki-backup-gcs.service | falha_job | enviado` na tabela.

Conferir o dono dos arquivos do banco depois da prova:

```bash
ssh -i ~/.ssh/atendimento_vps root@187.127.52.197 "ls -l /opt/stokki-eventos/dados/dados.db*"
```

Expected: tudo `www-data www-data`.

- [ ] **Step 7: Acompanhar o primeiro dia**

No dia seguinte à ativação:

```bash
ssh -i ~/.ssh/atendimento_vps root@187.127.52.197 "cd /opt/stokki-eventos && sudo -u www-data sqlite3 'file:dados/dados.db?mode=ro' \"select situacao, coalesce(motivo,''), count(*) from notificacoes_whatsapp where criado_em >= date('now','localtime','-1 day') group by 1,2\""
```

Relatar ao Hugo: quantas enviadas, quantas barradas e por qual motivo, quantas falhas. Se `teto diario atingido` aparecer, rever a lista `sempre_avisar` com ele antes de subir o teto.
