# -*- coding: utf-8 -*-
"""
Dados ficticios da aula da equipe: motoristas, pedidos, embarcadores e
transportadoras inventados (nenhum existe na producao). Tambem desenha as
"fotos" que o video sobe: pallet, caixa avariada e a folha de coleta
preenchida e assinada.
"""
import io
import math
import random
from datetime import date, datetime, timedelta
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from aula_equipe import RAIZ

PASTA_IMAGENS = RAIZ / "dados" / "aula_equipe_img"
RECEBEDOR = "Ana Lima"
MOTORISTA_COLETA, PLACA_COLETA = "Paulo Mendes", "FRL3A21"

# ── Retornos ───────────────────────────────────────────────────────────────────
# (motorista, cpf, rota, dias atras, [(codigo, destinatario, embarcador, caixas, situacao, motivo, failed_reason_id)])
MOTORISTAS = [
    ("Marcos Ribeiro", "55566677788", "Rota 03 · Centro", 1, [
        ("PS-91188", "Cantina Nona Rosa", "Laticínios Serra Azul", 5, "INSUCESSO", "Sem agendamento", 5807)]),
    ("Carlos Andrade", "11122233344", "Rota 07 · Zona Sul", 0, [
        ("PS-91201", "Restaurante Sabor da Vila", "Hortifruti Vale Verde", 4, "INSUCESSO", "Local fechado", 5431),
        ("PS-91202", "Mercado Bom Preço", "Laticínios Serra Azul", 3, "INSUCESSO", "Cliente ausente", 8490),
        ("PS-91203", "Padaria Pão Quente", "Hortifruti Vale Verde", 6, "PARCIAL", "Produto Divergente", 5732),
        ("PS-91204", "Empório Boa Mesa", "Laticínios Serra Azul", 2, "INSUCESSO", "Fora do horário de recebimento", 5452)]),
    ("Rafael Souza", "99988877766", "Rota 11 · Zona Oeste", 0, [
        ("PS-91215", "Bistrô Jardim", "Doces da Serra", 3, "INSUCESSO", "Cliente sem pedido / Não reconheceu", 5563)]),
]


def paradas_do_video() -> dict[str, int]:
    """codigo -> parada_id, na ordem em que semear_retornos insere (id = posicao)."""
    ids, n = {}, 0
    for _, _, _, _, pedidos in MOTORISTAS:
        for p in pedidos:
            n += 1
            ids[p[0]] = n
    return ids


def semear_retornos(conn, hoje: date | None = None) -> None:
    import retornos
    hoje = hoje or date.today()
    retornos.definir_inicio(conn, hoje - timedelta(days=5))   # o atrasado de ontem tambem aparece
    service = 70100
    for i, (motorista, cpf, rota, dias, pedidos) in enumerate(MOTORISTAS, start=1):
        dia = hoje - timedelta(days=dias)
        cur = conn.execute(
            "INSERT INTO nucleo_rotas (data_rota, nome, provedor, agent_id, motorista_nome, motorista_cpf, status, "
            "total_paradas, insucessos) VALUES (?, ?, 'APP', ?, ?, ?, 'CONCLUIDA', ?, ?)",
            (dia.isoformat(), rota, 9000 + i, motorista, cpf, len(pedidos), len(pedidos)))
        rota_id = cur.lastrowid
        for ordem, (codigo, dest, emb, caixas, situacao, motivo, falha) in enumerate(pedidos, start=1):
            service += 1
            baixa = datetime.combine(dia, datetime.min.time()) + timedelta(hours=10, minutes=35 * ordem + 7 * i)
            conn.execute(
                "INSERT INTO nucleo_paradas (rota_id, ordem, service_id, codigo, destinatario_nome, remetente_nome, "
                "volume_caixas, situacao, motivo_texto, failed_reason_id, completed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (rota_id, ordem, service, codigo, dest, emb, caixas, situacao, motivo, falha,
                 baixa.strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()


# ── Coletas ────────────────────────────────────────────────────────────────────
# (id_stokki, transportadora, embarcador, destinatario, nf, unidades)
FILA_COLETA = [
    (92011, "Transportes Rápido Sul", "Laticínios Serra Azul", "Mercado Bom Preço", "48120", 12),
    (92012, "Transportes Rápido Sul", "Laticínios Serra Azul", "Cantina Nona Rosa", "48121", 8),
    (92013, "Transportes Rápido Sul", "Laticínios Serra Azul", "Empório Boa Mesa", "48122", 5),
    (92014, "Transportes Rápido Sul", "Laticínios Serra Azul", "Padaria Pão Quente", "48123", 9),
    (92020, "Cliente retira", "Hortifruti Vale Verde", "Restaurante Sabor da Vila", "31507", 4),
]
SAEM_NA_COLETA = [92011, 92012, 92013]   # marcados na folha
NAO_SAIU = 92013                          # desmarcado no celular da doca


def semear_coletas(conn, agora: datetime | None = None) -> None:
    from coletas import banco
    agora = agora or datetime.now()
    visto = (agora - timedelta(hours=2, minutes=40)).strftime(banco.FMT)
    # NF e destinatario vem do envio do portal (coletas.regras._extras), quando a tabela existe
    tem_envios = conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'portal_envios'").fetchone()
    for id_stokki, transp, emb, dest, nf, unidades in FILA_COLETA:
        conn.execute("INSERT INTO expedicao_coletas_fila (id_stokki, codigo, embarcador, transportadora, retirada, "
                     "carga_inicial, visto_primeiro_em, visto_ultimo_em) VALUES (?, ?, ?, ?, 1, 0, ?, ?)",
                     (id_stokki, f"PS-{id_stokki}", emb, transp, visto, agora.strftime(banco.FMT)))
        if tem_envios:
            conn.execute("INSERT INTO portal_envios (cnpj_embarcador, chave_nfe, xml_path, numero_nf, destinatario_nome, volumes, "
                         "status, codigo_pedido, criado_em, atualizado_em) VALUES ('47821930000170', ?, '', ?, ?, ?, 'CRIADO', ?, ?, ?)",
                         (f"3526{id_stokki:040d}", nf, dest, unidades, f"PS-{id_stokki}", visto, visto))
    conn.execute("INSERT INTO expedicao_coletas_leituras (quando, resultado, obs) VALUES (?, 'ok', 'aula')",
                 ((agora - timedelta(minutes=4)).strftime(banco.FMT),))
    # uma coleta de ontem, ja concluida: o historico nao abre vazio
    ontem = agora - timedelta(days=1)
    cur = conn.execute(
        "INSERT INTO expedicao_coletas (transportadora, embarcador, status, criada_em, criada_por, confirmada_em, "
        "confirmada_por, motorista_nome, placa) VALUES ('Transportes Rápido Sul', 'Laticínios Serra Azul', 'CONCLUIDA', "
        "?, 'expedicao', ?, 'expedicao', 'Jorge Teixeira', 'GHT7B45')",
        (ontem.replace(hour=9, minute=50).strftime(banco.FMT), ontem.replace(hour=10, minute=20).strftime(banco.FMT)))
    for id_stokki in (91990, 91991):
        conn.execute("INSERT INTO expedicao_coletas_pedidos (coleta_id, id_stokki, codigo, embarcador, estado, mensagem) "
                     "VALUES (?, ?, ?, 'Laticínios Serra Azul', 'EXPEDIDO', 'ok')", (cur.lastrowid, id_stokki, f"PS-{id_stokki}"))
    conn.commit()


# ── Imagens ────────────────────────────────────────────────────────────────────

def _fonte(tamanho: int, nomes=("segoeuib.ttf", "arialbd.ttf")):
    for nome in nomes:
        for pasta in ("C:/Windows/Fonts", "/usr/share/fonts/truetype/dejavu"):
            try:
                return ImageFont.truetype(f"{pasta}/{nome}", tamanho)
            except OSError:
                pass
    return ImageFont.load_default()


def _caixa(d: ImageDraw.ImageDraw, x, y, w, h, prof, cor=(196, 154, 104)):
    escura, clara = tuple(int(c * .78) for c in cor), tuple(min(255, int(c * 1.08)) for c in cor)
    d.polygon([(x, y), (x + prof, y - prof * .6), (x + w + prof, y - prof * .6), (x + w, y)], fill=clara, outline=(120, 90, 60))
    d.polygon([(x + w, y), (x + w + prof, y - prof * .6), (x + w + prof, y + h - prof * .6), (x + w, y + h)], fill=escura,
              outline=(120, 90, 60))
    d.rectangle([x, y, x + w, y + h], fill=cor, outline=(120, 90, 60), width=2)
    d.rectangle([x + w * .42, y, x + w * .58, y + h * .35], fill=(214, 190, 150))       # fita
    d.rectangle([x + w * .12, y + h * .55, x + w * .5, y + h * .8], fill=(245, 245, 240))  # etiqueta
    for k in range(3):
        d.line([x + w * .15, y + h * (.6 + .06 * k), x + w * .45, y + h * (.6 + .06 * k)], fill=(150, 150, 150), width=2)


def _fundo(lado=(1600, 1200)) -> Image.Image:
    img = Image.new("RGB", lado, (128, 130, 134))
    d = ImageDraw.Draw(img)
    for y in range(0, lado[1], 6):   # piso de concreto com degrade
        tom = 118 + int(22 * y / lado[1])
        d.line([0, y, lado[0], y], fill=(tom, tom + 2, tom + 5), width=6)
    d.rectangle([0, 0, lado[0], 360], fill=(176, 180, 186))   # parede
    d.rectangle([0, 340, lado[0], 360], fill=(230, 196, 40))  # faixa amarela
    return img


def foto_pallet(caminho: Path) -> Path:
    img = _fundo()
    d = ImageDraw.Draw(img)
    d.rectangle([330, 930, 1270, 990], fill=(150, 112, 70), outline=(100, 72, 45), width=3)   # pallet
    for x in (350, 760, 1180):
        d.rectangle([x, 990, x + 70, 1040], fill=(130, 96, 60))
    for linha, y in enumerate((690, 450)):
        for col in range(3 - linha):
            _caixa(d, 360 + col * 290 + linha * 140, y, 260, 240, 70)
    img = img.filter(ImageFilter.GaussianBlur(0.8))
    caminho.parent.mkdir(parents=True, exist_ok=True)
    img.save(caminho, "JPEG", quality=88)
    return caminho


def foto_avaria(caminho: Path) -> Path:
    img = _fundo()
    d = ImageDraw.Draw(img)
    _caixa(d, 470, 470, 560, 470, 140)
    # canto amassado e rasgado
    d.polygon([(470, 470), (640, 470), (590, 560), (520, 640), (470, 690)], fill=(88, 62, 40))
    d.line([(640, 470), (590, 560), (520, 640), (470, 690)], fill=(60, 40, 25), width=6)
    random.seed(7)
    for _ in range(14):   # rasgo
        x0, y0 = 600 + random.randint(-40, 40), 520 + random.randint(-40, 60)
        d.line([(x0, y0), (x0 + random.randint(-30, 30), y0 + random.randint(10, 50))], fill=(70, 48, 30), width=3)
    d.ellipse([700, 860, 980, 930], fill=(92, 96, 104))   # mancha de liquido no chao
    img = img.filter(ImageFilter.GaussianBlur(0.8))
    caminho.parent.mkdir(parents=True, exist_ok=True)
    img.save(caminho, "JPEG", quality=88)
    return caminho


def folha_png(pdf: bytes) -> Image.Image:
    import pypdfium2 as pdfium
    pagina = pdfium.PdfDocument(pdf)[0]
    return pagina.render(scale=150 / 72).to_pil().convert("RGB")


def folha_preenchida(pdf: bytes, ids_que_sairam: list[int], pedidos: list[dict]) -> Image.Image:
    """A folha impressa com o que o motorista escreve: os "levou" marcados,
    nome, documento, placa, data e a assinatura, a caneta azul."""
    from coletas import folha_pdf as fp
    img = folha_png(pdf).resize(fp.A4)
    d = ImageDraw.Draw(img)
    caneta, letra = (28, 52, 150), _fonte(34, ("segoesc.ttf", "segoepr.ttf", "arial.ttf"))
    for i, p in enumerate(pedidos):
        if p["id_stokki"] in ids_que_sairam:
            y = fp.Y_LINHAS + i * fp.ALT_LINHA
            x = fp.MARGEM + 28
            d.line([(x + 3, y + 15), (x + 10, y + 24), (x + 26, y + 2)], fill=caneta, width=4)
    y0 = fp.Y_RODAPE + 86
    meio = fp.MARGEM + (fp.A4[0] - 2 * fp.MARGEM) // 2 + 20
    d.text((fp.MARGEM + 200, y0 - 14), MOTORISTA_COLETA, font=letra, fill=caneta)
    d.text((fp.MARGEM + 200, y0 + 62 - 14), "32.415.887-6", font=letra, fill=caneta)
    d.text((meio + 100, y0 + 62 - 14), PLACA_COLETA, font=letra, fill=caneta)
    d.text((fp.MARGEM + 200, y0 + 124 - 14), datetime.now().strftime("%d/%m %H:%M"), font=letra, fill=caneta)
    ya = y0 + 186 + 30 + 18   # assinatura: um rabisco continuo
    pontos = [(fp.MARGEM + 230 + t * 3.2, ya + 14 * math.sin(t / 5.0) - 10 * math.sin(t / 2.1)) for t in range(0, 120)]
    d.line(pontos, fill=caneta, width=4, joint="curve")
    return img


def foto_da_folha(img_folha: Image.Image, caminho: Path) -> Path:
    """A folha "fotografada" na doca: inclinada, sobre a mesa, com sombra."""
    folha = img_folha.resize((860, int(860 * img_folha.height / img_folha.width)))
    fundo = Image.new("RGB", (1200, 1600), (92, 74, 58))
    d = ImageDraw.Draw(fundo)
    for x in range(0, 1200, 9):   # veio da madeira
        tom = 86 + (x * 7919) % 17
        d.line([x, 0, x + 40, 1600], fill=(tom, tom - 16, tom - 30), width=5)
    sombra = Image.new("RGBA", folha.size, (0, 0, 0, 110)).rotate(-3, expand=True, fillcolor=(0, 0, 0, 0))
    fundo.paste(sombra, (182, 168), sombra)
    girada = folha.convert("RGBA").rotate(-3, expand=True, resample=Image.BICUBIC, fillcolor=(0, 0, 0, 0))
    fundo.paste(girada, (160, 140), girada)
    fundo = fundo.filter(ImageFilter.GaussianBlur(0.6))
    caminho.parent.mkdir(parents=True, exist_ok=True)
    fundo.save(caminho, "JPEG", quality=88)
    return caminho


def imagem_png(img: Image.Image) -> bytes:
    saida = io.BytesIO()
    img.save(saida, "PNG")
    return saida.getvalue()
