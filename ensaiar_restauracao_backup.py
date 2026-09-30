# -*- coding: utf-8 -*-
"""
ensaiar_restauracao_backup.py

Ensaio de restauração do backup do banco operacional (Etapa 0 do
DOC_EXECUCAO_CLAUDE_SAIDA_VUUPT.md). Backup que nunca foi restaurado não
é backup -- este script prova, com cronômetro, que dá pra ter o banco de
volta a partir do GCS:

  1. acha o snapshot mais recente de dados.db no bucket (horário ou diário);
  2. baixa num diretório temporário (mede o tempo);
  3. roda PRAGMA integrity_check COMPLETO na cópia (mede o tempo);
  4. compara a contagem de linhas de TODAS as tabelas com a produção
     (lida em modo somente leitura): tabela faltando, ou a cópia com MENOS
     de 90% das linhas de uma tabela crítica, reprova o ensaio;
  5. roda as consultas do núcleo que os serviços usam na partida
     (garantir_esquema numa cópia, rotas do dia anterior).

Nunca escreve no banco de produção nem no bucket. A cópia baixada é
apagada no fim (use --manter pra inspecionar).

COMO USAR (VPS, sempre como www-data):
    sudo -u www-data venv/bin/python ensaiar_restauracao_backup.py
    sudo -u www-data venv/bin/python ensaiar_restauracao_backup.py --enviar-email
    sudo -u www-data venv/bin/python ensaiar_restauracao_backup.py --arquivo backups/dados_db/dados_20260915_030003.db

Saída 0 = ensaio aprovado; 1 = reprovado (o timer semanal dispara o
alerta de falha pelo OnFailure do systemd).
"""
import argparse
import logging
import shutil
import sqlite3
import sys
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path

from backup_dados_gcs import PASTA_DADOS, RAIZ, _cliente_gcs, carregar_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("ensaio_restauracao")

PREFIXOS_SNAPSHOT = ["backups/dados_db_horario/", "backups/dados_db/"]

# Tabelas cuja perda seria sentida na operação no mesmo dia. Se a cópia
# tiver bem menos linhas que a produção, o backup está velho ou quebrado.
TABELAS_CRITICAS = [
    "nucleo_rotas", "nucleo_paradas", "nucleo_pedidos", "nucleo_eventos",
    "nucleo_comprovantes", "nucleo_pedagios", "motoristas",
    "rascunhos_rota", "rascunhos_parada", "clientes", "documentos_processados",
]
FRACAO_MINIMA = 0.90
IDADE_MAXIMA_HORAS = 26   # diário das 03h + folga; o horário deixa isso bem menor


def snapshot_mais_recente(config: dict, arquivo: str | None):
    cliente = _cliente_gcs(config)
    bucket_name = config.get("gcs", {}).get("bucket_name", "")
    if arquivo:
        blob = cliente.bucket(bucket_name).get_blob(arquivo)
        if blob is None:
            raise FileNotFoundError(f"gs://{bucket_name}/{arquivo} não existe.")
        return blob
    candidatos = []
    for prefixo in PREFIXOS_SNAPSHOT:
        candidatos.extend(b for b in cliente.list_blobs(bucket_name, prefix=prefixo) if b.name.endswith(".db"))
    if not candidatos:
        raise FileNotFoundError(f"Nenhum snapshot em {PREFIXOS_SNAPSHOT} no bucket {bucket_name}.")
    return max(candidatos, key=lambda b: b.time_created)


def contar_tabelas(caminho: Path) -> dict[str, int]:
    con = sqlite3.connect(f"{caminho.resolve().as_uri()}?mode=ro", uri=True)
    try:
        nomes = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return {n: con.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0] for n in nomes}
    finally:
        con.close()


def comparar(producao: dict[str, int], copia: dict[str, int]) -> tuple[list[str], list[str]]:
    """Devolve (problemas, avisos). Problema reprova o ensaio."""
    problemas, avisos = [], []
    for tabela, n_prod in producao.items():
        if tabela not in copia:
            (problemas if tabela in TABELAS_CRITICAS else avisos).append(
                f"tabela {tabela} ({n_prod} linhas na produção) não existe na cópia")
            continue
        n_copia = copia[tabela]
        if tabela in TABELAS_CRITICAS and n_prod > 0 and n_copia < n_prod * FRACAO_MINIMA:
            problemas.append(f"{tabela}: cópia {n_copia} × produção {n_prod} (< {FRACAO_MINIMA:.0%})")
    for tabela in TABELAS_CRITICAS:
        if tabela not in producao:
            avisos.append(f"tabela crítica {tabela} não existe nem na produção")
    return problemas, avisos


def consultas_de_partida(caminho: Path) -> str:
    """O que os serviços fazem ao subir: garantir_esquema (numa CÓPIA da
    cópia, pra não mexer no arquivo baixado) e a leitura das rotas do dia
    anterior pelo núcleo."""
    sys.path.insert(0, str(RAIZ))
    from nucleo import banco, rotas

    segunda = caminho.with_name(caminho.stem + "_partida.db")
    shutil.copyfile(caminho, segunda)
    conn = banco.conectar(segunda)
    try:
        ontem = (date.today() - timedelta(days=1)).isoformat()
        n_rotas = len(rotas.listar_rotas_dia(ontem, com_paradas=True, conn=conn))
        return f"garantir_esquema OK; {n_rotas} rota(s) de {ontem} lidas pelo núcleo"
    finally:
        conn.close()
        segunda.unlink(missing_ok=True)


def enviar_relatorio(config: dict, aprovado: bool, linhas: list[str]):
    from email_utils import COR_ACENTO, COR_ERRO, envelope_html, enviar_email

    destino = (config.get("notificacao_execucao", {}) or {}).get("destinatario") or "hugo@freshlogbr.com"
    titulo = "Ensaio de restauração do backup: APROVADO" if aprovado else "Ensaio de restauração do backup: REPROVADO"
    corpo = "<h2 style='margin:0 0 12px'>" + titulo + "</h2><ul>" + "".join(f"<li>{l}</li>" for l in linhas) + "</ul>"
    enviar_email([destino], titulo, envelope_html(corpo, cor_acento=COR_ACENTO if aprovado else COR_ERRO),
                 config.get("email", {}))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Ensaio de restauração do backup do dados.db (somente leitura).")
    parser.add_argument("--arquivo", help="caminho do snapshot no bucket (padrão: o mais recente)")
    parser.add_argument("--manter", action="store_true", help="não apaga a cópia baixada")
    parser.add_argument("--enviar-email", action="store_true", help="manda o resultado por e-mail")
    args = parser.parse_args(argv)

    config = carregar_config()
    linhas: list[str] = []
    inicio_total = time.monotonic()

    blob = snapshot_mais_recente(config, args.arquivo)
    idade_h = (time.time() - blob.time_created.timestamp()) / 3600
    linhas.append(f"Snapshot: gs://{blob.bucket.name}/{blob.name} ({blob.size / 1e6:.1f} MB, gerado há {idade_h:.1f} h)")

    pasta = Path(tempfile.mkdtemp(prefix="ensaio_restauracao_", dir=PASTA_DADOS if PASTA_DADOS.exists() else None))
    destino = pasta / Path(blob.name).name
    problemas: list[str] = []
    avisos: list[str] = []
    try:
        t0 = time.monotonic()
        blob.download_to_filename(str(destino))
        t_download = time.monotonic() - t0
        linhas.append(f"Download: {t_download:.1f} s")

        t0 = time.monotonic()
        con = sqlite3.connect(f"{destino.resolve().as_uri()}?mode=ro", uri=True)
        try:
            resultado = [r[0] for r in con.execute("PRAGMA integrity_check")]
        finally:
            con.close()
        t_integridade = time.monotonic() - t0
        if resultado != ["ok"]:
            problemas.append("integrity_check: " + "; ".join(resultado[:10]))
        linhas.append(f"integrity_check: {'ok' if resultado == ['ok'] else 'FALHOU'} ({t_integridade:.1f} s)")

        producao = contar_tabelas(PASTA_DADOS / "dados.db")
        copia = contar_tabelas(destino)
        p, a = comparar(producao, copia)
        problemas += p
        avisos += a
        criticas = ", ".join(f"{t} {copia.get(t, '—')}/{producao.get(t, '—')}" for t in TABELAS_CRITICAS if t in producao)
        linhas.append(f"Tabelas: {len(copia)} na cópia × {len(producao)} na produção. Críticas (cópia/produção): {criticas}")

        try:
            linhas.append("Partida: " + consultas_de_partida(destino))
        except Exception as exc:  # noqa: BLE001 -- o ensaio reporta, não explode
            problemas.append(f"consultas de partida falharam: {exc}")

        if idade_h > IDADE_MAXIMA_HORAS:
            problemas.append(f"snapshot mais recente tem {idade_h:.1f} h (máximo {IDADE_MAXIMA_HORAS} h)")
    finally:
        if args.manter:
            linhas.append(f"Cópia mantida em {destino}")
        else:
            shutil.rmtree(pasta, ignore_errors=True)

    tempo_total = time.monotonic() - inicio_total
    linhas.append(f"Tempo total até o banco utilizável: {tempo_total:.1f} s")
    aprovado = not problemas
    for l in linhas:
        logger.info(l)
    for a in avisos:
        logger.warning("Aviso: " + a)
    for p in problemas:
        logger.error("PROBLEMA: " + p)
    logger.info("=== ENSAIO %s ===", "APROVADO" if aprovado else "REPROVADO")

    if args.enviar_email:
        enviar_relatorio(config, aprovado, linhas + [f"Aviso: {a}" for a in avisos] + [f"PROBLEMA: {p}" for p in problemas])
    return 0 if aprovado else 1


if __name__ == "__main__":
    sys.exit(main())
