# -*- coding: utf-8 -*-
"""
executor.py

Roda um agente (script .py) em segundo plano via subprocess, sem
travar a resposta web, e guarda o histórico de execuções (status,
início/fim, código de saída, caminho do log) numa tabela própria em
dados/dados.db. O log de cada execução vai pra um arquivo separado em
dados/logs/, lido sob demanda pelo painel (polling simples via JS).
"""
import logging
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_RAIZ = Path(__file__).parent.parent  # painel_agentes/ -> raiz do projeto
DB_PATH = _RAIZ / "dados" / "dados.db"
LOGS_DIR = Path(__file__).parent / "dados" / "logs"

TIMEOUT_PADRAO_MINUTOS = 30  # se um agente não definir "timeout_minutos" próprio, usa esse

# Registro dos processos RODANDO agora mesmo (execucao_id -> Popen) --
# pedido do Hugo, 06/08: botão pra encerrar tudo de uma vez. Cada
# thread em _rodar_processo se registra aqui assim que o subprocess
# sobe, e se remove quando termina. _EXECUCOES_ENCERRADAS_MANUALMENTE
# marca quais IDs foram mortos pelo botão (não por timeout nem erro
# normal), pra thread original dar o status certo quando o wait()
# dela finalmente retornar (o kill faz o processo morrer, então o
# wait() que já estava rodando naquela thread vai retornar sozinho).
_PROCESSOS_RODANDO: dict[int, subprocess.Popen] = {}
_EXECUCOES_ENCERRADAS_MANUALMENTE: set[int] = set()
_LOCK_PROCESSOS = threading.Lock()


def _conectar():
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS painel_execucoes (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            agente_id       TEXT NOT NULL,
            agente_nome     TEXT NOT NULL,
            modo_teste      INTEGER NOT NULL,
            status          TEXT NOT NULL DEFAULT 'RODANDO',
            iniciado_em     TEXT NOT NULL,
            finalizado_em   TEXT,
            codigo_saida    INTEGER,
            log_path        TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def _raiz_do_agente(agente: dict) -> Path:
    """A maioria dos agentes mora dentro do agente_stokki_eventos (raiz
    padrão do painel) -- mas alguns são de OUTRO projeto inteiro (ex:
    agente_importacao_stokki, pedido do Hugo 03/08). Se o agente tiver
    'raiz_absoluta' definido, usa ela em vez da raiz padrão."""
    if agente.get("raiz_absoluta"):
        return Path(agente["raiz_absoluta"])
    return _RAIZ


def _montar_comando(agente: dict, modo_teste: bool, args_extra: list[str] | None = None) -> list[str]:
    script_path = _raiz_do_agente(agente) / agente["script"]
    comando = [sys.executable, str(script_path)]
    comando.extend(agente.get("args_fixos", []))
    if args_extra:
        comando.extend(args_extra)
    if modo_teste and agente.get("suporta_teste") and agente.get("flag_teste"):
        comando.append(agente["flag_teste"])
    return comando


def _matar_arvore_processo(processo: subprocess.Popen):
    """
    processo.kill() sozinho só mata o processo Python -- se ele tiver
    aberto um navegador (Playwright) ou outro processo filho, esses
    ficam órfãos rodando pra sempre. No Windows, taskkill /T mata a
    árvore inteira (processo + tudo que ele abriu). Chama os dois --
    kill() primeiro (garante pelo menos o processo principal, mesmo
    se taskkill falhar por algum motivo), taskkill depois (pega o resto).
    """
    try:
        processo.kill()
    except Exception:
        pass
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(processo.pid)],
            capture_output=True, timeout=15,
        )
    except Exception as e:
        logger.warning(f"taskkill não disponível/falhou pro PID {processo.pid} ({e}) -- "
                       f"processo principal ainda foi encerrado via kill(), mas processos "
                       f"filhos (ex: navegador do Playwright) podem ter ficado órfãos.")


def _rodar_processo(agente: dict, modo_teste: bool, execucao_id: int, log_path: Path,
                    args_extra: list[str] | None = None):
    comando = _montar_comando(agente, modo_teste, args_extra)
    cwd = _raiz_do_agente(agente) / agente.get("cwd", ".")
    timeout_minutos = agente.get("timeout_minutos", TIMEOUT_PADRAO_MINUTOS)
    timeout_segundos = timeout_minutos * 60

    with open(log_path, "w", encoding="utf-8") as log_file:
        log_file.write(f"Agente: {agente['nome']}\n")
        log_file.write(f"Comando: {' '.join(comando)}\n")
        log_file.write(f"Diretório: {cwd}\n")
        log_file.write(f"Limite de tempo: {timeout_minutos} min\n")
        log_file.write("=" * 72 + "\n\n")
        log_file.flush()

        try:
            processo = subprocess.Popen(
                comando, cwd=str(cwd), stdout=log_file, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
            )
            with _LOCK_PROCESSOS:
                _PROCESSOS_RODANDO[execucao_id] = processo

            try:
                codigo_saida = processo.wait(timeout=timeout_segundos)
                with _LOCK_PROCESSOS:
                    foi_encerrado_manualmente = execucao_id in _EXECUCOES_ENCERRADAS_MANUALMENTE
                    _EXECUCOES_ENCERRADAS_MANUALMENTE.discard(execucao_id)
                if foi_encerrado_manualmente:
                    status = "ENCERRADO_MANUALMENTE"
                    log_file.write(f"\n\n{'=' * 72}\nEncerrado manualmente pelo botão do painel.\n")
                else:
                    status = "SUCESSO" if codigo_saida == 0 else "ERRO"
            except subprocess.TimeoutExpired:
                # Achado em produção, 05/08: um agente pode travar de
                # verdade (ex: chamada de rede sem timeout que nunca
                # volta) -- sem esse limite, a thread fica presa pra
                # sempre e o registro nunca sai de RODANDO, mesmo com
                # o painel inteiro saudável (o Ctrl+C de antes não
                # cobre esse caso -- aqui o painel nunca caiu).
                logger.warning(f"Agente '{agente['nome']}' passou de {timeout_minutos} min -- encerrando à força.")
                log_file.write(f"\n\n{'=' * 72}\nTEMPO ESGOTADO ({timeout_minutos} min) -- processo encerrado à força.\n")
                _matar_arvore_processo(processo)
                codigo_saida = None
                status = "TIMEOUT"
            finally:
                with _LOCK_PROCESSOS:
                    _PROCESSOS_RODANDO.pop(execucao_id, None)
        except Exception as e:
            log_file.write(f"\n\nErro ao iniciar o processo: {e}\n")
            codigo_saida = -1
            status = "ERRO"

    agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = _conectar()
    conn.execute(
        "UPDATE painel_execucoes SET status=?, finalizado_em=?, codigo_saida=? WHERE id=?",
        (status, agora, codigo_saida, execucao_id),
    )
    conn.commit()
    conn.close()


def iniciar_execucao(agente: dict, modo_teste: bool, args_extra: list[str] | None = None) -> int:
    """Inicia o agente em background (thread própria) e retorna o ID
    da execução pra acompanhar depois. args_extra vai pro final do
    comando (depois de args_fixos) -- pedido do Hugo, 14/08: a barra de
    agentes do planejamento deixa filtrar a Importação por pedido/
    embarcador na hora, sem precisar de uma entrada nova em agentes.py
    pra cada combinação possível de filtro."""
    agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    carimbo = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = LOGS_DIR / f"{agente['id']}_{carimbo}.log"

    conn = _conectar()
    cursor = conn.execute("""
        INSERT INTO painel_execucoes (agente_id, agente_nome, modo_teste, status, iniciado_em, log_path)
        VALUES (?, ?, ?, 'RODANDO', ?, ?)
    """, (agente["id"], agente["nome"], 1 if modo_teste else 0, agora, str(log_path)))
    execucao_id = cursor.lastrowid
    conn.commit()
    conn.close()

    thread = threading.Thread(
        target=_rodar_processo, args=(agente, modo_teste, execucao_id, log_path, args_extra), daemon=True
    )
    thread.start()
    return execucao_id


def iniciar_sequencia(passos: list[dict]):
    """Roda uma lista de agentes em sequência, cada um esperando o
    anterior terminar de verdade antes do próximo começar -- pedido do
    Hugo, 14/08 ("Executar tudo" da barra de agentes do planejamento:
    Importação → Criar Rotas Diárias Rascunho → Incrementar Rotas →
    Gerar Romaneios). Mesmo motivo do -Wait do rodar_sequencial.ps1:
    evitar concorrência no SQLite/sessão Stokki entre etapas.

    Cada passo já vira uma execução normal em painel_execucoes, então o
    front acompanha pelo /etapas de sempre (buscar_ultima_execucao por
    agente_id) -- não precisa de um status de sequência à parte.

    passos: [{"agente": <dict de agentes.py>, "args_extra": [...] | None}, ...]
    """
    thread = threading.Thread(target=_rodar_sequencia, args=(passos,), daemon=True)
    thread.start()


def _rodar_sequencia(passos: list[dict]):
    for passo in passos:
        execucao_id = iniciar_execucao(passo["agente"], modo_teste=False, args_extra=passo.get("args_extra"))
        while True:
            execucao = buscar_execucao(execucao_id)
            if not execucao or execucao["status"] != "RODANDO":
                break
            time.sleep(2)


def buscar_execucao(execucao_id: int) -> dict | None:
    conn = _conectar()
    row = conn.execute("SELECT * FROM painel_execucoes WHERE id = ?", (execucao_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def buscar_ultima_execucao(agente_id: str) -> dict | None:
    conn = _conectar()
    row = conn.execute(
        "SELECT * FROM painel_execucoes WHERE agente_id = ? ORDER BY id DESC LIMIT 1", (agente_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def listar_execucoes_recentes(limite: int = 50) -> list[dict]:
    conn = _conectar()
    rows = conn.execute(
        "SELECT * FROM painel_execucoes ORDER BY id DESC LIMIT ?", (limite,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def ha_execucao_rodando(agente_id: str) -> bool:
    ultima = buscar_ultima_execucao(agente_id)
    return bool(ultima and ultima["status"] == "RODANDO")


def ler_log(execucao_id: int) -> str:
    execucao = buscar_execucao(execucao_id)
    if not execucao:
        return ""
    log_path = Path(execucao["log_path"])
    if not log_path.exists():
        return ""
    try:
        return log_path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"(falha ao ler o log: {e})"


def encerrar_todas_execucoes() -> int:
    """
    Botão de emergência (pedido do Hugo, 06/08): encerra à força TODOS
    os processos rodando agora mesmo, não só um que passou do próprio
    limite de tempo. Usa a mesma _matar_arvore_processo() do timeout
    individual (kill() + taskkill /T, pega navegador do Playwright
    junto se houver). Marca cada um em _EXECUCOES_ENCERRADAS_
    MANUALMENTE ANTES de matar -- assim a thread original (que está
    parada em processo.wait()) sabe, quando o wait() retornar (o kill
    faz isso acontecer), que foi um encerramento manual e não um erro
    comum, e grava o status certo sozinha.

    Retorna quantos processos foram encerrados.
    """
    with _LOCK_PROCESSOS:
        itens = list(_PROCESSOS_RODANDO.items())  # cópia -- não mexe no dict durante o kill
        for execucao_id, _processo in itens:
            _EXECUCOES_ENCERRADAS_MANUALMENTE.add(execucao_id)

    for execucao_id, processo in itens:
        logger.warning(f"Encerrando à força a execução {execucao_id} (botão 'encerrar tudo').")
        _matar_arvore_processo(processo)

    return len(itens)


def limpar_execucoes_travadas() -> int:
    """
    Marca como INTERROMPIDO qualquer execução que ficou presa em
    RODANDO -- acontece quando o processo do painel é encerrado à
    força (ex: Ctrl+C) no meio de uma execução: a thread que ia
    atualizar o status pra SUCESSO/ERRO morre junto, sem chance de
    rodar, e o registro fica preso pra sempre (bloqueando novas
    execuções desse agente). Chamado automaticamente toda vez que o
    painel sobe -- nada pode estar genuinamente RODANDO nesse momento
    (threads não sobrevivem a um restart do processo), então qualquer
    RODANDO encontrado aqui é, por definição, uma sobra de uma queda
    anterior. Retorna quantas execuções foram destravadas.
    """
    agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = _conectar()
    cursor = conn.execute(
        "UPDATE painel_execucoes SET status = 'INTERROMPIDO', finalizado_em = ? WHERE status = 'RODANDO'",
        (agora,),
    )
    conn.commit()
    quantidade = cursor.rowcount
    conn.close()
    if quantidade:
        logger.warning(f"{quantidade} execução(ões) travada(s) em RODANDO foram marcadas como INTERROMPIDO "
                       f"(provavelmente o painel foi encerrado à força na execução anterior).")
    return quantidade
