# -*- coding: utf-8 -*-
"""
replay_rotas.py

Replay da roteirizacao sobre dias PASSADOS (Hugo, 18/09): le as rotas
ENVIADAS de cada dia (rascunhos_rota/rascunhos_parada de uma copia do
banco de producao), roda o mesmo criar_rotas_diarias.planejar_sublotes
que a producao usa (sem Vuupt, sem geocodificar -- coordenadas ja vem
gravadas -- sem motorista, sem gravar nada, sem escrever no historico)
e compara as metricas (metricas_plano) do "enviado de verdade" com o
"novo". Serve pra calibrar constantes antes de subir.

Copia do banco (na VPS, consistente mesmo com WAL):
    ssh ... "cd /opt/stokki-eventos && sudo -u www-data venv/bin/python -c \\
      \"import sqlite3; s=sqlite3.connect('dados/dados.db'); d=sqlite3.connect('/tmp/dados_replay.db'); s.backup(d)\""
    scp ... root@187.127.52.197:/tmp/dados_replay.db dados/dados_replay.db

COMO USAR (da raiz):
    py -3.11 roteirizacao/replay_rotas.py --de 2026-08-12 --ate 2026-09-19
    py -3.11 roteirizacao/replay_rotas.py --de 2026-08-12 --ate 2026-09-19 --distancia-maxima 12
    py -3.11 roteirizacao/replay_rotas.py --de ... --ate ... --sem-polimento --separar-carga
Saida: tabela no terminal + roteirizacao/dados/replay_resultado.txt

Limite: roteiriza o conjunto que FOI enviado no dia, nao o pool inteiro
que o job viu as 22h (pedido removido/adiado a mao fica de fora). A
comparacao e justa porque os dois lados usam o mesmo conjunto.

LIMITACOES CONHECIDAS:
  1. DATA DE AGENDAMENTO NAO RECONSTRUIDA: a tabela rascunhos_parada nao
     guarda a data de agendamento do pedido. O pipeline usa essa data ao
     decidir se dois pedidos nivel 4 (exclusivos) do mesmo endereco e
     embarcador podem dividir rota (datas diferentes = nao podem). No replay
     essa info nunca existe, entao esses pedidos podem acabar juntos quando
     na operacao real ficariam separados. Escala: ~1% das paradas (31 paradas
     em 3008, espalhadas por 10 dos 31 dias testados). Atenuante: o efeito
     e identico em todas as configs comparadas (nao depende de trava de
     distancia), entao nao altera qual config vence, so desloca o tamanho
     absoluto do lado "novo".

  2. SEGUNDA CONEXAO DE BANCO NAO READ-ONLY: a funcao carregar_tipos_carga_por_sender
     (de regras/) abre o banco pelo caminho sem modo read-only. Na pratica
     ela so executa SELECT, entao nada e escrito, mas a garantia nao esta
     no codigo. Corrigir exigiria mexer em modulo compartilhado, fora do
     escopo deste script.

  3. DIA COM MENOS DE 2 PEDIDOS E PULADO SILENCIOSAMENTE: sem imprimir
     linha nenhuma de resultado nem aviso. Diferente do caminho de erro,
     que imprime linha comecando com ERRO. Quem reusar o script precisa
     saber desse pulo silencioso.
"""
import argparse
import logging
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

_RAIZ_LOCAL = Path(__file__).parent
_RAIZ_PROJETO = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ_PROJETO))
sys.path.insert(0, str(_RAIZ_LOCAL))
sys.path.insert(0, str(_RAIZ_PROJETO / "painel_agentes"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(name)s - %(message)s")
logger = logging.getLogger("replay_rotas")

from regras.tipo_carga_embarcador import carregar_tipos_carga_por_sender, classificar_tipo_carga
import roteirizacao_dados as rd
import metricas_plano as mp

COORDS_BASE_PADRAO = (-23.4930, -46.6640)  # Rua Zilda, Casa Verde Alta (aprox.); use --base pra sobrescrever
ARQUIVO_RESULTADO = _RAIZ_LOCAL / "dados" / "replay_resultado.txt"


def servicos_do_dia(conn: sqlite3.Connection, dia: str, mapa_tipos: dict) -> tuple[list[dict], list[list[dict]]]:
    """(todos os servicos do dia, rotas enviadas na ordem enviada) a
    partir dos rascunhos com status ENVIADO. Dict no formato que o
    pipeline aceita (chaves da Vuupt + as injetadas '_...')."""
    rotas_rows = conn.execute(
        "SELECT id FROM rascunhos_rota WHERE data_alvo = ? AND status = 'ENVIADO' ORDER BY criado_em, id",
        (dia,)).fetchall()
    servicos: list[dict] = []
    rotas: list[list[dict]] = []
    for r in rotas_rows:
        paradas = conn.execute(
            "SELECT * FROM rascunhos_parada WHERE rascunho_id = ? ORDER BY ordem", (r["id"],)).fetchall()
        rota: list[dict] = []
        for p in paradas:
            tipo, _ = classificar_tipo_carga(p["sender_id"], mapa_tipos)
            # Nota: chave '_data_agendamento' ausente de proposito (nao existe em rascunhos_parada).
            # Ver limitacao #1 no docstring do modulo.
            s = {
                "id": p["service_id"], "code": p["codigo"] or "", "title": p["titulo"] or "",
                "address": p["endereco"] or "", "latitude": p["latitude"], "longitude": p["longitude"],
                "sender_id": p["sender_id"], "dimension_3": p["volume_caixas"],
                "customer": {"code": "", "name": p["destinatario_nome"] or ""},
                "_nivel_dificuldade": p["nivel_dificuldade"] or 1, "_tipo_carga": tipo,
                "_janela_inicio": p["janela_inicio"], "_janela_fim": p["janela_fim"],
                "_janela_fonte": p["janela_fonte"],
            }
            rota.append(s)
            servicos.append(s)
        if rota:
            rotas.append(rota)
    return servicos, rotas


def _coords(s: dict):
    return rd.coordenada_embutida(s)


def _horas(sublotes: list[list[dict]]) -> list[float]:
    return [rd.estimar_tempo_rota(sub) if rd.exige_orcamento_horas(sub) else 0.0 for sub in sublotes]


def rodar_dia(servicos: list[dict], rotas_enviadas: list[list[dict]], coords_base: tuple[float, float],
              data_alvo: date, modelo_forcado: str | None = None) -> tuple[dict, dict]:
    """(metricas do enviado, metricas do plano novo) pro mesmo conjunto."""
    import criar_rotas_diarias as crd
    rd.definir_coords_base(*coords_base)
    rd.definir_hora_saida_base(rd.HORA_INICIO_ROTA)
    enviado = mp.metricas_plano(mp.plano_de_sublotes(rotas_enviadas, _coords), coords_base,
                                horas=_horas(rotas_enviadas), teto_horas=rd.ROTA_TEMPO_MAXIMO_HORAS)
    planos = crd.planejar_sublotes([dict(s) for s in servicos], coords_base, None, data_alvo,
                                   sufixo_label=" (replay)", modelo_forcado=modelo_forcado,
                                   registrar_historico=False)
    sublotes = [sub for p in planos for sub in p["sublotes"]]
    novo = mp.metricas_plano(mp.plano_de_sublotes(sublotes, _coords), coords_base,
                             horas=_horas(sublotes), teto_horas=rd.ROTA_TEMPO_MAXIMO_HORAS)
    return enviado, novo


def _somar(acumulado: dict, m: dict) -> None:
    """Soma as metricas somaveis do dia no acumulado. A lista de chaves
    tem que cobrir TODA chave somavel que formatar_metricas imprime --
    inclusive rotas_sem_coordenada, senao o TOTAL levanta KeyError."""
    for chave in ("rotas", "paradas", "rotas_pequenas", "km_total", "cruzadas", "pares_cruzados",
                  "rotas_acima_teto", "rotas_sem_coordenada"):
        acumulado[chave] = acumulado.get(chave, 0) + m[chave]
    acumulado.setdefault("diametros", []).append(m["diametro_mediano_km"])


def _fechar(acumulado: dict) -> dict:
    diam = sorted(acumulado.pop("diametros", [0.0]))
    rotas = acumulado.get("rotas", 0) or 1
    paradas = acumulado.get("paradas", 0)
    return {**acumulado, "media_paradas": paradas / rotas, "min_paradas": 0, "max_paradas": 0,
            "diametro_mediano_km": diam[len(diam) // 2], "diametro_max_km": diam[-1], "raio_medio_km": 0.0,
            "cruzadas_pct": (acumulado.get("cruzadas", 0) / paradas * 100.0) if paradas else 0.0}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Replay da roteirizacao sobre dias passados (somente leitura)")
    parser.add_argument("--de", required=True)
    parser.add_argument("--ate", required=True)
    parser.add_argument("--banco", default=str(_RAIZ_PROJETO / "dados" / "dados_replay.db"))
    parser.add_argument("--base", default=None, help="lat,lng da base (padrao: Rua Zilda aprox.)")
    parser.add_argument("--distancia-maxima", type=float, default=None, help="sobrescreve DISTANCIA_MAXIMA_ROTA_KM")
    parser.add_argument("--sem-polimento", action="store_true")
    parser.add_argument("--separar-carga", action="store_true", help="religa a particao Seco x Refrigerado")
    parser.add_argument("--modelo", default=None, help="forca um esquema (nome como no historico)")
    args = parser.parse_args(argv)

    import criar_rotas_diarias as crd
    if args.distancia_maxima is not None:
        crd.DISTANCIA_MAXIMA_ROTA_KM = args.distancia_maxima
    if args.sem_polimento:
        crd.POLIMENTO_ATIVO = False
    if args.separar_carga:
        crd.SEPARAR_POR_TIPO_CARGA = True
    coords_base = tuple(float(x) for x in args.base.split(",")) if args.base else COORDS_BASE_PADRAO

    conn = sqlite3.connect(f"file:{args.banco}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    # Aviso: segunda conexao de banco nao usa modo read-only (ver limitacao #2 no docstring).
    mapa_tipos = carregar_tipos_carga_por_sender(args.banco)

    cabecalho = (f"Replay {args.de} a {args.ate} | distancia_maxima={crd.DISTANCIA_MAXIMA_ROTA_KM} km | "
                 f"polimento={'off' if args.sem_polimento else 'on'} | separar_carga={'on' if args.separar_carga else 'off'}"
                 f"{' | modelo=' + args.modelo if args.modelo else ''}")
    linhas = [cabecalho, ""]
    total_env: dict = {}
    total_novo: dict = {}
    dia = date.fromisoformat(args.de)
    fim = date.fromisoformat(args.ate)
    while dia <= fim:
        servicos, rotas = servicos_do_dia(conn, dia.isoformat(), mapa_tipos)
        # Aviso: dias com menos de 2 pedidos sao pulados sem imprimir nada (ver limitacao #3 no docstring).
        if len(servicos) >= 2:
            try:
                env, novo = rodar_dia(servicos, rotas, coords_base, dia, args.modelo)
            except Exception as e:
                linhas.append(f"{dia}: ERRO {e}")
                dia += timedelta(days=1)
                continue
            _somar(total_env, env)
            _somar(total_novo, novo)
            linhas.append(mp.formatar_metricas(env, f"{dia} enviado"))
            linhas.append(mp.formatar_metricas(novo, f"{dia} novo   "))
        dia += timedelta(days=1)
    if total_env:
        linhas += ["", mp.formatar_metricas(_fechar(total_env), "TOTAL enviado"),
                   mp.formatar_metricas(_fechar(total_novo), "TOTAL novo   ")]
    texto = "\n".join(linhas)
    print(texto)
    ARQUIVO_RESULTADO.parent.mkdir(parents=True, exist_ok=True)
    with open(ARQUIVO_RESULTADO, "a", encoding="utf-8") as f:
        f.write(texto + "\n\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
