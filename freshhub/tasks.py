# -*- coding: utf-8 -*-
"""
freshhub/tasks.py

Escrita na tabela `tasks` -- fonte real do Kanban "Demandas" do Fresh
Hub (freshhub.com.br/demandas). Usado pelo encaminhamento à equipe de
operação dos pedidos parados classificados como Cancelados ou Devolução
Parcial (nunca duplicados na Vuupt -- ver TRATATIVAS_PEDIDOS_PARADOS.md
na raiz do projeto pro contexto de negócio completo).

Molde de cada tratativa (categoria/área já cadastradas no Fresh Hub,
confirmadas com o Hugo em 24/08 -- "Devolução Parcial" foi criada por
ele nesse dia em task_categories):

  Cancelados:
    category: "Cancelamento de Pedido e Retorno ao Estoque"
    area:     "Operações"
  Devolução Parcial:
    category: "Devolução Parcial"
    area:     "Operações"
  Descartar:
    (mesmo molde de Cancelados -- confirmado com o Hugo, 24/08: "mesmo
    comportamento de Cancelado, precisamos comunicar a equipe")

Em ambos: priority="alta", status inicial "aguardando_prazo",
requested_by = o próprio usuário de serviço logado (sessao.user_id) --
não o humano que classificou no nosso painel (decisão do Hugo, 24/08).
Título sempre "{categoria} — {client_name}", sem número do pedido.
"""
import logging

from freshhub.auth import SUPABASE_URL, FreshHubSession

logger = logging.getLogger(__name__)

# Molde por tratativa -- se o Hugo cadastrar mais tratativas que viram
# Demanda no futuro, é só adicionar uma entrada aqui.
MOLDE_TRATATIVA = {
    "Cancelados": {
        "category": "Cancelamento de Pedido e Retorno ao Estoque",
        "area": "Operações",
    },
    "Devolução Parcial": {
        "category": "Devolução Parcial",
        "area": "Operações",
    },
    # Mesmo molde de Cancelados -- confirmado com o Hugo, 24/08: mesmo
    # comportamento, a operação precisa ser avisada do mesmo jeito.
    "Descartar": {
        "category": "Cancelamento de Pedido e Retorno ao Estoque",
        "area": "Operações",
    },
}


def criar_demanda(
    sessao: FreshHubSession,
    *,
    title: str,
    category: str,
    area: str,
    client_name: str,
    priority: str = "alta",
    description: str | None = None,
) -> dict:
    """
    Cria uma Demanda (linha em `tasks`) com status inicial
    "aguardando_prazo" -- aparece na coluna "Aguardando prazo" do
    Kanban de Demandas.

    Retorna o registro criado (dict com o `id` gerado, entre outros
    campos -- pede `return=representation` implicitamente via
    resp.json(), igual o Fresh Hub faz no próprio POST).
    """
    body = {
        "title": title,
        "description": description,
        "client_name": client_name,
        "category": category,
        "priority": priority,
        "area": area,
        "requested_by": sessao.user_id,
        "status": "aguardando_prazo",
    }
    resp = sessao.post(
        f"{SUPABASE_URL}/rest/v1/tasks",
        params={"select": "id"},
        headers={"Prefer": "return=representation"},
        json=body,
    )
    resp.raise_for_status()
    criada = resp.json()
    # PostgREST devolve lista OU objeto único dependendo do Accept/Prefer
    # -- normaliza pra sempre devolver um dict aqui.
    if isinstance(criada, list):
        criada = criada[0] if criada else {}
    logger.info(f"Demanda criada no Fresh Hub: id={criada.get('id')} title={title!r}")
    return criada


def criar_demanda_para_tratativa(sessao: FreshHubSession, tratativa: str, client_name: str) -> dict:
    """
    Atalho pras duas tratativas que hoje viram Demanda (Cancelados e
    Devolução Parcial) -- usa o molde de MOLDE_TRATATIVA em vez de
    montar os campos na mão em cada ponto de chamada.
    """
    if tratativa not in MOLDE_TRATATIVA:
        raise ValueError(
            f"Tratativa {tratativa!r} não tem molde de Demanda cadastrado "
            f"(só existem: {', '.join(MOLDE_TRATATIVA)}). Ver "
            "TRATATIVAS_PEDIDOS_PARADOS.md antes de adicionar uma nova."
        )
    molde = MOLDE_TRATATIVA[tratativa]
    title = f"{molde['category']} — {client_name}"
    return criar_demanda(
        sessao,
        title=title,
        category=molde["category"],
        area=molde["area"],
        client_name=client_name,
    )