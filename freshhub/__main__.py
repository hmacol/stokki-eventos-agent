"""`py -3.11 -m freshhub`: sobe a API local (dev). Em producao o systemd
chama o uvicorn direto (plano 0.3)."""
import sys

import uvicorn

from freshhub.api.app import criar_app
from freshhub.config import ConfiguracaoInvalida

if __name__ == "__main__":
    try:
        app = criar_app()
    except ConfiguracaoInvalida as e:
        # mensagem curta, sem traceback: a URL do banco carrega a senha
        print(f"freshhub: {e}", file=sys.stderr)  # noqa: T201 - saida de linha de comando
        raise SystemExit(2) from None
    uvicorn.run(app, host="127.0.0.1", port=8100, log_config=None)
