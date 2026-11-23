# -*- coding: utf-8 -*-
"""
stokki/sessao_uso.py

Trava cooperativa de "quem está usando a Stokki agora" (pedido do Hugo,
08/09/2026, pra máscara de envio de pedidos do portal do cliente): a
Stokki derruba a sessão anterior quando outro processo faz login com o
mesmo usuário (ver memória stokki-sessao-e-anexo-limitacoes), então quem
vai abrir um login novo (worker do portal, importador por e-mail) checa
antes se alguém está no meio de uma execução e, se estiver, ESPERA a vez
em vez de atropelar.

É uma linha por conta na tabela `stokki_sessao_uso` do dados.db (mesmo
banco que o painel e o portal compartilham na VPS), com dono + validade
(expira_em) -- processo que morre sem liberar não trava ninguém pra
sempre. Além da linha, `em_uso()` também considera "em uso" qualquer
execução do painel de agentes em RODANDO (painel_execucoes), já que quase
todo agente disparado pelo painel abre a StokkiSession.

05/10/2026 (Hugo, "segunda alternativa de login para o caso de
congestionamento"): a trava passou a ser POR CONTA (stokki/contas.py:
"principal", "reserva" e "provider" -- a Stokki só derruba sessão do
mesmo usuário). A linha antiga (chave 'principal') continua sendo a da
conta principal. `adquirir(..., alternativa=True)` pega a conta reserva
(admin) quando a principal está com outro processo; `escolher_conta_login`
faz o mesmo na hora do login. Sem alternativa (padrão) ou sem
stokki.reserva no config, tudo funciona como antes -- é o caso do
agente_importacao_stokki, que importa este módulo e loga sempre com o
usuário principal.

05/10/2026 (Hugo, "deixar um agente na fila do outro"): FILA de verdade,
por ordem de chegada. Quem espera tira uma senha (tabela stokki_fila) e
só fica com a conta quando ela está livre E a senha dele é a primeira da
fila -- quem chega depois não fura mais a fila. A senha é renovada a cada
volta da espera (visto_em); processo que morreu na fila sai sozinho em
1 min. Quem pega a conta renova a trava sozinho (thread) até liberar ou
o processo terminar. A execução do painel em RODANDO deixou de ocupar as
contas: o script que o painel dispara entra na fila como qualquer outro.

Uso:
    from stokki.sessao_uso import adquirir, liberar, em_uso

    conta = adquirir("portal-envios", ttl_segundos=900, esperar_segundos=1800, alternativa=True)
    if conta:
        try:
            ...usa a Stokki com a conta `conta`...
        finally:
            liberar("portal-envios")
"""
import atexit
import logging
import os
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from stokki import contas

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent.parent / "dados" / "dados.db"
_FMT = "%Y-%m-%d %H:%M:%S"

# Senha na fila sem sinal de vida há mais que isso = processo que morreu
# esperando: sai da fila.
SENHA_VENCE_SEGUNDOS = 60

# Rotinas que não podem se perder esperam a vez até 2h e, se não chegar,
# desistem sem logar por cima de ninguém (Hugo, 05/10).
ESPERA_MAXIMA_SEGUNDOS = 2 * 60 * 60

# Donos adquiridos por ESTE processo, com a conta de cada um -- o login da
# StokkiSession (auth.py) não pode esperar por uma trava que o próprio
# processo segura (ex.: notificar_transportadoras adquire e depois abre a
# sessão), e usa a conta que o processo pegou.
_DONOS_DESTE_PROCESSO: dict[str, str] = {}
_TRAVA_LOCAL = threading.Lock()


def _conectar() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS stokki_sessao_uso (
            chave      TEXT PRIMARY KEY,
            dono       TEXT NOT NULL,
            desde      TEXT NOT NULL,
            expira_em  TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS stokki_fila (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            conta     TEXT NOT NULL,
            dono      TEXT NOT NULL,
            entrou_em TEXT NOT NULL,
            visto_em  TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def _agora() -> datetime:
    return datetime.now().replace(microsecond=0)


def _dono_valido(conn: sqlite3.Connection, conta: str) -> str | None:
    """Dono da linha da conta, se a trava ainda não venceu."""
    row = conn.execute("SELECT dono, expira_em FROM stokki_sessao_uso WHERE chave = ?", (conta,)).fetchone()
    if not row:
        return None
    try:
        if datetime.strptime(row["expira_em"], _FMT) <= _agora():
            return None
    except ValueError:
        return None
    return row["dono"]


def em_uso(conn: sqlite3.Connection | None = None, ignorar_dono: str | None = None,
           conta: str = contas.PRINCIPAL) -> str | None:
    """Quem está usando a conta agora (o dono da trava), ou None se está
    livre. Trava vencida conta como livre."""
    proprio = conn is None
    conn = conn or _conectar()
    try:
        dono = _dono_valido(conn, conta)
        if dono is None or dono == ignorar_dono:
            return None
        return dono
    finally:
        if proprio:
            conn.close()


def fila(conta: str = contas.PRINCIPAL) -> list[str]:
    """Donos esperando a conta, na ordem (pra tela/log)."""
    conn = _conectar()
    try:
        return [r["dono"] for r in conn.execute(
            "SELECT dono FROM stokki_fila WHERE conta = ? ORDER BY id", (conta,))]
    finally:
        conn.close()


def _renovar_em_segundo_plano(dono: str, ttl_segundos: int) -> None:
    """Mantém a trava viva enquanto o dono não liberar (trabalho mais longo
    que o ttl não perde a vez no meio)."""
    def laco():
        while True:
            time.sleep(max(5, ttl_segundos // 3))
            if dono not in _DONOS_DESTE_PROCESSO:
                return
            try:
                renovar(dono, ttl_segundos)
            except Exception as e:
                logger.warning(f"[sessao_uso] Falha ao renovar a trava de '{dono}': {e}")
    threading.Thread(target=laco, name=f"renova-stokki-{dono}", daemon=True).start()


def adquirir(dono: str, ttl_segundos: int = 900, esperar_segundos: int | None = 0, intervalo: float = 10.0,
             conta: str = contas.PRINCIPAL, alternativa: bool = False) -> str | bool:
    """Tenta ficar com a conta da Stokki. Ocupada (ou com gente na fila na
    frente), entra na fila e espera a vez por ordem de chegada -- até
    esperar_segundos (None = sem limite; 0 = não entra na fila). Pedido
    do Hugo: é a "fila" de um agente atrás do outro. alternativa=True: se
    a principal estiver ocupada e a reserva livre, fica com a reserva.
    Devolve o nome da conta que conseguiu (verdadeiro) ou False.
    ttl_segundos é a validade da trava; ela é renovada sozinha até
    `liberar` ou o fim do processo."""
    limite = None if esperar_segundos is None else time.monotonic() + max(0, esperar_segundos)
    ordem = contas.ordem(conta, alternativa)
    senhas: dict[str, int] = {}
    avisado = None
    try:
        while True:
            conn = _conectar()
            try:
                agora = _agora()
                if senhas:
                    conn.execute(f"UPDATE stokki_fila SET visto_em = ? WHERE id IN ({','.join('?' * len(senhas))})",
                                 (agora.strftime(_FMT), *senhas.values()))
                conn.execute("DELETE FROM stokki_fila WHERE visto_em < ?",
                             ((agora - timedelta(seconds=SENHA_VENCE_SEGUNDOS)).strftime(_FMT),))
                conn.commit()
                ocupante = None
                for c in ordem:
                    ocupante_c = em_uso(conn, ignorar_dono=dono, conta=c)
                    if not ocupante_c:
                        primeiro = conn.execute("SELECT id, dono FROM stokki_fila WHERE conta = ? ORDER BY id LIMIT 1",
                                                (c,)).fetchone()
                        if primeiro and primeiro["id"] != senhas.get(c) and primeiro["dono"] != dono:
                            ocupante_c = f"fila: {primeiro['dono']}"
                    if ocupante_c:
                        ocupante = ocupante or ocupante_c
                        continue
                    conn.execute("""
                        INSERT INTO stokki_sessao_uso (chave, dono, desde, expira_em) VALUES (?, ?, ?, ?)
                        ON CONFLICT(chave) DO UPDATE SET dono = excluded.dono, desde = excluded.desde,
                                                         expira_em = excluded.expira_em
                    """, (c, dono, agora.strftime(_FMT), (agora + timedelta(seconds=ttl_segundos)).strftime(_FMT)))
                    conn.commit()
                    with _TRAVA_LOCAL:
                        _DONOS_DESTE_PROCESSO[dono] = c
                    _renovar_em_segundo_plano(dono, ttl_segundos)
                    if c != conta:
                        logger.info(f"[sessao_uso] Conta '{conta}' em uso por '{ocupante}' -- '{dono}' usa a conta '{c}'.")
                    elif avisado:
                        logger.info(f"[sessao_uso] Stokki liberada por '{avisado}' -- '{dono}' assumiu.")
                    return c
                if esperar_segundos is not None and esperar_segundos <= 0:
                    return False
                for c in ordem:
                    if c not in senhas:
                        cur = conn.execute("INSERT INTO stokki_fila (conta, dono, entrou_em, visto_em) VALUES (?, ?, ?, ?)",
                                           (c, dono, agora.strftime(_FMT), agora.strftime(_FMT)))
                        senhas[c] = cur.lastrowid
                conn.commit()
            finally:
                conn.close()
            if ocupante != avisado:
                logger.info(f"[sessao_uso] Stokki em uso por '{ocupante}' -- '{dono}' na fila.")
                avisado = ocupante
            if limite is not None and time.monotonic() >= limite:
                return False
            espera = intervalo if limite is None else min(intervalo, max(0.0, limite - time.monotonic()))
            time.sleep(espera or 0.1)
    finally:
        if senhas:
            try:
                conn = _conectar()
                conn.execute(f"DELETE FROM stokki_fila WHERE id IN ({','.join('?' * len(senhas))})",
                             tuple(senhas.values()))
                conn.commit()
                conn.close()
            except Exception as e:
                logger.warning(f"[sessao_uso] Senha de '{dono}' não saiu da fila ({e}) -- vence sozinha em 1 min.")


def renovar(dono: str, ttl_segundos: int = 900) -> None:
    conn = _conectar()
    try:
        conn.execute("UPDATE stokki_sessao_uso SET expira_em = ? WHERE dono = ?",
                     ((_agora() + timedelta(seconds=ttl_segundos)).strftime(_FMT), dono))
        conn.commit()
    finally:
        conn.close()


def liberar(dono: str) -> None:
    with _TRAVA_LOCAL:
        _DONOS_DESTE_PROCESSO.pop(dono, None)
    conn = _conectar()
    try:
        conn.execute("DELETE FROM stokki_sessao_uso WHERE dono = ?", (dono,))
        conn.commit()
    finally:
        conn.close()


@atexit.register
def _liberar_tudo_ao_sair() -> None:
    """Processo que termina sem liberar não segura a conta até o ttl."""
    for dono in list(_DONOS_DESTE_PROCESSO):
        try:
            liberar(dono)
        except Exception:
            pass


def conta_do_processo() -> str | None:
    """Conta ADMIN (principal/reserva) da trava que ESTE processo segura
    (a mais recente), ou None -- é a conta que a StokkiSession e o portal
    usam. A trava da provider não conta: aquela conta não entra na área
    admin."""
    admin = [c for c in _DONOS_DESTE_PROCESSO.values() if c != contas.PROVIDER]
    return admin[-1] if admin else None


def _dono_do_processo(sufixo: str = "") -> str:
    nome = Path(sys.argv[0]).stem if sys.argv and sys.argv[0] else "python"
    return f"{nome}{sufixo}:{os.getpid()}"


def garantir_vez(preferida: str, esperar_segundos: int | None, alternativa: bool = True) -> str | None:
    """Pra quem vai usar a Stokki sem ter pegado trava antes (StokkiSession
    dos scripts, documentos, Estação): entra na fila e, quando chega a
    vez, fica com a conta até o processo terminar (renovação e liberação
    automáticas). Processo que já segura uma das contas possíveis usa essa
    -- nunca espera por si mesmo. Devolve a conta, ou None se a espera
    acabou sem chegar a vez (quem chama desiste, sem logar por cima)."""
    ordem = contas.ordem(preferida, alternativa)
    proprias = set(_DONOS_DESTE_PROCESSO.values())
    for c in ordem:
        if c in proprias:
            return c
    conta = adquirir(_dono_do_processo(f"/{preferida}"), ttl_segundos=600, esperar_segundos=esperar_segundos,
                     conta=preferida, alternativa=alternativa)
    return conta or None


def ocupante_externo(conta: str = contas.PRINCIPAL) -> str | None:
    """Dono da trava da conta (a linha, válida) quando ela é de OUTRO
    processo."""
    conn = _conectar()
    try:
        dono = _dono_valido(conn, conta)
    finally:
        conn.close()
    if not dono or dono in _DONOS_DESTE_PROCESSO:
        return None
    return dono


def aguardar_vez_para_login(esperar_segundos: int, intervalo: float = 10.0,
                            conta: str = contas.PRINCIPAL) -> str | None:
    """Espera enquanto outro processo segura a trava da conta, SEM entrar
    na fila nem pegar a conta (uso antigo; quem vai usar a Stokki deve
    chamar adquirir/garantir_vez). Devolve None quando está livre, ou o
    dono que ainda segura a trava ao fim da espera."""
    _, ocupante = escolher_conta_login(conta, esperar_segundos, intervalo, alternativa=False)
    return ocupante


def escolher_conta_login(preferida: str, esperar_segundos: int, intervalo: float = 10.0,
                         alternativa: bool = True) -> tuple[str, str | None]:
    """Conta livre pra um login que NÃO fica na fila (telas do painel,
    threads do waitress: espera 0). Devolve (conta, None) ou, ocupadas
    todas, (preferida, ocupante)."""
    ordem = contas.ordem(preferida, alternativa)
    proprias = set(_DONOS_DESTE_PROCESSO.values())
    for c in ordem:
        if c in proprias:
            return c, None
    limite = time.monotonic() + max(0, esperar_segundos)
    avisado = None
    while True:
        ocupante = None
        for c in ordem:
            ocupante_c = ocupante_externo(c)
            if not ocupante_c:
                if c != preferida:
                    logger.info(f"[sessao_uso] Conta '{preferida}' em uso por '{ocupante}' -- login pela conta '{c}'.")
                elif avisado:
                    logger.info(f"[sessao_uso] Stokki liberada por '{avisado}' -- seguindo com o login.")
                return c, None
            ocupante = ocupante or ocupante_c
        if ocupante != avisado:
            logger.info(f"[sessao_uso] Stokki em uso por '{ocupante}' -- login aguardando a vez.")
            avisado = ocupante
        if time.monotonic() >= limite:
            return preferida, ocupante
        time.sleep(min(intervalo, max(0.0, limite - time.monotonic())) or 0.1)


def vez_da_provider_para_login() -> None:
    """Logins Playwright diretos na conta provider sem trava própria
    (documentos, Estação de Impressão): entra na fila da provider e fica
    com ela até o processo terminar -- na thread principal. Em thread do
    painel não entra na fila. Sem chegar a vez em 2h, levanta
    RuntimeError: quem chama desiste em vez de derrubar a expedição."""
    if threading.current_thread() is not threading.main_thread():
        return
    if not garantir_vez(contas.PROVIDER, ESPERA_MAXIMA_SEGUNDOS, alternativa=False):
        raise RuntimeError(f"Conta provider da Stokki ocupada por '{em_uso(conta=contas.PROVIDER)}' há mais de "
                           f"{ESPERA_MAXIMA_SEGUNDOS // 3600}h -- login desistido pra não derrubar quem está usando.")


def na_vez_da_provider(func):
    """Decorador pras funções da Estação de Impressão (login Playwright
    direto na conta provider, chamadas de dentro do pipeline/executar_tudo):
    entra na fila da provider, roda e LIBERA ao terminar -- segurar até o
    fim do processo deixaria a expedição esperando o pipeline inteiro. Em
    thread do painel, ou se o processo já segura a provider, só roda."""
    import functools

    @functools.wraps(func)
    def envolvida(*args, **kwargs):
        if (threading.current_thread() is not threading.main_thread()
                or contas.PROVIDER in _DONOS_DESTE_PROCESSO.values()):
            return func(*args, **kwargs)
        dono = _dono_do_processo(f"/{func.__name__}")
        try:
            conseguiu = adquirir(dono, ttl_segundos=600, esperar_segundos=ESPERA_MAXIMA_SEGUNDOS, conta=contas.PROVIDER)
        except sqlite3.Error as e:
            logger.warning(f"[sessao_uso] Fila da Stokki indisponível ({e}) -- seguindo sem esperar.")
            return func(*args, **kwargs)
        if not conseguiu:
            raise RuntimeError(f"Conta provider da Stokki ocupada por '{em_uso(conta=contas.PROVIDER)}' há mais de "
                               f"{ESPERA_MAXIMA_SEGUNDOS // 3600}h -- {func.__name__} desistiu pra não derrubar quem está usando.")
        try:
            return func(*args, **kwargs)
        finally:
            liberar(dono)
    return envolvida
