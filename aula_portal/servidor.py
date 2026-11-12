# -*- coding: utf-8 -*-
"""
Portal da gravacao da aula: o app real, com tres pecas roteirizadas pra
o chat sair igual em toda gravacao, sem Claude e sem Vuupt:
  - assistente._pedidos_do_cliente -> pedidos do dia ficticio;
  - assistente._cliente_anthropic  -> "modelo" que devolve a fala do roteiro;
  - chamados: atendimento em horario e com uma atendente online.
O resto (telas, API, banco ficticio) e o portal de verdade.

Guarda de rede do processo (o navegador tem a dele): SMTP recusado e socket
so pra loopback; cada tentativa vai pra ARQ_BLOQUEIOS e o gravar reprova o video.

    py -3.11 -m aula_portal.servidor --porta 8097   (de dentro de portal_cliente/)
"""
import argparse
import ipaddress
import json
import smtplib
import socket
from contextlib import contextmanager
from datetime import date
from types import SimpleNamespace

from aula_portal import RAIZ, dados_teste as dt

ARQ_BLOQUEIOS = RAIZ / "dados" / "aula_bloqueios.log"

ATENDENTE = {"nome": "Juliana", "usuario": "juliana"}
RESPOSTA_ASSISTENTE = {
    "resposta": "Entendi: você quer receber o pedido #PS-90108 só depois das 14h. Esse ajuste é com a nossa "
                "equipe. Clique em Falar com atendente que eu já passo o seu pedido com o resumo.",
    "area": "entrega",
    "assunto": "Receber o PS-90108 depois das 14h",
    "resumo": "Cliente pede que o pedido #PS-90108 (Mercearia Santa Luz) seja entregue só depois das 14h.",
    "precisa_atendente": True,
}


class _ModeloRoteirizado:
    class messages:  # noqa: N801  (mesma forma do cliente anthropic)
        @staticmethod
        def create(**_kw):
            return SimpleNamespace(stop_reason="end_turn",
                                   content=[SimpleNamespace(type="text", text=json.dumps(RESPOSTA_ASSISTENTE))])


def _pedidos(_cliente, _config):
    d = dt.dia(date.today())
    return d["pedidos"] + d["agendados_futuros"], d["kpis"]


@contextmanager
def roteirizado():
    import assistente
    import chamados
    trocas = [(assistente, "_pedidos_do_cliente", _pedidos),
              (assistente, "_cliente_anthropic", lambda _config: _ModeloRoteirizado()),
              (chamados, "atendentes_online", lambda _conn: [dict(ATENDENTE)]),
              (chamados, "situacao_horario", lambda *_a, **_k: {"dentro": True, "motivo": None, "volta_em": ""})]
    antigos = [(m, nome, getattr(m, nome)) for m, nome, _ in trocas]
    for m, nome, novo in trocas:
        setattr(m, nome, novo)
    try:
        yield
    finally:
        for m, nome, velho in antigos:
            setattr(m, nome, velho)


def _registrar(arquivo, texto: str) -> None:
    with open(arquivo, "a", encoding="utf-8") as f:
        f.write(texto + "\n")


class _SMTPBloqueado:
    arquivo = None

    def __init__(self, *args, **_kw):
        _registrar(self.arquivo, f"smtp {args[:2]}")
        raise OSError("aula: SMTP bloqueado na gravacao")


def _loopback(endereco) -> bool:
    try:
        return ipaddress.ip_address(str(endereco[0])).is_loopback
    except (ValueError, IndexError, TypeError):
        return str(endereco[0]) == "localhost"


@contextmanager
def guarda_rede(arquivo=ARQ_BLOQUEIOS):
    original_connect, original_ex = socket.socket.connect, socket.socket.connect_ex
    smtp, smtp_ssl = smtplib.SMTP, smtplib.SMTP_SSL

    def barrar(sock, endereco):
        if sock.family in (socket.AF_INET, socket.AF_INET6) and not _loopback(endereco):
            _registrar(arquivo, f"socket {endereco}")
            raise OSError(f"aula: conexao pra fora bloqueada na gravacao ({endereco})")

    def connect(sock, endereco):
        barrar(sock, endereco)
        return original_connect(sock, endereco)

    def connect_ex(sock, endereco):
        barrar(sock, endereco)
        return original_ex(sock, endereco)

    _SMTPBloqueado.arquivo = arquivo
    socket.socket.connect, socket.socket.connect_ex = connect, connect_ex
    smtplib.SMTP = smtplib.SMTP_SSL = _SMTPBloqueado
    try:
        yield
    finally:
        socket.socket.connect, socket.socket.connect_ex = original_connect, original_ex
        smtplib.SMTP, smtplib.SMTP_SSL = smtp, smtp_ssl


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--porta", type=int, required=True)
    porta = ap.parse_args().porta
    with guarda_rede():
        import app
        with roteirizado():
            app.app.run(host="127.0.0.1", port=porta)


if __name__ == "__main__":
    main()
