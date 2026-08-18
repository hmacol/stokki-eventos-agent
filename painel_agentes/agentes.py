# -*- coding: utf-8 -*-
"""
agentes.py

Registro de todos os agentes/scripts do agente_stokki_eventos que
podem ser disparados e acompanhados pelo painel web -- pedido do
Hugo, 03/08: "rodar e acompanhar o processamento numa tela web mais
amigável que o cmd".

Cada entrada:
  id            -- identificador único (usado na URL)
  nome          -- nome amigável, mostrado no painel
  descricao     -- 1 frase do que o agente faz
  script        -- caminho do .py relativo à raiz do projeto
  cwd           -- diretório de trabalho relativo à raiz (onde o
                   script espera ser executado, mesma pasta do script
                   na maioria dos casos)
  suporta_teste -- se aceita rodar em modo de teste
  flag_teste    -- a flag exata de modo teste (varia entre agentes:
                   a maioria usa --modo-teste, um usa --teste)
  args_fixos    -- argumentos sempre incluídos (ex: relatório precisa
                   de --diario ou --semanal, não tem um modo "geral")
  categoria     -- agrupamento visual no painel
"""

AGENTES = [
    {
        "id": "executar_tudo",
        "nome": "Executar Tudo",
        "descricao": "Pipeline completo: respostas de agendamento/insucesso, impressão, expedição e importação.",
        "script": "executar_tudo.py",
        "cwd": ".",
        "suporta_teste": True,
        "flag_teste": "--modo-teste",
        "args_fixos": [],
        "categoria": "Pipeline principal",
    },
    {
        "id": "somente_importacao",
        "nome": "Somente Importação",
        "descricao": "Só a etapa de importação: resolve endereço/telefone/skill/agendamento e cria/atualiza no VUUPT.",
        "script": "pipeline.py",
        "cwd": ".",
        "suporta_teste": True,
        "flag_teste": "--modo-teste",
        "args_fixos": [],
        "categoria": "Pipeline principal",
    },
    {
        "id": "somente_expedicao",
        "nome": "Somente Expedição",
        "descricao": "Só a etapa de expedição: entregues com canhoto validado expedem na Stokki, insucessos são tratados à parte.",
        "script": "expedir_pedidos.py",
        "cwd": ".",
        "suporta_teste": True,
        "flag_teste": "--modo-teste",
        "args_fixos": [],
        "categoria": "Pipeline principal",
    },
    {
        "id": "somente_impressao",
        "nome": "Somente Impressão",
        "descricao": "Só a Estação de Impressão: move pedidos faturados de 'Em espera' para 'Aguardando Transportador'.",
        "script": "somente_impressao.py",
        "cwd": ".",
        "suporta_teste": True,
        "flag_teste": "--modo-teste",
        "args_fixos": [],
        "categoria": "Pipeline principal",
    },
    {
        "id": "verificar_duplicados_vuupt",
        "nome": "Verificar Pedidos Duplicados no VUUPT",
        "descricao": "Audita e cancela cópias sobressalentes não atribuídas de serviços duplicados no VUUPT.",
        "script": "verificar_pedidos_duplicados_vuupt.py",
        "cwd": ".",
        "suporta_teste": True,
        "flag_teste": "--modo-teste",
        "args_fixos": [],
        "categoria": "Pipeline principal",
    },
    {
        "id": "atualizar_agendamentos",
        "nome": "Atualizar Agendamentos Confirmados",
        "descricao": "Aplica no VUUPT os agendamentos já confirmados por e-mail.",
        "script": "atualizar_agendamentos_confirmados.py",
        "cwd": ".",
        "suporta_teste": True,
        "flag_teste": "--modo-teste",
        "args_fixos": [],
        "categoria": "Agendamento",
    },
    {
        "id": "ler_planilha_entregas_nuu",
        "nome": "Ler Planilha de Entregas (NUU)",
        "descricao": "Lê o ENTREGAS.xlsx que a NUU/Maria Dolores manda por e-mail: agendamento confirmado e endereço divergente por NF.",
        "script": "ler_planilha_entregas_nuu.py",
        "cwd": ".",
        "suporta_teste": True,
        "flag_teste": "--modo-teste",
        "args_fixos": [],
        "categoria": "Agendamento",
    },
    {
        "id": "criar_rotas_diarias",
        "nome": "Criar Rotas Diárias",
        "descricao": "Cria as rotas do dia seguinte a partir dos pedidos not_assigned (job das 13h). "
                    "Ainda cria direto na VUUPT -- é o que o agendamento automático (rodar_sequencial.ps1) "
                    "usa; passa a rodar em modo rascunho quando a tela de planejamento (Fase 2/3) estiver pronta.",
        "script": "roteirizacao/criar_rotas_diarias.py",
        "cwd": "roteirizacao",
        "suporta_teste": True,
        "flag_teste": "--modo-teste",
        "args_fixos": [],
        "categoria": "Roteirização",
    },
    {
        "id": "criar_rotas_diarias_rascunho",
        "nome": "Criar Rotas Diárias (Rascunho)",
        "descricao": "Mesmo cálculo de rotas de sempre, mas grava como RASCUNHO local (dados/dados.db) "
                    "em vez de criar direto na VUUPT -- pra testar/gerar rascunhos pra revisão no painel "
                    "de planejamento, sem mexer no fluxo automático de produção.",
        "script": "roteirizacao/criar_rotas_diarias.py",
        "cwd": "roteirizacao",
        "suporta_teste": False,
        "flag_teste": None,
        "args_fixos": ["--gerar-rascunho"],
        "categoria": "Roteirização",
    },
    {
        "id": "incrementar_rotas",
        "nome": "Incrementar Rotas",
        "descricao": "Aloca pedidos novos nas rotas do dia já criadas (job de hora em hora).",
        "script": "roteirizacao/incrementar_rotas.py",
        "cwd": "roteirizacao",
        "suporta_teste": True,
        "flag_teste": "--modo-teste",
        "args_fixos": [],
        "categoria": "Roteirização",
    },
    {
        "id": "gerar_romaneios",
        "nome": "Gerar PDFs de Romaneio",
        "descricao": "Gera 1 PDF por rota do dia com NFs e boletos na ordem de visita (job das 04h).",
        "script": "roteirizacao/gerar_pdf_romaneios.py",
        "cwd": "roteirizacao",
        "suporta_teste": True,
        "flag_teste": "--modo-teste",
        "args_fixos": [],
        "categoria": "Roteirização",
    },
    {
        "id": "notificar_espera",
        "nome": "Notificar Pedidos em Espera",
        "descricao": "Notifica embarcadores sobre pedidos parados aguardando faturamento/liberação.",
        "script": "notificar_pedidos_em_espera.py",
        "cwd": ".",
        "suporta_teste": True,
        "flag_teste": "--modo-teste",
        "args_fixos": [],
        "categoria": "Notificações",
    },
    {
        "id": "relatorio_diario",
        "nome": "Relatório Operacional Diário",
        "descricao": "Envia o relatório operacional do dia (hoje) por e-mail.",
        "script": "relatorio_operacional.py",
        "cwd": ".",
        "suporta_teste": True,
        "flag_teste": "--teste",
        "args_fixos": ["--diario"],
        "categoria": "Relatórios",
    },
    {
        "id": "relatorio_semanal",
        "nome": "Relatório Operacional Semanal",
        "descricao": "Envia o relatório operacional dos últimos 7 dias por e-mail.",
        "script": "relatorio_operacional.py",
        "cwd": ".",
        "suporta_teste": True,
        "flag_teste": "--teste",
        "args_fixos": ["--semanal"],
        "categoria": "Relatórios",
    },
    {
        "id": "atualizar_dashboard_embarcadores",
        "nome": "Atualizar Dashboard de Embarcadores",
        "descricao": "Atualiza o cache de dados do dashboard de comparativo de embarcadores.",
        "script": "dashboard_embarcadores/atualizar_dashboard.py",
        "cwd": "dashboard_embarcadores",
        "suporta_teste": False,
        "flag_teste": None,
        "args_fixos": [],
        "categoria": "Dashboard",
    },
    {
        "id": "importacao_stokki_emporio_quatro_estrelas",
        "nome": "Importação Stokki — Empório Quatro Estrelas",
        "descricao": "Baixa XMLs de pedido por e-mail, processa e importa na Stokki (Playwright).",
        "script": "main.py",
        "cwd": ".",
        "raiz_absoluta": r"C:\agente_importacao_stokki",
        "suporta_teste": True,
        "flag_teste": "--modo-teste",
        "args_fixos": ["Empório Quatro Estrelas"],
        "categoria": "Importação Stokki",
    },
    {
        "id": "processar_documentos",
        "nome": "Processar Documentos (NF, Boleto, CC, Agendamento)",
        "descricao": "Busca documentos por e-mail e na Stokki, classifica, casa com o pedido e envia pro GCS.",
        "script": "processar_documentos.py",
        "cwd": "documentos_pedido",
        "suporta_teste": True,
        "flag_teste": "--modo-teste",
        "args_fixos": [],
        "categoria": "Documentos",
    },
]


def buscar_agente(agente_id: str) -> dict | None:
    return next((a for a in AGENTES if a["id"] == agente_id), None)


def categorias_ordenadas() -> list[str]:
    """Ordem de exibição das categorias no painel (não alfabética --
    segue a ordem natural do fluxo operacional do dia)."""
    ordem = ["Pipeline principal", "Agendamento", "Roteirização", "Notificações", "Relatórios",
            "Importação Stokki", "Documentos", "Dashboard"]
    presentes = {a["categoria"] for a in AGENTES}
    return [c for c in ordem if c in presentes]
