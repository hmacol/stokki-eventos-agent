# -*- coding: utf-8 -*-
"""
notificar_whatsapp.py

Aviso curto no grupo interno de WhatsApp para notificacoes que ja saem
por e-mail (pedido do Hugo, 28/09/2026). O e-mail continua sendo o
registro completo; aqui vai so o essencial: contagens e nome da rotina,
NUNCA nome de cliente, endereco, NF ou log.

Excecao (pedido do Hugo, 29/09/2026): o aviso de insucesso de entrega
leva codigo do pedido, destinatario, remetente e motivo. Endereco e NF
continuam de fora.

Quatro origens chamam este modulo, sempre DEPOIS do e-mail:
    notificar_execucao_agente.notificar_execucao  -> avisar_execucao
    alertar_falha_job.main                        -> avisar_falha_job
    verificar_entregues_nao_expedidos.main        -> avisar_nao_expedidos
    expedir_pedidos.main                          -> avisar_insucessos

Quinta origem (pedido do Hugo, 29/09/2026), em grupo SEPARADO: chamado do
atendimento que passou a depender de gente, com o link da tela. Leva so o
nome do embarcador.
    portal_cliente/chamados.whatsapp_para_atendimento -> avisar_chamado

Origem PARA FORA da empresa (pedido do Hugo, 29/09/2026): o embarcador que
ficou 10 min sem responder a equipe recebe o aviso no numero que ele mesmo
cadastrou no portal. Destino individual (<numero>@c.us), teto proprio
(clientes.teto_diario), sem janela de repeticao (a rotina garante uma
tentativa por mensagem da equipe) e consulta de numero antes de enviar.
    avisar_cliente_sem_resposta.py -> avisar_cliente_sem_resposta

Mesma origem de cliente (Hugo, 03/10/2026, dias fixos v2): pedido com data
fora do dia de visita da regiao virou envio dedicado. Um aviso por pedido
(quem garante e roteirizacao/registro_dia_fixo.py), mesmo teto e mesmo
forcar_destino dos clientes.
    roteirizacao/notificar_fora_dia_fixo.py -> avisar_cliente_fora_dia_fixo

O numero que envia e o do proprio Hugo, por um gateway nao-oficial
(integracao_openwa.py). Por isso: desligado por padrao, teto diario,
intervalo minimo, sem repeticao e SEM reenvio automatico (licao do erro
463 em 27/08: insistir piora).

Falha aqui NUNCA derruba a rotina que chamou.

config.yaml:
    whatsapp_notificacoes:
      ativo: false
      base_url: "http://127.0.0.1:2785/api"
      api_key: "..."
      sessao: "..."
      grupo_id: "...@g.us"
      grupo_atendimento_id: "...@g.us"   # grupo dos avisos de chamado
      avisar_chamados: true              # desliga so o aviso de chamado
      sempre_avisar: [cancelar_rotas_sem_motorista, executar_tudo, criar_rotas_diarias]
      teto_diario: 20
      intervalo_min_seg: 20
      janela_repeticao_min: 120
      falhas_para_alerta: 3
      pausa_canal_min: 60        # descanso depois de N falhas seguidas
      clientes:                  # aviso ao embarcador (avisar_cliente_sem_resposta.py)
        ativo: false
        minutos: 10
        dias_max: 3
        teto_diario: 30
        forcar_destino: ""       # numero do Hugo enquanto for piloto; vazio = envio real
"""
import logging
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import integracao_openwa

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).resolve().parent / "dados" / "dados.db"
MAX_DETALHE = 200
MAX_MENSAGEM = 200
MAX_ETAPAS_ERRO = 3
MAX_INSUCESSOS = 10
ORIGEM_CLIENTE = "cliente_sem_resposta"
TETO_CLIENTES_PADRAO = 30

_SCHEMA = """
CREATE TABLE IF NOT EXISTS notificacoes_whatsapp (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    criado_em    TEXT NOT NULL,
    origem       TEXT NOT NULL,
    tipo         TEXT NOT NULL,
    assinatura   TEXT,
    situacao     TEXT NOT NULL,
    motivo       TEXT,
    id_mensagem  TEXT
)"""


# --- Texto --------------------------------------------------------------------

def _uma_linha(texto, limite: int = MAX_DETALHE) -> str:
    """Uma linha so, sem os caracteres de formatacao do WhatsApp, cortada."""
    limpo = re.sub(r"[*_~`]", "", str(texto or ""))
    limpo = re.sub(r"\s+", " ", limpo).strip()
    return limpo if len(limpo) <= limite else limpo[:limite - 1].rstrip() + "…"


def _plural(n: int, singular: str, plural: str) -> str:
    return f"{n} {singular if n == 1 else plural}"


# Linguagem do grupo (pedido do Hugo, 29/09/2026): quem le nao e tecnico.
# Nada de nome de unit, codigo de saida ou excecao crua; isso fica no e-mail.
# A mensagem INTEIRA cabe em MAX_MENSAGEM caracteres, sem cortar no meio.
# Excecao decidida pelo Hugo: o aviso de insucesso, que lista os pedidos.
RODAPE_EMAIL = "Detalhes no e-mail."

NOMES_SIMPLES = {
    "Pipeline": "Importação de pedidos",
    "Execução completa": "Rotina de pedidos",
    "Agente Stokki Eventos": "Rotina automática",
}

MOTIVOS_SIMPLES = [
    (r"timeout|timed out|tempo esgotado", "o sistema demorou a responder"),
    (r"(?:http|status|erro|error|code|c[oó]digo)\W*40[13]\b|^\W*40[13]\W*$|unauthorized|forbidden"
     r"|n[aã]o autorizado", "o login no sistema caiu"),
    (r"(?:http|status|erro|error|code|c[oó]digo)\W*50[0234]\b|^\W*50[0234]\W*$|bad gateway|service unavailable",
     "o outro sistema estava fora do ar"),
    (r"connection|conex[aã]o|\bssl\b|\bdns\b|network|unreachable", "falha de conexão"),
    (r"database is locked", "banco de dados ocupado"),
]
CARA_DE_ERRO_TECNICO = r"Error|Exception|Traceback|[<>{}\[\]]|0x|\w\.\w+\("

# Chave = nome da unit sem "stokki-" e sem ".service" (infra/*.service).
NOMES_DAS_TAREFAS = {
    "acompanhar-retiradas": "Acompanhamento das retiradas no galpão",
    "avisar-cliente-sem-resposta": "WhatsApp ao cliente que não respondeu o chamado",
    "backup-gcs": "Cópia de segurança diária dos dados",
    "batimento-pedidos": "Batimento diário dos pedidos",
    "backup-horario": "Cópia de segurança de hora em hora",
    "cancelar-rotas-sem-motorista": "Cancelamento das rotas de hoje sem motorista",
    "coleta-emporio-quatro-estrelas": "Coleta diária do Empório Quatro Estrelas",
    "comparar-pool": "Conferência dos pedidos que aguardam rota",
    "comparar-vuupt": "Conferência entre a Vuupt e o nosso sistema",
    "dedicados-financeiro": "E-mail dos pedidos dedicados para o financeiro",
    "documentos-incremental": "Busca das notas fiscais dos pedidos",
    "ensaio-restauracao": "Teste semanal da cópia de segurança",
    "expedicao-frequente": "Baixa das entregas na Stokki",
    "informativo-mensal": "Informativo mensal de dias de entrega",
    "exportar-vuupt": "Cópia do histórico da Vuupt",
    "lancar-lalamove": "Pedido de corridas na Lalamove",
    "notificar-entregas": "E-mail de entrega concluída para os clientes",
    "notificar-nfs-em-rota": "E-mail da manhã com as notas em rota",
    "notificar-pedidos-em-espera": "Aviso de pedidos em espera",
    "notificar-transportadoras": "Aviso às transportadoras",
    "nucleo-sincronizar-servicos": "Atualização dos pedidos fora de rota",
    "nucleo-sincronizar-vuupt": "Atualização das rotas no app dos motoristas",
    "pipeline-horario": "Importação de pedidos de hora em hora",
    "reconciliar-retirada": "Conferência dos pedidos que o cliente retira",
    "resumo-diario-embarcador": "E-mail da noite com o resumo do dia",
    "romaneios-manha": "Geração dos romaneios da manhã",
    "sequencia-incremento": "Incremento das rotas das 18h",
    "sequencia-noite": "Rotina da noite",
    "sequencia-tarde": "Rotina da tarde",
    "sincronizar-confirmacoes": "Leitura das confirmações de rota dos motoristas",
    "sincronizar-lalamove": "Atualização das corridas da Lalamove",
    "verificar-entregues-nao-expedidos": "Conferência diária das entregas",
    "vigia-documentos": "Vigia dos documentos dos pedidos",
    "vigia-pedidos": "Vigia dos pedidos em aberto",
    "wms-recebimentos": "Atualização dos recebimentos do estoque",
    "wms-reservar-pedidos": "Reserva de estoque para os pedidos",
    "wms-sincronizar-produtos": "Atualização do cadastro de produtos do galpão",
}

RESULTADOS_SIMPLES = {
    "exit-code": "parou com erro",
    "timeout": "demorou demais e foi interrompida",
    "oom-kill": "foi interrompida por falta de memória",
    "signal": "foi interrompida no meio",
    "core-dump": "foi interrompida no meio",
}


def _nome_simples(nome, limite: int) -> str:
    limpo = _uma_linha(nome, limite)
    return NOMES_SIMPLES.get(limpo, limpo)


def _motivo_simples(detalhe, limite: int = MAX_DETALHE) -> str:
    """Erro conhecido vira frase; excecao crua vira 'erro técnico'; texto
    escrito por gente (ex.: '2 pedidos nao devolvidos') passa, cortado."""
    texto = _uma_linha(detalhe, 10_000)
    for padrao, frase in MOTIVOS_SIMPLES:
        if re.search(padrao, texto, re.IGNORECASE):
            return frase
    return "erro técnico" if re.search(CARA_DE_ERRO_TECNICO, texto) else _uma_linha(texto, limite)


def _nome_da_tarefa(unidade: str, info: dict) -> str:
    chave = re.sub(r"^stokki-|\.service$", "", str(unidade or ""))
    if chave in NOMES_DAS_TAREFAS:
        return NOMES_DAS_TAREFAS[chave]
    descricao = re.sub(r"^Stokki Eventos - |\s*\([^)]*\)", "", str(info.get("Description") or ""))
    return _uma_linha(descricao or unidade, 70)


def _tamanho(linhas: list) -> int:
    return len("\n".join(linhas))


def texto_execucao(resumo_etapas: dict, duracao_seg: float, titulo: str | None = None,
                   agora: datetime | None = None) -> str:
    from notificar_execucao_agente import etapas_com_erro, formatar_duracao, titulo_da_rotina
    resumo_etapas = resumo_etapas or {}
    erros = etapas_com_erro(resumo_etapas)
    nome = _nome_simples(titulo_da_rotina(resumo_etapas, titulo), 60)
    quando = (agora or datetime.now()).strftime("%d/%m %H:%M")
    total = len(resumo_etapas)
    if not erros:
        resumo = ("Rodou, mas não informou o que fez" if not total
                  else "Terminou sem problemas" if total == 1
                  else f"As {total} etapas terminaram sem problemas")
        return f"✅ *{nome}* · {quando}\n{resumo} ({formatar_duracao(duracao_seg)})"
    linhas = [f"❌ *{nome}* · {quando}"]
    if total == 1:
        livre = MAX_MENSAGEM - _tamanho(linhas + ["Não funcionou: ", RODAPE_EMAIL])
        motivo = _motivo_simples((resumo_etapas[erros[0]] or {}).get("detalhe"), livre)
        linhas.append(f"Não funcionou: {motivo}" if motivo else "Não funcionou")
    else:
        linhas.append(f"{len(erros)} das {total} etapas falharam:")
        # Entra o que couber em MAX_MENSAGEM: etapa com motivo, senao so o
        # nome, senao vai pra conta do "e mais N".
        mostradas = 0
        for etapa in erros[:MAX_ETAPAS_ERRO]:
            nome_etapa = _nome_simples(etapa, 45)
            motivo = _motivo_simples((resumo_etapas.get(etapa) or {}).get("detalhe"), 40)
            resto = [f"• e mais {len(erros) - mostradas - 1}"] if len(erros) - mostradas > 1 else []
            opcoes = ([f"• {nome_etapa}: {motivo}"] if motivo else []) + [f"• {nome_etapa}"]
            linha = next((o for o in opcoes
                          if _tamanho(linhas + [o] + resto + [RODAPE_EMAIL]) <= MAX_MENSAGEM), None)
            if not linha:
                break
            linhas.append(linha)
            mostradas += 1
        if len(erros) > mostradas:
            linhas.append(f"• e mais {len(erros) - mostradas}")
    linhas.append(RODAPE_EMAIL)
    return "\n".join(linhas)


def texto_falha_job(unidade: str, info: dict, agora: datetime | None = None) -> str:
    info = info or {}
    quando = (agora or datetime.now()).strftime("%d/%m %H:%M")
    resultado = RESULTADOS_SIMPLES.get(info.get("Result"), "não terminou como deveria")
    return "\n".join([
        f"🚨 *Tarefa automática falhou* · {quando}",
        f"{_nome_da_tarefa(unidade, info)}: {resultado}.",
        RODAPE_EMAIL,
    ])


def texto_nao_expedidos(n_alertas: int, n_rotas: int, n_retiradas: int) -> str:
    linhas = ["⚠️ *Conferência das entregas*"]
    if n_alertas:
        linhas.append("• " + _plural(n_alertas, "pedido entregue sem baixa na Stokki",
                                     "pedidos entregues sem baixa na Stokki"))
    if n_rotas:
        linhas.append("• " + _plural(n_rotas, "rota antiga não encerrada", "rotas antigas não encerradas"))
    if n_retiradas:
        linhas.append("• " + _plural(n_retiradas, "retirada no galpão há mais de 7 dias",
                                     "retiradas no galpão há mais de 7 dias"))
    linhas.append("Lista no e-mail.")
    return "\n".join(linhas)


def texto_rotas_fracas(data_alvo, resumo: dict) -> str:
    """Resumo das rotas fracas da rodada (roteirizacao/rotas_fracas.py).
    `resumo` e o dict de criar_rotas_diarias._aplicar_segurar."""
    linhas = [f"⚠️ *Rotas fracas* · rotas de {data_alvo:%d/%m}"]
    if resumo.get("juntadas"):
        linhas.append("• " + _plural(resumo["juntadas"], "juntada em rota vizinha", "juntadas em rotas vizinhas"))
    if resumo.get("seguradas"):
        linhas.append("• " + _plural(resumo["seguradas"], "segurada", "seguradas")
                      + f" para {resumo['data_nova']:%d/%m} ("
                      + _plural(resumo["pedidos_segurados"], "pedido", "pedidos") + ")")
    if resumo.get("sobraram"):
        linhas.append("• " + _plural(resumo["sobraram"], "saiu fraca", "saíram fracas") + " ("
                      + _plural(resumo["pedidos_sobraram"], "pedido", "pedidos") + ", "
                      + _plural(resumo["caixas_sobraram"], "caixa", "caixas") + ")")
    linhas.append("Veja no Planejamento.")
    return "\n".join(linhas)


def texto_insucessos(insucessos: list, agora: datetime | None = None) -> str:
    """Cada item: {"codigo", "destinatario", "remetente", "motivo"}."""
    quando = (agora or datetime.now()).strftime("%d/%m %H:%M")
    linhas = [f"⚠️ *Insucesso na entrega* · {quando}",
              _plural(len(insucessos), "pedido novo", "pedidos novos")]
    for item in insucessos[:MAX_INSUCESSOS]:
        linha = _uma_linha(item.get("codigo"), 30).lstrip("#")
        destinatario = _uma_linha(item.get("destinatario"), 60)
        remetente = _uma_linha(item.get("remetente"), 40)
        if destinatario:
            linha += f" · {destinatario}"
        if remetente:
            linha += f" ({remetente})"
        linhas.append(_uma_linha(f"{linha}: {_uma_linha(item.get('motivo')) or 'motivo não informado'}"))
    resto = len(insucessos) - MAX_INSUCESSOS
    if resto > 0:
        linhas.append(f"e mais {resto}")
    linhas.append("Detalhes no e-mail e na Torre.")
    return "\n".join(linhas)


def texto_chamado(chamado: dict, link: str, agora: datetime | None = None) -> str:
    """So o nome do embarcador (motorista sai sem nome); assunto e conversa
    ficam de fora, quem abre o link le na tela."""
    quando = (agora or datetime.now()).strftime("%d/%m %H:%M")
    if chamado.get("tipo") == "MOTORISTA":
        quem = "Motorista (app)"
    else:
        quem = _uma_linha(chamado.get("nome_cliente"), 35) or "Cliente"
    linhas = [f"🙋 *Atendimento precisa de gente* · {quando}", f"Chamado #{chamado['id']} · {quem}"]
    partes = []
    if chamado.get("area_rotulo"):
        partes.append(f"Área: {_uma_linha(chamado['area_rotulo'], 30)}")
    if chamado.get("pedido_ref"):
        partes.append(f"Pedido: {_uma_linha(chamado['pedido_ref'], 30)}")
    # O link nunca e cortado: a linha de area/pedido e que encolhe.
    while partes and _tamanho(linhas + [" · ".join(partes), link]) > MAX_MENSAGEM:
        partes.pop()
    if partes:
        linhas.append(" · ".join(partes))
    linhas.append(link)
    return "\n".join(linhas)


def texto_clientes_agenda(novos: int, total: int, link: str, agora: datetime | None = None) -> str:
    """Destinatarios com data de agendamento informada esperando o Hugo
    autorizar a marcacao AGENDA (marcar_clientes_agenda.py). So contagens;
    os nomes ficam na tela."""
    quando = (agora or datetime.now()).strftime("%d/%m %H:%M")
    frase = (f"{novos} novo aguarda" if novos == 1 else f"{novos} novos aguardam")
    return "\n".join([
        f"📅 *Clientes para marcar como AGENDA* · {quando}",
        f"{frase} sua autorização ({total} no total).",
        link,
    ])


def texto_cliente_sem_resposta(chamado_id: int, link: str) -> str:
    """Pro cliente: sem nome, sem assunto, sem conteudo da conversa. Pede
    pra responder no portal (a resposta no WhatsApp cai no celular do Hugo
    e nao entra no chamado)."""
    return (f"Fresh Log: respondemos o seu chamado #{chamado_id} e aguardamos o seu retorno.\n"
            f"Responda pelo portal: {link}")


def texto_fora_dia_fixo(codigo: str, data, regiao: str, dias: str, valor: float | None) -> str:
    """Pro embarcador, ate MAX_MENSAGEM. Encurta tirando primeiro a regiao e
    os dias, depois o valor -- codigo, data e "envio dedicado" ficam sempre."""
    codigo = str(codigo or "").lstrip("#")
    quando = data.strftime("%d/%m")
    if valor is None:
        preco = "valor a confirmar"
    else:
        preco = "R$ " + f"{float(valor):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    fim = "Para outra data, responda pelo portal."
    opcoes = [
        f"Fresh Log: o pedido {codigo} está para {quando}, fora dos dias de visita da região {regiao} ({dias}). "
        f"Vai como envio dedicado, {preco}. {fim}",
        f"Fresh Log: o pedido {codigo} está para {quando}, fora dos dias de visita da região. "
        f"Vai como envio dedicado, {preco}. {fim}",
        f"Fresh Log: o pedido {codigo} está para {quando}, fora dos dias de visita da região. "
        f"Vai como envio dedicado. {fim}",
    ]
    return next((t for t in opcoes if len(t) <= MAX_MENSAGEM), opcoes[-1][:MAX_MENSAGEM])


def texto_cadastro_motorista(cadastro: dict, link: str, agora: datetime | None = None) -> str:
    """Motorista novo pediu cadastro pelo app e ja mandou CNH e CRLV
    (nucleo/cadastro_motorista.py). O Hugo aprova no painel."""
    quando = (agora or datetime.now()).strftime("%d/%m %H:%M")
    nome = _uma_linha(cadastro.get("nome"), 40) or "Motorista"
    veiculo = _uma_linha(cadastro.get("tipo_veiculo"), 12)
    linhas = [f"🧑‍✈️ *Motorista novo pediu cadastro* · {quando}",
              f"{nome}" + (f" · {veiculo}" if veiculo else "") + f" · placa {cadastro.get('placa') or '-'}",
              "Aprove no painel (precisa do agente na Vuupt antes):",
              link]
    return "\n".join(linhas)


def texto_alteracao_cadastro(nome: str, mudancas: list[dict], agora: datetime | None = None) -> str:
    """Motorista mudou telefone/e-mail/PIX/placa pelo app. Vale na hora
    (decisao do Hugo, 08/10); o aviso e so pra ele saber."""
    quando = (agora or datetime.now()).strftime("%d/%m %H:%M")
    linhas = [f"✏️ *Motorista alterou o cadastro* · {quando}", _uma_linha(nome, 40) or "Motorista"]
    for m in mudancas[:4]:
        linhas.append(f"{m.get('rotulo') or m.get('campo')}: {_uma_linha(m.get('de') or '-', 30)} → {_uma_linha(m.get('para') or '-', 30)}")
    return "\n".join(linhas)


# --- Envio --------------------------------------------------------------------

def texto_informativo_mensal(titulo_mes: str, feriados: list, link: str, agora: datetime | None = None) -> str:
    """Virada de mês (Hugo, 07/10/2026): link da página pública + feriados em
    dia útil. O link nunca é cortado; o título do mês é o que encurta."""
    if feriados:
        datas = " e ".join(f.strftime("%d/%m") for f in feriados[:3]) + (" e outros" if len(feriados) > 3 else "")
        feriado = f"Feriados no mês: {datas} (sem visita)."
    else:
        feriado = "Sem feriado em dia útil no mês."
    fixo = len(f"Dias de entrega de  já estão no site:\n{link}\n{feriado}\n{RODAPE_EMAIL}")
    titulo = _uma_linha(titulo_mes, max(1, MAX_MENSAGEM - fixo))
    return f"Dias de entrega de {titulo} já estão no site:\n{link}\n{feriado}\n{RODAPE_EMAIL}"


def _cfg(config: dict | None) -> dict:
    return (config or {}).get("whatsapp_notificacoes") or {}


def _cfg_clientes(config: dict | None) -> dict:
    return _cfg(config).get("clientes") or {}


def _inteiro(cfg: dict, chave: str, padrao: int) -> int:
    try:
        return int(cfg.get(chave, padrao))
    except (TypeError, ValueError):
        return padrao


def _iso(quando: datetime) -> str:
    return quando.isoformat(timespec="seconds")


def _registrar(conn, agora, origem, tipo, assinatura, situacao, motivo=None, id_mensagem=None):
    conn.execute(
        "INSERT INTO notificacoes_whatsapp (criado_em, origem, tipo, assinatura, situacao, motivo, id_mensagem) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (_iso(agora), origem, tipo, assinatura, situacao, motivo, id_mensagem))
    conn.commit()


def _enviados_hoje(conn, agora: datetime, origem_cliente: bool) -> int:
    """Teto interno e teto dos clientes nao se misturam: aviso de cliente
    nao consome a cota dos alertas de falha, nem o contrario."""
    inicio_do_dia = _iso(agora.replace(hour=0, minute=0, second=0, microsecond=0))
    comparador = "=" if origem_cliente else "!="
    return conn.execute(
        f"SELECT COUNT(*) FROM notificacoes_whatsapp WHERE situacao = 'enviado' AND criado_em >= ? "
        f"AND origem {comparador} ?", (inicio_do_dia, ORIGEM_CLIENTE)).fetchone()[0]


def _teto(cfg: dict, origem: str) -> int:
    if origem == ORIGEM_CLIENTE:
        return _inteiro(cfg.get("clientes") or {}, "teto_diario", TETO_CLIENTES_PADRAO)
    return _inteiro(cfg, "teto_diario", 20)


def _motivo_para_nao_enviar(conn, cfg: dict, origem: str, assinatura: str | None, agora: datetime) -> str | None:
    # A origem dos clientes nao usa a janela: a rotina so chama uma vez por
    # mensagem da equipe, e a janela deixaria repetir depois de 2 h.
    if assinatura and origem != ORIGEM_CLIENTE:
        desde = _iso(agora - timedelta(minutes=_inteiro(cfg, "janela_repeticao_min", 120)))
        if conn.execute(
                "SELECT 1 FROM notificacoes_whatsapp WHERE origem = ? AND assinatura = ? "
                "AND situacao = 'enviado' AND criado_em >= ? LIMIT 1",
                (origem, assinatura, desde)).fetchone():
            return "repetida dentro da janela"
    # Disjuntor: depois de N falhas seguidas o canal descansa. Sem isso, rotina
    # que erra a cada rodada bateria no gateway o dia inteiro (insistir piora).
    limite = max(1, _inteiro(cfg, "falhas_para_alerta", 3))
    ultimas = conn.execute(
        "SELECT situacao, criado_em FROM notificacoes_whatsapp WHERE situacao IN ('enviado', 'falhou') "
        "ORDER BY id DESC LIMIT ?", (limite,)).fetchall()
    if len(ultimas) == limite and all(s == "falhou" for s, _ in ultimas):
        pausa = timedelta(minutes=_inteiro(cfg, "pausa_canal_min", 60))
        if agora - datetime.fromisoformat(ultimas[0][1]) < pausa:
            return "canal em pausa"
    if _enviados_hoje(conn, agora, origem == ORIGEM_CLIENTE) >= _teto(cfg, origem):
        return "teto diario atingido"
    return None


def saldo_clientes(conn, config: dict, agora: datetime | None = None) -> int:
    """Quantos avisos a clientes ainda cabem hoje. A rotina consulta antes
    de comecar e para quando acaba, em vez de gravar 'nao_enviado' (que
    contaria como a unica tentativa daquela mensagem)."""
    agora = agora or datetime.now()
    conn.execute(_SCHEMA)
    return max(0, _teto(_cfg(config), ORIGEM_CLIENTE) - _enviados_hoje(conn, agora, True))


def _esperar_intervalo(conn, cfg: dict, agora: datetime, dormir) -> datetime:
    """Devolve o horario em que o envio de fato acontece (agora + espera) --
    e ESSE que vai pro registro, senao o proximo calcula o intervalo errado."""
    ultima = conn.execute(
        "SELECT MAX(criado_em) FROM notificacoes_whatsapp WHERE situacao = 'enviado'").fetchone()[0]
    if not ultima:
        return agora
    intervalo = _inteiro(cfg, "intervalo_min_seg", 20)
    falta = intervalo - (agora - datetime.fromisoformat(ultima)).total_seconds()
    if 0 < falta <= intervalo:
        dormir(falta)
        return agora + timedelta(seconds=falta)
    return agora


def _alertar_se_canal_parou(conn, cfg: dict, config: dict) -> None:
    """Um e-mail so, exatamente na N-esima falha seguida. A N+1 nao repete;
    um envio com sucesso zera a contagem."""
    limite = _inteiro(cfg, "falhas_para_alerta", 3)
    seguidas = 0
    for (situacao,) in conn.execute(
            "SELECT situacao FROM notificacoes_whatsapp WHERE situacao IN ('enviado', 'falhou') "
            "ORDER BY id DESC LIMIT ?", (limite + 1,)):
        if situacao != "falhou":
            break
        seguidas += 1
    if seguidas != limite:
        return
    destino = (config.get("notificacao_execucao") or {}).get("destinatario") \
        or (config.get("email") or {}).get("remetente")
    if not destino:
        return
    from email_utils import envelope_html, enviar_email
    corpo = (f"<h2 style='margin:0 0 12px;color:#EF4444'>WhatsApp das notificações parou</h2>"
             f"<p>{limite} envios seguidos falharam. O gateway pode estar fora do ar ou o número "
             f"desconectado. Os e-mails continuam saindo normalmente.</p>"
             f"<p>Conferir na VPS: <code>docker ps</code> e a tabela <code>notificacoes_whatsapp</code>.</p>")
    enviar_email([destino], "[ALERTA] WhatsApp das notificações parou", envelope_html(corpo),
                 config.get("email", {}))


def _despachar(config, origem, tipo, texto, assinatura, modo_teste, conn, agora, dormir, grupo_id) -> str:
    cfg = _cfg(config)
    grupo_id = grupo_id or cfg.get("grupo_id")
    if not cfg.get("ativo") or not integracao_openwa.configurado(cfg) or not grupo_id:
        return "desligado"
    if modo_teste:
        logger.info(f"[MODO TESTE] WhatsApp nao enviado ({origem} -> {grupo_id}). Texto:\n{texto}")
        return "modo_teste"
    agora = agora or datetime.now()
    fechar = conn is None
    if fechar:
        conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        conn.execute(_SCHEMA)
        motivo = _motivo_para_nao_enviar(conn, cfg, origem, assinatura, agora)
        if motivo:
            _registrar(conn, agora, origem, tipo, assinatura, "nao_enviado", motivo)
            logger.info(f"WhatsApp nao enviado ({origem}): {motivo}.")
            return "nao_enviado"
        agora = _esperar_intervalo(conn, cfg, agora, dormir)
        ok, id_mensagem = integracao_openwa.enviar_texto(cfg, grupo_id, texto)
        if ok:
            _registrar(conn, agora, origem, tipo, assinatura, "enviado", None, id_mensagem)
            logger.info(f"WhatsApp enviado ({origem} -> {grupo_id}).")
            return "enviado"
        _registrar(conn, agora, origem, tipo, assinatura, "falhou", "gateway fora do ar ou envio recusado")
        _alertar_se_canal_parou(conn, cfg, config)
        return "falhou"
    finally:
        if fechar:
            conn.close()


def despachar(config: dict, origem: str, tipo: str, texto: str, assinatura: str | None = None,
              modo_teste: bool = False, conn=None, agora: datetime | None = None,
              dormir=time.sleep, grupo_id: str | None = None) -> str:
    """Aplica as regras e envia. Devolve a situacao: desligado | modo_teste |
    nao_enviado | enviado | falhou. Nunca levanta excecao. `grupo_id` troca
    o destino (padrao: whatsapp_notificacoes.grupo_id)."""
    try:
        return _despachar(config, origem, tipo, texto, assinatura, modo_teste, conn, agora, dormir, grupo_id)
    except Exception as exc:
        logger.warning(f"Falha na notificacao por WhatsApp (nao afeta a rotina): {exc}")
        return "falhou"


# --- Pontos de chamada -----------------------------------------------------------

def avisar_execucao(resumo_etapas: dict, duracao_seg: float, modo_teste: bool, config: dict,
                    titulo: str | None = None, **kw) -> str:
    """Resumo de rotina. So vai pro grupo se houve erro ou se o script
    esta em whatsapp_notificacoes.sempre_avisar."""
    try:
        if not _cfg(config).get("ativo"):
            return "desligado"
        from notificar_execucao_agente import etapas_com_erro
        origem = Path(sys.argv[0]).stem if sys.argv and sys.argv[0] else ""
        erros = etapas_com_erro(resumo_etapas or {})
        if not erros and origem not in (_cfg(config).get("sempre_avisar") or []):
            return "nao_relevante"
        assinatura = "erro:" + ",".join(erros) if erros else None
        texto = texto_execucao(resumo_etapas, duracao_seg, titulo, kw.get("agora"))
        return despachar(config, origem or "desconhecido", "execucao", texto, assinatura, modo_teste, **kw)
    except Exception as exc:
        logger.warning(f"Falha na notificacao por WhatsApp (nao afeta a rotina): {exc}")
        return "falhou"


def avisar_falha_job(unidade: str, info: dict, config: dict, **kw) -> str:
    """Sem regra de repeticao aqui: alertar_falha_job.py ja tem a janela
    anti-enxurrada e so chama quando o e-mail tambem saiu."""
    try:
        return despachar(config, unidade, "falha_job", texto_falha_job(unidade, info, kw.get("agora")), **kw)
    except Exception as exc:
        logger.warning(f"Falha na notificacao por WhatsApp (nao afeta a rotina): {exc}")
        return "falhou"


def avisar_nao_expedidos(n_alertas: int, n_rotas: int, n_retiradas: int, config: dict, **kw) -> str:
    try:
        return despachar(config, "verificar_entregues_nao_expedidos", "nao_expedidos",
                         texto_nao_expedidos(n_alertas, n_rotas, n_retiradas), **kw)
    except Exception as exc:
        logger.warning(f"Falha na notificacao por WhatsApp (nao afeta a rotina): {exc}")
        return "falhou"


def texto_cancelamento_pendente(envio: dict, motivo: str, erro: str) -> str:
    """Cancelamento pedido pelo cliente no portal que o worker nao conseguiu
    fazer sozinho (06/10). Cabe em MAX_MENSAGEM."""
    nf = envio.get("numero_nf") or envio.get("referencia") or "?"
    ps = envio.get("codigo_pedido") or "sem PS"
    emb = (envio.get("nome_embarcador") or envio.get("cnpj_embarcador") or "")[:30]
    detalhe = f"falhou: {erro}" if erro else motivo
    texto = f"Cancelamento pedido pelo cliente: NF {nf} · {ps} · {emb} · {detalhe} -- precisa cancelar à mão"
    return texto[:MAX_MENSAGEM - 1] + "…" if len(texto) > MAX_MENSAGEM else texto


def avisar_cancelamento_pendente(envio: dict, motivo: str, erro: str, config: dict, **kw) -> str:
    """Grupo do ATENDIMENTO (grupo_atendimento_id), nao o dos alertas.
    Assinatura por envio: o mesmo pedido nao repete no dia."""
    try:
        grupo = _cfg(config).get("grupo_atendimento_id")
        if not grupo:
            return "desligado"
        return despachar(config, "portal_cancelamento", "cancelamento",
                         texto_cancelamento_pendente(envio, motivo, erro), f"cancelamento:{envio['id']}",
                         grupo_id=grupo, **kw)
    except Exception as exc:
        logger.warning(f"Falha na notificacao por WhatsApp (nao afeta a rotina): {exc}")
        return "falhou"


def _ja_enviada_alguma_vez(conn, origem: str, assinatura: str) -> bool:
    """Ja saiu (situacao 'enviado') alguma vez, sem limite de tempo? Falha
    ao ler (banco travado, tabela que ainda nao existe) conta como nao."""
    fechar = conn is None
    try:
        if fechar:
            conn = sqlite3.connect(DB_PATH, timeout=10)
        try:
            return conn.execute(
                "SELECT 1 FROM notificacoes_whatsapp WHERE origem = ? AND assinatura = ? "
                "AND situacao = 'enviado' LIMIT 1", (origem, assinatura)).fetchone() is not None
        finally:
            if fechar:
                conn.close()
    except Exception as exc:
        logger.info(f"Historico do WhatsApp ilegivel ({origem}), seguindo sem deduplicar: {exc}")
        return False


def avisar_rotas_fracas(data_alvo, resumo: dict, config: dict, modo_teste: bool = False, **kw) -> str:
    """Uma mensagem por data alvo, sem limite de tempo (spec 6.8): a janela
    de repeticao do despachar so cobre 2h, e o job pode rodar de novo no
    dia seguinte pra mesma data."""
    try:
        if not (resumo.get("juntadas") or resumo.get("seguradas") or resumo.get("sobraram")):
            return "nao_relevante"
        origem, assinatura = "criar_rotas_diarias", f"rotas_fracas:{data_alvo.isoformat()}"
        if _cfg(config).get("ativo") and not modo_teste and _ja_enviada_alguma_vez(kw.get("conn"), origem, assinatura):
            logger.info(f"WhatsApp nao enviado ({origem}): rotas fracas de {data_alvo:%d/%m} ja avisadas.")
            return "nao_enviado"
        return despachar(config, origem, "rotas_fracas", texto_rotas_fracas(data_alvo, resumo),
                         assinatura, modo_teste=modo_teste, **kw)
    except Exception as exc:
        logger.warning(f"Falha na notificacao por WhatsApp (nao afeta a rotina): {exc}")
        return "falhou"


def avisar_insucessos(insucessos: list, config: dict, modo_teste: bool = False, **kw) -> str:
    """Insucessos NOVOS da rodada de expedicao, uma mensagem por rodada.
    Sem regra de repeticao aqui: quem chama so passa o que ainda nao foi
    avisado (fingerprint_notificacao_interna)."""
    try:
        if not insucessos:
            return "nao_relevante"
        return despachar(config, "expedir_pedidos", "insucesso",
                         texto_insucessos(insucessos, kw.get("agora")), modo_teste=modo_teste, **kw)
    except Exception as exc:
        logger.warning(f"Falha na notificacao por WhatsApp (nao afeta a rotina): {exc}")
        return "falhou"


def chamados_ligado(config: dict) -> bool:
    cfg = _cfg(config)
    return bool(cfg.get("ativo") and cfg.get("avisar_chamados", True) and cfg.get("grupo_atendimento_id"))


def clientes_ligado(config: dict) -> bool:
    cfg = _cfg(config)
    return bool(cfg.get("ativo") and _cfg_clientes(config).get("ativo") and integracao_openwa.configurado(cfg))


def _registrar_sem_whatsapp(conn, agora, assinatura: str) -> None:
    fechar = conn is None
    if fechar:
        conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        conn.execute(_SCHEMA)
        _registrar(conn, agora or datetime.now(), ORIGEM_CLIENTE, "chamado", assinatura,
                   "nao_enviado", "numero sem whatsapp")
    finally:
        if fechar:
            conn.close()


def avisar_cliente_sem_resposta(chamado_id: int, msg_id: int, telefone: str, link: str, config: dict,
                                modo_teste: bool = False, **kw) -> str:
    """Aviso direto ao embarcador (avisar_cliente_sem_resposta.py). `msg_id`
    e a mensagem da equipe sem resposta: vira a assinatura `msg:<id>`, que
    a rotina usa pra nunca repetir. Situacoes: desligado | modo_teste |
    numero_sem_whatsapp | indeterminado | nao_enviado | enviado | falhou.
    `indeterminado` = o gateway nao soube dizer se o numero existe: nada e
    gravado, a rotina tenta na proxima rodada. Com clientes.forcar_destino
    a mensagem vai pra esse numero com o destino real na primeira linha."""
    try:
        telefone = re.sub(r"\D", "", str(telefone or ""))
        if not clientes_ligado(config) or not telefone:
            return "desligado"
        texto = texto_cliente_sem_resposta(chamado_id, link)
        assinatura = f"msg:{msg_id}"
        destino = re.sub(r"\D", "", str(_cfg_clientes(config).get("forcar_destino") or ""))
        if destino:
            texto = f"[teste → +{telefone}]\n{texto}"
        else:
            destino = telefone
        if not modo_teste:
            existe = integracao_openwa.numero_existe(_cfg(config), destino)
            if existe is None:
                logger.warning(f"WhatsApp nao enviado (chamado #{chamado_id}): gateway nao confirmou o numero.")
                return "indeterminado"
            if not existe:
                _registrar_sem_whatsapp(kw.get("conn"), kw.get("agora"), assinatura)
                logger.info(f"WhatsApp nao enviado (chamado #{chamado_id}): numero sem WhatsApp.")
                return "numero_sem_whatsapp"
        return despachar(config, ORIGEM_CLIENTE, "chamado", texto, assinatura, modo_teste=modo_teste,
                         grupo_id=f"{destino}@c.us", **kw)
    except Exception as exc:
        logger.warning(f"Falha na notificacao por WhatsApp (nao afeta a rotina): {exc}")
        return "falhou"


def avisar_cliente_fora_dia_fixo(codigo: str, telefone: str, texto: str, config: dict,
                                 modo_teste: bool = False, **kw) -> str:
    """Aviso direto ao embarcador (roteirizacao/notificar_fora_dia_fixo.py):
    data escolhida fora do dia de visita virou envio dedicado. Sem janela de
    repeticao (origem de cliente): quem chama garante um aviso por pedido.
    Com clientes.forcar_destino a mensagem vai pra esse numero com o destino
    real na primeira linha."""
    try:
        telefone = re.sub(r"\D", "", str(telefone or ""))
        if not clientes_ligado(config) or not telefone:
            return "desligado"
        destino = re.sub(r"\D", "", str(_cfg_clientes(config).get("forcar_destino") or ""))
        if destino:
            texto = f"[teste → +{telefone}]\n{texto}"
        else:
            destino = telefone
        if not modo_teste:
            existe = integracao_openwa.numero_existe(_cfg(config), destino)
            if existe is None:
                logger.warning(f"WhatsApp nao enviado (fora do dia fixo {codigo}): gateway nao confirmou o numero.")
                return "indeterminado"
            if not existe:
                logger.info(f"WhatsApp nao enviado (fora do dia fixo {codigo}): numero sem WhatsApp.")
                return "numero_sem_whatsapp"
        return despachar(config, ORIGEM_CLIENTE, "fora_dia_fixo", texto, f"fora_dia_fixo:{codigo}",
                         modo_teste=modo_teste, grupo_id=f"{destino}@c.us", **kw)
    except Exception as exc:
        logger.warning(f"Falha na notificacao por WhatsApp (nao afeta a rotina): {exc}")
        return "falhou"


def avisar_chamado(chamado: dict, link: str, config: dict, **kw) -> str:
    """Chamado que passou a depender de gente (portal_cliente/chamados.py).
    Vai pro grupo do atendimento, NUNCA pro de alertas; um aviso por chamado
    dentro da janela de repeticao."""
    try:
        if not chamados_ligado(config):
            return "desligado"
        return despachar(config, "atendimento", "chamado", texto_chamado(chamado, link, kw.get("agora")),
                         f"chamado:{chamado['id']}", grupo_id=_cfg(config)["grupo_atendimento_id"], **kw)
    except Exception as exc:
        logger.warning(f"Falha na notificacao por WhatsApp (nao afeta a rotina): {exc}")
        return "falhou"


def avisar_informativo_mensal(titulo_mes: str, feriados: list, link: str, config: dict,
                              modo_teste: bool = False, **kw) -> str:
    """Dia 1 (gerar_informativo_mensal.py): uma mensagem por mês no grupo."""
    try:
        return despachar(config, "informativo_mensal", "informativo_mensal",
                         texto_informativo_mensal(titulo_mes, feriados, link, kw.get("agora")),
                         assinatura=f"informativo:{titulo_mes}", modo_teste=modo_teste, **kw)
    except Exception as exc:
        logger.warning(f"Falha na notificacao por WhatsApp (nao afeta a rotina): {exc}")
        return "falhou"


def avisar_clientes_agenda(novos: int, total: int, link: str, config: dict, modo_teste: bool = False, **kw) -> str:
    """Pendentes novos da rodada de marcar_clientes_agenda.py, uma mensagem
    por rodada, so quando entrou alguem."""
    try:
        if not novos:
            return "nao_relevante"
        return despachar(config, "marcar_clientes_agenda", "clientes_agenda",
                         texto_clientes_agenda(novos, total, link, kw.get("agora")), modo_teste=modo_teste, **kw)
    except Exception as exc:
        logger.warning(f"Falha na notificacao por WhatsApp (nao afeta a rotina): {exc}")
        return "falhou"
