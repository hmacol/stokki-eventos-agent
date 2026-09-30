# -*- coding: utf-8 -*-
"""
portal_cliente/gerenciar_clientes.py

Gestão das contas do portal do cliente pela linha de comando (VPS ou
local -- mesmo dados.db):

    py -3 portal_cliente/gerenciar_clientes.py listar
    py -3 portal_cliente/gerenciar_clientes.py checar maiz            # antes do convite: conta, grupo, insucessos, envio (CNPJ ou nome)
    py -3 portal_cliente/gerenciar_clientes.py enviar-link 29909190000146
    py -3 portal_cliente/gerenciar_clientes.py enviar-link 29909190000146 --para hugo@freshlogbr.com   # convite vai pra quem repassa, não pro cliente
    py -3 portal_cliente/gerenciar_clientes.py definir-pin 29909190000146 123456
    py -3 portal_cliente/gerenciar_clientes.py desativar 29909190000146
    py -3 portal_cliente/gerenciar_clientes.py ativar 29909190000146
    py -3 portal_cliente/gerenciar_clientes.py link 29909190000146   # só imprime o link, sem e-mail

Grupo econômico (17/09) -- um login enxerga os pedidos de várias empresas:
    py -3 portal_cliente/gerenciar_clientes.py grupos                                   # lista os grupos
    py -3 portal_cliente/gerenciar_clientes.py grupo 54993021000184                     # mostra as empresas do login
    py -3 portal_cliente/gerenciar_clientes.py grupo 54993021000184 48654566000163 ...  # DEFINE os membros (substitui a lista)
    py -3 portal_cliente/gerenciar_clientes.py grupo 54993021000184 --desfazer

Máscara de envio de pedidos (08/09):
    py -3 portal_cliente/gerenciar_clientes.py envio 68146976000100                       # mostra os parâmetros da Stokki
    py -3 portal_cliente/gerenciar_clientes.py envio 68146976000100 --regra muai          # regra de transformação do XML
    py -3 portal_cliente/gerenciar_clientes.py envio 68146976000100 --desativar|--ativar  # pausa/libera o envio
    py -3 portal_cliente/gerenciar_clientes.py envio 68146976000100 --carrier 12          # transportadora do wizard Excel (planilha, 09/09)
    py -3 portal_cliente/gerenciar_clientes.py solicitacoes                               # pendências (cancelar/em espera/reagendar)
    py -3 portal_cliente/gerenciar_clientes.py concluir 12 "cancelado na Stokki"         # marca a solicitação 12 como atendida
    py -3 portal_cliente/gerenciar_clientes.py fila                                       # o que está NA_FILA/ENVIANDO/ERRO

Quem pode ter conta é quem está em `interno` com sender_id -- pra liberar
um embarcador novo, primeiro cadastre CNPJ/sender_id/e-mail lá (é a
mesma tabela usada pelas notificações de insucesso).
"""
import argparse
import re
import sys
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(Path(__file__).parent))

import yaml

import auth_cliente as auth
import envio_pedidos as envios


def _comandos_envio(args, cfg: dict) -> int:
    conn = envios.conectar()
    try:
        if args.cmd == "envio":
            mudancas = {}
            if args.regra:
                mudancas["regra_xml"] = args.regra
            if args.warehouse:
                mudancas["warehouse_id"] = args.warehouse
            if args.transporte:
                mudancas["tipo_transporte"] = args.transporte
            if args.embalagem:
                mudancas["embalagem"] = args.embalagem
            if args.carrier is not None:
                mudancas["carrier_id"] = args.carrier
            if args.prioridade is not None:
                mudancas["prioridade"] = args.prioridade
            if args.ativar or args.desativar:
                mudancas["envio_ativo"] = bool(args.ativar)
            if mudancas:
                envios.definir_parametros_cliente(conn, args.cnpj, **mudancas)
            c = envios.config_stokki_cliente(conn, args.cnpj, cfg)
            print(f"{c['nome']} ({auth.formatar_cnpj(c['cnpj'])})")
            print(f"  client_id (interno.stkkc_id): {c['client_id'] or '(FALTA -- cadastre em interno.stkkc_id)'}")
            print(f"  regra_xml: {c['regra_xml']} -- {envios.REGRAS_XML[c['regra_xml']]}")
            print(f"  warehouse_id: {c['warehouse_id']}  transporte: {c['tipo_transporte']}  embalagem: {c['embalagem']}")
            print(f"  planilha (wizard Excel): carrier_id: {c['carrier_id'] or '(automático: ' + (c['carrier_nome'] or 'primeira transportadora do cliente') + ')'}"
                  f"  prioridade: {c['prioridade'] or '(nenhuma)'}")
            print(f"  envio {'ATIVO' if c['envio_ativo'] else 'DESATIVADO'}  e-mails: {', '.join(c['emails']) or '(nenhum)'}")
            return 0
        if args.cmd == "solicitacoes":
            itens = envios.listar_solicitacoes_pendentes(conn)
            if not itens:
                print("Nenhuma solicitação pendente.")
            for s in itens:
                print(f"#{s['id']:<4} {s['criado_em'][:16]}  {envios.ROTULOS_SOLICITACAO.get(s['tipo'], s['tipo']):<26} "
                      f"{envios.rotulo_envio(s):<16} {s['codigo_pedido'] or '(sem código)':<10} {s['destinatario_nome']}  "
                      f"[{s['status_envio']}] {s['detalhes'] or ''}  (por {s['solicitado_por']})")
            return 0
        if args.cmd == "concluir":
            envios.concluir_solicitacao(conn, args.id, args.resposta, recusada=args.recusar)
            print(f"Solicitação #{args.id} marcada como {'RECUSADA' if args.recusar else 'CONCLUÍDA'}.")
            return 0
        if args.cmd == "fila":
            rows = conn.execute("SELECT e.id, e.status, e.criado_em, e.numero_nf, e.referencia, e.destinatario_nome, e.tentativas, e.erro, "
                                "COALESCE(i.apelido, i.nome_remetente, e.cnpj_embarcador) AS emb FROM portal_envios e "
                                "LEFT JOIN interno i ON i.cnpj_embarcador = e.cnpj_embarcador "
                                "WHERE e.status IN ('NA_FILA','ENVIANDO','ERRO') ORDER BY e.status, e.criado_em").fetchall()
            if not rows:
                print("Fila vazia e sem erros.")
            for r in rows:
                print(f"#{r['id']:<5} {r['status']:<9} {r['criado_em'][:16]}  {r['emb'][:28]:<28} {envios.rotulo_envio(dict(r)):<16} "
                      f"{(r['destinatario_nome'] or '')[:30]:<30} tent.{r['tentativas']}  {(r['erro'] or '')[:80]}")
            try:
                from stokki.sessao_uso import em_uso
                print(f"Stokki em uso agora: {em_uso() or 'não'}")
            except Exception:
                pass
            return 0
    finally:
        conn.close()
    return 0


def _config() -> dict:
    with open(_RAIZ / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _link(conn, cfg, cnpj) -> str:
    secret = cfg.get("portal_cliente", {}).get("secret_key")
    if not secret:
        raise SystemExit("portal_cliente.secret_key ausente no config.yaml")
    url_base = (cfg.get("portal_cliente", {}).get("url_base") or "https://app.freshhub.com.br/cliente").rstrip("/")
    return f"{url_base}/definir-pin/{auth.gerar_token_definir_pin(secret, conn, cnpj)}"


def _sem_acento(texto: str) -> str:
    return unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode().lower()


def procurar(conn, termo: str) -> list[dict]:
    """Embarcadores de `interno` pelo CNPJ (com ou sem máscara) ou por
    pedaço do nome, sem ligar pra acento/caixa ("maiz" acha "MAÍZ FOOD")."""
    if len(auth.normalizar_cnpj(termo)) >= 13:
        emb = auth.buscar_embarcador(conn, termo)
        return [emb] if emb else []
    alvo = _sem_acento(termo)
    return [e for e in auth.listar_embarcadores(conn) if alvo in _sem_acento(e["nome"])]


def checar(conn, cnpj: str, agora: datetime | None = None) -> dict:
    """O que conferir antes do convite (24/09): conta, grupo, insucessos
    PENDENTES (aparecem no 1º acesso com botões que agem de verdade),
    stkkc_id/envio (máscara de pedidos) e e-mail repetido em outro
    embarcador (sinal de grupo econômico, como a Marchef)."""
    emb = auth.buscar_embarcador(conn, cnpj)
    agora = agora or datetime.now()
    conta = auth.buscar_conta(conn, emb["cnpj"])
    estado = "sem conta" if not conta else ("ATIVA" if conta["ativo"] else "desativada")
    grupo = auth.empresas_do_login(conn, emb["cnpj"])[1:]
    membro_de = [r["cnpj_login"] for r in conn.execute(
        "SELECT cnpj_login FROM portal_grupos WHERE cnpj_membro = ?", (emb["cnpj"],))]
    meus = {e.lower() for e in emb["emails"]}
    mesmo_email = [e for e in auth.listar_embarcadores(conn)
                   if e["cnpj"] != emb["cnpj"] and meus & {x.lower() for x in e["emails"]}]
    pend = [r["primeira_notificacao_em"] for r in conn.execute(
        "SELECT primeira_notificacao_em FROM insucessos_aguardando_resposta WHERE sender_id = ? AND status = 'PENDENTE' "
        "ORDER BY primeira_notificacao_em", (emb["sender_id"],))]
    corte = (agora - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    velhos = [p for p in pend if p < corte]
    stkkc = conn.execute("SELECT stkkc_id FROM interno WHERE cnpj_embarcador = ?", (emb["cnpj"],)).fetchone()["stkkc_id"]
    envio = conn.execute("SELECT envio_ativo FROM portal_clientes_envio WHERE cnpj = ?", (emb["cnpj"],)).fetchone()
    envio_ativo = bool(envio["envio_ativo"]) if envio else True

    alertas = []
    if conta:
        alertas.append(f"já tem conta ({estado}, último login {conta.get('ultimo_login_em') or '-'}): convite novo troca o PIN")
    if not emb["emails"]:
        alertas.append("sem e-mail em `interno`: só dá pra mandar com --para")
    if pend:
        alertas.append(f"{len(pend)} insucesso(s) PENDENTE(s), {len(velhos)} com mais de 7 dias (desde {pend[0][:10]}): "
                       f"aparecem em 'Precisa da sua atenção' no 1º acesso")
    if not stkkc:
        alertas.append("sem stkkc_id em `interno`: o envio de pedidos (XML/planilha) não funciona")
    elif not envio_ativo:
        alertas.append("envio de pedidos desativado (gerenciar_clientes.py envio <cnpj> --ativar)")
    if membro_de:
        alertas.append("já enxergado pelo grupo de " + ", ".join(auth.formatar_cnpj(c) for c in membro_de)
                       + ": o convite normalmente vai pro login do grupo")
    if mesmo_email and not grupo and not membro_de:
        alertas.append(f"e-mail igual ao de {len(mesmo_email)} outro(s) embarcador(es): confirmar se é grupo econômico")
    return {"emb": emb, "conta": estado, "ultimo_login": (conta or {}).get("ultimo_login_em"),
            "grupo": grupo, "membro_de": membro_de, "mesmo_email": mesmo_email,
            "insucessos_pendentes": len(pend), "insucessos_velhos": len(velhos),
            "stkkc_id": stkkc, "envio_ativo": envio_ativo, "alertas": alertas}


def _imprimir_checagem(r: dict) -> None:
    emb = r["emb"]
    print(f"{emb['nome']} ({auth.formatar_cnpj(emb['cnpj'])})  sender {emb['sender_id']}")
    print(f"  e-mails: {', '.join(emb['emails']) or '(nenhum)'}")
    print(f"  conta no portal: {r['conta']}" + (f", último login {r['ultimo_login'] or '-'}" if r["conta"] != "sem conta" else ""))
    print(f"  insucessos PENDENTES: {r['insucessos_pendentes']} ({r['insucessos_velhos']} com mais de 7 dias)")
    print(f"  envio de pedidos: stkkc_id {r['stkkc_id'] or '(FALTA)'}, {'ativo' if r['envio_ativo'] else 'DESATIVADO'}")
    if r["grupo"]:
        print(f"  login de grupo, enxerga também: " + ", ".join(f"{e['nome']} ({auth.formatar_cnpj(e['cnpj'])})" for e in r["grupo"]))
    for e in r["mesmo_email"]:
        print(f"  mesmo e-mail: {e['nome']} ({auth.formatar_cnpj(e['cnpj'])})")
    if r["alertas"]:
        print("ATENÇÃO antes do convite:")
        for a in r["alertas"]:
            print(f"  - {a}")
    else:
        print("Pronto pro convite: gerenciar_clientes.py enviar-link " + emb["cnpj"] + " --para <quem repassa>")


def _imprimir_grupo(empresas: list[dict]) -> None:
    login = empresas[0]
    print(f"{login['nome']} ({auth.formatar_cnpj(login['cnpj'])}) -- login enxerga {len(empresas)} empresa(s):")
    for e in empresas:
        print(f"  {auth.formatar_cnpj(e['cnpj']):<20} sender {e['sender_id']:<9} {e['nome']}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Contas do portal do cliente")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("listar")
    ck = sub.add_parser("checar", help="o que conferir antes do convite (aceita CNPJ ou pedaço do nome)")
    ck.add_argument("termo")
    for nome in ("enviar-link", "link", "desativar", "ativar"):
        sp = sub.add_parser(nome)
        sp.add_argument("cnpj")
        if nome == "enviar-link":
            sp.add_argument("--para", help="manda o convite SÓ pra este e-mail (quem repassa ao cliente), não pros do cadastro")
    sub.add_parser("grupos")
    gp = sub.add_parser("grupo", help="empresas que o login enxerga (grupo econômico)")
    gp.add_argument("cnpj", help="CNPJ que faz o login")
    gp.add_argument("membros", nargs="*", help="CNPJs dos membros como estão em `interno` (substitui a lista atual)")
    gp.add_argument("--desfazer", action="store_true")
    dp = sub.add_parser("definir-pin")
    dp.add_argument("cnpj")
    dp.add_argument("pin")
    ev = sub.add_parser("envio", help="parâmetros da máscara de envio (Stokki) por embarcador")
    ev.add_argument("cnpj")
    ev.add_argument("--regra", choices=sorted(envios.REGRAS_XML))
    ev.add_argument("--warehouse")
    ev.add_argument("--transporte")
    ev.add_argument("--embalagem")
    ev.add_argument("--carrier", help="carrier_id da transportadora na Stokki pro wizard Excel (planilha); '' = automático")
    ev.add_argument("--prioridade", help="delivery do wizard Excel: marketplace|same_day_delivery|express_delivery|... ; '' = nenhuma")
    g = ev.add_mutually_exclusive_group()
    g.add_argument("--ativar", action="store_true")
    g.add_argument("--desativar", action="store_true")
    sub.add_parser("solicitacoes")
    sub.add_parser("fila")
    cc = sub.add_parser("concluir")
    cc.add_argument("id", type=int)
    cc.add_argument("resposta", nargs="?", default="")
    cc.add_argument("--recusar", action="store_true")
    args = p.parse_args(argv)

    cfg = _config()
    if args.cmd in ("envio", "solicitacoes", "fila", "concluir"):
        return _comandos_envio(args, cfg)
    conn = auth.conectar()
    try:
        if args.cmd == "listar":
            for e in auth.listar_embarcadores(conn):
                estado = "sem conta" if e["ativo"] is None else ("ATIVA" if e["ativo"] else "desativada")
                if e["ativo"] is not None and not e["tem_pin"]:
                    estado += " (sem PIN)"
                print(f"{auth.formatar_cnpj(e['cnpj'])}  sender {e['sender_id']:<9}  {estado:<20}  "
                      f"último login {e['ultimo_login_em'] or '-':<19}  {e['nome']}  {', '.join(e['emails']) or '(sem e-mail)'}")
            return 0

        if args.cmd == "checar":
            achados = procurar(conn, args.termo)
            if len(achados) != 1:
                print("Nenhum embarcador com sender_id em `interno` bate com isso." if not achados
                      else "Mais de um embarcador bate -- use o CNPJ:")
                for e in achados:
                    print(f"  {auth.formatar_cnpj(e['cnpj'])}  {e['nome']}")
                return 2
            _imprimir_checagem(checar(conn, achados[0]["cnpj"]))
            return 0

        if args.cmd == "grupos":
            grupos = auth.listar_grupos(conn)
            if not grupos:
                print("Nenhum grupo cadastrado.")
            for login in grupos:
                _imprimir_grupo(auth.empresas_do_login(conn, login))
            return 0

        emb = auth.buscar_embarcador(conn, args.cnpj)
        if not emb:
            print("CNPJ não está em `interno` com sender_id -- cadastre lá primeiro.")
            return 2

        if args.cmd == "grupo":
            if args.desfazer or args.membros:
                try:
                    auth.definir_grupo(conn, args.cnpj, [] if args.desfazer else args.membros)
                except ValueError as e:
                    print(f"Nada gravado: {e}")
                    return 2
            _imprimir_grupo(auth.empresas_do_login(conn, args.cnpj))
            return 0

        if args.cmd == "definir-pin":
            auth.definir_pin(conn, args.cnpj, args.pin)
            print(f"PIN definido pra {emb['nome']} ({auth.formatar_cnpj(emb['cnpj'])}).")
        elif args.cmd == "link":
            print(_link(conn, cfg, emb["cnpj"]))
        elif args.cmd == "enviar-link":
            para = (args.para or "").strip()
            if para and not re.fullmatch(r"[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+", para):
                print(f"--para {para!r} não é um e-mail válido. Nada enviado.")
                return 2
            if not para and not emb["emails"]:
                print("Embarcador sem e-mail em `interno`.")
                return 2
            from email_utils import enviar_email, envelope_html
            link = _link(conn, cfg, emb["cnpj"])
            destinos = [para] if para else emb["emails"]
            # Convite pra repassar (17/09, Hugo: "mandar pra mim os convites"): o
            # topo diz de quem é e pra quem vai; o resto é o e-mail que o cliente receberia.
            aviso = (f"<p style='background:#FEF3C7;border:1px solid #FCD34D;border-radius:7px;padding:10px 12px;font-size:13px'>"
                     f"<b>Convite pra repassar.</b> Acesso de <b>{emb['nome']}</b> (CNPJ {auth.formatar_cnpj(emb['cnpj'])}). "
                     f"E-mail do cadastro: {', '.join(emb['emails']) or '(nenhum)'}. "
                     f"O link abaixo vale 24 horas e só funciona uma vez -- quem abrir primeiro define o PIN.</p>") if para else ""
            corpo = envelope_html(
                aviso +
                f"<p>Olá, <strong>{emb['nome']}</strong>.</p>"
                f"<p>A Fresh Log liberou o acesso ao portal de acompanhamento de entregas "
                f"(CNPJ {auth.formatar_cnpj(emb['cnpj'])}). Defina seu PIN pelo botão abaixo:</p>"
                f"<p style='margin:24px 0'><a href='{link}' style='background:#0EA575;color:#fff;padding:12px 22px;"
                f"border-radius:7px;text-decoration:none;font-weight:700'>Definir meu PIN</a></p>"
                f"<p style='color:#6B7280;font-size:13px'>O link vale por 24 horas. Depois é só entrar com CNPJ + PIN em "
                f"{cfg.get('portal_cliente', {}).get('url_base', 'https://app.freshhub.com.br/cliente')}.</p>",
                rodape="Fresh Log · Portal de acompanhamento de entregas")
            assunto = (f"[Convite pra repassar] {emb['nome']} · acesso ao portal de entregas" if para
                       else "Fresh Log · Seu acesso ao portal de entregas")
            ok = enviar_email(destinos, assunto, corpo, cfg.get("email", {}))
            print(("Enviado pra " if ok else "FALHOU ao enviar pra ") + ", ".join(destinos))
            return 0 if ok else 1
        elif args.cmd in ("desativar", "ativar"):
            if auth.definir_ativo(conn, emb["cnpj"], args.cmd == "ativar"):
                print(f"Conta de {emb['nome']} {'ativada' if args.cmd == 'ativar' else 'desativada'}.")
            else:
                print("Esse CNPJ ainda não tem conta no portal (nada a mudar).")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
