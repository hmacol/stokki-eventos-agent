# -*- coding: utf-8 -*-
"""
Portal da gravacao da aula: o app real, com pecas roteirizadas pra
o video sair igual em toda gravacao, sem Claude, sem Vuupt e sem Google:
  - assistente._pedidos_do_cliente -> pedidos do dia ficticio;
  - assistente._cliente_anthropic  -> "modelo" que devolve a fala do roteiro;
  - chamados: atendimento em horario e com uma atendente online;
  - cotacao: CEP, coordenadas e rota (km + pedagio) ficticios; o e-mail da
    proposta nao sai, fica em ARQ_EMAIL pro video mostrar.
O resto (telas, API, banco ficticio) e o portal de verdade.

Guarda de rede do processo (o navegador tem a dele): SMTP recusado e socket
so pra loopback (menos o proxy morto, que tambem conta como tentativa de sair); cada tentativa vai pra ARQ_BLOQUEIOS e o gravar reprova o video.

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
ARQ_EMAIL = RAIZ / "dados" / "aula_email_proposta.html"   # corpo do e-mail da proposta (video da cotacao)
PORTA_PROXY_MORTO = 9   # ambiente.PROXY_MORTO

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


def _coordenadas(paradas, _config):
    for i, p in enumerate(paradas):
        p["lat"], p["lng"] = -23.55 + 0.03 * i, -46.65 + 0.02 * i
    return paradas


def _email(_destinos, assunto, corpo_html, _config_email, **_kw) -> bool:
    """Nenhum e-mail sai; o da proposta fica guardado pro video abrir."""
    if "Proposta de frete" in assunto:
        ARQ_EMAIL.write_text(corpo_html, encoding="utf-8")
    return True


@contextmanager
def roteirizado():
    import assistente
    import chamados
    import cotacao
    trocas = [(assistente, "_pedidos_do_cliente", _pedidos),
              (assistente, "_cliente_anthropic", lambda _config: _ModeloRoteirizado()),
              (chamados, "atendentes_online", lambda _conn: [dict(ATENDENTE)]),
              (chamados, "situacao_horario", lambda *_a, **_k: {"dentro": True, "motivo": None, "volta_em": ""}),
              (cotacao, "consultar_cep", dt.cep_cotacao),
              (cotacao, "resolver_coordenadas", _coordenadas),
              (cotacao, "calcular_trajeto", lambda paradas, _regras, _config: dt.trajeto_cotacao(len(paradas))),
              (cotacao, "enviar_email", _email)]
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
        if sock.family not in (socket.AF_INET, socket.AF_INET6):
            return
        # o proxy morto (ambiente.PROXY_MORTO, 127.0.0.1:9) e loopback, mas quem bate nele queria sair
        if not _loopback(endereco) or (len(endereco) > 1 and endereco[1] == PORTA_PROXY_MORTO):
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
