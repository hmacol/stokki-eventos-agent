# -*- coding: utf-8 -*-
"""
nucleo/motoristas_cli.py

Administração do cadastro de login do app (tabela `motoristas`) e do
piloto -- Fase B do DOC_EXECUCAO_CLAUDE_APP_MOTORISTAS.md.

    # usuário de TESTE do Hugo (agent_id fictício, nunca colide com a VUUPT)
    py -3.11 nucleo/motoristas_cli.py criar --cpf 00000000000 --nome "Hugo (teste)" --pin 123456 --perfil TESTE --agent-id 999001

    # replica uma rota real (já espelhada pelo sync/painel) como rota APP do usuário de teste
    py -3.11 nucleo/motoristas_cli.py replicar-rota --vuupt-route-id 5129058 --para-cpf 00000000000
    py -3.11 nucleo/motoristas_cli.py replicar-rota --rota-id 17 --para-cpf 00000000000 --data 2026-08-27

    # motoristas reais: importa da planilha BD_MOTORISTAS.xlsx (só quem tem CPF), PIN aleatório impresso UMA vez
    py -3.11 nucleo/motoristas_cli.py importar-planilha
    py -3.11 nucleo/motoristas_cli.py resetar-pin --cpf 12345678901 --pin 654321
    py -3.11 nucleo/motoristas_cli.py listar
"""
import argparse
import json
import secrets
import sys
from datetime import date
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from nucleo import auth_motorista as auth, banco
from nucleo.rotas import registrar_evento

AGENT_ID_TESTE_PADRAO = 999001


def _config() -> dict:
    import yaml
    with open(_RAIZ / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def cmd_criar(args):
    conn = banco.conectar()
    try:
        agent_id = args.agent_id
        if agent_id is None and args.perfil == auth.PERFIL_TESTE:
            agent_id = AGENT_ID_TESTE_PADRAO
        m = auth.criar_ou_atualizar_motorista(
            conn, args.cpf, args.nome, args.pin, agent_id=agent_id, telefone=args.telefone,
            tipo_veiculo=args.tipo_veiculo, perfil=args.perfil,
        )
        print(f"OK: {m['nome']} (cpf {m['cpf']}, agent_id {m['agent_id']}, perfil {m['perfil']})")
    finally:
        conn.close()


def cmd_resetar_pin(args):
    conn = banco.conectar()
    try:
        m = auth.buscar_motorista(conn, args.cpf)
        if not m:
            print("Motorista não encontrado.")
            return 1
        auth.criar_ou_atualizar_motorista(conn, m["cpf"], m["nome"], args.pin, perfil=m["perfil"] or auth.PERFIL_MOTORISTA)
        print(f"PIN redefinido pra {m['nome']} (sessões antigas invalidadas).")
    finally:
        conn.close()


def cmd_listar(_args):
    conn = banco.conectar()
    try:
        rows = conn.execute("SELECT cpf, nome, agent_id, tipo_veiculo, perfil, ativo, ultimo_login_em, push_token IS NOT NULL AS push FROM motoristas ORDER BY nome").fetchall()
        for r in rows:
            print(f"{r['cpf']}  {r['nome']:35s} agent={str(r['agent_id']):7s} veic={str(r['tipo_veiculo']):8s} "
                  f"{r['perfil']:9s} ativo={r['ativo']} login={r['ultimo_login_em'] or '-':19s} push={'sim' if r['push'] else 'não'}")
        print(f"{len(rows)} motorista(s).")
    finally:
        conn.close()


def cmd_importar_planilha(args):
    """Cria login pra cada motorista ATIVO da planilha que tem CPF (11
    dígitos). Quem já existe só tem agent_id/telefone/tipo atualizados
    (PIN preservado). PIN novo é aleatório e impresso UMA vez -- Hugo
    repassa ao motorista; depois só via resetar-pin."""
    from regras.preferencias_motoristas import CatalogoMotoristas
    cfg = _config().get("motoristas", {})
    catalogo = CatalogoMotoristas.carregar(cfg.get("planilha", ""), cfg.get("json_fallback", ""))
    conn = banco.conectar()
    try:
        novos, atualizados, sem_cpf = [], 0, []
        for m in catalogo.motoristas:
            if not m.ativo:
                continue
            if not m.cpf:
                sem_cpf.append(m.nome)
                continue
            existente = auth.buscar_motorista(conn, m.cpf)
            pin = None if existente else (args.pin_inicial or f"{secrets.randbelow(10**6):06d}")
            auth.criar_ou_atualizar_motorista(
                conn, m.cpf, m.nome, pin, agent_id=m.agent_id, vehicle_id=m.vehicle_id, telefone=m.telefone,
                email=m.email, tipo_veiculo=m.tipo_veiculo, perfil=auth.PERFIL_MOTORISTA,
            )
            if existente:
                atualizados += 1
            else:
                novos.append((m.nome, m.cpf, pin))
        print(f"Atualizados: {atualizados}. Novos: {len(novos)}. Sem CPF na planilha (ignorados): {len(sem_cpf)}.")
        for nome, cpf, pin in novos:
            print(f"  NOVO  {nome:35s} cpf={cpf}  PIN={pin}   <-- anotar, não aparece de novo")
        for nome in sem_cpf:
            print(f"  SEM CPF  {nome}")
    finally:
        conn.close()


def cmd_replicar_rota(args):
    """Copia uma rota do núcleo (espelho VUUPT ou outra APP) como rota
    NOVA com provedor=APP pro motorista de teste: paradas zeradas
    (PENDENTE), nome prefixado [TESTE], data de hoje por padrão. A rota
    original não é tocada."""
    conn = banco.conectar()
    try:
        alvo = auth.buscar_motorista(conn, args.para_cpf)
        if not alvo:
            print("Motorista de destino não encontrado -- crie com `criar` antes.")
            return 1
        if alvo.get("agent_id") is None:
            print("Motorista de destino sem agent_id.")
            return 1
        if args.vuupt_route_id:
            origem = conn.execute("SELECT * FROM nucleo_rotas WHERE vuupt_route_id = ?", (args.vuupt_route_id,)).fetchone()
        else:
            origem = conn.execute("SELECT * FROM nucleo_rotas WHERE id = ?", (args.rota_id,)).fetchone()
        if not origem:
            print("Rota de origem não está no núcleo -- rode nucleo/sincronizar_vuupt.py --data <dia> antes.")
            return 1
        data_rota = args.data or date.today().isoformat()
        start_at = f"{data_rota}{(origem['start_at'] or ' 07:00:00')[10:]}"
        cur = conn.execute("""
            INSERT INTO nucleo_rotas (data_rota, nome, provedor, vuupt_route_id, rascunho_id, agent_id, vehicle_id,
                                      motorista_nome, motorista_cpf, tipo_veiculo, start_at, start_location_base_id,
                                      end_location_base_id, km_estimado, km_fonte, status, total_paradas, dados_json)
            VALUES (?, ?, 'APP', NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PLANEJADA', ?, ?)
        """, (
            data_rota, f"[TESTE] {origem['nome']}", alvo["agent_id"], alvo.get("vehicle_id"), alvo["nome"], alvo["cpf"],
            origem["tipo_veiculo"], start_at, origem["start_location_base_id"], origem["end_location_base_id"],
            origem["km_estimado"], "ESTIMADO" if origem["km_estimado"] is not None else None, 0,
            json.dumps({"replica_de": {"rota_id": origem["id"], "vuupt_route_id": origem["vuupt_route_id"]}}),
        ))
        nova_id = cur.lastrowid
        n = 0
        for p in conn.execute("SELECT * FROM nucleo_paradas WHERE rota_id = ? AND situacao != 'CANCELADA' ORDER BY ordem", (origem["id"],)):
            n += 1
            conn.execute("""
                INSERT INTO nucleo_paradas (rota_id, ordem, service_id, codigo, titulo, destinatario_nome, endereco,
                                            latitude, longitude, sender_id, remetente_nome, nivel_dificuldade,
                                            volume_caixas, janela_inicio, janela_fim, situacao, dados_json)
                VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDENTE', ?)
            """, (nova_id, n, p["codigo"], p["titulo"], p["destinatario_nome"], p["endereco"], p["latitude"],
                  p["longitude"], p["sender_id"], p["remetente_nome"], p["nivel_dificuldade"], p["volume_caixas"],
                  p["janela_inicio"], p["janela_fim"], p["dados_json"]))
        conn.execute("UPDATE nucleo_rotas SET total_paradas = ? WHERE id = ?", (n, nova_id))
        registrar_evento(conn, "ROTA_ENVIADA", banco.ORIGEM_PAINEL, rota_id=nova_id, agent_id=alvo["agent_id"],
                         dados={"provedor": "APP", "replica_de": origem["id"]})
        conn.commit()
        print(f"OK: rota APP #{nova_id} '[TESTE] {origem['nome']}' em {data_rota} com {n} parada(s) pra {alvo['nome']}.")
    finally:
        conn.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Cadastro de login do app de motoristas / piloto.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("criar"); p.add_argument("--cpf", required=True); p.add_argument("--nome", required=True)
    p.add_argument("--pin", required=True); p.add_argument("--agent-id", type=int); p.add_argument("--telefone")
    p.add_argument("--tipo-veiculo"); p.add_argument("--perfil", default=auth.PERFIL_MOTORISTA, choices=[auth.PERFIL_MOTORISTA, auth.PERFIL_TESTE])
    p.set_defaults(fn=cmd_criar)

    p = sub.add_parser("resetar-pin"); p.add_argument("--cpf", required=True); p.add_argument("--pin", required=True)
    p.set_defaults(fn=cmd_resetar_pin)

    p = sub.add_parser("listar"); p.set_defaults(fn=cmd_listar)

    p = sub.add_parser("importar-planilha"); p.add_argument("--pin-inicial", help="mesmo PIN pra todos os novos (padrão: aleatório por motorista)")
    p.set_defaults(fn=cmd_importar_planilha)

    p = sub.add_parser("replicar-rota"); g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--vuupt-route-id", type=int); g.add_argument("--rota-id", type=int)
    p.add_argument("--para-cpf", required=True); p.add_argument("--data", help="YYYY-MM-DD (padrão: hoje)")
    p.set_defaults(fn=cmd_replicar_rota)

    args = parser.parse_args(argv)
    return args.fn(args) or 0


if __name__ == "__main__":
    sys.exit(main())
