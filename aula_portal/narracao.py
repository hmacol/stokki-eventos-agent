# -*- coding: utf-8 -*-
"""Narracao da aula: uma fala por passo, voz neural da Thalita (edge-tts),
com cache por hash de voz+texto (regravar nao sintetiza de novo)."""
import asyncio
import hashlib
import re
import subprocess
import time
from pathlib import Path

import edge_tts
import imageio_ffmpeg

VOZ = "pt-BR-ThalitaMultilingualNeural"


def gerar(texto: str, pasta_cache: Path) -> Path:
    pasta_cache = Path(pasta_cache)
    pasta_cache.mkdir(parents=True, exist_ok=True)
    nome = hashlib.sha1(f"{VOZ}|{texto}".encode("utf-8")).hexdigest()[:16]
    caminho = pasta_cache / f"{nome}.mp3"
    if not caminho.exists() or caminho.stat().st_size == 0:
        temporario = caminho.with_suffix(".parcial.mp3")
        for tentativa in range(5):  # o servico da voz as vezes devolve "sem audio" e na seguinte vai
            try:
                asyncio.run(edge_tts.Communicate(texto, VOZ).save(str(temporario)))
                break
            except edge_tts.exceptions.EdgeTTSException:
                if tentativa == 4:
                    raise
                time.sleep(3 * (tentativa + 1))
        temporario.replace(caminho)
    return caminho


def duracao(arquivo: Path) -> float:
    """Segundos do audio/video, lidos do 'Duration:' que o ffmpeg imprime."""
    r = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-i", str(arquivo)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    m = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", r.stderr)
    if not m:
        raise RuntimeError(f"duracao ilegivel: {arquivo}")
    h, mi, s = m.groups()
    return int(h) * 3600 + int(mi) * 60 + float(s)
