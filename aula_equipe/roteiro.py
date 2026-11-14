# -*- coding: utf-8 -*-
"""
Roteiros da aula da equipe. Mesmo formato da aula do cliente
(aula_portal.roteiro.Passo: fala + acoes); as acoes estao em gravar.ACOES.
Alem das do cliente: ("celular",) passa a agir na tela dentro da moldura de
celular, ("computador",) volta pra pagina, ("assinar", seletor_canvas)
rabisca a assinatura.
"""
import sqlite3
from dataclasses import dataclass, field
from typing import Callable

from aula_equipe import RAIZ, dados
from aula_portal.roteiro import Passo

ASSINATURA = "Aula da equipe Freshlog"


@dataclass
class Video:
    numero: int
    slug: str
    titulo: str
    resumo: str
    estado: str                  # ambiente.ESTADOS: o banco ficticio do video
    login: str                   # ambiente.USUARIOS: com que login fixo a tela abre
    inicio: str                  # pagina aberta antes do 1o passo (o login fica fora do video)
    formato: str                 # gravar.FORMATOS: retrato (celular) | paisagem (computador)
    passos: list[Passo]
    intercepta: dict[str, Callable] = field(default_factory=dict)
    publicar: bool = True


def _abertura(titulo: str) -> Passo:
    return Passo(f"{titulo}.", [("cartao", titulo, ASSINATURA), ("pausa", 2.2)])


def _fecho(proximo: str | None, assunto: str) -> Passo:
    if proximo:
        return Passo(f"Pronto! No próximo vídeo: {proximo}.", [("cartao", f"Próximo: {proximo}", ASSINATURA)])
    return Passo(f"Pronto! Você terminou a aula de {assunto}. Dúvida, fale com o seu líder.",
                 [("cartao", "Pronto!", "Dúvida? Fale com o seu líder")])


# ── 1. Retornos (celular, em pe) ───────────────────────────────────────────────

_P = dados.paradas_do_video()   # codigo -> parada_id
_CAIXAS, _AVARIA, _PARCIAL, _NAO_VEIO = (_P[c] for c in ("PS-91201", "PS-91202", "PS-91203", "PS-91204"))
_FOTO_PALLET = dados.PASTA_IMAGENS / "pallet.jpg"
_FOTO_AVARIA = dados.PASTA_IMAGENS / "avaria.jpg"


def _fotos_retornos() -> None:
    dados.foto_pallet(_FOTO_PALLET)
    dados.foto_avaria(_FOTO_AVARIA)


_V1 = Video(
    numero=1, slug="retornos", titulo="Recebimento de retornos",
    resumo="Conferir o que o motorista trouxe de volta: caixas, avaria com foto, o que não veio, foto da carga e assinatura.",
    estado="retornos", login="galpao", inicio="/retornos", formato="retrato",
    passos=[
        Passo("Recebimento de retornos.", [("py", _fotos_retornos), ("cartao", "Recebimento de retornos", ASSINATURA),
                                           ("pausa", 2.2)]),
        Passo("Quando o motorista volta ao galpão com pedidos que não foram entregues, abra a tela Retornos no celular.",
              [("fechar_cartao",), ("destacar", ".seg")],
              legenda="Motorista voltou com pedidos? Abra Retornos no celular."),
        Passo("Cada cartão é um motorista, com os pedidos que ele precisa devolver. "
              "O selo vermelho avisa que o retorno está atrasado.",
              [("destacar", "button.cartao[data-i='0']"), ("pausa", 1.2), ("destacar", "button.cartao[data-i='0'] .selo.atraso")],
              legenda="Cada cartão é um motorista. Vermelho: retorno atrasado."),
        Passo("Toque no motorista que está descarregando.", [("clicar", "button.cartao[data-i='1']")]),
        Passo("Para cada pedido, conte as caixas que voltaram de verdade. Aqui eram quatro, mas chegaram três: "
              "ajuste no botão de menos.",
              [("destacar", f"#ped-{_CAIXAS} .contador"), ("clicar", f"[data-menos='{_CAIXAS}']")],
              legenda="Conte as caixas. Eram 4, chegaram 3: ajuste no menos."),
        Passo("Confira também o motivo. Se o motorista marcou errado, troque aqui.",
              [("destacar", f"[data-motivo='{_CAIXAS}']")],
              legenda="Motivo errado? Troque aqui."),
        Passo("Caixa amassada, molhada ou aberta? Marque avaria: sim, e tire a foto. "
              "Sem a foto da avaria, o recebimento não é confirmado.",
              [("rolar", f"#ped-{_AVARIA}"), ("clicar", f"[data-avaria='{_AVARIA}'][data-v='1']"),
               ("arquivo", f"[data-arq-avaria='{_AVARIA}']", [_FOTO_AVARIA], f"[data-fotos-avaria='{_AVARIA}'] .btn-foto")],
              legenda="Avaria? Marque Sim e tire a foto. Sem foto, não confirma."),
        Passo("Na entrega parcial, a quantidade vem em branco. Conte e digite quantas caixas voltaram.",
              [("rolar", f"#ped-{_PARCIAL}"), ("digitar", f"[data-caixas='{_PARCIAL}']", "2")],
              legenda="Parcial: conte e digite quantas caixas voltaram."),
        Passo("Se um pedido não veio nesta descarga, desmarque. Ele continua pendente e volta na próxima.",
              [("rolar", f"#ped-{_NAO_VEIO}"), ("clicar", f"[data-veio='{_NAO_VEIO}']")],
              legenda="Não veio? Desmarque. Ele continua pendente."),
        Passo("Depois, tire a foto da carga no pallet.",
              [("arquivo", "#arq-carga", [_FOTO_PALLET], "#fotos-carga .btn-foto")]),
        Passo("Escreva o seu nome e peça para o motorista assinar com o dedo.",
              [("digitar", "#recebedor", dados.RECEBEDOR), ("assinar", "#canvas-ass")]),
        Passo("Toque em Confirmar recebimento. Diferença de caixas, avaria e parcial viram alerta automático para a equipe.",
              [("clicar", "#btn-confirmar"), ("esperar", "text=Recebimento confirmado")],
              legenda="Confirmar. Diferença, avaria e parcial viram alerta para a equipe."),
        Passo("O pedido que não veio continua na lista, esperando a próxima descarga.",
              [("esperar", "button.cartao[data-i='1']"), ("destacar", "button.cartao[data-i='1']")]),
        Passo("Na aba Recebidos fica tudo o que já foi conferido, com as fotos e a assinatura.",
              [("clicar", ".seg button[data-aba='recebidos']"), ("clicar", "button.cartao[data-id]"),
               ("esperar", ".assinatura-img"), ("rolar", ".assinatura-img")],
              legenda="Recebidos: tudo o que já foi conferido."),
        _fecho("Coletas", "Retornos"),
    ],
)


# ── 2. Coletas (computador, deitado; a doca numa moldura de celular) ───────────

_COLETA_NOVA = 2   # a 1 e a de ontem (historico)
_FOLHA_PNG = dados.PASTA_IMAGENS / "folha.png"
_FOTO_FOLHA = dados.PASTA_IMAGENS / "folha_assinada.jpg"
_URL_QR = f"https://app.freshhub.com.br/painel/expedicao/coletas/{_COLETA_NOVA}"


def _coleta_nova() -> dict:
    from coletas import regras
    conn = sqlite3.connect(RAIZ / "dados" / "dados.db")
    conn.row_factory = sqlite3.Row
    try:
        return regras.coleta(conn, _COLETA_NOVA)
    finally:
        conn.close()


def _desenhar_folha() -> None:
    from coletas import folha_pdf
    c = _coleta_nova()
    pdf = folha_pdf.gerar(c, url=_URL_QR)
    _FOLHA_PNG.parent.mkdir(parents=True, exist_ok=True)
    dados.folha_png(pdf).save(_FOLHA_PNG, "PNG")
    sairam = [i for i in dados.SAEM_NA_COLETA if i != dados.NAO_SAIU]
    dados.foto_da_folha(dados.folha_preenchida(pdf, sairam, c["pedidos"]), _FOTO_FOLHA)


def _folha(route, request):
    route.fulfill(status=200, content_type="image/png", body=_FOLHA_PNG.read_bytes())


MOLDURA = """<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><title>Celular da doca</title>
<style>
  html, body { margin: 0; height: 100%; background: #141428; font-family: 'Segoe UI', Arial, sans-serif; overflow: hidden; }
  .palco { display: flex; align-items: flex-start; justify-content: center; gap: 48px; padding-top: 12px; }
  .lado { color: #fff; width: 250px; margin-top: 110px; }
  .lado .sub { color: #00C896; font-weight: 700; font-size: 15px; letter-spacing: .14em; text-transform: uppercase; }
  .lado .tit { font-weight: 800; font-size: 28px; line-height: 1.15; margin-top: 10px; }
  .celular { background: #0b0b16; border-radius: 30px; padding: 10px 10px 14px; box-shadow: 0 20px 50px rgba(0,0,0,.5);
             border: 2px solid #2b2b44; }
  .celular .alto { width: 60px; height: 5px; border-radius: 3px; background: #2b2b44; margin: 0 auto 8px; }
  iframe { display: block; width: 360px; height: 450px; border: 0; border-radius: 6px; background: #fff; }
</style></head><body><div class="palco">
  <div class="lado"><div class="sub">Na doca</div><div class="tit">O QR code da folha abre a coleta no celular</div></div>
  <div class="celular"><div class="alto"></div><iframe id="celular" name="celular" src="__SRC__"></iframe></div>
  <div class="lado"></div>
</div></body></html>"""


def _moldura(route, request):
    html = MOLDURA.replace("__SRC__", f"/expedicao/coletas/{_COLETA_NOVA}")
    route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html.encode("utf-8"))


_GERAR = "section[data-grupo]:has(.vai[value='92011'])"

_V2 = Video(
    numero=2, slug="coletas", titulo="Coletas",
    resumo="Saída do pedido de retirada: marcar, gerar a folha, e na doca confirmar com nome, placa e a foto da folha assinada.",
    estado="coletas", login="expedicao", inicio="/expedicao/coletas", formato="paisagem",
    intercepta={"**/__aula/celular*": _moldura, "**/__aula/folha.png*": _folha},
    passos=[
        _abertura("Coletas"),
        Passo("Coletas é a saída do pedido de retirada: a transportadora ou o próprio cliente vem buscar no galpão.",
              [("fechar_cartao",), ("destacar", ".coletas h1")],
              legenda="Coletas: saída do pedido de retirada."),
        Passo("Os pedidos aparecem sozinhos, lidos da Stokki a cada dez minutos, separados por transportadora.",
              [("destacar", _GERAR)],
              legenda="Os pedidos aparecem sozinhos, por transportadora."),
        Passo("Marque os pedidos que o motorista vai levar e toque em Gerar folha.",
              [("clicar", f"{_GERAR} .vai[value='92011']"), ("clicar", f"{_GERAR} .vai[value='92012']"),
               ("clicar", f"{_GERAR} .vai[value='92013']"), ("clicar", f"{_GERAR} [data-acao='gerar']"),
               ("esperar", "section.cartao.aberta"), ("py", _desenhar_folha)]),
        Passo("A folha sai para imprimir. O motorista marca o que levou, preenche nome, documento e placa, e assina.",
              [("js", f"window.aulaEquipe.mostrarFolha('/__aula/folha.png', 'COL-{_COLETA_NOVA}.pdf')"),
               ("pausa", 1.0)],
              legenda="Imprima a folha. O motorista preenche e assina."),
        Passo("Enquanto a saída não é confirmada, a folha fica aqui, em Folhas em andamento.",
              [("js", "window.aulaEquipe.fecharFolha()"), ("destacar", "section.cartao.aberta")],
              legenda="A folha fica em andamento até a confirmação."),
        Passo("Na doca, aponte a câmera do celular para o QR code da folha. A coleta abre no celular.",
              [("ir", "/__aula/celular"), ("celular",), ("esperar", "[data-acao='confirmar']")],
              legenda="Na doca: aponte o celular para o QR code da folha."),
        Passo("Desmarque o pedido que não saiu. Ele volta para a lista e pode ir em outra folha.",
              [("clicar", ".saiu[value='92013']")],
              legenda="Não saiu? Desmarque. Ele volta para a lista."),
        Passo("Digite o nome do motorista e a placa do veículo.",
              [("digitar", ".c-nome", dados.MOTORISTA_COLETA), ("digitar", ".c-placa", dados.PLACA_COLETA)]),
        Passo("Tire a foto da folha preenchida e assinada. Sem a foto, o nome e a placa, o botão não libera.",
              [("destacar", "[data-acao='confirmar']"), ("pausa", 0.8),
               ("arquivo", ".c-foto", [_FOTO_FOLHA], ".c-foto")],
              legenda="Foto da folha assinada: sem ela, não confirma."),
        Passo("Toque em Confirmar coleta. Os pedidos são marcados como Enviado na Stokki.",
              [("clicar", "[data-acao='confirmar']"), ("esperar", ".aviso.ok")],
              legenda="Confirmar coleta: os pedidos viram Enviado na Stokki."),
        Passo("No computador, a coleta aparece em Coletas concluídas, com o motorista, a placa e a foto da folha.",
              [("ir", "/expedicao/coletas"), ("rolar", "table.hist"), ("destacar", "table.hist tr:nth-child(2)")],
              legenda="No computador: Coletas concluídas, com motorista, placa e foto."),
        _fecho(None, "Coletas"),
    ],
)

VIDEOS = [_V1, _V2]
