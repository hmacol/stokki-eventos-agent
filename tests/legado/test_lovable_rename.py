"""Garante que o pacote do Lovable continua importavel com os mesmos
nomes publicos depois da renomeacao freshhub/ -> lovable/ (Fase 0.1)."""
import importlib
import subprocess
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]


def test_nomes_publicos_do_lovable():
    sys.path.insert(0, str(RAIZ))
    auth = importlib.import_module("lovable.auth")
    parados = importlib.import_module("lovable.pedidos_parados")
    tasks = importlib.import_module("lovable.tasks")
    assert hasattr(auth, "FreshHubSession")
    assert hasattr(auth, "SUPABASE_URL")
    assert auth.SESSAO_PATH == RAIZ / "lovable" / "sessao_freshhub.json"
    assert hasattr(parados, "listar_pedidos_parados")
    assert hasattr(parados, "normalizar_order_number")
    assert hasattr(tasks, "criar_demanda_para_tratativa")


def test_ninguem_mais_importa_freshhub_do_lovable():
    saida = subprocess.run(
        ["git", "grep", "-l", "-E",
         r"from freshhub\.(auth|tasks|pedidos_parados)|import freshhub\.(auth|tasks|pedidos_parados)",
         "--", "*.py"],
        cwd=RAIZ, capture_output=True, text=True,
    )
    assert saida.stdout.strip() == "", f"ainda importam o Lovable pelo nome antigo:\n{saida.stdout}"
