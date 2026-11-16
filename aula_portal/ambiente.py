# -*- coding: utf-8 -*-
"""
Ambiente de gravacao da aula: o portal real (deste checkout) com config e
banco ficticios, sem nenhuma credencial e com proxy morto pra qualquer
chamada de saida. Nada chega a Stokki, Vuupt, SMTP, WhatsApp ou Claude.

Travas: so escreve config.yaml/dados.db que ele mesmo criou (marca no
arquivo). Um config.yaml ou dados.db de verdade faz AmbienteRecusado.
"""
import os
import secrets
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from aula_portal import RAIZ, dados_teste as dt

ESTADOS = ("primeiro_acesso", "padrao", "status")
PORTA_PADRAO = 8097
MARCA_CONFIG = "# aula_portal: config de gravacao (ficticio, sem segredos)"
TABELA_MARCA = "aula_gravacao"
PROXY_MORTO = "http://127.0.0.1:9"
# Variaveis de ambiente que nunca passam pro portal da gravacao.
_ENV_SENSIVEL = ("KEY", "TOKEN", "SECRET", "PASSWORD", "SENHA")


class AmbienteRecusado(RuntimeError):
    pass


# ── Config ─────────────────────────────────────────────────────────────────────

def escrever_config(raiz: Path) -> Path:
    """config.yaml minimo: so o segredo da sessao (aleatorio) e as chaves que
    mudam a tela. Sem email, vuupt_api, stokki, freshhub_api, Claude..."""
    caminho = Path(raiz) / "config.yaml"
    if caminho.exists() and not caminho.read_text(encoding="utf-8").startswith(MARCA_CONFIG):
        raise AmbienteRecusado(f"{caminho} existe e nao e de gravacao: nao sobrescrevo.")
    caminho.write_text(
        f"{MARCA_CONFIG}\n"
        "portal_cliente:\n"
        f"  secret_key: {secrets.token_hex(32)}\n"
        "  alteracao_entrega:\n"
        "    ativo: true\n"
        "  envios:\n"
        "    bloqueio_area_ativo: false\n"
        "    endereco_entrega_ativo: true\n",
        encoding="utf-8")
    return caminho


# ── Banco ──────────────────────────────────────────────────────────────────────

def _esquema_padrao() -> Path | None:
    """dados.db do checkout principal (o desta maquina), so pra copiar o
    ESQUEMA das tabelas que o portal le e nao cria sozinho. Nenhuma linha."""
    try:
        comum = subprocess.run(["git", "-C", str(RAIZ), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                               capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    caminho = Path(comum).parent / "dados" / "dados.db"
    return caminho if caminho.exists() else None


def _copiar_esquema(conn: sqlite3.Connection, origem: Path) -> None:
    fonte = sqlite3.connect(f"file:{origem.as_posix()}?mode=ro", uri=True)
    try:
        sqls = [r[0] for r in fonte.execute(
            "SELECT sql FROM sqlite_master WHERE type IN ('table', 'index') AND sql IS NOT NULL "
            "AND name NOT LIKE 'sqlite_%' ORDER BY type DESC")]
    finally:
        fonte.close()
    for sql in sqls:
        try:
            conn.execute(sql)
        except sqlite3.Error:
            pass  # virtual/FTS ou indice de tabela que nao veio: o modulo cria se precisar


@contextmanager
def _modulos_no_banco(db: Path):
    """Aponta os modulos do portal pro banco da gravacao enquanto semeia."""
    import auth_cliente as auth
    import envio_pedidos as envios
    import preferencias_notificacao as prefs
    antigos = [(m, m.DB_PATH) for m in (auth, envios, prefs)]
    for m, _ in antigos:
        m.DB_PATH = db
    try:
        yield
    finally:
        for m, caminho in antigos:
            m.DB_PATH = caminho


_INTERNO = ("CREATE TABLE IF NOT EXISTS interno (cnpj_embarcador TEXT PRIMARY KEY, habilidade TEXT, "
            "nome_remetente TEXT, apelido TEXT, fator_ponderado REAL, observacoes TEXT, criado_em TEXT, "
            "atualizado_em TEXT, email TEXT, notificar_email INTEGER, sender_id INTEGER, latitude REAL, "
            "longitude REAL, endereco_hash TEXT, stkkc_id INTEGER)")

# (nf, destinatario, doc, status, codigo_pedido, erro)
_ENVIOS = [
    ("5150", "Cantina Nona Rosa", "22333444000155", "CRIADO", "PS-90109", None),
    ("5151", "Doceria Açúcar Fino", "33444555000166", "CRIADO", "PS-90110", None),
    ("5152", "Armazém do Bairro", "55666777000144", "ERRO", None,
     "A Stokki recusou o pedido: produto MX-114 não cadastrado. Fale com a Fresh Log ou reenvie."),
    ("5153", "Pizzaria Forno a Lenha", "66777888000133", "EM_ESPERA_CLIENTE", "PS-90112", None),
]
# so no estado "status" (video Status do pedido): um envio em cada status que falta
_ENVIOS_STATUS = [
    ("5154", "Mercadinho Estrela", "12121212000112", "NA_FILA", None, None),
    ("5155", "Quitanda do Bairro", "23232323000123", "ENVIANDO", None, None),
    ("5156", "Restaurante Sabor da Vila", "11222333000181", "DUPLICADO", "PS-90102", None),
    ("5157", "Café Central", "34343434000134", "CANCELANDO", "PS-90106", None),
    ("5158", "Bistrô Jardim", "77888999000163", "CANCELADO", None, None),
    ("5159", "Empório Boa Mesa", "45454545000145", "RETOMANDO", "PS-90101", None),
    ("5160", "Sítio Boa Vista", "56565656000156", "AGUARDANDO_LIBERACAO", None, None),
]


def semear_banco(raiz: Path, estado: str, esquema: Path | None | str = "padrao") -> Path:
    """Apaga e recria raiz/dados/dados.db so com o cliente ficticio. `estado`:
    primeiro_acesso (PIN padrao a trocar + cadastro aberto) ou padrao."""
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
    conn.row_factory = sqlite3.Row
    conn.execute(f"CREATE TABLE {TABELA_MARCA} (criado_em TEXT)")
    conn.execute(f"INSERT INTO {TABELA_MARCA} VALUES (?)", (datetime.now().isoformat(timespec="seconds"),))
    origem = _esquema_padrao() if esquema == "padrao" else esquema
    if origem:
        _copiar_esquema(conn, Path(origem))
    conn.execute(_INTERNO)
    conn.commit()
    conn.close()

    import cadastro_embarcador as cad_emb
    import preferencias_notificacao as prefs
    from nucleo import fluxo_embarcador
    with _modulos_no_banco(db):
        import auth_cliente as auth
        import envio_pedidos as envios
        auth.conectar().close()
        conn = envios.conectar()
    try:
        prefs._garantir_tabela(conn)
        cad_emb.garantir_tabela(conn)
        fluxo_embarcador.garantir_coluna(conn)
        agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("INSERT INTO interno (cnpj_embarcador, habilidade, nome_remetente, apelido, criado_em, "
                     "atualizado_em, email, notificar_email, sender_id, stkkc_id) VALUES (?, 'SECO', ?, ?, ?, ?, ?, 1, ?, ?)",
                     (dt.CNPJ, dt.RAZAO, dt.NOME, agora, agora, dt.EMAIL, dt.SENDER_ID, dt.STKKC_ID))
        conn.commit()
        auth.definir_pin(conn, dt.CNPJ, dt.PIN_GRAVACAO, trocar_pin=(estado == "primeiro_acesso"))
        if estado == "primeiro_acesso":
            cad_emb.registrar(conn, dt.CNPJ, dt.STKKC_ID, cad_emb.PENDENTE, celular="11999990000", email=dt.EMAIL)
        else:
            _semear_envios(conn, pasta, _ENVIOS + (_ENVIOS_STATUS if estado == "status" else []))
            if estado == "status":   # area nao atendida: retido ate a Fresh Log liberar
                conn.execute("UPDATE portal_envios SET bloqueio_motivo = 'sp_nao_atendido', "
                             "destinatario_municipio = 'Bragança Paulista' WHERE status = 'AGUARDANDO_LIBERACAO'")
                conn.commit()
    finally:
        conn.close()
    return db


def _semear_envios(conn: sqlite3.Connection, pasta: Path, envios: list) -> None:
    hoje = datetime.now()
    ontem = (hoje - timedelta(days=1)).replace(hour=15, minute=40, second=0, microsecond=0)
    xml_dir = pasta / "aula_nfe"
    xml_dir.mkdir(exist_ok=True)
    for i, (nf, nome, doc, status, codigo, erro) in enumerate(envios):
        chave = (f"3526110{dt.CNPJ}55001{int(nf):09d}2" + "0" * 44)[:44]
        xml = xml_dir / f"{chave}.xml"
        xml.write_text(f"<nfe-ficticia numero='{nf}'/>", encoding="utf-8")
        criado = (ontem + timedelta(minutes=7 * i)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "INSERT INTO portal_envios (cnpj_embarcador, chave_nfe, numero_nf, serie, emitida_em, destinatario_doc, "
            "destinatario_nome, destinatario_endereco, destinatario_bairro, destinatario_municipio, destinatario_uf, "
            "destinatario_cep, volumes, peso_kg, valor_nf, itens, xml_path, status, tentativas, erro, codigo_pedido, "
            "enviado_por, criado_em, atualizado_em, enviado_stokki_em, criado_stokki_em, origem) "
            "VALUES (?, ?, ?, '1', ?, ?, ?, 'Rua do Exemplo, 100', 'Centro', 'São Paulo', 'SP', '01001000', 3, 12.5, "
            "890.0, 2, ?, ?, ?, ?, ?, 'cliente', ?, ?, ?, ?, 'xml')",
            (dt.CNPJ, chave, nf, criado, doc, nome, str(xml), status, 1 if erro else 0, erro, codigo,
             criado, criado, criado if codigo else None, criado if codigo else None))
    conn.commit()


def pin_confere(db: Path, pin: str) -> bool:
    import auth_cliente as auth
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        auth.autenticar(conn, dt.CNPJ, pin)
        return True
    except auth.AutenticacaoInvalida:
        return False
    finally:
        conn.close()


# ── Portal ─────────────────────────────────────────────────────────────────────

def _porta_livre(porta: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", porta)) != 0


def _env_isolado(raiz: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if not any(p in k.upper() for p in _ENV_SENSIVEL)}
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        env[k] = PROXY_MORTO
    env.update({"NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost", "PORTAL_CLIENTE_DEV": "1",
                "PYTHONIOENCODING": "utf-8",
                "PYTHONPATH": os.pathsep.join((str(raiz), str(Path(raiz) / "portal_cliente")))})
    return env


def subir_portal(raiz: Path, porta: int = PORTA_PADRAO) -> subprocess.Popen:
    """Sobe o portal deste checkout na porta (que precisa estar livre: nunca
    reaproveita nem derruba processo alheio) e espera /saude."""
    raiz = Path(raiz)
    cfg = raiz / "config.yaml"
    if not cfg.exists() or not cfg.read_text(encoding="utf-8").startswith(MARCA_CONFIG):
        raise AmbienteRecusado("config.yaml de gravacao ausente: rode escrever_config antes.")
    if not _porta_livre(porta):
        raise AmbienteRecusado(f"porta {porta} ocupada: escolha outra com --porta.")
    log = open(raiz / "dados" / "aula_portal.log", "w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "-m", "aula_portal.servidor", "--porta", str(porta)],
                            cwd=raiz / "portal_cliente", env=_env_isolado(raiz), stdout=log, stderr=subprocess.STDOUT)
    sem_proxy = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    limite = time.time() + 60
    while time.time() < limite:
        if proc.poll() is not None:
            log.close()
            raise RuntimeError("o portal saiu ao subir:\n" + (raiz / "dados" / "aula_portal.log").read_text(encoding="utf-8")[-2000:])
        try:
            sem_proxy.open(f"http://127.0.0.1:{porta}/saude", timeout=2)
            return proc
        except OSError:
            time.sleep(0.5)
    parar_portal(proc)
    raise RuntimeError("o portal nao respondeu /saude em 60 s")


def parar_portal(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
