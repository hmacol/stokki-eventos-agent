"""Retiradas no galpão (Hugo, 28/08/2026).

Pedido cuja transportadora é RETIRADA (cliente retira / transportadora
terceira coleta no galpão) NUNCA sobe pra VUUPT desde 06/11 (Hugo): o
pipeline ignora. De 28/08 a 06/11 virava SERVIÇO AVULSO "[RETIRADA] ..."
atribuído ao agente fixo de retiradas; os que sobraram lá o
acompanhar_retiradas.py fecha/cancela conforme a Stokki. Ver
regras_retirada.py (identificação do serviço).
"""
