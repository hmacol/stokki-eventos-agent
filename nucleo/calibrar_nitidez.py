# -*- coding: utf-8 -*-
"""
nucleo/calibrar_nitidez.py

Escolhe o `nitidez_minima` da validação automática de fotos
(nucleo/validacao_fotos.py) medindo fotos REAIS em vez de chutar.

Mede a variância do Laplaciano de cada foto e, pra cada uma, a mesma
foto com desfoque gaussiano crescente -- o desfoque simula o "tremido"
que a gente quer derrubar. O limiar bom fica entre o pior valor das
fotos boas e o melhor valor das borradas.

    py -3.11 nucleo/calibrar_nitidez.py                      # dados/comprovantes (fotos do app)
    py -3.11 nucleo/calibrar_nitidez.py --pasta dados/canhotos   # PDFs do VUUPT
    py -3.11 nucleo/calibrar_nitidez.py --limite 100

Medição de 12/09 com 40 canhotos do VUUPT: fotos boas de 158 a 2.390
(mediana ~680); com desfoque de raio 3 caíram pra 4-50. Daí o padrão 40.
Refaça com fotos do PRÓPRIO app (que tira em resolução nativa, qualidade
0.7) antes de ligar a validação em produção.
"""
import argparse
import hashlib
import io
import logging
import statistics
import sys
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))

from nucleo.validacao_fotos import medir_nitidez

logging.getLogger("pypdf").setLevel(logging.ERROR)

EXTENSOES = {".jpg", ".jpeg", ".png", ".webp"}
RAIOS = (1.5, 3.0, 5.0)


def _imagens_do_pdf(caminho: Path, vistos: set) -> list[tuple[str, bytes]]:
    """Fotos embutidas no PDF do /print do VUUPT, em resolução original.
    Mesmo filtro do validar_checklists.py: descarta logotipo e miniatura."""
    from PIL import Image
    from pypdf import PdfReader

    saida = []
    for page in PdfReader(str(caminho)).pages:
        for img in page.images:
            dados = img.data
            digest = hashlib.sha1(dados).hexdigest()
            if digest in vistos:
                continue
            vistos.add(digest)
            try:
                im = Image.open(io.BytesIO(dados))
            except Exception:
                continue
            if min(im.size) < 300 or len(dados) < 30_000:
                continue
            saida.append((f"{caminho.stem} ({im.size[0]}x{im.size[1]})", dados))
    return saida


def _borrar(dados: bytes, raio: float) -> bytes:
    from PIL import Image, ImageFilter
    im = Image.open(io.BytesIO(dados)).convert("RGB").filter(ImageFilter.GaussianBlur(raio))
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=70)
    return buf.getvalue()


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--pasta", default="dados/comprovantes", help="pasta com as fotos (ou PDFs do VUUPT)")
    p.add_argument("--limite", type=int, default=40, help="quantas fotos medir")
    args = p.parse_args()

    pasta = Path(args.pasta)
    if not pasta.is_absolute():
        pasta = _RAIZ / pasta
    if not pasta.exists():
        print(f"Pasta não encontrada: {pasta}")
        return 1

    vistos: set = set()
    amostras: list[tuple[str, bytes]] = []
    for caminho in sorted(pasta.rglob("*")):
        if len(amostras) >= args.limite:
            break
        if caminho.suffix.lower() in EXTENSOES:
            amostras.append((caminho.name, caminho.read_bytes()))
        elif caminho.suffix.lower() == ".pdf":
            try:
                amostras.extend(_imagens_do_pdf(caminho, vistos)[:1])
            except Exception as e:
                print(f"  (pulou {caminho.name}: {e})")
    if not amostras:
        print(f"Nenhuma foto em {pasta}.")
        return 1

    print(f"{'foto':38} {'original':>9}" + "".join(f"{f'blur{r}':>9}" for r in RAIOS))
    boas, borradas = [], []
    for nome, dados in amostras[:args.limite]:
        orig = medir_nitidez(dados)
        if orig is None:
            continue
        linha = [orig]
        for r in RAIOS:
            v = medir_nitidez(_borrar(dados, r))
            linha.append(v if v is not None else 0.0)
        boas.append(orig)
        borradas.append(linha[2])          # raio 3 = o piso do "não dá pra ler"
        print(f"{nome[:38]:38} {orig:9.1f}" + "".join(f"{v:9.1f}" for v in linha[1:]))

    if not boas:
        return 1
    print()
    print(f"Fotos medidas: {len(boas)}")
    print(f"Boas       -> mínimo {min(boas):.1f} | mediana {statistics.median(boas):.1f} | máximo {max(boas):.1f}")
    print(f"Borradas   -> mínimo {min(borradas):.1f} | mediana {statistics.median(borradas):.1f} | máximo {max(borradas):.1f}")
    # Duas sugestões, porque a escolha não é neutra: reprovar foto boa
    # irrita o motorista no cliente, e quem julga legibilidade de verdade
    # é o modelo -- este filtro existe só pra não gastar modelo com foto
    # perdida. Por isso o padrão do projeto (40) é o conservador.
    conservador = max(borradas) * 1.5
    agressivo = (max(borradas) + min(boas)) / 2
    if max(borradas) >= min(boas):
        print(f"\nATENÇÃO: as faixas se sobrepõem (borrada até {max(borradas):.1f}, boa a partir de {min(boas):.1f}).")
        print("Nesse caso use o conservador: o limiar não consegue separar sem reprovar foto boa.")
        agressivo = conservador
    print(f"\nSugestões pra api_motorista.validacao_fotos.nitidez_minima:")
    print(f"  conservador {conservador:.0f}  -- só derruba o que está perdido (recomendado)")
    print(f"  agressivo   {agressivo:.0f}  -- economiza mais chamadas, arrisca reprovar foto boa")
    return 0


if __name__ == "__main__":
    sys.exit(main())
