# -*- coding: utf-8 -*-
"""
metricas_plano.py

Metricas geometricas de um plano de rotas (recalibracao de 18/09, spec
docs/superpowers/specs/2026-09-18-recalibracao-roteirizacao-design.md).
Modulo PURO: recebe listas de coordenadas, nao le banco, nao geocodifica.
Usado pelo replay (replay_rotas.py) e pelos testes; serve pra medir
"espalhamento" (diametro/raio), "sobreposicao" (parada cuja vizinha mais
proxima esta em outra rota; pares de rotas cujas bolhas se cruzam) e
tamanho das rotas.

Todas as distancias sao haversine em km, e o km total e o do trajeto
base -> p1 -> ... -> pN, SEM volta a base (mesma convencao de
roteirizacao_dados.calcular_km_estimado a partir de 18/09).
"""
import itertools
import math

PARADAS_ROTA_PEQUENA = 6      # rota com ate este numero de paradas conta como "pequena"
FATOR_CRUZAMENTO_BOLHAS = 0.8  # bolhas se cruzam se centroides < 0,8 x (raio_a + raio_b)


def _hav(a: tuple[float, float], b: tuple[float, float]) -> float:
    R = 6371.0
    la1, lo1 = math.radians(a[0]), math.radians(a[1])
    la2, lo2 = math.radians(b[0]), math.radians(b[1])
    d = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * R * math.asin(math.sqrt(d))


def _km_rota(pts: list[tuple[float, float]], base: tuple[float, float]) -> float:
    if not pts:
        return 0.0
    total = _hav(base, pts[0])
    for a, b in zip(pts, pts[1:]):
        total += _hav(a, b)
    return total


def _centroide(pts: list[tuple[float, float]]) -> tuple[float, float]:
    return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))


def plano_de_sublotes(sublotes: list[list[dict]], coords_fn) -> list[list[tuple[float, float]]]:
    """Converte sublotes (dicts de servico) em listas de coordenadas na
    ordem dada; parada sem coordenada e descartada da medicao."""
    plano = []
    for sub in sublotes:
        pts = [c for c in (coords_fn(s) for s in sub) if c]
        plano.append(pts)
    return plano


def metricas_plano(rotas: list[list[tuple[float, float]]], base: tuple[float, float],
                   horas: list[float] | None = None, teto_horas: float = 9.0) -> dict:
    """Ver docstring do modulo. `horas` (opcional): duracao estimada por
    rota, na mesma ordem de `rotas`, pra contar rotas acima do teto.
    `rotas_sem_coordenada` sinaliza rotas que ficaram inteiramente vazias
    (todas sem coordenada). A contagem de paradas individuais descartadas nao
    e observavel aqui porque plano_de_sublotes ja as removeu."""

    # Contar rotas descartadas (rotas vazias na entrada) e filtrar
    # pelos mesmos indices
    rotas_sem_coordenada = sum(1 for r in rotas if not r)
    indices_validos = [i for i, r in enumerate(rotas) if r]
    rotas = [rotas[i] for i in indices_validos]

    # Filtrar horas pelos mesmos indices
    if horas:
        horas = [horas[i] for i in indices_validos]

    n_rotas = len(rotas)
    tamanhos = [len(r) for r in rotas]
    paradas = sum(tamanhos)
    vazio = {
        "rotas": n_rotas, "paradas": paradas, "media_paradas": 0.0, "min_paradas": 0, "max_paradas": 0,
        "rotas_pequenas": 0, "diametro_mediano_km": 0.0, "diametro_max_km": 0.0, "raio_medio_km": 0.0,
        "km_total": 0.0, "cruzadas": 0, "cruzadas_pct": 0.0, "pares_cruzados": 0, "rotas_acima_teto": 0,
        "rotas_sem_coordenada": rotas_sem_coordenada,
    }
    if not rotas:
        return vazio

    diametros, raios, centroides = [], [], []
    for pts in rotas:
        diametros.append(max((_hav(a, b) for a, b in itertools.combinations(pts, 2)), default=0.0))
        c = _centroide(pts)
        centroides.append(c)
        raios.append(max(_hav(c, p) for p in pts))

    todas = [(p, i) for i, pts in enumerate(rotas) for p in pts]
    cruzadas = 0
    if len(todas) >= 2:
        for k, (p, i) in enumerate(todas):
            melhor = None
            for m, (q, j) in enumerate(todas):
                if m == k:
                    continue
                d = _hav(p, q)
                if melhor is None or d < melhor[0]:
                    melhor = (d, j)
            if melhor is not None and melhor[1] != i:
                cruzadas += 1

    pares = 0
    for a, b in itertools.combinations(range(n_rotas), 2):
        if _hav(centroides[a], centroides[b]) < (raios[a] + raios[b]) * FATOR_CRUZAMENTO_BOLHAS:
            pares += 1

    ordenados = sorted(diametros)
    acima = sum(1 for h in (horas or []) if h > teto_horas)
    return {
        "rotas": n_rotas,
        "paradas": paradas,
        "media_paradas": paradas / n_rotas,
        "min_paradas": min(tamanhos),
        "max_paradas": max(tamanhos),
        "rotas_pequenas": sum(1 for t in tamanhos if t <= PARADAS_ROTA_PEQUENA),
        "diametro_mediano_km": ordenados[len(ordenados) // 2],
        "diametro_max_km": ordenados[-1],
        "raio_medio_km": sum(raios) / n_rotas,
        "km_total": sum(_km_rota(pts, base) for pts in rotas),
        "cruzadas": cruzadas,
        "cruzadas_pct": (cruzadas / paradas * 100.0) if paradas else 0.0,
        "pares_cruzados": pares,
        "rotas_acima_teto": acima,
        "rotas_sem_coordenada": rotas_sem_coordenada,
    }


def formatar_metricas(m: dict, titulo: str = "") -> str:
    """Uma linha, pra tabela do replay e pro log."""
    prefixo = f"{titulo}: " if titulo else ""
    linha = (f"{prefixo}{m['rotas']} rotas | {m['paradas']} paradas | media {m['media_paradas']:.1f} "
            f"(min {m['min_paradas']}, max {m['max_paradas']}) | pequenas {m['rotas_pequenas']} | "
            f"diam med {m['diametro_mediano_km']:.1f} km (max {m['diametro_max_km']:.1f}) | "
            f"raio med {m['raio_medio_km']:.1f} km | km {m['km_total']:.0f} | "
            f"cruzadas {m['cruzadas']} ({m['cruzadas_pct']:.0f}%) | pares {m['pares_cruzados']} | "
            f"acima do teto {m['rotas_acima_teto']}")

    if m.get("rotas_sem_coordenada", 0) > 0:
        linha += f" | sem coordenada: {m['rotas_sem_coordenada']} rota(s)"

    return linha
