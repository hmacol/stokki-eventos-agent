# -*- coding: utf-8 -*-
"""
mapa_util.py

Funções compartilhadas entre as telas de mapa do painel (mapa_rotas.py,
somente leitura, e planejamento_rotas.py, editor de rascunhos, Fase 2
do plano de roteirizador do Hugo, 12/08) -- extraídas de mapa_rotas.py
pra não duplicar (a célula de grade em especial precisa ficar
sincronizada com roteirizacao_dados.py::agrupar_por_regiao, então só
faz sentido existir num lugar só).
"""
import sqlite3
from pathlib import Path

_RAIZ = Path(__file__).parent.parent

# Mesmo valor de roteirizacao_dados.py::agrupar_por_regiao() -- não
# importado direto de lá pra não arrastar as dependências de
# geocodificação daquele módulo aqui; é só uma constante, mantida
# sincronizada manualmente. Se mudar lá, mudar aqui também.
TAMANHO_GRADE_GRAUS = 0.1


def carregar_remetentes_por_sender_id() -> dict[int, str]:
    """sender_id -> nome pra mostrar (apelido, ou nome_remetente se não
    tiver apelido) -- mesmo padrão usado em notificar_area_nao_atendida.py."""
    db_path = _RAIZ / "dados" / "dados.db"
    if not db_path.exists():
        return {}
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT sender_id, nome_remetente, apelido FROM interno WHERE sender_id IS NOT NULL"
    ).fetchall()
    conn.close()
    return {sender_id: (apelido or nome_remetente or f"Remetente {sender_id}")
           for sender_id, nome_remetente, apelido in rows}


def celula_grade(lat: float, lng: float) -> dict:
    """Mesmo arredondamento de agrupar_por_regiao() -- devolve o centro
    e os limites da célula de ~11km que contém esse ponto."""
    lat_grade = round(lat / TAMANHO_GRADE_GRAUS) * TAMANHO_GRADE_GRAUS
    lng_grade = round(lng / TAMANHO_GRADE_GRAUS) * TAMANHO_GRADE_GRAUS
    meia_grade = TAMANHO_GRADE_GRAUS / 2
    return {
        "chave": f"{lat_grade:.2f},{lng_grade:.2f}",
        "lat_min": lat_grade - meia_grade, "lat_max": lat_grade + meia_grade,
        "lng_min": lng_grade - meia_grade, "lng_max": lng_grade + meia_grade,
    }


def extrair_servicos_da_rota(rota: dict) -> list[dict]:
    """Mesma lógica de incrementar_rotas.py: 'services' vem embrulhado
    como {"data": [...]}, não lista direta."""
    servicos_wrapper = rota.get("services")
    if isinstance(servicos_wrapper, dict):
        return servicos_wrapper.get("data", []) or []
    if isinstance(servicos_wrapper, list):
        return servicos_wrapper
    return []
