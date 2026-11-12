# -*- coding: utf-8 -*-
"""
Dados ficticios da gravacao: o cliente "Mercado Exemplo", o dia que a tela
de acompanhamento mostra (no formato de /api/dia, montado pelas proprias
funcoes do dados_cliente) e as NF-e de exemplo do envio. Nada aqui e de
cliente real; codigos de pedido na faixa 90xxx, longe dos reais.
"""
import io
from datetime import date, datetime, timedelta
from pathlib import Path

from aula_portal import RAIZ  # noqa: F401  (garante o sys.path do portal)
import dados_cliente as dc

CNPJ = "47821930000170"   # ficticio, nao existe em producao (conferido 12/11); nunca o 00.000.000/0001-91 do cliente teste
NOME = "Mercado Exemplo"
RAZAO = "MERCADO EXEMPLO LTDA"
EMAIL = "compras@example.com"
SENDER_ID = 900001
STKKC_ID = 99999
# PIN so do banco ficticio, digitado em campo mascarado. Nunca o PIN padrao.
PIN_GRAVACAO = "482915"
MOTIVO_INSUCESSO = 5452   # "Fora do horário de recebimento"

# (codigo, destinatario, endereco, volumes)
_DESTINOS = [
    ("PS-90101", "Empório Boa Mesa", "Rua das Palmeiras, 120 - Vila Mariana, São Paulo - SP, 04101-000", 3),
    ("PS-90102", "Restaurante Sabor da Vila", "Avenida dos Ipês, 845 - Moema, São Paulo - SP, 04077-000", 5),
    ("PS-90103", "Padaria Pão Quente", "Rua do Trigo, 32 - Mooca, São Paulo - SP, 03101-000", 2),
    ("PS-90104", "Hortifruti Vida Verde", "Rua das Hortênsias, 410 - Pinheiros, São Paulo - SP, 05422-000", 8),
    ("PS-90105", "Bistrô Jardim", "Alameda das Acácias, 77 - Jardins, São Paulo - SP, 01415-000", 2),
    ("PS-90106", "Café Central", "Rua da Estação, 15 - Santana, São Paulo - SP, 02012-000", 1),
    ("PS-90107", "Lanchonete Ponto Certo", "Rua dos Girassóis, 230 - Tatuapé, São Paulo - SP, 03310-000", 4),
    ("PS-90108", "Mercearia Santa Luz", "Rua do Comércio, 501 - Lapa, São Paulo - SP, 05060-000", 6),
    ("PS-90109", "Cantina Nona Rosa", "Rua das Videiras, 66 - Bela Vista, São Paulo - SP, 01313-000", 3),
    ("PS-90110", "Doceria Açúcar Fino", "Rua dos Cravos, 19 - Perdizes, São Paulo - SP, 05015-000", 2),
    ("PS-90111", "Armazém do Bairro", "Avenida das Flores, 1200 - Saúde, São Paulo - SP, 04140-000", 7),
    ("PS-90112", "Pizzaria Forno a Lenha", "Rua do Moinho, 88 - Ipiranga, São Paulo - SP, 04210-000", 3),
]
_ROTAS = {"R1": ("Rota 12", "Carlos Souza", "FLX1A23"), "R2": ("Rota 15", "Ana Lima", "FLX2B45")}


def _servico(i: int, criado: datetime) -> dict:
    codigo, nome, endereco, vol = _DESTINOS[i]
    return {"id": 9000 + i, "sender_id": SENDER_ID, "code": codigo, "address": endereco,
            "customer": {"name": nome, "phone_number": "11999990000"},
            "dimension_3": vol, "created_at": criado.strftime("%Y-%m-%d %H:%M:%S"), "note": ""}


def _nfs() -> dict[str, str]:
    return {dc.codigo_base(c): str(5100 + n) for n, (c, *_r) in enumerate(_DESTINOS)}


def _na_rota(rota: str, ordem: int, total: int, dia: date) -> dict:
    nome, motorista, placa = _ROTAS[rota]
    return {"rota": nome, "motorista": motorista, "placa": placa, "ordem": ordem, "total_paradas": total,
            "data": dia.isoformat(), "data_rotulo": f"{dc._dia_semana(dia)} {dc._ddmm(dia)}"}


def dia(hoje: date, dias: int | None = None) -> dict:
    """O que /api/dia devolve pro Mercado Exemplo num dia movimentado:
    entregues, em rota, programados, aguardando saida, um insucesso de hoje
    pedindo resposta, uma reentrega (historico de tentativas) e um agendado."""
    ontem = hoje - timedelta(days=1)
    cedo = datetime.combine(ontem, datetime.min.time()).replace(hour=16)
    nfs = _nfs()
    s = [_servico(i, cedo) for i in range(len(_DESTINOS))]
    linhas = [
        dc._linha(s[0], "entregue", nfs, **_na_rota("R1", 1, 14, hoje), detalhe="Entregue 08:52",
                  concluido_em="08:52", comprovante=True),
        dc._linha(s[1], "entregue", nfs, **_na_rota("R1", 2, 14, hoje), detalhe="Entregue 09:31",
                  concluido_em="09:31", comprovante=True),
        dc._linha(s[2], "entregue", nfs, **_na_rota("R2", 1, 11, hoje), detalhe="Entregue 09:05",
                  concluido_em="09:05", comprovante=True),
        dc._linha(s[3], "em_rota", nfs, **_na_rota("R1", 5, 14, hoje), detalhe="Parada 5 de 14 · 4 já feitas"),
        dc._linha(s[4], "em_rota", nfs, **_na_rota("R1", 7, 14, hoje), detalhe="Parada 7 de 14 · 4 já feitas"),
        dc._linha(s[5], "em_rota", nfs, **_na_rota("R2", 4, 11, hoje), detalhe="Parada 4 de 11 · 3 já feitas"),
        dc._linha(s[6], "insucesso", nfs, **_na_rota("R2", 2, 11, hoje),
                  detalhe=dc.texto_do_motivo(MOTIVO_INSUCESSO), motivo=dc.texto_do_motivo(MOTIVO_INSUCESSO),
                  failed_reason_id=MOTIVO_INSUCESSO),
        dc._linha(s[7], "programado", nfs, **_na_rota("R2", 9, 11, hoje), detalhe="Parada 9 de 11 · saída 13:30"),
        dc._linha(s[8], "aguardando_saida", nfs, detalhe="Aguardando roteirização"),
        dc._linha(s[9], "aguardando_saida", nfs, detalhe="Aguardando roteirização"),
        # reentrega: ontem nao entregou, hoje esta na rota de novo
        dc._linha(s[10], "insucesso", nfs, **_na_rota("R1", 9, 13, ontem),
                  detalhe=dc.texto_do_motivo(5433), motivo=dc.texto_do_motivo(5433), failed_reason_id=5433),
        dc._linha({**s[10], "id": 9100, "code": "PS-90111-R1"}, "em_rota", nfs, **_na_rota("R1", 11, 14, hoje),
                  detalhe="Parada 11 de 14 · 4 já feitas"),
        dc._linha(s[0] | {"id": 9200, "code": "PS-90091"}, "entregue", nfs, **_na_rota("R2", 3, 10, ontem),
                  detalhe="Entregue 10:14", concluido_em="10:14", comprovante=True),
    ]
    agendada = hoje + timedelta(days=4)
    futuros = [dc._linha(s[11], "agendado", nfs, agendado_para=agendada.isoformat(),
                         detalhe=f"Agendado {dc._dia_semana(agendada)} {dc._ddmm(agendada)} · 14:00")]
    pedidos, futuros = dc._unir_com_futuros(dc._carimbar_dia(linhas, hoje), futuros, hoje)
    pedidos.sort(key=lambda p: (dc.ORDEM_SITUACAO.get(p["situacao"], 9), p.get("rota") or "",
                                p.get("ordem") or 0, p["codigo"]))
    pedidos.sort(key=lambda p: p["data"], reverse=True)
    atencao = [{"sender_id": SENDER_ID, "failed_reason_id": MOTIVO_INSUCESSO,
                "motivo": dc.texto_do_motivo(MOTIVO_INSUCESSO), "pergunta": dc.pergunta_do_motivo(MOTIVO_INSUCESSO),
                "codigos": ["#" + s[6]["code"]], "service_ids": [s[6]["id"]],
                "desde": f"{hoje.isoformat()} 10:12:00"}]
    do_dia = [p for p in pedidos if p["data"] == hoje.isoformat()]
    dias = dias if dias in dc.DIAS_JANELA_OPCOES else dc.DIAS_JANELA
    de = hoje - timedelta(days=dias - 1)
    return {
        "data": hoje.isoformat(),
        "data_rotulo": f"{dc._dia_semana(hoje)} {hoje.strftime('%d/%m/%Y')}",
        "hoje": True,
        "atualizado_em": "10:30",
        "kpis": dc._kpis(do_dia, atencao),
        "periodo": {"dias": dias, "de": de.isoformat(), "ate": hoje.isoformat(),
                    "rotulo": f"Últimos {dias} dias", "intervalo": f"{dc._ddmm(de)} a {dc._ddmm(hoje)}",
                    "total": len(pedidos), "entregues": sum(p["situacao"] == "entregue" for p in pedidos),
                    "insucessos": sum(p["situacao"] == "insucesso" for p in pedidos)},
        "pedidos": pedidos,
        "atencao": atencao,
        "aguardando_saida": [p for p in do_dia if p["situacao"] in ("aguardando_saida", "programado")],
        "proximos_dias": dc._proximos_dias(futuros),
        "agendados_futuros": futuros,
        "historico": {"dias": 30, "entregues": 412, "insucessos": 9, "primeira_tentativa_pct": 97},
        "empresas": [{"sender_id": SENDER_ID, "nome": NOME}],
        "alteracao_entrega": True,
    }


def alteracao(service_id: int) -> dict:
    """O que GET /api/pedidos/<id>/alteracao devolve pro pedido programado
    (PS-90108): o que esta combinado hoje e que da pra pedir mudanca."""
    _codigo, nome, endereco, _vol = _DESTINOS[7]
    return {"pode": True, "motivo": "", "em_rota": False, "destinatario": nome,
            "atual": {"endereco": endereco.rsplit(",", 1)[0], "data": date.today().isoformat(),
                      "horario": {"inicio": "08:00", "fim": "18:00"}}}


# ── NF-e de exemplo (envio de pedidos) ─────────────────────────────────────────

_NFE = """<?xml version="1.0" encoding="UTF-8"?>
<nfeProc xmlns="http://www.portalfiscal.inf.br/nfe" versao="4.00"><NFe><infNFe Id="NFe{chave}" versao="4.00">
<ide><cUF>35</cUF><natOp>VENDA</natOp><mod>55</mod><serie>1</serie><nNF>{numero}</nNF><dhEmi>{emitida}</dhEmi><tpNF>1</tpNF></ide>
<emit><CNPJ>{emitente}</CNPJ><xNome>{emitente_nome}</xNome><xFant>{emitente_nome}</xFant></emit>
<dest><CNPJ>{dest_doc}</CNPJ><xNome>{dest_nome}</xNome><enderDest><xLgr>{lgr}</xLgr><nro>{nro}</nro><xBairro>{bairro}</xBairro>
<cMun>3550308</cMun><xMun>São Paulo</xMun><UF>SP</UF><CEP>{cep}</CEP><fone>11999990000</fone></enderDest></dest>
<det nItem="1"><prod><cProd>MX-001</cProd><xProd>Produto de exemplo</xProd></prod></det>
<total><ICMSTot><vNF>{valor}</vNF></ICMSTot></total>
<transp><modFrete>0</modFrete><vol><qVol>{volumes}</qVol><pesoB>{peso}</pesoB></vol></transp>
</infNFe></NFe></nfeProc>
"""

# (numero, destinatario, doc, logradouro, nro, bairro, cep, volumes, emitente)
_NOTAS = [
    ("5201", "Restaurante Sabor da Vila", "11222333000181", "Avenida dos Ipês", "845", "Moema", "04077000", 4, CNPJ),
    ("5202", "Padaria Pão Quente", "44555666000172", "Rua do Trigo", "32", "Mooca", "03101000", 2, CNPJ),
    # emitida por outro CNPJ: a previa recusa ("nao e o da sua empresa")
    ("5203", "Bistrô Jardim", "77888999000163", "Alameda das Acácias", "77", "Jardins", "01415000", 1,
     "11111111000191"),
]


def _chave(numero: str, emitente: str) -> str:
    base = f"3526110{emitente}55001{int(numero):09d}1"
    return (base + "0" * 44)[:44]


def xmls_nfe(pasta: Path) -> list[Path]:
    """Grava as 3 NF-e de exemplo em `pasta/nfe/` e devolve os caminhos: duas
    boas e a ultima com erro (emitente de outra empresa)."""
    destino = Path(pasta) / "nfe"
    destino.mkdir(parents=True, exist_ok=True)
    emitida = datetime.now().replace(hour=8, minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:%S-03:00")
    caminhos = []
    for numero, nome, doc, lgr, nro, bairro, cep, vol, emitente in _NOTAS:
        xml = _NFE.format(chave=_chave(numero, emitente), numero=numero, emitida=emitida, emitente=emitente,
                          emitente_nome=RAZAO if emitente == CNPJ else "OUTRA EMPRESA LTDA",
                          dest_doc=doc, dest_nome=nome, lgr=lgr, nro=nro, bairro=bairro, cep=cep,
                          valor="890.00", volumes=vol, peso=f"{vol * 4.5:.1f}")
        caminho = destino / f"NF-{numero}.xml"
        caminho.write_text(xml, encoding="utf-8")
        caminhos.append(caminho)
    return caminhos


def boleto_pdf(pasta: Path) -> Path:
    """Boleto ficticio da NF 5201 (primeira nota boa). O portal casa o PDF com
    o pedido pelo numero no nome do arquivo."""
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (1240, 560), "white")
    d = ImageDraw.Draw(im)
    d.rectangle((40, 40, 1200, 520), outline="black", width=3)
    d.text((70, 70), "BOLETO DE EXEMPLO - SEM VALOR", fill="black")
    d.text((70, 120), f"Beneficiario: {RAZAO}", fill="black")
    d.text((70, 160), "Pagador: Restaurante Sabor da Vila", fill="black")
    d.text((70, 200), "Referente a NF 5201  -  Valor R$ 890,00", fill="black")
    for i in range(60):  # "codigo de barras"
        d.rectangle((70 + i * 17, 380, 70 + i * 17 + (4 if i % 3 else 9), 480), fill="black")
    destino = Path(pasta) / "nfe"
    destino.mkdir(parents=True, exist_ok=True)
    caminho = destino / "boleto-NF-5201.pdf"
    im.save(caminho, "PDF")
    return caminho


def canhoto_png(nf: str) -> bytes:
    """Foto de canhoto ficticia: papel com o texto do recibo, NF, data e uma
    assinatura rabiscada."""
    from PIL import Image, ImageDraw, ImageFont

    def fonte(tam):
        for nome in ("segoeui.ttf", "arial.ttf"):
            try:
                return ImageFont.truetype(nome, tam)
            except OSError:
                continue
        return ImageFont.load_default()

    im = Image.new("RGB", (1100, 520), (244, 241, 232))
    d = ImageDraw.Draw(im)
    d.rectangle((30, 30, 1070, 490), outline=(60, 60, 60), width=3)
    d.line((30, 150, 1070, 150), fill=(60, 60, 60), width=2)
    d.line((760, 30, 760, 490), fill=(60, 60, 60), width=2)
    d.text((50, 46), f"RECEBEMOS DE {RAZAO}", font=fonte(24), fill=(30, 30, 30))
    d.text((50, 80), "OS PRODUTOS CONSTANTES DA NOTA FISCAL", font=fonte(24), fill=(30, 30, 30))
    d.text((50, 114), "INDICADA AO LADO", font=fonte(24), fill=(30, 30, 30))
    d.text((790, 50), "NF-e", font=fonte(30), fill=(30, 30, 30))
    d.text((790, 92), f"Nº {nf}  Série 1", font=fonte(26), fill=(30, 30, 30))
    d.text((50, 170), "DATA DE RECEBIMENTO", font=fonte(20), fill=(90, 90, 90))
    d.text((50, 210), date.today().strftime("%d/%m/%Y"), font=fonte(40), fill=(20, 40, 120))
    d.text((360, 170), "IDENTIFICAÇÃO E ASSINATURA DO RECEBEDOR", font=fonte(20), fill=(90, 90, 90))
    pontos = [(380 + i * 9, 330 + int(28 * ((-1) ** i) * (0.4 + (i % 5) / 6))) for i in range(36)]
    d.line(pontos, fill=(20, 40, 120), width=4, joint="curve")
    d.text((380, 400), "Recebido por: equipe do cliente", font=fonte(24), fill=(20, 40, 120))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def exportacao_xlsx() -> bytes:
    """Planilha que o botao Exportar baixa (o download nao aparece no video)."""
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append(["Pedido", "Destinatário", "Situação"])
    for codigo, nome, *_r in _DESTINOS:
        ws.append([codigo, nome, "Entregue"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
