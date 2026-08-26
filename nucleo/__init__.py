# -*- coding: utf-8 -*-
"""
nucleo/

Núcleo próprio de pedidos, rotas, paradas, eventos e comprovantes --
Fase A do DOC_EXECUCAO_CLAUDE_APP_MOTORISTAS.md (decisão do Hugo, 25/08:
deixar de usar a VUUPT; o app de motoristas próprio e o painel passam a
ler/gravar aqui).

Módulos:
  banco.py              -- esquema (idempotente) + conexão em dados/dados.db
  pedidos.py            -- espelho de pedidos (dual-write do pipeline.py)
  rotas.py              -- rotas/paradas materializadas (snapshot do rascunho enviado)
  sincronizar_vuupt.py  -- job que puxa status/timestamps da VUUPT enquanto ela existir
  financeiro.py         -- extrato do motorista sobre nucleo_rotas + regras/tarifa_motorista.py

Regra de ouro da Fase A: NENHUM gancho daqui pode derrubar o que roda hoje
-- toda chamada a partir de pipeline.py / rascunhos_rota.py é best-effort
(try/except + warning no log).
"""