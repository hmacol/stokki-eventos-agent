"""Log do pacote (provisorio: a Task 8 troca por JSON estruturado)."""
import logging


def configurar_log(nivel: str) -> None:
    logging.basicConfig(level=nivel.upper())
