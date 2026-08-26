# -*- coding: utf-8 -*-
"""
atendimento/gerenciar_usuarios.py

CLI pra criar o primeiro usuário admin da central de atendimento --
depois do primeiro, os demais atendentes são criados pela própria tela
/usuarios (só admin), sem precisar mexer na VPS de novo.

COMO USAR (na VPS, mesmo venv do resto do projeto):
    python -m atendimento.gerenciar_usuarios --criar --login hugo --nome "Hugo" --senha "..." --papel admin
    python -m atendimento.gerenciar_usuarios --listar
"""
import argparse
import sys
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))

from werkzeug.security import generate_password_hash

from atendimento import banco


def criar(login: str, nome: str, senha: str, papel: str):
    if len(senha) < 8:
        raise SystemExit("Senha precisa ter pelo menos 8 caracteres.")
    conn = banco.conectar()
    try:
        conn.execute(
            "INSERT INTO usuarios (login, nome, senha_hash, papel) VALUES (?, ?, ?, ?)",
            (login, nome, generate_password_hash(senha), papel),
        )
        conn.commit()
    except Exception as exc:
        raise SystemExit(f"Falha ao criar usuário (login já existe?): {exc}")
    finally:
        conn.close()
    print(f"Usuário '{login}' ({papel}) criado.")


def listar():
    conn = banco.conectar()
    try:
        linhas = conn.execute(
            "SELECT login, nome, papel, ativo, ultimo_login_em FROM usuarios ORDER BY nome",
        ).fetchall()
    finally:
        conn.close()
    if not linhas:
        print("Nenhum usuário cadastrado ainda.")
        return
    for linha in linhas:
        status = "ativo" if linha["ativo"] else "INATIVO"
        print(f"{linha['login']:<20} {linha['nome']:<30} {linha['papel']:<12} {status:<8} último login: {linha['ultimo_login_em'] or 'nunca'}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--criar", action="store_true", help="Cria um usuário novo")
    parser.add_argument("--listar", action="store_true", help="Lista os usuários cadastrados")
    parser.add_argument("--login", default="")
    parser.add_argument("--nome", default="")
    parser.add_argument("--senha", default="")
    parser.add_argument("--papel", default="admin", choices=["admin", "atendente"])
    args = parser.parse_args()

    if args.criar:
        if not args.login or not args.nome or not args.senha:
            raise SystemExit("--criar exige --login, --nome e --senha.")
        criar(args.login, args.nome, args.senha, args.papel)
    elif args.listar:
        listar()
    else:
        parser.print_help()
