# -*- coding: utf-8 -*-
"""
http_retry.py

Utilitário compartilhado pra lidar com rate limit (HTTP 429) da API do
VUUPT -- achado em produção, 02/08: incrementar_rotas.py fazendo várias
chamadas seguidas (uma por destinatário, pra checar agendamento) sem
nenhuma pausa, disparou 429 repetidamente e chegou a derrubar a
execução inteira (listar_rotas também foi atingido, mesmo sendo só 1
chamada, por já estar dentro da janela de bloqueio).

chamar_com_retry() espera exponencialmente (3s, 6s, 12s, 24s...) e
tenta de novo quando recebe 429 -- outros códigos de erro (4xx/5xx que
não sejam 429) não são retentados aqui, retornam normalmente pra quem
chama decidir (ex: chamar resp.raise_for_status() como já fazia antes).
"""
import logging
import time

logger = logging.getLogger(__name__)


def chamar_com_retry(func, *args, max_tentativas: int = 5, espera_inicial: float = 3.0, **kwargs):
    """
    Executa func(*args, **kwargs) (uma chamada requests.get/post/put/
    delete, ou session.get/post/etc.) com retry exponencial em caso de
    HTTP 429. Retorna o objeto Response normalmente (com status_code
    != 429, ou a última resposta 429 se esgotar as tentativas -- quem
    chama trata igual trataria uma chamada direta, ex: raise_for_status()).
    """
    resp = None
    for tentativa in range(max_tentativas):
        resp = func(*args, **kwargs)
        if resp.status_code != 429:
            return resp
        espera = espera_inicial * (2 ** tentativa)
        logger.warning(
            f"HTTP 429 (limite de requisições da API) -- aguardando {espera:.0f}s "
            f"antes de tentar de novo (tentativa {tentativa + 1}/{max_tentativas})..."
        )
        time.sleep(espera)
    return resp
