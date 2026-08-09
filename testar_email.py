# -*- coding: utf-8 -*-
"""
debug/testar_email.py

Teste ISOLADO de envio de e-mail -- não depende de nenhum agente,
nenhum pedido, nenhuma API do VUUPT. Só confirma se o login SMTP e o
envio em si estão funcionando -- pedido do Hugo, 04/08: "não estou
recebendo notificações no e-mail" (pode ser um problema só no envio,
não nos agentes em si).

COMO USAR:
    py -3.11 debug\\testar_email.py
"""
import smtplib
import sys
from email.mime.text import MIMEText
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

import yaml


def main():
    config = yaml.safe_load(open(RAIZ / "config.yaml", encoding="utf-8"))
    cfg_email = config.get("email", {})

    usuario = cfg_email.get("remetente", "")
    senha = cfg_email.get("senha_app") or cfg_email.get("senha", "")
    host = cfg_email.get("smtp_host", "smtp.gmail.com")
    port = int(cfg_email.get("smtp_port", 587))

    print("=" * 72)
    print("Teste isolado de envio de e-mail")
    print("=" * 72)
    print(f"  Remetente: {usuario or '(vazio -- config.yaml sem email.remetente)'}")
    print(f"  Senha configurada: {'sim, ' + str(len(senha)) + ' caractere(s)' if senha else 'NÃO -- vazia'}")
    print(f"  Servidor: {host}:{port}")
    print()

    if not usuario or not senha:
        print("Faltam credenciais no config.yaml (seção 'email') -- não dá pra testar o envio.")
        return

    print("Conectando no servidor SMTP...")
    try:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            print("  Conectado. Iniciando TLS...")
            smtp.starttls()
            print("  TLS ok. Fazendo login...")
            smtp.login(usuario, senha)
            print("  Login OK!")

            print("Enviando e-mail de teste pra você mesmo...")
            msg = MIMEText(
                "Esse é um e-mail de teste do debug/testar_email.py -- se você recebeu isso, "
                "o envio de e-mail está funcionando normalmente. O problema de notificações "
                "não recebidas deve estar em outro lugar (tarefa agendada não rodando, ou o "
                "agente específico não teve nada pra notificar)."
            )
            msg["Subject"] = "[TESTE] Envio de e-mail funcionando"
            msg["From"] = usuario
            msg["To"] = usuario
            smtp.send_message(msg)
            print("  Enviado!")

        print()
        print("=" * 72)
        print(f"SUCESSO -- confira a caixa de entrada de {usuario} (pode levar alguns segundos).")
        print("Se você RECEBEU esse e-mail, o envio está funcionando -- o problema de notificação")
        print("está em outro lugar (tarefa agendada não rodando, ou nada pra notificar mesmo).")
        print("Se você NÃO recebeu, confira também a pasta de spam/lixo eletrônico.")
        print("=" * 72)

    except smtplib.SMTPAuthenticationError as e:
        print()
        print("=" * 72)
        print("FALHA DE AUTENTICAÇÃO -- a senha configurada não é aceita pelo Gmail.")
        print("Causas mais comuns:")
        print("  - A 'senha de app' expirou ou foi revogada (Gmail exige gerar uma nova de vez")
        print("    em quando, principalmente se a senha da conta principal mudou)")
        print("  - Copiou a senha de app com espaço a mais/a menos")
        print(f"Detalhe técnico: {e}")
        print("=" * 72)
    except Exception as e:
        print()
        print("=" * 72)
        print(f"FALHA: {type(e).__name__}: {e}")
        print("=" * 72)


if __name__ == "__main__":
    main()
