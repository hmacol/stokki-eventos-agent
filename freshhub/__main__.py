"""`py -3.11 -m freshhub`: sobe a API local (dev). Em producao o systemd
chama o uvicorn direto (plano 0.3)."""
import uvicorn

from freshhub.api.app import criar_app

if __name__ == "__main__":
    uvicorn.run(criar_app(), host="127.0.0.1", port=8100, log_config=None)
