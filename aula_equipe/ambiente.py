# -*- coding: utf-8 -*-
"""
Ambiente de gravacao da aula da equipe: o painel real (deste checkout) com
config e banco ficticios, sem credencial nenhuma e com proxy morto pra
qualquer chamada de saida. Nada chega a Stokki, Vuupt, GCS, SMTP ou WhatsApp.

Mesmas travas da aula do cliente (e a mesma marca, entao os dois geradores
se revezam no mesmo worktree): so escreve config.yaml/dados.db que um deles
criou; um config.yaml ou dados.db de verdade faz AmbienteRecusado.
"""
import secrets
import sqlite3
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from aula_equipe import dados
from aula_portal.ambiente import (MARCA_CONFIG, TABELA_MARCA, AmbienteRecusado, _copiar_esquema, _env_isolado,
                                  _esquema_padrao, _porta_livre, parar_portal as parar_painel)

ESTADOS = ("retornos", "coletas")
PORTA_PADRAO = 8098
USUARIOS = {"galpao": "galpao", "expedicao": "expedicao"}   # nivel -> login fixo do config ficticio

__all__ = ["AmbienteRecusado", "escrever_config", "semear_banco", "subir_painel", "parar_painel", "SENHA"]

SENHA = "aula-" + secrets.token_hex(6)   # so vale no config ficticio desta gravacao


def escrever_config(raiz: Path) -> Path:
    """config.yaml minimo: segredo da sessao e os logins fixos do galpao e da
    expedicao. Sem vuupt_api, stokki, gcs, email, freshhub_api..."""
    caminho = Path(raiz) / "config.yaml"
    if caminho.exists() and not caminho.read_text(encoding="utf-8").startswith(MARCA_CONFIG):
        raise AmbienteRecusado(f"{caminho} existe e nao e de gravacao: nao sobrescrevo.")
    caminho.write_text(
        f"{MARCA_CONFIG}\n"
        "painel_agentes:\n"
        f"  secret_key: {secrets.token_hex(32)}\n"
        f"  usuario: aula-total\n"
        f"  senha: {secrets.token_hex(12)}\n"
        f"  usuario_galpao: {USUARIOS['galpao']}\n"
        f"  senha_galpao: {SENHA}\n"
        f"  usuario_expedicao: {USUARIOS['expedicao']}\n"
        f"  senha_expedicao: {SENHA}\n",
        encoding="utf-8")
    return caminho


def semear_banco(raiz: Path, estado: str, esquema: Path | None | str = "padrao") -> Path:
    """Apaga e recria raiz/dados/dados.db so com os dados ficticios do video."""
    if estado not in ESTADOS:
        raise ValueError(f"estado invalido: {estado}")
    pasta = Path(raiz) / "dados"
    pasta.mkdir(parents=True, exist_ok=True)
    db = pasta / "dados.db"
    if db.exists():
        conn = sqlite3.connect(db)
        try:
            nosso = conn.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (TABELA_MARCA,)).fetchone()
        finally:
            conn.close()
        if not nosso:
            raise AmbienteRecusado(f"{db} existe e nao e de gravacao: nao apago.")
        for sufixo in ("", "-wal", "-shm"):
            Path(f"{db}{sufixo}").unlink(missing_ok=True)

    conn = sqlite3.connect(db)
    conn.execute(f"CREATE TABLE {TABELA_MARCA} (criado_em TEXT)")
    conn.execute(f"INSERT INTO {TABELA_MARCA} VALUES (?)", (datetime.now().isoformat(timespec="seconds"),))
    origem = _esquema_padrao() if esquema == "padrao" else esquema
    if origem:
        _copiar_esquema(conn, Path(origem))
    conn.commit()
    conn.close()

    from nucleo import banco as nucleo_banco
    from coletas import banco as coletas_banco
    import retornos
    conn = nucleo_banco.conectar(db)          # cria/migra as tabelas do nucleo
    conn.close()
    conn = retornos.conectar(db)              # cria as do recebimento (e o "inicio" = hoje)
    try:
        coletas_banco.garantir_esquema(conn)
        if estado == "retornos":
            dados.semear_retornos(conn)
        else:
            dados.semear_coletas(conn)
    finally:
        conn.close()
    return db


def subir_painel(raiz: Path, porta: int = PORTA_PADRAO) -> subprocess.Popen:
    """Sobe o painel deste checkout na porta (que precisa estar livre: nunca
    reaproveita nem derruba processo alheio) e espera a tela de login."""
    raiz = Path(raiz)
    cfg = raiz / "config.yaml"
    if not cfg.exists() or not cfg.read_text(encoding="utf-8").startswith(MARCA_CONFIG):
        raise AmbienteRecusado("config.yaml de gravacao ausente: rode escrever_config antes.")
    if not _porta_livre(porta):
        raise AmbienteRecusado(f"porta {porta} ocupada: escolha outra com --porta.")
    env = _env_isolado(raiz)
    env["PYTHONPATH"] = str(raiz)   # o servidor poe painel_agentes/ na frente (aula_equipe/__init__)
    env.pop("PORTAL_CLIENTE_DEV", None)
    arq_log = raiz / "dados" / "aula_equipe.log"
    log = open(arq_log, "w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "-m", "aula_equipe.servidor", "--porta", str(porta)],
                            cwd=raiz / "painel_agentes", env=env, stdout=log, stderr=subprocess.STDOUT)
    sem_proxy = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    limite = time.time() + 90
    while time.time() < limite:
        if proc.poll() is not None:
            log.close()
            raise RuntimeError("o painel saiu ao subir:\n" + arq_log.read_text(encoding="utf-8")[-3000:])
        try:
            sem_proxy.open(f"http://127.0.0.1:{porta}/login", timeout=2)
            return proc
        except OSError:
            time.sleep(0.5)
    parar_painel(proc)
    raise RuntimeError("o painel nao respondeu /login em 90 s")
