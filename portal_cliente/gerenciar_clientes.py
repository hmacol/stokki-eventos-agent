# -*- coding: utf-8 -*-
"""
portal_cliente/gerenciar_clientes.py

Gestão das contas do portal do cliente pela linha de comando (VPS ou
local -- mesmo dados.db):

    py -3 portal_cliente/gerenciar_clientes.py listar
    py -3 portal_cliente/gerenciar_clientes.py enviar-link 29909190000146
    py -3 portal_cliente/gerenciar_clientes.py definir-pin 29909190000146 123456
    py -3 portal_cliente/gerenciar_clientes.py desativar 29909190000146
    py -3 portal_cliente/gerenciar_clientes.py ativar 29909190000146
    py -3 portal_cliente/gerenciar_clientes.py link 29909190000146   # só imprime o link, sem e-mail

Máscara de envio de pedidos (08/09):
    py -3 portal_cliente/gerenciar_clientes.py envio 68146976000100                       # mostra os parâmetros da Stokki
    py -3 portal_cliente/gerenciar_clientes.py envio 68146976000100 --regra muai          # regra de transformação do XML
    py -3 portal_cliente/gerenciar_clientes.py envio 68146976000100 --desativar|--ativar  # pausa/libera o envio
    py -3 portal_cliente/gerenciar_clientes.py solicitacoes                               # pendências (cancelar/em espera/reagendar)
    py -3 portal_cliente/gerenciar_clientes.py concluir 12 "cancelado na Stokki"         # marca a solicitação 12 como atendida
    py -3 portal_cliente/gerenciar_clientes.py fila                                       # o que está NA_FILA/ENVIANDO/ERRO

Quem pode ter conta é quem está em `interno` com sender_id -- pra liberar
um embarcador novo, primeiro cadastre CNPJ/sender_id/e-mail lá (é a
mesma tabela usada pelas notificações de insucesso).
"""
import argparse
import sys
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
            if args.ativar or args.desativar:
                mudancas["envio_ativo"] = bool(args.ativar)
            if mudancas:
                envios.definir_parametros_cliente(conn, args.cnpj, **mudancas)
            c = envios.config_stokki_cliente(conn, args.cnpj, cfg)
            print(f"{c['nome']} ({auth.formatar_cnpj(c['cnpj'])})")
            print(f"  client_id (interno.stkkc_id): {c['client_id'] or '(FALTA -- cadastre em interno.stkkc_id)'}")
            print(f"  regra_xml: {c['regra_xml']} -- {envios.REGRAS_XML[c['regra_xml']]}")
            print(f"  warehouse_id: {c['warehouse_id']}  transporte: {c['tipo_transporte']}  embalagem: {c['embalagem']}")
            print(f"  envio {'ATIVO' if c['envio_ativo'] else 'DESATIVADO'}  e-mails: {', '.join(c['emails']) or '(nenhum)'}")
            return 0
        if args.cmd == "solicitacoes":
            itens = envios.listar_solicitacoes_pendentes(conn)
            if not itens:
                print("Nenhuma solicitação pendente.")
            for s in itens:
                print(f"#{s['id']:<4} {s['criado_em'][:16]}  {envios.ROTULOS_SOLICITACAO.get(s['tipo'], s['tipo']):<26} "
                      f"NF {s['numero_nf']:<8} {s['codigo_pedido'] or '(sem código)':<10} {s['destinatario_nome']}  "
                      f"[{s['status_envio']}] {s['detalhes'] or ''}  (por {s['solicitado_por']})")
            return 0
        if args.cmd == "concluir":
            envios.concluir_solicitacao(conn, args.id, args.resposta, recusada=args.recusar)
            print(f"Solicitação #{args.id} marcada como {'RECUSADA' if args.recusar else 'CONCLUÍDA'}.")
            return 0
        if args.cmd == "fila":
            rows = conn.execute("SELECT e.id, e.status, e.criado_em, e.numero_nf, e.destinatario_nome, e.tentativas, e.erro, "
                                "COALESCE(i.apelido, i.nome_remetente, e.cnpj_embarcador) AS emb FROM portal_envios e "
                                "LEFT JOIN interno i ON i.cnpj_embarcador = e.cnpj_embarcador "
                                "WHERE e.status IN ('NA_FILA','ENVIANDO','ERRO') ORDER BY e.status, e.criado_em").fetchall()
            if not rows:
                print("Fila vazia e sem erros.")
            for r in rows:
                print(f"#{r['id']:<5} {r['status']:<9} {r['criado_em'][:16]}  {r['emb'][:28]:<28} NF {r['numero_nf']:<8} "
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


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Contas do portal do cliente")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("listar")
    for nome in ("enviar-link", "link", "desativar", "ativar"):
        sub.add_parser(nome).add_argument("cnpj")
    dp = sub.add_parser("definir-pin")
    dp.add_argument("cnpj")
    dp.add_argument("pin")
    ev = sub.add_parser("envio", help="parâmetros da máscara de envio (Stokki) por embarcador")
    ev.add_argument("cnpj")
    ev.add_argument("--regra", choices=sorted(envios.REGRAS_XML))
    ev.add_argument("--warehouse")
    ev.add_argument("--transporte")
    ev.add_argument("--embalagem")
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

        emb = auth.buscar_embarcador(conn, args.cnpj)
        if not emb:
            print("CNPJ não está em `interno` com sender_id -- cadastre lá primeiro.")
            return 2

        if args.cmd == "definir-pin":
            auth.definir_pin(conn, args.cnpj, args.pin)
            print(f"PIN definido pra {emb['nome']} ({auth.formatar_cnpj(emb['cnpj'])}).")
        elif args.cmd == "link":
            print(_link(conn, cfg, emb["cnpj"]))
        elif args.cmd == "enviar-link":
            if not emb["emails"]:
                print("Embarcador sem e-mail em `interno`.")
                return 2
            from email_utils import enviar_email, envelope_html
            link = _link(conn, cfg, emb["cnpj"])
            corpo = envelope_html(
                f"<p>Olá, <strong>{emb['nome']}</strong>.</p>"
                f"<p>A Fresh Log liberou o acesso ao portal de acompanhamento de entregas "
                f"(CNPJ {auth.formatar_cnpj(emb['cnpj'])}). Defina seu PIN pelo botão abaixo:</p>"
                f"<p style='margin:24px 0'><a href='{link}' style='background:#0EA575;color:#fff;padding:12px 22px;"
                f"border-radius:7px;text-decoration:none;font-weight:700'>Definir meu PIN</a></p>"
                f"<p style='color:#6B7280;font-size:13px'>O link vale por 24 horas. Depois é só entrar com CNPJ + PIN em "
                f"{cfg.get('portal_cliente', {}).get('url_base', 'https://app.freshhub.com.br/cliente')}.</p>",
                rodape="Fresh Log · Portal de acompanhamento de entregas")
            ok = enviar_email(emb["emails"], "Fresh Log · Seu acesso ao portal de entregas", corpo, cfg.get("email", {}))
            print(("Enviado pra " if ok else "FALHOU ao enviar pra ") + ", ".join(emb["emails"]))
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
