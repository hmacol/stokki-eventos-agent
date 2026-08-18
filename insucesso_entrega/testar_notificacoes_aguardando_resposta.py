# -*- coding: utf-8 -*-
"""
testar_notificacoes_aguardando_resposta.py

Dispara, de verdade, um e-mail de teste PRA VOCÊ MESMO (hugo@freshlogbr.com,
via EMAIL_TESTE em notificar_insucesso_aguardando_resposta.py) pra CADA UM
dos 6 motivos de insucesso que aguardam resposta -- pedido do Hugo, 06/08:
"testar cada um deles no modo teste enviando as notificações para mim
mesmo".

Não mexe em nada de verdade no VUUPT nem no banco de dados (usa
modo_teste=True, que nunca marca como notificado -- pode rodar quantas
vezes quiser sem se preocupar com o rate-limit diário).

COMO USAR:
    py -3.11 insucesso_entrega\\testar_notificacoes_aguardando_resposta.py
"""
import sqlite3
import sys
import time
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(Path(__file__).parent))

import yaml

from motivos_falha import MOTIVOS_FALHA
from notificar_insucesso_aguardando_resposta import notificar_remetentes

MOTIVOS_AGUARDAM_RESPOSTA = [
    (fid, info) for fid, info in MOTIVOS_FALHA.items() if info.get("aguarda_resposta")
]


def _carregar_config() -> dict:
    with open(_RAIZ / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _achar_sender_id_de_teste() -> int:
    """
    Pega o primeiro sender_id real do banco (não importa qual -- em
    modo_teste o e-mail vai pra você de qualquer jeito, só usamos um
    remetente real pra pegar um nome de exibição válido no cabeçalho).
    """
    db_path = _RAIZ / "dados" / "dados.db"
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT sender_id FROM interno WHERE sender_id IS NOT NULL AND email IS NOT NULL LIMIT 1"
    ).fetchone()
    conn.close()
    if not row:
        raise RuntimeError(
            "Não achei nenhum remetente com sender_id+email cadastrado em 'interno' -- "
            "precisa de pelo menos 1 pra montar o e-mail de teste."
        )
    return row[0]


def main():
    config = _carregar_config()
    config_email = config.get("email", {})
    config_resposta = config.get("resposta_insucesso", {})
    sender_id_teste = _achar_sender_id_de_teste()

    print("=" * 72)
    print(f"Testando os {len(MOTIVOS_AGUARDAM_RESPOSTA)} motivo(s) que aguardam resposta")
    print(f"Todos os e-mails vão pra: hugo@freshlogbr.com (modo teste)")
    print("=" * 72)

    enviados = 0
    falhas = 0

    for failed_reason_id, info in MOTIVOS_AGUARDAM_RESPOSTA:
        pedido_fake = {
            "id": 900000 + failed_reason_id,  # id fake alto, não colide com pedido real
            "code": f"PS-TESTE-{failed_reason_id}",
            "title": f"PS-TESTE-{failed_reason_id} - Pedido de teste - {info['texto']}",
            "sender_id": sender_id_teste,
            "failed_reason_id": failed_reason_id,
        }

        print(f"\n[{info['texto']}] (failed_reason_id={failed_reason_id})")
        try:
            resultado = notificar_remetentes([pedido_fake], config_email, config_resposta, modo_teste=True)
            if resultado["enviados"] == 1:
                print(f"  OK -- e-mail enviado.")
                enviados += 1
            else:
                print(f"  FALHOU -- resultado: {resultado}")
                falhas += 1
        except Exception as e:
            print(f"  ERRO: {e}")
            falhas += 1

        time.sleep(1)  # não estressa o SMTP, 6 e-mails seguidos é pouco mas por garantia

    print()
    print("=" * 72)
    print(f"Concluído: {enviados} enviado(s), {falhas} falha(s) de {len(MOTIVOS_AGUARDAM_RESPOSTA)} motivo(s).")
    print("Confira sua caixa de entrada (e o spam, por garantia) -- devem chegar 6 e-mails,")
    print("um por motivo, cada um com a pergunta específica daquele caso.")
    print("=" * 72)


if __name__ == "__main__":
    main()
