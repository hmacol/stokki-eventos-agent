# Pedido de grupo do motorista novo ao Atendimento

Data: 16/11/2026. Aprovado pelo Hugo na conversa.

## O que e por quê

Quando o motorista conclui o cadastro pelo link (`/motorista/cadastro`), além das
boas-vindas no privado (como hoje), o sistema manda uma mensagem ao Atendimento
pedindo que criem à mão o grupo de WhatsApp daquele motorista, já com o nome
certo. O Hugo queria o grupo; criar pelo sistema foi descartado por risco de
banimento do número que manda os alertas.

## Caminhos considerados

1. **Sistema cria o grupo pelo gateway** (pedido original). Descartado pelo Hugo:
   criação automática de grupo com número desconhecido aumenta o risco de
   banimento, exige permissão maior na chave do OpenWA e falha com quem bloqueia
   ser adicionado a grupos.
2. **Sistema só pede, o Atendimento cria** (escolhido). O gateway continua só
   mandando texto.

## Comportamento

- Boas-vindas ao motorista: sem mudança.
- Depois que as boas-vindas ficam `enviado`, sai UMA mensagem por cadastro
  (assinatura `cadastro_link:<id>:pedido_grupo`) para o número do Atendimento,
  no privado.
- Não sai quando as boas-vindas terminaram em outra situação final
  (`agente_existente`, `numero_sem_whatsapp`, `desligado`): motorista que já
  existia ou que não tem WhatsApp não ganha pedido. (Decisão do Claude.)
- Cadastro em conflito ou pendente: nada sai até resolver (já é assim com as
  boas-vindas).
- Nome do grupo (definido pelo Hugo):
  `FRESH + <Primeiro e Último Nome> - <Carro> (<temperatura>)`,
  ex.: `FRESH + João Silva - Fiorino (Refrigerado)`. Temperatura = Refrigerado
  ou Seco, do campo `refrigerado`. Carro = `tipo_veiculo` em nome legível.
- Texto:

  ```
  👥 Criar grupo do motorista novo
  Nome do grupo: FRESH + João Silva - Fiorino (Refrigerado)
  Motorista: João da Silva · (11) 99876-5432
  Adicionar: https://wa.me/5511998765432
  ```

- Destino: `cadastro_motorista_link.atendimento_whatsapp` no config. Sem a
  chave, o pedido não sai (`desligado`) e o cadastro conclui normalmente.
- Piloto: vale a mesma trava das boas-vindas (`forcar_destino_whatsapp` com
  número manda pra ele, com `[teste → +<atendimento>]` na primeira linha).
- Falha de envio (`falhou`, `nao_enviado`): o cadastro fica pendente e o timer
  de 15 min tenta de novo, igual aos outros avisos. Não segura as boas-vindas.
- Teto: conta no teto dos motoristas (`motoristas.teto_diario`).

## Fora do escopo

- Conferir se o grupo foi criado.
- Mandar qualquer coisa dentro do grupo.

## Onde mexe

- `notificar_whatsapp.py`: `nome_grupo_motorista`, `texto_pedido_grupo_motorista`,
  `avisar_atendimento_pedido_grupo`.
- `nucleo/cadastro_motorista_link.py`: passo novo depois das boas-vindas.
- `nucleo/banco.py`: coluna `pedido_grupo_situacao` em `motoristas_cadastros`.
- Testes dos dois módulos e `MAPA_DO_SISTEMA.txt`.
- Config da VPS: a chave `atendimento_whatsapp` é do Hugo (ou com o OK dele no deploy).
