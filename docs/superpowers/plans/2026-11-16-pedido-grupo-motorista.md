# Pedido de grupo do motorista novo — plano

**Objetivo:** depois das boas-vindas, pedir ao Atendimento (WhatsApp, privado) que crie o grupo do motorista novo.

**Spec:** `docs/superpowers/specs/2026-11-16-pedido-grupo-motorista-design.md`

**Regras gerais:** `py -3.11`; testes com unittest; nada de envio real nos testes; sem commit até o Hugo pedir.

## Pontos de atenção na revisão

- Nome de uma palavra só ("Madonna") → `FRESH + Madonna - ...`, sem repetir.
- Nome com espaços sobrando ou minúsculas → usa como veio, só enxuga espaços.
- `tipo_veiculo` desconhecido → usa o código como está.
- Config sem `atendimento_whatsapp` → `desligado`, cadastro conclui.
- Cadastros já concluídos antes do deploy → não recebem pedido retroativo (`_ignorar` barra).

## Tarefa 1: textos e envio (`notificar_whatsapp.py`, teste `test_notificar_whatsapp_cadastro_link.py`)

- `nome_grupo_motorista(nome: str, carro: str, refrigerado) -> str`
  → `FRESH + <Primeiro Último> - <carro> (Refrigerado|Seco)`.
- `texto_pedido_grupo_motorista(nome: str, telefone: str, carro: str, refrigerado) -> str`
  → 4 linhas da spec; telefone só dígitos com 55 no link `wa.me`.
- `avisar_atendimento_pedido_grupo(cadastro_id: int, texto: str, config: dict, modo_teste=False, **kw) -> str`
  → destino `cadastro_motorista_link.atendimento_whatsapp`; ausente/vazio = `desligado`;
  mesma trava de piloto das boas-vindas (marca `[teste → +<atendimento>]`);
  `despachar(config, ORIGEM_MOTORISTA, "pedido_grupo", texto, f"cadastro_link:{id}:pedido_grupo", grupo_id=...)`.
- Testes: nome (2+ nomes, 1 nome, seco), texto, destino real, piloto, sem chave.

## Tarefa 2: passo no processador (`nucleo/cadastro_motorista_link.py`, `nucleo/banco.py`, teste `nucleo/test_cadastro_motorista_link.py`)

- Coluna `pedido_grupo_situacao TEXT` em `motoristas_cadastros`.
- `processar(..., avisar_atendimento=None)`; padrão chama `avisar_atendimento_pedido_grupo`.
- Em `_processar`, depois das boas-vindas: se `boas_vindas_situacao == "enviado"` e
  `pedido_grupo_situacao` vazio ou em `("falhou", "nao_enviado")`, envia e grava; se ficou
  em uma dessas duas, devolve `_pendente(..., "pedido de grupo: <situacao>")`.
- Testes: fluxo feliz manda 1 pedido com o nome certo; falha retenta sem repetir boas-vindas;
  boas-vindas `numero_sem_whatsapp` não manda pedido; conflito não manda.

## Tarefa 3: `MAPA_DO_SISTEMA.txt` e docstring de config do `notificar_whatsapp.py`
