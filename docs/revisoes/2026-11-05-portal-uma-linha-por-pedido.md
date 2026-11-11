# Revisão: portal com uma linha por pedido + tentativas (05/11)

Diff: portal_cliente/dados_cliente.py (unir_tentativas), templates/acompanhamento.html (selo e histórico),
app.py (coluna Tentativas no xlsx), test_tentativas.py, MAPA.

- Escopo de dados igual ao de antes: a união só reagrupa as linhas que `linhas_das_rotas`/`_coletar_sem_rota`
  já filtram pelos remetentes do login. A chave é (sender_id, códigos base): login de grupo não mistura empresas.
- Campos novos (`tentativas`, `tentativas_texto`, `codigo_servico`) saem dos mesmos dicts; nada de entrada do usuário.
- Template: data, rótulo, motivo, rota e a chave do selo passam por `esc()`; `n` e `colspan` são números.
- "Responder" casa por `codigo_servico` (o code do serviço, igual ao de insucessos_aguardando_resposta).
- Ações por service_id (canhoto, alteração) usam o service_id da última tentativa: o canhoto continua sendo
  conferido no servidor pelo remetente do login (sem mudança).

Achados: nenhum ALTO/MÉDIO. BAIXO: pedido só com "não coletado" vira "Aguardando saída" carregando o
service_id do serviço finalizado; "Pedir alteração" nele é reavaliado no servidor (alterar_entrega), sem efeito na Vuupt.
