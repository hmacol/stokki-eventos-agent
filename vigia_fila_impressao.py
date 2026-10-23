# -*- coding: utf-8 -*-
"""
vigia_fila_impressao.py

Vigia da Estação de Impressão da Stokki (pedido do Hugo, 22/10/2026: "nunca
mais deixar passar pedido faturado"). SÓ LÊ: não clica em nada. Olha o
resultado, não o robô -- se o pedido ficou parado, avisa, seja qual for o
motivo (robô quebrado, timer parado, sessão caída, tela mudada).

Regras (decididas pelo Hugo em 22/10):
  - aba "Faturados para imprimir": faturado há mais de 2 h -> alerta
  - aba "Aguardando Faturamento" (Em espera antes do faturamento): parado
    há mais de 24 h -> alerta. "Desde" = "Solicitado em"; sem ele, a data
    do pedido.
  - 1 alerta por pedido por dia (tabela vigia_impressao_alertas).
  - destino: grupo ALERTAS FRESH (notificar_whatsapp) + e-mail de
    notificacao_execucao.destinatario.
  - não conseguiu ler a Stokki -> o script falha e o OnFailure do systemd
    (alertar_falha_job.py) avisa por e-mail e no grupo.

config.yaml (opcional, tudo com padrão):
    vigia_impressao:
      ativo: true
      horas_faturado: 2
      horas_em_espera: 24
      ignorar_codigos: ["PS-35908"]   # pedido sabidamente abandonado

Pedidos ignorados também ficam na tabela vigia_impressao_ignorados (sem
mexer no config; vale a união das duas listas):
  venv/bin/python vigia_fila_impressao.py --ignorar PS-35908 PS-35937 --motivo "abandonado"
  venv/bin/python vigia_fila_impressao.py --designorar PS-35908
  venv/bin/python vigia_fila_impressao.py --listar-ignorados

Agendamento (VPS): stokki-vigia-fila-impressao.timer -- seg-sex 08h35-16h35
de hora em hora + 18h35 e 22h35 todo dia (logo depois de cada rodada da
Estação).

Execute:
  py -3.11 vigia_fila_impressao.py --modo-teste   (lê a Stokki, não avisa nem marca)
"""
import argparse
import html
import logging
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import yaml

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])
logger = logging.getLogger("vigia_fila_impressao")

RAIZ = Path(__file__).resolve().parent
DB_PATH = RAIZ / "dados" / "dados.db"

HORAS_FATURADO_PADRAO = 2
HORAS_EM_ESPERA_PADRAO = 24

FATURADO = "faturado"
EM_ESPERA = "em_espera"
_ABA_DO_TIPO = {FATURADO: "billed", EM_ESPERA: "awaiting_billing"}

_SCHEMA = """CREATE TABLE IF NOT EXISTS vigia_impressao_alertas (
    dia        TEXT NOT NULL,
    tipo       TEXT NOT NULL,
    codigo     TEXT NOT NULL,
    criado_em  TEXT NOT NULL,
    PRIMARY KEY (dia, tipo, codigo)
)"""

_SCHEMA_IGNORADOS = """CREATE TABLE IF NOT EXISTS vigia_impressao_ignorados (
    codigo     TEXT PRIMARY KEY,
    motivo     TEXT,
    criado_em  TEXT NOT NULL
)"""


def _config() -> dict:
    with open(RAIZ / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _codigo(pedido: dict) -> str:
    return (pedido.get("codigo_ps") or "").lstrip("#")


def _normalizar(codigo: str) -> str:
    """'#ps-35908' / '35908' -> 'PS-35908'."""
    c = str(codigo).strip().lstrip("#").upper()
    return c if c.startswith("PS-") else f"PS-{c}"


def ignorados(conn) -> dict:
    """{codigo: motivo} da tabela."""
    conn.execute(_SCHEMA_IGNORADOS)
    return dict(conn.execute("SELECT codigo, motivo FROM vigia_impressao_ignorados ORDER BY codigo"))


def ignorar(conn, codigos: list, motivo: str, agora: datetime) -> None:
    conn.execute(_SCHEMA_IGNORADOS)
    conn.executemany(
        "INSERT INTO vigia_impressao_ignorados (codigo, motivo, criado_em) VALUES (?, ?, ?) "
        "ON CONFLICT(codigo) DO UPDATE SET motivo = excluded.motivo",
        [(_normalizar(c), motivo, agora.isoformat(timespec="seconds")) for c in codigos])
    conn.commit()


def designorar(conn, codigos: list) -> None:
    conn.execute(_SCHEMA_IGNORADOS)
    conn.executemany("DELETE FROM vigia_impressao_ignorados WHERE codigo = ?",
                     [(_normalizar(c),) for c in codigos])
    conn.commit()


def atrasados(pedidos: list, agora: datetime, horas: float, ignorar: set) -> list:
    """Pedidos parados há mais de `horas`, do mais antigo pro mais novo.
    Sem data legível o pedido entra (melhor um alerta a mais que um furo)."""
    limite = agora - timedelta(hours=horas)
    saida = [p for p in pedidos
             if _codigo(p) not in ignorar and (p.get("desde") is None or p["desde"] <= limite)]
    return sorted(saida, key=lambda p: p.get("desde") or datetime.min)


def _horas_parado(pedido: dict, agora: datetime) -> str:
    if not pedido.get("desde"):
        return "?"
    horas = (agora - pedido["desde"]).total_seconds() / 3600
    return f"{horas:.0f}h" if horas < 48 else f"{horas / 24:.0f} dias"


def separar_novos(conn, dia: str, tipo: str, pedidos: list) -> list:
    """Só os que ainda não foram avisados hoje."""
    conn.execute(_SCHEMA)
    ja = {r[0] for r in conn.execute(
        "SELECT codigo FROM vigia_impressao_alertas WHERE dia = ? AND tipo = ?", (dia, tipo))}
    return [p for p in pedidos if _codigo(p) not in ja]


def marcar_avisados(conn, dia: str, tipo: str, pedidos: list, agora: datetime) -> None:
    conn.execute(_SCHEMA)
    conn.executemany(
        "INSERT OR IGNORE INTO vigia_impressao_alertas (dia, tipo, codigo, criado_em) VALUES (?, ?, ?, ?)",
        [(dia, tipo, _codigo(p), agora.isoformat(timespec="seconds")) for p in pedidos])
    conn.commit()


def texto_whatsapp(faturados: list, em_espera: list, horas_faturado: float, horas_em_espera: float,
                   limite: int) -> str:
    """Mensagem curta pro grupo (cabe em `limite` caracteres, sem cortar
    no meio): tira códigos do fim e põe "e mais N" até caber."""
    def montar(n_codigos: int) -> str:
        linhas = ["⚠️ *Estação de Impressão*"]
        for itens, frase in ((faturados, f"faturado há +{horas_faturado:g}h sem imprimir"),
                             (em_espera, f"em espera há +{horas_em_espera:g}h sem faturar")):
            if not itens:
                continue
            codigos = [_codigo(p) for p in itens[:n_codigos]]
            resto = len(itens) - len(codigos)
            lista = ", ".join(codigos) + (f" e mais {resto}" if resto else "")
            linhas.append(f"• {len(itens)} {frase}: {lista}")
        linhas.append("Detalhes no e-mail.")
        return "\n".join(linhas)

    n = max(len(faturados), len(em_espera))
    texto = montar(n)
    while len(texto) > limite and n > 0:
        n -= 1
        texto = montar(n)
    return texto


def corpo_email(faturados: list, em_espera: list, horas_faturado: float, horas_em_espera: float,
                agora: datetime) -> str:
    def tabela(titulo: str, itens: list, rotulo_desde: str) -> str:
        if not itens:
            return ""
        corpo = []
        for p in itens:
            desde = f"{p['desde']:%d/%m %H:%M}" if p.get("desde") else "?"
            corpo.append(
                "<tr>"
                f"<td style='padding:4px 8px'>{html.escape(_codigo(p))}</td>"
                f"<td style='padding:4px 8px'>{html.escape(p.get('cliente') or '')}</td>"
                f"<td style='padding:4px 8px'>{html.escape(p.get('transportadora') or '')}</td>"
                f"<td style='padding:4px 8px'>{desde}</td>"
                f"<td style='padding:4px 8px'>{_horas_parado(p, agora)}</td>"
                "</tr>")
        return (f"<h3 style='margin:16px 0 6px'>{html.escape(titulo)} ({len(itens)})</h3>"
                "<table style='border-collapse:collapse;font-size:13px'>"
                "<tr style='text-align:left'><th style='padding:4px 8px'>Pedido</th>"
                "<th style='padding:4px 8px'>Depositante</th><th style='padding:4px 8px'>Transportadora</th>"
                f"<th style='padding:4px 8px'>{rotulo_desde}</th><th style='padding:4px 8px'>Parado</th></tr>"
                + "".join(corpo) + "</table>")

    return (
        f"<p>Vigia da Estação de Impressão, {agora:%d/%m %H:%M}. Pedidos parados na Stokki "
        f"(cada um é avisado uma vez por dia):</p>"
        + tabela(f"Faturados há mais de {horas_faturado:g}h sem imprimir", faturados, "Faturado em")
        + tabela(f"Em espera há mais de {horas_em_espera:g}h sem faturar", em_espera, "Em espera desde")
        + "<p>Faturado parado: conferir se o robô rodou (e-mail da Impressão) ou clicar em "
          "\"Iniciar Impressão\" na Estação. Em espera parado: conferência aberta ou NF que "
          "não chegou -- ver com o galpão / o embarcador.</p>"
    )


def main(modo_teste: bool = False, agora: datetime | None = None) -> int:
    config = _config()
    cfg = config.get("vigia_impressao") or {}
    if not cfg.get("ativo", True):
        logger.info("vigia_impressao.ativo=false -- nada feito.")
        return 0
    horas_faturado = float(cfg.get("horas_faturado", HORAS_FATURADO_PADRAO))
    horas_em_espera = float(cfg.get("horas_em_espera", HORAS_EM_ESPERA_PADRAO))
    lista_ignorar = {_normalizar(c) for c in (cfg.get("ignorar_codigos") or [])}
    conn_ign = sqlite3.connect(DB_PATH, timeout=30)
    try:
        lista_ignorar |= set(ignorados(conn_ign))
    finally:
        conn_ign.close()
    if lista_ignorar:
        logger.info(f"Ignorados de propósito: {sorted(lista_ignorar)}")

    from stokki.estacao_impressao import ler_filas
    filas = ler_filas(config)  # falha aqui = job falha = alerta-falha do systemd
    agora = agora or datetime.now()
    dia = agora.date().isoformat()
    logger.info(f"Fila lida: {len(filas['billed'])} faturado(s), "
                f"{len(filas['awaiting_billing'])} aguardando faturamento.")

    parados = {
        FATURADO: atrasados(filas[_ABA_DO_TIPO[FATURADO]], agora, horas_faturado, lista_ignorar),
        EM_ESPERA: atrasados(filas[_ABA_DO_TIPO[EM_ESPERA]], agora, horas_em_espera, lista_ignorar),
    }
    for tipo, itens in parados.items():
        logger.info(f"Parados ({tipo}): {[_codigo(p) for p in itens]}")

    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        novos = {tipo: separar_novos(conn, dia, tipo, itens) for tipo, itens in parados.items()}
        if not any(novos.values()):
            logger.info("Nada novo pra avisar hoje.")
            return 0

        import notificar_whatsapp
        from email_utils import COR_DESTAQUE, envelope_html, enviar_email
        texto = texto_whatsapp(novos[FATURADO], novos[EM_ESPERA], horas_faturado, horas_em_espera,
                               notificar_whatsapp.MAX_MENSAGEM)
        n = len(novos[FATURADO]) + len(novos[EM_ESPERA])
        assunto = f"[ALERTA] Estação de Impressão: {n} pedido(s) parado(s) na Stokki ({agora:%d/%m %H:%M})"
        corpo = corpo_email(novos[FATURADO], novos[EM_ESPERA], horas_faturado, horas_em_espera, agora)
        if modo_teste:
            logger.info(f"[MODO TESTE] nada enviado nem marcado. Assunto: {assunto}\nWhatsApp:\n{texto}")
            return 0

        destino = (config.get("notificacao_execucao", {}) or {}).get("destinatario") or "hugo@freshlogbr.com"
        ok_email = enviar_email([destino], assunto, envelope_html(corpo, cor_acento=COR_DESTAQUE),
                                config.get("email", {}))
        situacao = notificar_whatsapp.despachar(config, "vigia_fila_impressao", "fila_impressao", texto)
        logger.info(f"Aviso: e-mail {'enviado' if ok_email else 'FALHOU'} pra {destino}; WhatsApp {situacao}.")
        if ok_email or situacao == "enviado":
            for tipo, itens in novos.items():
                marcar_avisados(conn, dia, tipo, itens, agora)
        return 0 if ok_email or situacao == "enviado" else 1
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Vigia da fila da Estação de Impressão (só leitura)")
    parser.add_argument("--modo-teste", action="store_true", help="lê a Stokki mas não avisa nem marca")
    parser.add_argument("--ignorar", nargs="+", metavar="PS-X", help="não avisar mais estes pedidos")
    parser.add_argument("--motivo", default="", help="por que ignorar (vai pra tabela)")
    parser.add_argument("--designorar", nargs="+", metavar="PS-X", help="voltar a avisar estes pedidos")
    parser.add_argument("--listar-ignorados", action="store_true")
    args = parser.parse_args()
    if args.ignorar or args.designorar or args.listar_ignorados:
        with sqlite3.connect(DB_PATH, timeout=30) as conn:
            if args.ignorar:
                ignorar(conn, args.ignorar, args.motivo, datetime.now())
            if args.designorar:
                designorar(conn, args.designorar)
            for codigo, motivo in ignorados(conn).items():
                print(f"{codigo}\t{motivo or ''}")
        sys.exit(0)
    sys.exit(main(modo_teste=args.modo_teste))
