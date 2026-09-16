# -*- coding: utf-8 -*-
"""
alertar_falha_job.py

Alerta por e-mail quando um job do systemd falha (Etapa 0 do
DOC_EXECUCAO_CLAUDE_SAIDA_VUUPT.md). Até aqui uma falha de timer só
aparecia se alguém fosse olhar o journal -- o sync do núcleo, o backup ou
a expedição podiam ficar dias parados sem ninguém saber.

Ligado pelo systemd: cada unidade crítica em infra/ tem
    OnFailure=stokki-alerta-falha@%n.service
e o template infra/stokki-alerta-falha@.service chama este script com o
nome da unidade que falhou. Ele junta o resultado (systemctl show) e as
últimas linhas do log DAQUELA execução (journalctl pelo InvocationID) e
manda pro destinatário dos alertas internos.

Anti-enxurrada: job que roda todo minuto (stokki-lancar-lalamove) e quebra
de vez mandaria 60 e-mails por hora. Um alerta por unidade a cada
`janela_min` (padrão 120); as falhas no meio são contadas e aparecem no
próximo e-mail.

config.yaml (opcional):
    alertas_jobs:
      ativo: true
      destinatario: hugo@freshlogbr.com     # padrão: notificacao_execucao.destinatario
      janela_min: 120

Teste manual (não precisa de falha de verdade):
    sudo -u www-data venv/bin/python alertar_falha_job.py stokki-backup-gcs.service --forcar
"""
import argparse
import html
import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import yaml

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])
logger = logging.getLogger("alertar_falha_job")

RAIZ = Path(__file__).resolve().parent
JANELA_PADRAO_MIN = 120
LINHAS_LOG = 60


def _config() -> dict:
    with open(RAIZ / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _arquivo_estado() -> Path:
    base = os.environ.get("STATE_DIRECTORY")   # StateDirectory= do template
    pasta = Path(base.split(":")[0]) if base else RAIZ / "dados"
    return pasta / "alertas_jobs.json"


def _rodar(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        return f"(não foi possível rodar {' '.join(cmd)}: {exc})"


def detalhes_da_unidade(unidade: str) -> dict:
    saida = _rodar(["systemctl", "show", unidade, "--no-pager",
                    "--property=Description,Result,ExecMainStatus,ExecMainExitTimestamp,InvocationID,ActiveState"])
    info = {}
    for linha in saida.splitlines():
        if "=" in linha:
            chave, valor = linha.split("=", 1)
            info[chave] = valor
    return info


def log_da_execucao(unidade: str, invocation_id: str | None) -> str:
    if invocation_id:
        texto = _rodar(["journalctl", f"_SYSTEMD_INVOCATION_ID={invocation_id}", "--no-pager",
                        "-o", "short-iso", "-n", str(LINHAS_LOG)])
        if texto.strip() and "-- No entries --" not in texto:
            return texto
    return _rodar(["journalctl", "-u", unidade, "--no-pager", "-o", "short-iso", "-n", str(LINHAS_LOG)])


def decidir_envio(estado: dict, unidade: str, agora: datetime, janela_min: int) -> tuple[bool, int]:
    """(enviar?, falhas suprimidas desde o último alerta). Atualiza `estado`."""
    registro = estado.get(unidade) or {}
    ultimo = registro.get("ultimo_alerta")
    suprimidos = int(registro.get("suprimidos", 0))
    if ultimo:
        try:
            if agora - datetime.fromisoformat(ultimo) < timedelta(minutes=janela_min):
                estado[unidade] = {"ultimo_alerta": ultimo, "suprimidos": suprimidos + 1}
                return False, suprimidos + 1
        except ValueError:
            pass
    estado[unidade] = {"ultimo_alerta": agora.isoformat(timespec="seconds"), "suprimidos": 0}
    return True, suprimidos


def montar_corpo(unidade: str, info: dict, log: str, suprimidos: int) -> str:
    linhas = [
        ("Unidade", unidade),
        ("Descrição", info.get("Description", "")),
        ("Resultado", f"{info.get('Result', '?')} (código de saída {info.get('ExecMainStatus', '?')})"),
        ("Quando", info.get("ExecMainExitTimestamp", "")),
    ]
    if suprimidos:
        linhas.append(("Falhas anteriores sem alerta", f"{suprimidos} (dentro da janela anti-enxurrada)"))
    tabela = "".join(
        f"<tr><td style='padding:6px 12px;font-weight:600'>{html.escape(k)}</td>"
        f"<td style='padding:6px 12px'>{html.escape(str(v))}</td></tr>" for k, v in linhas
    )
    return (
        "<h2 style='margin:0 0 12px;color:#EF4444'>Job da VPS falhou</h2>"
        f"<table>{tabela}</table>"
        "<p style='margin:16px 0 6px;font-weight:600'>Últimas linhas do log desta execução:</p>"
        "<pre style='white-space:pre-wrap;font-size:11px;background:#F3F4F6;padding:12px;border-radius:6px'>"
        f"{html.escape(log[-12000:])}</pre>"
        f"<p style='font-size:12px;color:#6B7280'>Ver tudo: <code>journalctl -u {html.escape(unidade)} -n 200</code></p>"
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Alerta por e-mail de job do systemd que falhou.")
    parser.add_argument("unidade", help="nome da unidade (ex.: stokki-backup-gcs.service)")
    parser.add_argument("--forcar", action="store_true", help="ignora a janela anti-enxurrada (teste manual)")
    args = parser.parse_args(argv)

    config = _config()
    cfg = config.get("alertas_jobs", {}) or {}
    if not cfg.get("ativo", True):
        logger.info("alertas_jobs.ativo=false -- nada enviado.")
        return 0
    destino = cfg.get("destinatario") or (config.get("notificacao_execucao", {}) or {}).get("destinatario") \
        or "hugo@freshlogbr.com"
    janela = int(cfg.get("janela_min", JANELA_PADRAO_MIN))

    caminho_estado = _arquivo_estado()
    try:
        estado = json.loads(caminho_estado.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        estado = {}
    if args.forcar:
        estado.pop(args.unidade, None)
    enviar, suprimidos = decidir_envio(estado, args.unidade, datetime.now(), janela)
    caminho_estado.parent.mkdir(parents=True, exist_ok=True)
    caminho_estado.write_text(json.dumps(estado, ensure_ascii=False, indent=1), encoding="utf-8")
    if not enviar:
        logger.info(f"{args.unidade}: falha nº {suprimidos} dentro da janela de {janela} min -- sem novo e-mail.")
        return 0

    info = detalhes_da_unidade(args.unidade)
    log = log_da_execucao(args.unidade, info.get("InvocationID"))
    from email_utils import COR_ERRO, envelope_html, enviar_email

    assunto = f"[ALERTA VPS] Job falhou: {args.unidade}"
    ok = enviar_email([destino], assunto, envelope_html(montar_corpo(args.unidade, info, log, suprimidos),
                                                        cor_acento=COR_ERRO), config.get("email", {}))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
