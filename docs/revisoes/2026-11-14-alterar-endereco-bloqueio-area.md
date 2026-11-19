# Revisão de segurança: alterar endereço de entrega no pedido aguardando liberação

- Data: 14/11/2026
- Diff: `origin/master` (15d9203) .. ramo `endereco-bloqueio-area`, só `portal_cliente/`
- Profundidade: triagem focada (mudança pequena, 1 rota existente ganhou 1 tipo de ação). Não houve modelagem adversarial por agente separado.

## O que mudou

- `POST /api/envios/<id>/acao` aceita `tipo: alterar_endereco` (`app.py`), que chama `envio_pedidos.alterar_endereco_bloqueado`.
- `bloqueio_area.avisar_endereco_alterado`: mensagem de sistema no chamado do bloqueio.
- `_envios.html`: botão e janela do endereço.
- `GET /api/envios` devolve `endereco_entrega_ativo` e `pode_alterar_endereco` por linha.

## Conferido

| Ponto | Resultado |
|---|---|
| Acesso por embarcador | A rota busca o envio por `id` + CNPJ da empresa da sessão (`buscar_envio`). Outro embarcador recebe 404 (teste `test_outro_embarcador_nao_alcanca`). |
| Guardas da rota | `@requer_cliente`, `@exige_mesma_origem` e `_exige_pode_enviar` continuam valendo (equipe em nível leitura não altera). |
| Furar o bloqueio de área | A área é reavaliada no servidor com o endereço novo; cidade, UF e código IBGE vêm da consulta do CEP, não do navegador. O pedido só sai do bloqueio se a regra de área aprovar o endereço para onde ele realmente vai. |
| Texto digitado no XML da Stokki | Passa por `montar_endereco` (tira caractere de controle, limite de 60) e `inserir_entrega_xml` (escape). Mesmo caminho já usado na prévia do envio. |
| Texto digitado no chat | `atendimento.html` e o chat do portal exibem a mensagem com `esc()`. |
| Corrida com a liberação da equipe | `UPDATE ... WHERE status = 'AGUARDANDO_LIBERACAO'`; se mudou no meio, nada é gravado. |
| Nota com `<entrega>` próprio | Recusada no servidor relendo o XML (evita erro do worker ao inserir segundo `<entrega>`). |

## Achados

### BAIXO 1 (corrigido): ação sem limite de repetição

Cliente logado podia repetir a troca em laço enquanto o pedido continuasse bloqueado: cada chamada consulta CEP, geocodifica (custo) e escreve uma mensagem no chamado. Corrigido reaproveitando `_cep_acima_do_limite` (mesma janela de 60 s por cliente) antes de abrir o banco; teste `test_limite_por_minuto`.

### BAIXO 2 (aceito, já existia): falha na geocodificação deixa passar

`classificar_area_envio` devolve `None` quando a geocodificação falha, por decisão antiga ("nunca segura pedido por falta de dado"). Com a ação nova, uma falha nessa hora solta um pedido que estava bloqueado. É o mesmo comportamento do envio normal; fica registrado para o Hugo decidir se, nesta ação, falha deveria manter o bloqueio.

## Sem achado ALTO ou MÉDIO.

## Complemento de 16/11: dia de visita da região nova (ainda sem commit)

A troca passou a conferir a data de agendamento contra o dia de visita da região do endereço novo (`_agendamento_no_endereco_novo`). Fora do dia: a rota devolve 409 com o aviso e o cliente manda outra data ou confirma assim mesmo (envio dedicado), igual à prévia do envio.

- Entradas novas do navegador: `agendamento_data` (data ISO validada no servidor: de hoje até 120 dias) e `aceita_fora_dia_fixo` (mesma confirmação que a prévia já aceita).
- A data nova só vale em pedido que já tinha data de agendamento; não cria agendamento em quem não tinha.
- A conferência roda antes da geocodificação e antes de qualquer escrita; o limite por minuto continua na frente.
- Sem achado novo.

## Limites desta revisão

- A janela não foi aberta em navegador; o JavaScript só teve a sintaxe conferida.
- Não revisei de novo `montar_endereco` / `inserir_entrega_xml` (revisados em 30/10 e 12/11).
