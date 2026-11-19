# Revisão de segurança: ponte app próprio → Vuupt (16/11/2026)

Escopo: PR #29 (`f37ca47` + `f6755e2`, merge `dfa6d3e`). Módulos expostos tocados: `nucleo/api_motorista.py`, `nucleo/operacao.py`. Novo: `nucleo/ponte_vuupt.py`. Também `nucleo/banco.py`, `nucleo/sincronizar_vuupt.py`, `vuupt_client.py`.

Origem: revisão automática do commit (plugin security-guidance) + leitura do diff. Deployado em 16/11 ~20h20 sem homologação, com o OK do Hugo.

## Achados

| # | Nível | Onde | Achado | Situação |
|---|---|---|---|---|
| 1 | MÉDIO | `ponte_vuupt._juntar_fotos` | Juntar canhotos ampliava todas as fotos para a largura da maior: poucas fotos pequenas + uma grande viravam uma imagem enorme (memória/CPU do timer). | CORRIGIDO em `f6755e2`: desce para a largura da menor (teto 1280 px), no máximo 6 fotos. Teste `test_juntar_fotos_nunca_amplia`. |
| 2 | MÉDIO | `ponte_vuupt._executar` | A ação é executada depois de registrada, com o agente e a rota lidos na hora da execução. Rota que trocou de motorista ou serviço que saiu da rota seria concluído na Vuupt em nome errado; havia ainda um `atribuir_agente` para serviço sem rota. | CORRIGIDO em `f6755e2`: a fila guarda o agente que registrou; na execução exige rota ainda operável, mesmo agente e serviço ainda na rota, senão DESISTIU (alerta). `atribuir_agente` removido. Teste `test_acao_nao_vale_se_a_rota_trocou_de_motorista_ou_o_servico_saiu_dela`. |

Nenhum achado ALTO.

## Conferido sem achado

- Autorização: os endpoints do app seguem passando por `rota_do_motorista(conn, rota_id, agent_id())`; a ponte só amplia o que já é do próprio motorista (`rota_editavel`), e só para agente listado em `ponte_vuupt.agentes`.
- Desligada por padrão: sem a chave no config, `rota_operavel` é falso e a rota da Vuupt continua somente leitura (teste `test_sem_ponte_rota_da_vuupt_segue_somente_leitura`).
- Idempotência: reenvio da fila offline (mesmo uuid) não enfileira de novo.
- Token da Vuupt: lido do config pelo timer; não aparece em log nem em `resultado`/`ultimo_erro` (erros cortados em 500 caracteres; vale reconferir se a Vuupt devolver cabeçalho em mensagem de erro).

## Riscos aceitos (não são falha de segurança)

- Entrega parcial vai como sucesso na Vuupt.
- Foto que não chega em 30 min: pedido concluído na Vuupt sem checklist.
- A rota fica `assigned` na Vuupt; fechamento manual no piloto.

## Limite desta revisão

A prova real na Vuupt (rota 5472713) foi feita antes das duas correções; os dois casos novos estão cobertos só por teste com Vuupt simulada.
