# -*- coding: utf-8 -*-
"""
Roteiros dos videos da aula. Cada Video e uma lista de Passos; cada Passo
tem a fala (narrada e, por padrao, usada como legenda) e as acoes do
navegador, que acontecem enquanto a fala toca.

Acoes (ver gravar.ACOES): ("ir", caminho) · ("clicar", seletor) ·
("digitar", seletor, texto) · ("preencher", seletor, valor) ·
("escolher", seletor_select, valor) · ("enviar_nfe", seletor_input, seletor_visivel) · ("arquivo", seletor_input, [Path], seletor_visivel)
· ("esperar", seletor) · ("rolar", seletor) · ("destacar", seletor) ·
("cartao", titulo, subtitulo) · ("fechar_cartao",) · ("pausa", segundos) ·
("js", codigo) · ("py", funcao_sem_argumentos).

Interceptacoes: {padrao_glob_do_playwright ("**/api/x*"): handler(route, request)}; ver
json_fixo/arquivo_fixo.
"""
import json
from datetime import date, timedelta
from dataclasses import dataclass, field
from typing import Callable

from aula_portal import dados_teste as dt


@dataclass
class Passo:
    fala: str
    acoes: list[tuple] = field(default_factory=list)
    legenda: str | None = None


@dataclass
class Video:
    numero: int
    slug: str
    titulo: str
    resumo: str
    estado: str
    passos: list[Passo]
    intercepta: dict[str, Callable] = field(default_factory=dict)
    publicar: bool = True
    entrar: bool = True          # login invisivel antes do primeiro passo (cortado do video)
    inicio: str = "/login"       # sem login invisivel, o video comeca nesta pagina
    mostrar_chat: bool = False   # balao "Precisa de ajuda?" (texto muda com a hora)


def json_fixo(dados) -> Callable:
    """Handler que responde JSON; `dados` pode ser chamavel(request) -> obj."""
    def handler(route, request):
        corpo = dados(request) if callable(dados) else dados
        route.fulfill(status=200, content_type="application/json", body=json.dumps(corpo, ensure_ascii=False))
    return handler


def arquivo_fixo(conteudo: bytes, tipo: str) -> Callable:
    def handler(route, request):
        route.fulfill(status=200, content_type=tipo, body=conteudo)
    return handler


def _abertura(titulo: str) -> Passo:
    return Passo(f"{titulo}.", [("cartao", titulo, "Portal do cliente Fresh Log"), ("pausa", 2.2)])  # cartao fica no ar mesmo com titulo curto


def _fecho(proximo: str | None) -> Passo:
    if proximo:
        return Passo(f"No próximo vídeo: {proximo}.", [("cartao", f"Próximo: {proximo}", "Aprenda a usar o portal")])
    return Passo("Pronto! Você terminou a aula. Qualquer dúvida, chame a gente pelo chat do portal.",
                 [("cartao", "Pronto!", "Qualquer dúvida, fale com a Fresh Log pelo chat")])


# ── 1. Primeiro acesso ─────────────────────────────────────────────────────────

PIN_NOVO = "730518"   # o PIN "criado" no video; so existe no banco ficticio

_LIMPAR = "(() => {{ const e = document.querySelector('{0}'); e.value = ''; e.dispatchEvent(new Event('input')); }})()"

_V1 = Video(
    numero=1, slug="primeiro-acesso", titulo="Primeiro acesso",
    resumo="Entrar com o CNPJ e o PIN recebido, criar o seu PIN e completar o cadastro.",
    estado="primeiro_acesso", entrar=False, inicio="/login",
    passos=[
        _abertura("Primeiro acesso"),
        Passo("Para entrar no portal, abra o link que a Fresh Log enviou e digite o CNPJ da sua empresa.",
              [("fechar_cartao",), ("digitar", "#cnpj", dt.CNPJ)]),
        Passo("Depois, digite o PIN que você recebeu da Fresh Log e clique em Entrar.",
              [("digitar", "#pin", dt.PIN_GRAVACAO), ("clicar", "button.principal")]),
        Passo("No primeiro acesso, o portal pede para você criar um PIN só seu, de seis números. "
              "Ele vai ser pedido toda vez que você entrar.",
              [("esperar", "#pin2"), ("destacar", "h1")]),
        Passo("Digite o novo PIN duas vezes e clique em Salvar e entrar.",
              [("digitar", "#pin", PIN_NOVO), ("digitar", "#pin2", PIN_NOVO), ("clicar", "button.principal")]),
        Passo("Em seguida, complete o cadastro da empresa. Primeiro, escolha o tipo de carga que vocês enviam.",
              [("esperar", "label.opcao"), ("clicar", "label.opcao:nth-of-type(1)")]),
        Passo("Confira os e-mails que vão receber os avisos de entrega, um por linha.",
              [("js", _LIMPAR.format("#emails")), ("digitar", "#emails", dt.EMAIL)]),
        Passo("O WhatsApp é opcional. Ele serve para avisar quando um chamado está esperando a sua resposta.",
              [("js", _LIMPAR.format("#whatsapp")), ("digitar", "#whatsapp", "11999990000")]),
        Passo("Clique em Salvar e entrar. Pronto: você já está no portal, vendo as entregas do dia.",
              [("clicar", "button.principal"), ("esperar", "#tabela"), ("pausa", 1.5)]),
        Passo("Se um dia esquecer o PIN, use o link Primeiro acesso, esqueci o PIN, na tela de entrada. "
              "Você recebe um e-mail para criar um novo.",
              [("ir", "/sair"), ("destacar", "a[href$='/primeiro-acesso']")]),
    ],
)


# ── 2. Acompanhar o dia ────────────────────────────────────────────────────────

_LIMPAR_BUSCA = ("(() => { const b = document.querySelector('#busca'); b.value = ''; "
                 "b.dispatchEvent(new Event('input')); })()")

_V2 = Video(
    numero=2, slug="acompanhar-o-dia", titulo="Acompanhar o dia",
    resumo="Ver onde está cada pedido: entregues, em rota, aguardando saída e insucessos.",
    estado="padrao",
    passos=[
        _abertura("Acompanhar o dia"),
        Passo("Esta é a tela de acompanhamento. Ela mostra todos os seus pedidos e onde cada um está agora.",
              [("fechar_cartao",), ("pausa", 1)]),
        Passo("No topo ficam os números do dia: pedidos, entregues, em rota, aguardando saída e insucessos.",
              [("destacar", ".tile[data-aba='todos']"), ("destacar", ".tile[data-aba='entregues']"),
               ("destacar", ".tile[data-aba='em_rota']")]),
        Passo("Clique em um desses números para filtrar a lista. Em rota, por exemplo, mostra só o que está na rua.",
              [("clicar", ".tile[data-aba='em_rota']"), ("rolar", "#tabela")]),
        Passo("Em cada linha você vê o pedido, a nota fiscal, o destinatário, a situação, o motorista e a rota.",
              [("destacar", "#tabela tbody tr:nth-child(2)")]),
        Passo("Quando um pedido teve mais de uma tentativa de entrega, clique no selo para ver o histórico.",
              [("clicar", ".selo-tent"), ("pausa", 1.5)]),
        Passo("Para achar um pedido, use a busca: vale o número do pedido, a nota fiscal ou o nome do cliente.",
              [("clicar", "#abas button[data-aba='todos']"), ("digitar", "#busca", "Bistrô"), ("pausa", 1)]),
        Passo("Em Colunas, você escolhe o que aparece na tabela.",
              [("js", _LIMPAR_BUSCA), ("clicar", "#btn-colunas"), ("pausa", 1.5), ("clicar", "#colunas-fechar")]),
        Passo("E aqui você muda o período da lista: últimos sete, catorze ou trinta dias.",
              [("js", "window.scrollTo({top: 0, behavior: 'smooth'})"), ("escolher", "#periodo", "30")]),
        Passo("Na lateral ficam o resumo de hoje, o que ainda aguarda saída e as entregas agendadas para os "
              "próximos dias. A tela se atualiza sozinha a cada cinco minutos.",
              [("destacar", "#lista-saida"), ("destacar", "#lista-proximos")]),
    ],
)


# ── 3. Status do pedido ────────────────────────────────────────────────────────
# Banco no estado "status" (ambiente._ENVIOS_STATUS): um envio em cada status.

def _st(situacao: str) -> tuple:
    return ("destacar", f"#tabela .badge.b-{situacao}")


def _env(status: str) -> tuple:
    return ("destacar", f"#env-tabela .badge.b-{status}")


_V_STATUS = Video(
    numero=3, slug="status-do-pedido", titulo="Status do pedido",
    resumo="O que quer dizer cada situação do acompanhamento e cada status da lista de envios.",
    estado="status",
    passos=[
        _abertura("Status do pedido"),
        Passo("Na tela de acompanhamento, cada pedido mostra a sua situação. Veja o que cada uma quer dizer.",
              [("fechar_cartao",), ("rolar", "#tabela")]),
        Passo("Aguardando saída: o pedido está no armazém, esperando entrar numa rota.",
              [_st("aguardando_saida")]),
        Passo("Programado: ele já está numa rota, que ainda não saiu para entrega.",
              [_st("programado")]),
        Passo("Em rota: o motorista já saiu, e o pedido está a caminho.",
              [_st("em_rota")]),
        Passo("Entregue: a entrega foi concluída, e o canhoto fica disponível.",
              [_st("entregue")]),
        Passo("Insucesso: a entrega não deu certo. O motivo aparece ao lado, e às vezes a Fresh Log "
              "pergunta o que fazer.",
              [_st("insucesso")]),
        Passo("Agendado: a entrega tem data marcada para os próximos dias. Esses pedidos ficam na aba Agendados.",
              [("clicar", "#abas button[data-aba='agendados']"), _st("agendado")]),
        Passo("Agora, a lista da aba Enviar pedidos. Ela mostra o caminho de cada nota até virar um pedido de entrega.",
              [("clicar", "button[data-vista='envios']"), ("rolar", "#env-tabela")]),
        Passo("Na fila e Enviando à Stokki: o pedido está sendo criado no sistema do armazém. Leva poucos minutos.",
              [_env("NA_FILA"), _env("ENVIANDO")]),
        Passo("Criado na Stokki: deu certo, o pedido já existe e entra na programação de entregas. "
              "Já existia na Stokki quer dizer que essa nota já tinha sido enviada antes.",
              [_env("CRIADO"), _env("DUPLICADO")]),
        Passo("Erro: algo impediu a criação do pedido. O motivo aparece logo abaixo. Depois de resolver, é só reenviar.",
              [_env("ERRO")]),
        Passo("Aguardando liberação: o endereço fica fora da área que atendemos. O pedido fica retido "
              "até a nossa equipe avaliar e falar com você.",
              [_env("AGUARDANDO_LIBERACAO")]),
        Passo("Em espera, a seu pedido: você pediu para segurar o pedido. Retomando: você pediu para liberar, "
              "e ele está voltando para a fila de entregas.",
              [_env("EM_ESPERA_CLIENTE"), _env("RETOMANDO")]),
        Passo("Cancelando: o cancelamento foi pedido e está sendo feito. Cancelado: pronto, o pedido não sai mais.",
              [_env("CANCELANDO"), _env("CANCELADO")]),
    ],
)


# ── 5. Canhoto e planilha ──────────────────────────────────────────────────────

_V4 = Video(
    numero=5, slug="canhoto-e-planilha", titulo="Canhoto e planilha",
    resumo="Abrir a foto do canhoto assinado e exportar os pedidos do período para o Excel.",
    estado="padrao",
    intercepta={"**/api/canhoto/*": arquivo_fixo(dt.canhoto_png("5100"), "image/png"),
                "**/exportar.xlsx*": arquivo_fixo(dt.exportacao_xlsx(),
                                                 "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    passos=[
        _abertura("Canhoto e planilha"),
        Passo("Quando a entrega é concluída, o canhoto assinado fica disponível no portal.",
              [("fechar_cartao",), ("clicar", ".tile[data-aba='entregues']"), ("rolar", "#tabela")]),
        Passo("Clique em Canhoto, na linha do pedido entregue. A foto abre numa nova aba do navegador.",
              [("clicar", "#tabela a[href*='/api/canhoto/']"), ("pausa", 1)]),
        Passo("Você pode salvar ou imprimir a imagem como comprovante da entrega.", [("pausa", 1)]),
        Passo("Para voltar ao portal, é só fechar essa aba.",
              [("js", "history.back()"), ("esperar", "#tabela"), ("pausa", 1)]),
        Passo("O canhoto aparece assim que o motorista envia a foto, normalmente logo depois da entrega. "
              "Se ainda não apareceu, volte um pouco mais tarde.",
              [("clicar", ".tile[data-aba='entregues']"), ("destacar", "#tabela a[href*='/api/canhoto/']")]),
        Passo("Agora, a planilha. Primeiro escolha o período: a planilha traz os mesmos dias que a lista mostra.",
              [("js", "window.scrollTo({top: 0, behavior: 'smooth'})"), ("escolher", "#periodo", "30")]),
        Passo("Depois clique em Exportar planilha do período.",
              [("clicar", "#exportar")]),
        Passo("A planilha baixa no seu computador com todos os pedidos do período, em qualquer situação, "
              "pronta para abrir no Excel.",
              [("pausa", 1)]),
    ],
)


# ── 6. Enviar pedidos ──────────────────────────────────────────────────────────
# Roda de verdade contra o banco ficticio: sem worker, nada vai pra Stokki.

_ABA_ENVIOS = "button[data-vista='envios']"
_TOPO = "window.scrollTo({top: 0, behavior: 'smooth'})"


def _dest(n: int, campo: str) -> str:
    return f"#previa-dest > div:nth-child({n}) .{campo}"


_V5 = Video(
    numero=6, slug="enviar-pedidos", titulo="Enviar pedidos",
    resumo="Subir os XMLs das notas (ou a planilha), conferir a prévia e confirmar o envio.",
    estado="padrao",
    passos=[
        _abertura("Enviar pedidos"),
        Passo("Para mandar pedidos novos, clique na aba Enviar pedidos.",
              [("fechar_cartao",), ("clicar", _ABA_ENVIOS)]),
        Passo("Arraste os XMLs das notas fiscais para esta área, ou clique nela e escolha os arquivos. "
              "Pode mandar várias notas de uma vez, ou um arquivo ZIP.",
              [("destacar", "#zona")]),
        Passo("Vamos mandar três notas e um boleto, tudo junto.",
              [("enviar_nfe", "#arquivos", "#zona"), ("esperar", "#previa-tabela tbody tr")]),
        Passo("O portal confere tudo antes de enviar. Esta nota foi recusada porque o CNPJ emitente não é o da "
              "sua empresa. Ela fica de fora, e as outras seguem.",
              [("destacar", "#previa-rej")]),
        Passo("Na prévia você confere o destinatário, o endereço, os volumes, o peso e o valor de cada pedido.",
              [("destacar", "#previa-tabela")]),
        Passo("O boleto foi colocado sozinho no pedido certo, pelo número da nota. "
              "Ele vai impresso junto com a nota na entrega.",
              [("destacar", "#previa-tabela .anx")]),
        Passo("Na primeira vez que um destinatário aparece, informe o horário em que ele recebe. "
              "Se ele só recebe com agendamento, marque a caixa ao lado.",
              [("rolar", "#previa-dest"), ("preencher", _dest(1, "h-ini"), "08:00"), ("preencher", _dest(1, "h-fim"), "12:00"),
               ("preencher", _dest(2, "h-ini"), "07:00"), ("preencher", _dest(2, "h-fim"), "11:00")]),
        Passo("Marque que conferiu o documento e clique em Confirmar envio.",
              [("clicar", "#anx-conf"), ("clicar", "#btn-confirmar"), ("pausa", 1)]),
        Passo("Pronto: os pedidos entram na fila e, em instantes, são criados automaticamente. "
              "Acompanhe cada envio na lista abaixo.",
              [("rolar", "#env-tabela"), ("destacar", "#env-abas")]),
        Passo("Se um pedido der erro, o motivo aparece em destaque no topo. Depois de resolver, é só clicar em Reenviar.",
              [("js", _TOPO), ("pausa", 0.8), ("destacar", "#env-atencao")]),
        Passo("Não tem o XML da nota? Baixe o modelo de planilha, preencha uma linha por item e suba do mesmo jeito.",
              [("destacar", "#btn-modelo")]),
    ],
)


# ── 8. Cancelar um pedido ──────────────────────────────────────────────────────

_LINHA_CRIADO = "#env-tabela tbody tr:nth-child(3)"   # NF 5151, ja criado (semeado em ambiente._ENVIOS)

_V7 = Video(
    numero=8, slug="cancelar-pedido", titulo="Cancelar um pedido",
    resumo="Pedir o cancelamento de um pedido enviado e acompanhar até a Fresh Log concluir.",
    estado="padrao",
    passos=[
        _abertura("Cancelar um pedido"),
        Passo("Para cancelar, abra a aba Enviar pedidos e encontre o pedido na lista.",
              [("fechar_cartao",), ("clicar", _ABA_ENVIOS), ("rolar", "#env-tabela"), ("destacar", _LINHA_CRIADO)]),
        Passo("Na linha do pedido, clique em Cancelar.",
              [("clicar", f"{_LINHA_CRIADO} [data-acao='cancelar']")]),
        Passo("Se o pedido ainda não foi criado, ele é cancelado na hora. Se já foi criado, como este, "
              "a Fresh Log faz o cancelamento e você acompanha por aqui.",
              [("destacar", "#modal-caixa")]),
        Passo("Se quiser, conte o motivo. Depois confirme em Cancelar pedido.",
              [("digitar", "#m-motivo", "O cliente desistiu da compra."), ("clicar", "#m-ok"), ("pausa", 1)]),
        Passo("O pedido fica marcado como cancelamento solicitado até a nossa equipe concluir. "
              "Ele não sai mais para entrega.",
              [("destacar", _LINHA_CRIADO)]),
        Passo("Se o pedido já estiver na rua, o cancelamento passa pela nossa equipe, que confirma com você.",
              [("pausa", 1)]),
    ],
)


# ── 9. Retomar um pedido em espera ─────────────────────────────────────────────

_V8 = Video(
    numero=9, slug="retomar-em-espera", titulo="Retomar um pedido em espera",
    resumo="Segurar um pedido (tirar da rota) e liberar de novo quando quiser.",
    estado="padrao",
    passos=[
        _abertura("Pedido em espera"),
        Passo("Quando você pede para segurar um pedido, ele fica em espera e não sai para entrega.",
              [("fechar_cartao",), ("clicar", _ABA_ENVIOS), ("rolar", "#env-tabela")]),
        Passo("Para segurar, use o botão Tirar da rota, na linha do pedido. A Fresh Log aplica e avisa aqui.",
              [("destacar", "#env-tabela [data-acao='em_espera']")]),
        Passo("Os pedidos em espera ficam em Pendências.",
              [("clicar", "#env-abas [data-aba='pendencias']")]),
        Passo("Quando quiser liberar, clique em Retomar entrega.",
              [("clicar", "#env-tabela [data-acao='retomar']")]),
        Passo("Escolha se ele volta na próxima rota da sua região, ou se prefere agendar uma data e um horário.",
              [("clicar", "input[name='m-modo'][value='agendamento']"), ("pausa", 1.2),
               ("clicar", "input[name='m-modo'][value='proxima_rota']")]),
        Passo("Confirme em Retomar entrega. O pedido volta para a fila de entregas.",
              [("clicar", "#m-ok"), ("pausa", 1.2)]),
    ],
)


# ── 4. Alterar endereco e pedidos agendados ────────────────────────────────────
# Regra de 12/11: o cliente altera sozinho (6feeb7a, tela "Alterar entrega").

def _alteracao(route, request):
    sid = int(request.url.rstrip("/").split("/")[-2])
    if request.method == "POST":
        corpo = dt.resposta_alteracao(sid, json.loads(request.post_data or "{}"))
    else:
        corpo = dt.alteracao(sid)
    route.fulfill(status=200, content_type="application/json", body=json.dumps(corpo, ensure_ascii=False))


_NOVA_DATA = (date.today() + timedelta(days=6)).isoformat()
_END = "#previa-tabela td.c-end"

_V3 = Video(
    numero=4, slug="alterar-endereco-e-agendados", titulo="Alterar endereço e pedidos agendados",
    resumo="Mudar o endereço de entrega no envio e alterar endereço, data ou horário de um pedido já enviado.",
    estado="padrao",
    intercepta={"**/api/pedidos/*/alteracao": _alteracao,
                "**/api/envios/cep/*": json_fixo(lambda req: dt.cep(req.url.rstrip("/").split("/")[-1]))},
    passos=[
        _abertura("Alterar endereço e agendamento"),
        Passo("Dá para mudar o endereço de entrega em dois momentos: no envio do pedido, "
              "e depois, enquanto ele ainda não saiu para entrega. Primeiro, no envio.",
              [("fechar_cartao",), ("clicar", _ABA_ENVIOS)]),
        Passo("Suba as notas como sempre. Na prévia, a coluna Endereço de entrega mostra o endereço da nota.",
              [("enviar_nfe", "#arquivos", "#zona"), ("esperar", "#previa-tabela tbody tr"), ("destacar", _END)]),
        Passo("Se a entrega for em outro lugar, clique em Alterar endereço de entrega.",
              [("clicar", f"{_END} [data-ent='abrir']")]),
        Passo("Digite o CEP: a rua, o bairro e a cidade vêm sozinhos. Complete com o número e, se quiser, uma referência.",
              [("digitar", f"{_END} [data-f='cep']", "05425000"), ("pausa", 1),
               ("digitar", f"{_END} [data-f='numero']", "300"),
               ("digitar", f"{_END} [data-f='complemento']", "Entrada pela doca 2")]),
        Passo("Clique em Usar este endereço. O pedido vai para o endereço novo, e o da nota fica riscado, só para conferência.",
              [("clicar", f"{_END} [data-ent='usar']"), ("destacar", _END)]),
        Passo("Se o destinatário já recebeu em outro endereço, o portal pergunta onde entregar desta vez. "
              "Depois, é só confirmar o envio.",
              [("destacar", "#btn-confirmar")]),
        Passo("E depois que o pedido foi enviado? Volte ao acompanhamento e abra a aba Agendados.",
              [("clicar", "button[data-vista='acompanhamento']"), ("esperar", "#tabela"),
               ("clicar", "#abas button[data-aba='agendados']"), ("rolar", "#tabela")]),
        Passo("Na linha do pedido, clique em Alterar entrega.",
              [("clicar", "#tabela a[data-alterar='9011']"), ("esperar", "[data-alt-toggle='data']")]),
        Passo("Aparece o que está combinado: endereço, data e horário. Clique em Alterar no que quer mudar. "
              "Aqui, a data da entrega.",
              [("destacar", "#alt-caixa"), ("clicar", "[data-alt-toggle='data']"),
               ("preencher", "[data-f='data']", _NOVA_DATA)]),
        Passo("E o horário de recebimento. Ele passa a valer também para os próximos pedidos desse destinatário.",
              [("clicar", "[data-alt-toggle='hor']"), ("preencher", "[data-f='hini']", "14:00"),
               ("preencher", "[data-f='hfim']", "18:00")]),
        Passo("Clique em Salvar. A alteração vale na hora.",
              [("clicar", "#alt-salvar"), ("esperar", ".alt-msg.ok:not(.pulsando)"), ("destacar", "#alt-msg")]),
        Passo("Agora, um pedido que já está numa rota que ainda não saiu. Mudamos o horário e clicamos em Salvar.",
              [("clicar", "[data-alt-fechar]"), ("clicar", "#abas button[data-aba='todos']"),
               ("clicar", "#tabela a[data-alterar='9007']"), ("esperar", "[data-alt-toggle='hor']"),
               ("clicar", "[data-alt-toggle='hor']"), ("preencher", "[data-f='hini']", "15:00"),
               ("preencher", "[data-f='hfim']", "18:00"), ("clicar", "#alt-salvar"), ("esperar", ".alt-avisos")]),
        Passo("O portal avisa: o pedido sai da rota, e a entrega pode atrasar um dia. "
              "Se estiver de acordo, clique em Alterar mesmo assim.",
              [("destacar", ".alt-avisos"), ("destacar", "#alt-salvar")],
              legenda="Pedido em rota: a entrega pode atrasar 1 dia. Você decide."),
        Passo("Dá para alterar enquanto o pedido não saiu para entrega. Se ele já estiver na rua, "
              "fale com a gente pelo chat.",
              [("clicar", "[data-alt-fechar]")]),
    ],
)


# ── 7. Responder a um insucesso ────────────────────────────────────────────────

_REAGENDAR_PARA = (date.today() + timedelta(days=2)).isoformat()
_insucesso = {"respondido": False}


def _dia_insucesso(route, request):
    d = dt.dia(date.today())
    if _insucesso["respondido"]:   # depois da resposta, o cartao de atencao some
        d["atencao"] = []
        d["kpis"]["aguardando_resposta"] = 0
    route.fulfill(status=200, content_type="application/json", body=json.dumps(d, ensure_ascii=False))


def _responder(route, request):
    _insucesso["respondido"] = True
    corpo = {"ok": True, "resultados": [{"code": "PS-90107", "resultado": "reagendado", "data": _REAGENDAR_PARA}]}
    route.fulfill(status=200, content_type="application/json", body=json.dumps(corpo))


def _zerar_insucesso():
    _insucesso["respondido"] = False


_V6 = Video(
    numero=7, slug="responder-insucesso", titulo="Responder a um insucesso",
    resumo="Quando uma entrega não dá certo: reentregar, escolher outra data ou não reenviar.",
    estado="padrao",
    intercepta={"**/api/dia*": _dia_insucesso, "**/api/responder": _responder},
    passos=[
        _abertura("Responder a um insucesso"),
        Passo("Quando uma entrega não dá certo, o pedido aparece em destaque no topo, em Precisa da sua atenção.",
              [("py", _zerar_insucesso), ("fechar_cartao",), ("destacar", "#atencao")]),
        Passo("Ali estão o motivo, o número do pedido e a pergunta da Fresh Log.",
              [("destacar", "#atencao .pergunta")]),
        Passo("Você escolhe o que fazer: reentregar na próxima rota, escolher outra data ou não reenviar.",
              [("destacar", "#atencao [data-acao='manter']"), ("destacar", "#atencao [data-acao='abrir-reagendar']"),
               ("destacar", "#atencao [data-acao='cancelar']")]),
        Passo("Aqui, vamos escolher outra data e o horário a partir do qual o cliente recebe.",
              [("clicar", "#atencao [data-acao='abrir-reagendar']"), ("preencher", "#atencao .rg-data", _REAGENDAR_PARA),
               ("preencher", "#atencao .rg-hora", "14:00")]),
        Passo("Confirme o reagendamento. A Fresh Log recebe a sua resposta na hora e reprograma a entrega.",
              [("clicar", "#atencao [data-acao='reagendar']"), ("pausa", 2.5)]),
        Passo("Quanto antes você responder, mais rápido a entrega volta para a rota.", [("pausa", 1)]),
    ],
)


# ── 10. Chat e chamados ─────────────────────────────────────────────────────────
# O portal da gravacao (aula_portal.servidor) roteiriza a fala do assistente.

def _equipe_responde():
    import chamados as ch
    conn = ch.conectar()
    try:
        ultimo = conn.execute("SELECT id FROM portal_chamados ORDER BY id DESC LIMIT 1").fetchone()
        ch.adicionar_mensagem(conn, ch.buscar_chamado(conn, ultimo[0]), ch.ORIGEM_EQUIPE, "Juliana",
                              "Oi! Já ajustei: o #PS-90108 vai ser entregue depois das 14h. Qualquer coisa, é só chamar.")
    finally:
        conn.close()


_V9 = Video(
    numero=10, slug="chat-e-chamados", titulo="Chat e chamados",
    resumo="Falar com a Fresh Log pelo chat do portal e acompanhar os chamados.",
    estado="padrao", mostrar_chat=True,
    passos=[
        _abertura("Chat e chamados"),
        Passo("Para falar com a Fresh Log, clique no balão de conversa, no canto da tela.",
              [("fechar_cartao",), ("clicar", "#atd-abrir")]),
        Passo("Primeiro, escolha o assunto. Aqui, é sobre uma entrega.",
              [("clicar", "#atd-corpo [data-area='entrega']")]),
        Passo("O assistente pergunta qual é o pedido. Escolha da lista, ou digite o número.",
              [("esperar", ".atd-chip[data-valor*='90108']"), ("clicar", ".atd-chip[data-valor*='90108']")]),
        Passo("Agora conte o que você precisa, com as suas palavras.",
              [("digitar", "#atd-texto", "Preciso receber esse pedido depois das 14h, o recebimento fecha de manhã."),
               ("clicar", "#atd-enviar")]),
        Passo("O assistente entende, resume, e passa para a nossa equipe quando precisa de uma pessoa.",
              [("esperar", ".atd-chip[data-acao='atendente']"), ("clicar", ".atd-chip[data-acao='atendente']")]),
        Passo("A equipe responde aqui mesmo, e você também recebe a resposta por e-mail.",
              [("py", _equipe_responde), ("esperar", "text=Já ajustei"), ("pausa", 1.5)]),
        Passo("Seus chamados ficam guardados na aba Chamados, com o histórico de cada conversa.",
              [("clicar", "#atd-widget .atd-abas button[data-aba='chamados']"), ("pausa", 1)]),
    ],
)


# ── 11. Notificacoes ───────────────────────────────────────────────────────────

_V10 = Video(
    numero=11, slug="notificacoes", titulo="Notificações",
    resumo="Escolher quais e-mails a sua empresa recebe e em qual endereço.",
    estado="padrao",
    passos=[
        _abertura("Notificações"),
        Passo("Para escolher quais avisos a sua empresa recebe, clique em Notificações, no topo do portal.",
              [("fechar_cartao",), ("clicar", "#btn-notificacoes"), ("esperar", ".ntf-chave")]),
        Passo("Primeiro, os e-mails que recebem os avisos. Pode colocar mais de um, um por linha.",
              [("destacar", "#ntf-emails")]),
        Passo("Notas em rota: toda manhã, a lista das notas que saem para entrega no dia. "
              "E Entrega concluída: um e-mail por pedido, com o canhoto ou o motivo da falha.",
              [("destacar", ".ntf-chave[data-tipo='nfs_em_rota']"), ("destacar", ".ntf-chave[data-tipo='entrega_concluida']")]),
        Passo("Faltas no recebimento avisa quando chega menos do que foi anunciado. "
              "O Resumo diário traz, no fim do dia, a situação de todas as notas.",
              [("destacar", ".ntf-chave[data-tipo='faltas_recebimento']"), ("destacar", ".ntf-chave[data-tipo='resumo_diario']")]),
        Passo("Também há avisos de insucesso esperando a sua decisão, de pedidos em espera e de agendamento.",
              [("destacar", ".ntf-chave[data-tipo='insucesso']"), ("destacar", ".ntf-chave[data-tipo='pedidos_em_espera']"),
               ("destacar", ".ntf-chave[data-tipo='agendamento']")]),
        Passo("E, no WhatsApp, o aviso de chamado esperando a sua resposta.",
              [("destacar", ".ntf-chave[data-tipo='chamado_sem_resposta']")]),
        Passo("Ligue o que quiser receber, desligue o que não precisa e clique em Salvar.",
              [("clicar", ".ntf-chave[data-tipo='resumo_diario']"), ("clicar", "#ntf-salvar"), ("pausa", 1.5)]),
    ],
)


# ── 12. Regioes e dias de atendimento ──────────────────────────────────────────

_V11 = Video(
    numero=12, slug="regioes-e-dias", titulo="Regiões e dias de atendimento",
    resumo="Onde a Fresh Log entrega, em que dias passa em cada região e o calendário do mês.",
    estado="padrao",
    passos=[
        _abertura("Regiões e dias de atendimento"),
        Passo("Para saber em que dias a Fresh Log passa em cada região, clique em Dias de atendimento, no topo do portal.",
              [("fechar_cartao",), ("clicar", "#btn-regioes")]),
        Passo("Aqui está cada região que atendemos, as cidades de cada uma e os dias da semana em que a rota passa.",
              [("esperar", "table"), ("destacar", "table")]),
        Passo("A frequência mostra se a região é atendida todo dia, toda semana ou a cada quinze dias.",
              [("rolar", "table tbody tr:nth-child(4)")]),
        Passo("Mais abaixo fica o calendário do mês, com os feriados em que não há entrega.",
              [("rolar", ".mes-nav"), ("destacar", ".mes-nav")]),
        Passo("Use as setas para ver os próximos meses e se planejar com antecedência.",
              [("clicar", ".mes-nav a:last-of-type"), ("pausa", 1)]),
        Passo("Essa página também abre sem login. Pode mandar o link para quem cuida dos pedidos na sua empresa.",
              [("js", "window.scrollTo({top: 0, behavior: 'smooth'})"), ("pausa", 1)]),
    ],
)

VIDEOS: list[Video] = [_V1, _V2, _V_STATUS, _V3, _V4, _V5, _V6, _V7, _V8, _V9, _V10, _V11]


def _amarrar_fechos(videos: list[Video]) -> None:
    """Cada video termina anunciando o proximo PUBLICADO (a pagina so lista
    esses). Liberou o 3 ou o 10: regravar o anterior a ele."""
    for v in videos:
        seguinte = next((o for o in sorted(videos, key=lambda o: o.numero) if o.numero > v.numero and o.publicar), None)
        v.passos.append(_fecho(seguinte.titulo if seguinte else None))


_amarrar_fechos(VIDEOS)
