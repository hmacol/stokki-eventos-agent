# Documento de Especificação e Execução: Notificação de Rotas aos Motoristas

Este documento contém todas as instruções, regras de negócio, estrutura de código e comandos de validação para que o **Claude Code** (ou qualquer agente/desenvolvedor) implemente de forma autônoma e completa o módulo de **notificação de rotas aos motoristas** (`roteirizacao/avisar_motoristas_rotas.py`) no projeto `agente_stokki_eventos`.

---

## 1. Contexto e Objetivos

Após a execução da alocação de rotas (`alocacao_motoristas.py` / `criar_rotas_diarias.py`), a equipe de logística precisa notificar os motoristas sobre as rotas que realizarão no dia seguinte (ou em uma data específica).

A solução é dividida em duas fases:
- **Fase 1 (Atual - Simples & Imediata):** Geração de mensagens individuais e de grupo formatadas para WhatsApp (prontas para copiar e colar) + envio automático de e-mail de aviso (se o motorista possuir e-mail cadastrado).
- **Fase 2 (Evolução Futura - Robusta & Automatizada):** Envio direto por API de WhatsApp Web/Oficial, confirmação interativa de leitura ("Ciente") e alertas de ausência de confirmação.

---

## 2. Regras de Negócio e Funcionalidades

### 2.1. Consulta de Rotas Alocadas na API VUUPT
1. Consultar os serviços agendados no VUUPT para a data de destino (padrão: `amanhã`).
2. Agrupar os serviços por `agent_id` (ID do motorista).
3. Para cada motorista com ao menos 1 serviço alocado:
   - Identificar a quantidade de entregas.
   - Identificar as zonas/regiões atendidas (ex: `ZONA LESTE`, `CAMPINAS / VIAGEM`).
   - Identificar o horário estimado da primeira parada.

---

### 2.2. Leitura de Contatos dos Motoristas
Expandir `regras/preferencias_motoristas.py` para incluir os campos de contato:
- `TELEFONE_MOTORISTA`
- `EMAIL_MOTORISTA`

---

### 2.3. Formatação das Mensagens (WhatsApp)

#### Mensagem Individual (Copiar/Colar por Motorista):
```text
🚚 *AVISO DE ROTA - FRESHLOG*
Olá, *[Nome do Motorista]*!
Você possui *[N]* rota(s) alocada(s) para amanhã (*[Data]*).

📍 *Região/Zona:* [Zona/Cidade]
⏱️ *Previsão Primeira Parada:* [Horário]
📦 *Total de Clientes:* [N] entregas
🔗 *Acesse o app da VUUPT para visualizar o roteiro completo.*

Por gentileza, responda a esta mensagem confirmando o recebimento!
```

#### Mensagem de Grupo (Escala Geral Broadcast):
```text
📋 *ESCALA DE ROTAS - [DIA DA SEMANA] ([DATA])*

✅ [Motorista 1] - [Zona 1] ([N] entregas)
✅ [Motorista 2] - [Zona 2] ([N] entregas)

⚠️ Motoristas escalados, por favor confirmem o ciente no privado!
```

---

## 3. Especificação dos Arquivos a Criar e Alterar

### 3.1. `regras/preferencias_motoristas.py` (Alteração)
Incluir atributos `telefone: str | None` e `email: str | None` no dataclass `MotoristaPreferencias` e na leitura do Excel/JSON.

### 3.2. `roteirizacao/avisar_motoristas_rotas.py` (Novo Script)
Suporta os seguintes parâmetros CLI:
```bash
# Simulação/auditoria para o dia de amanhã
py -3.11 roteirizacao/avisar_motoristas_rotas.py --modo-teste --data amanhã

# Execução real com envio de e-mails para motoristas cadastrados
py -3.11 roteirizacao/avisar_motoristas_rotas.py --data amanhã --enviar-emails
```

---

## 4. Estrutura do Código `avisar_motoristas_rotas.py`

```python
# -*- coding: utf-8 -*-
"""
roteirizacao/avisar_motoristas_rotas.py

Gera mensagens de aviso de rotas para motoristas escalados para amanhã
(WhatsApp + E-mail).
"""
import argparse
import logging
from datetime import date, timedelta
from pathlib import Path

from email_utils import enviar_email, envelope_html
from regras.preferencias_motoristas import CatalogoMotoristas
from vuupt_client import VuuptClient

logger = logging.getLogger(__name__)

# Lógica de extração e formatação...
```

---

## 5. Critérios de Aceite

- [x] Extração correta dos serviços e motoristas alocados para a data especificada no VUUPT.
- [x] Geração de arquivo `dados/mensagens_whatsapp_amanha.txt` com mensagens individuais e de grupo prontas para envio.
- [x] Suporte ao envio opcional de e-mails para motoristas cadastrados com a infraestrutura `email_utils.py`.
- [x] Exibição de resumo no terminal e integração com o fluxo de roteirização.
