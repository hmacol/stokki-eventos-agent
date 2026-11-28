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

from aula_equipe import RAIZ, ambiente, dados
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
# Hugo, 16/11: todos os passos, inclusive os do celular (QR + login, camera,
# cancelar folha, falha na Stokki, tentar de novo e tirar da coleta).

_COLETA_NOVA = 2        # a 1 e a de ontem (historico); a 3 e a folha cancelada
_COLETA_CANCELADA = 3
_FOLHA_PNG = dados.PASTA_IMAGENS / "folha.png"
_FOTO_FOLHA = dados.PASTA_IMAGENS / "folha_assinada.jpg"
_CAMERA_QR = dados.PASTA_IMAGENS / "camera_qr.png"
_URL_QR = f"https://app.freshhub.com.br/painel/expedicao/coletas/{_COLETA_NOVA}"
_CANCELADO = 92014      # aula_equipe.servidor.FALHAS_DO_VIDEO["cancelado"]


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
    impressa = dados.folha_png(pdf)
    impressa.save(_FOLHA_PNG, "PNG")
    dados.camera_no_qr(impressa, _CAMERA_QR)
    sairam = [i for i in dados.SAEM_NA_COLETA if i != dados.NAO_SAIU]
    dados.foto_da_folha(dados.folha_preenchida(pdf, sairam, c["pedidos"]), _FOTO_FOLHA)


def _arquivo(caminho, tipo: str) -> Callable:
    def handler(route, request):
        route.fulfill(status=200, content_type=tipo, body=caminho.read_bytes())
    return handler


MOLDURA = """<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><title>Celular da doca</title>
<style>
  html, body { margin: 0; height: 100%; background: #141428; font-family: 'Segoe UI', Arial, sans-serif; overflow: hidden; }
  [hidden] { display: none !important; }
  .palco { display: flex; align-items: flex-start; justify-content: center; gap: 48px; padding-top: 12px; }
  .lado { color: #fff; width: 250px; margin-top: 110px; }
  .lado .sub { color: #00C896; font-weight: 700; font-size: 15px; letter-spacing: .14em; text-transform: uppercase; }
  .lado .tit { font-weight: 800; font-size: 28px; line-height: 1.15; margin-top: 10px; }
  .celular { background: #0b0b16; border-radius: 30px; padding: 10px 10px 14px; box-shadow: 0 20px 50px rgba(0,0,0,.5);
             border: 2px solid #2b2b44; }
  .celular .alto { width: 60px; height: 5px; border-radius: 3px; background: #2b2b44; margin: 0 auto 8px; }
  .tela { position: relative; width: 360px; height: 450px; border-radius: 6px; overflow: hidden; background: #fff; }
  iframe { display: block; width: 360px; height: 450px; border: 0; background: #fff; }
  .cam { position: absolute; inset: 0; background: #000; }
  .cam img { width: 100%; height: 100%; object-fit: cover; display: block; }
  .pilula { position: absolute; left: 50%; bottom: 64px; transform: translateX(-50%); background: rgba(255,255,255,.95);
            border: 0; border-radius: 14px; padding: 9px 16px; font: 13px 'Segoe UI', Arial, sans-serif; color: #141428;
            box-shadow: 0 6px 18px rgba(0,0,0,.4); white-space: nowrap; cursor: pointer; }
  .pilula b { display: block; font-size: 14px; }
  .disparar { position: absolute; left: 50%; bottom: 18px; transform: translateX(-50%); width: 58px; height: 58px;
              border-radius: 50%; background: #fff; border: 5px solid rgba(255,255,255,.45); background-clip: padding-box;
              box-shadow: 0 0 0 2px #fff; cursor: pointer; }
  .flash { position: absolute; inset: 0; background: #fff; opacity: 0; pointer-events: none; transition: opacity .12s; }
  .flash.on { opacity: .9; }
</style></head><body><div class="palco">
  <div class="lado"><div class="sub">Na doca</div><div class="tit">O QR code da folha abre a coleta no celular</div></div>
  <div class="celular"><div class="alto"></div><div class="tela">
    <iframe id="celular" name="celular" src="about:blank"></iframe>
    <div class="cam" id="cam">
      <img id="cam-img" src="/__aula/camera_qr.png" alt="">
      <button type="button" class="pilula" id="cam-abrir"><b>app.freshhub.com.br</b>Abrir a coleta __NUMERO__</button>
      <button type="button" class="disparar" id="cam-disparar" aria-label="Tirar a foto" hidden></button>
      <div class="flash" id="cam-flash"></div>
    </div>
  </div></div>
  <div class="lado"></div>
</div>
<script>
  const $ = (id) => document.getElementById(id);
  window.aulaCel = {
    abrir() { $('cam').hidden = true; $('celular').src = '__SRC__'; },
    foto() { $('cam-img').src = '/__aula/foto_folha.jpg'; $('cam-abrir').hidden = true; $('cam-disparar').hidden = false;
             $('cam').hidden = false; },
    disparar() { $('cam-flash').classList.add('on'); setTimeout(() => { $('cam').hidden = true; $('cam-flash').classList.remove('on'); }, 260); },
  };
  $('cam-abrir').addEventListener('click', () => window.aulaCel.abrir());
  $('cam-disparar').addEventListener('click', () => window.aulaCel.disparar());
</script></body></html>"""


def _moldura(route, request):
    html = (MOLDURA.replace("__SRC__", f"/expedicao/coletas/{_COLETA_NOVA}")
            .replace("__NUMERO__", f"COL-{_COLETA_NOVA}"))
    route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html.encode("utf-8"))


_GERAR = "section[data-grupo]:has(.vai[value='92011'])"
_RETIRA = "section[data-grupo]:has(.vai[value='92020'])"
_FOLHA = f"section[data-coleta='{_COLETA_NOVA}']"
_FOLHA_CANCELAR = f"section[data-coleta='{_COLETA_CANCELADA}']"

_V2 = Video(
    numero=2, slug="coletas", titulo="Coletas",
    resumo="Saída do pedido de retirada, do começo ao fim: gerar a folha, cancelar a que não vai sair, ler o QR code "
           "no celular da doca, foto da folha assinada, confirmar e resolver o que falhar na Stokki.",
    estado="coletas", login="expedicao", inicio="/expedicao/coletas", formato="paisagem",
    intercepta={"**/__aula/celular*": _moldura, "**/__aula/folha.png*": _arquivo(_FOLHA_PNG, "image/png"),
                "**/__aula/camera_qr.png*": _arquivo(_CAMERA_QR, "image/png"),
                "**/__aula/foto_folha.jpg*": _arquivo(_FOTO_FOLHA, "image/jpeg")},
    passos=[
        _abertura("Coletas"),
        Passo("Coletas é a saída do pedido de retirada: a transportadora ou o próprio cliente vem buscar no galpão.",
              [("fechar_cartao",), ("destacar", ".coletas h1")],
              legenda="Coletas: saída do pedido de retirada."),
        Passo("Os pedidos aparecem sozinhos, lidos da Stokki a cada dez minutos, separados por transportadora.",
              [("destacar", _GERAR)],
              legenda="Os pedidos aparecem sozinhos, por transportadora."),
        Passo("Marque os pedidos que o motorista vai levar, ou use Marcar todos, e clique em Gerar folha.",
              [("clicar", f"{_GERAR} [data-acao='todos']"), ("pausa", 0.6), ("clicar", f"{_GERAR} [data-acao='gerar']"),
               ("esperar", _FOLHA), ("py", _desenhar_folha)]),
        Passo("A folha abre para imprimir. No canto fica o QR code, que o celular da doca vai ler. "
              "O motorista marca o que levou, preenche os dados e assina.",
              [("js", f"window.aulaEquipe.mostrarFolha('/__aula/folha.png', 'COL-{_COLETA_NOVA}.pdf')"),
               ("pausa", 1.0)],
              legenda="Imprima a folha. O QR code fica no canto."),
        Passo("Enquanto a saída não é confirmada, a folha fica em Folhas em andamento. Perdeu a folha? Imprima de novo aqui.",
              [("js", "window.aulaEquipe.fecharFolha()"), ("destacar", _FOLHA), ("destacar", f"{_FOLHA} .acoes a.bt")],
              legenda="A folha fica em andamento. Dá para imprimir de novo."),
        Passo("E se a coleta não vai acontecer? Aqui, o cliente avisou que não vem mais buscar. A folha dele já foi gerada.",
              [("clicar", f"{_RETIRA} .vai[value='92020']"), ("clicar", f"{_RETIRA} [data-acao='gerar']"),
               ("esperar", _FOLHA_CANCELAR), ("destacar", _FOLHA_CANCELAR)],
              legenda="Coleta que não vai acontecer?"),
        Passo("Clique em Cancelar folha e confirme. O pedido volta para a lista A coletar.",
              [("clicar", f"{_FOLHA_CANCELAR} [data-acao='cancelar']"), ("esperar", f"{_RETIRA} .vai[value='92020']"),
               ("destacar", f"{_RETIRA} li")],
              legenda="Cancelar folha: o pedido volta para a lista."),
        Passo("Na doca, aponte a câmera do celular para o QR code da folha e toque no link que aparece.",
              [("sair",), ("ir", "/__aula/celular"), ("destacar", "#cam-abrir"), ("clicar", "#cam-abrir")],
              legenda="Na doca: aponte a câmera para o QR code da folha."),
        Passo("Na primeira vez, o celular pede o login da expedição. Depois de entrar, a coleta abre direto, "
              "e das próximas vezes nem pede mais.",
              [("celular",), ("esperar", "#usuario"), ("digitar", "#usuario", ambiente.USUARIOS["expedicao"]),
               ("digitar", "#senha", ambiente.SENHA), ("clicar", "button.entrar"), ("esperar", "[data-acao='confirmar']")],
              legenda="1ª vez: login da expedição. Depois, abre direto."),
        Passo("Desmarque o pedido que não saiu. Ele volta para a lista e pode ir em outra folha.",
              [("clicar", f".saiu[value='{dados.NAO_SAIU}']")],
              legenda="Não saiu? Desmarque. Ele volta para a lista."),
        Passo("Digite o nome do motorista e a placa do veículo.",
              [("digitar", ".c-nome", dados.MOTORISTA_COLETA), ("digitar", ".c-placa", dados.PLACA_COLETA)]),
        Passo("Toque no campo da foto: a câmera abre. Fotografe a folha preenchida e assinada.",
              [("destacar", ".c-foto"), ("computador",), ("js", "window.aulaCel.foto()"), ("pausa", 1.4),
               ("clicar", "#cam-disparar"), ("celular",), ("arquivo", ".c-foto", [_FOTO_FOLHA])],
              legenda="Foto da folha assinada, pela câmera do celular."),
        Passo("Sem a foto, o nome e a placa, o botão não libera. Com tudo preenchido, toque em Confirmar coleta.",
              [("clicar", "[data-acao='confirmar']"), ("esperar", ".andamento")],
              legenda="Sem foto, nome e placa, não confirma."),
        Passo("A saída vai para a Stokki. Cada pedido leva cerca de trinta segundos, e a tela acompanha sozinha.",
              [("destacar", ".andamento"), ("esperar", ".estado.FALHOU")],
              legenda="A saída vai para a Stokki. A tela acompanha sozinha."),
        Passo("Às vezes um pedido não sai. A tela mostra qual foi e o motivo.",
              [("destacar", ".lista-ped li:has(.estado.FALHOU)")],
              legenda="Pedido que não saiu: a tela mostra o motivo."),
        Passo("No computador, a folha continua em andamento enquanto houver pedido com falha.",
              [("ir", "/expedicao/coletas"), ("destacar", _FOLHA)],
              legenda="No computador: a folha segue em andamento."),
        Passo("Quando a Stokki não respondeu, clique em Tentar de novo os que falharam.",
              [("clicar", f"{_FOLHA} [data-acao='tentar']"), ("esperar", f"{_FOLHA} [data-acao='retirar']")],
              legenda="Stokki não respondeu? Tentar de novo."),
        Passo("Este pedido foi cancelado na Stokki e não vai sair. Use Tirar da coleta, e confirme.",
              [("destacar", f"li[data-ped='{_CANCELADO}'] .msg"),
               ("clicar", f"li[data-ped='{_CANCELADO}'] [data-acao='retirar']"), ("esperar", "table.hist")],
              legenda="Cancelado na Stokki? Tirar da coleta."),
        Passo("Pronto: a coleta foi concluída e aparece em Coletas concluídas, com o motorista, a placa e a foto da folha.",
              [("rolar", "table.hist"), ("destacar", "table.hist tr:nth-child(2)")],
              legenda="Coletas concluídas: motorista, placa e foto."),
        _fecho(None, "Coletas"),
    ],
)

VIDEOS = [_V1, _V2]
