# Revisao de seguranca: rota sem aceite vira oferta no app (ramo rota-recusada-oferta)

Escopo: nucleo/operacao.py, nucleo/api_motorista.py e o modulo novo nucleo/ofertas_rota_enviada.py
(expostos pela API do motorista).

O que entra
- GET /abrir/ofertas: pagina publica FIXA (sem parametro, sem dado), so redireciona pro esquema do app.
- /api/ofertas (ja existia) passa a listar tambem ofertas de rota enviada; escolher/cancelar reusam os
  endpoints do marketplace com id = 1e9 + rota_id.
- aceitar rota: recusa (409) se outro motorista ja escolheu a rota em oferta.
- listar rotas: quem recusou deixa de ver a rota em oferta.

Conferido
- Quem ve/escolhe: so agent_id presente em elegiveis_json, checado no servidor em listar e em escolher.
- Disputa: UPDATE ... WHERE status = 'ABERTA' (um vencedor); cancelar so do proprio escolhido_por.
- A oferta entrega so o resumo (regiao, cidades, bairros, caixas, peso, saida, valor): nada de endereco,
  destinatario ou codigo de pedido. Teste cobre.
- Valor mostrado e calculado pelo veiculo de quem esta logado (a propria tarifa dele).
- SQL: tudo parametrizado; o unico f-string de coluna usa nomes fixos do codigo.
- A escolha nao troca motorista nem chama a Vuupt.

ALTO: nenhum. MEDIO: nenhum.

BAIXO
- B1. Bairros/cidades da rota ficam visiveis a todos os elegiveis antes de alguem assumir (mesma
  exposicao que o marketplace ja tem, por decisao do Hugo em 22/08).
- B2. /abrir/ofertas nao tem freio de requisicoes; e pagina estatica de ~600 bytes.
- B3. O aviso no WhatsApp leva valor aproximado ao grupo: no piloto vai so pro grupo interno.

Testes: nucleo.test_ofertas_rota_enviada (11), test_api_motorista, test_ponte_vuupt, test_notificar_whatsapp: OK.
