"""Retiradas no galpão (Hugo, 28/08/2026).

Pedido cuja transportadora é RETIRADA (cliente retira / transportadora
terceira coleta no galpão) é importado na VUUPT como SERVIÇO AVULSO --
nunca entra em rota -- com título "[RETIRADA] ..." e atribuído ao agente
fixo de retiradas. Ver regras_retirada.py (payload/identificação) e
acompanhar_retiradas.py (fecha/cancela conforme a Stokki).
"""
