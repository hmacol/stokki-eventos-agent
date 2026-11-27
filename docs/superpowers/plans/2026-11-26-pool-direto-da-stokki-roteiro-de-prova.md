# Pool direto da Stokki: roteiro de prova e portao

Ramo `pool-stokki` (so commits locais ate o Hugo decidir). Chave: `planejamento.fonte_pool: stokki`.
Spec: `docs/superpowers/specs/2026-11-26-pool-direto-da-stokki-design.md`. Mapa: secao Pool em `MAPA_DO_SISTEMA.txt`.

## 1. Pre-requisitos (decisao do Hugo)

1. Push do ramo `pool-stokki` (nada foi pushado ate aqui).
2. `py -3.11 .claude/scripts/vps_ops.py homolog subir pool-stokki` (skill `homologar`; "deploy" continua sendo producao).
3. No config da homologacao, `planejamento.fonte_pool: stokki`. Nunca rodar script a mao em `/opt/stokki-homolog`.
4. Em homologacao a Vuupt e so GET e a Stokki nunca: **a criacao do servico na Vuupt NAO se prova la**.
   - Em homologacao prova-se o que e local: pool com pedido sem servico (id negativo), rascunho, edicao local,
     cancelar, portal, /pedidos. No "Confirmar e enviar" a materializacao deve ser **recusada/bloqueada pela guarda**
     (nada sai) -- a confirmar (nao verificado).
   - A materializacao se prova por mock/`--modo-teste`: `py -3.11 -m unittest nucleo.test_materializar nucleo.test_adocao_servico`
     (Vuupt falsa, banco temporario) e `--modo-teste` nos scripts de lote (nao escreve na Vuupt/Stokki; lembrar que
     `--modo-teste` pode mandar e-mail real: `forcar_destino`).
   - O que fica para o **1o dia de producao supervisionado**: criar o servico de verdade, ver o `not_assigned` na Vuupt,
     medir o tempo do envio.

## 2. Sombra (portao, parte 1)

Na VPS, como `www-data`, com a chave ainda em `nucleo`/`vuupt`:

    sudo -u www-data venv/bin/python nucleo/comparar_pool.py --salvar --email --fonte stokki   # 1x por hora, como as outras fontes
    sudo -u www-data venv/bin/python nucleo/comparar_pool.py --historico 10 --fonte stokki      # placar por dia

- Pedido do nucleo sem servico na Vuupt aparece como "sem servico" (informativo, fora do placar).
- Divergencia = pedido que a Vuupt tem e o nucleo nao, pedido so no nucleo COM servico, ou campo diferente
  (endereco, coordenadas, caixas, janela, data, dia fixo).
- Timer: o timer existente e `infra/stokki-comparar-pool.*` (a unit existente roda nucleo e freshhub, nao inclui a fonte stokki); a fonte stokki NAO ganhou timer neste ramo:
  rodar a mao ou criar um par com `--fonte stokki` se o Hugo aprovar.
- Pedido sem servico que JA existe na Vuupt com o mesmo codigo segue a comparacao normal (id difere = divergencia), por desenho.

## 3. Passos de prova (homologacao, depois producao supervisionada)

| # | Passo | Esperado |
|---|-------|----------|
| 1 | Rodar o pipeline em `--modo-teste` e abrir o pool (`/planejamento`) | Pedido sem servico aparece, com selo, id negativo no JSON do pool |
| 2 | Montar rascunho com um pedido sem servico | Parada entra com id negativo; nada chamado na Vuupt |
| 3 | Editar endereco/janela do pedido sem servico | Grava em `edicao_local_json`; o pipeline seguinte nao regrava o campo |
| 4 | Cancelar pedido sem servico (e reagendar) | Fecha/atualiza so no nucleo; sai do pool e do rascunho |
| 5 | Confirmar e enviar o rascunho | **Producao**: servico criado na Vuupt (so leitura confirma `not_assigned`/atribuido), id real adotado (`adotar_servico`), rota enviada. **Homologacao**: nada sai |
| 6 | Pedido agrupado / reentrega -R / pedido nao ABERTO | Recusado com motivo na tela, sem servico duplicado |
| 7 | Incremento (`incrementar_rotas`) | Pedido sem servico e materializado antes de entrar |
| 8 | Rota ja enviada + adicionar parada | Materializa e adiciona |
| 9 | Portal do cliente (pedido sem servico) | Listado e tratado pelo nucleo (cancelar, alterar entrega) |
| 10 | `/pedidos` | Acoes usam o id sintetico (nao service_id nulo) |
| 11 | Torre, feriado, laboratorio, vigia, area nao atendida | Leem o nucleo; area nao atendida avisa pelo codigo, sem repetir aviso |

## 4. Consultas de verificacao (somente leitura)

    SELECT codigo, id_pool, vuupt_service_id, status FROM nucleo_pedidos WHERE vuupt_service_id IS NULL AND status='ABERTO';
    SELECT * FROM nucleo_materializacao;                 -- travas presas (devem sumir)
    SELECT nome, valor FROM nucleo_contadores;
    SELECT substr(rodado_em,1,10) dia, MAX(divergencias_proprias) FROM nucleo_reconciliacoes_pool WHERE fonte='stokki' GROUP BY dia;
    SELECT codigo FROM pedidos_area_notificada WHERE codigo IS NOT NULL ORDER BY rowid DESC LIMIT 20;

Na Vuupt (GET): o servico do codigo existe uma unica vez (`buscar_servico_por_code`), sem duplicado.

## 5. Criterios de aceite

- Todos os testes novos do ramo verdes; testes antigos de `nucleo`, `portal_cliente`, `roteirizacao` e `painel_agentes`
  (cada pacote em comando separado) sem regressao.
- Nenhum id negativo chega a Vuupt (log e `nucleo_materializacao` sem travas presas).
- Pedido nunca vira servico duplicado; pedido recusado mostra o motivo.
- Pool da tela = pool da sombra (so "sem servico" a mais).

## 6. PORTAO para ligar em producao (decisao do Hugo)

1. **3 dias uteis seguidos em 0** divergencias proprias na sombra `--fonte stokki` (`dias_limpos_seguidos`); e
2. a prova acima (homologacao para o que e local; mock/`--modo-teste` para a materializacao); e
3. o 1o dia de producao supervisionado, com poucos pedidos: criar o servico, conferir na Vuupt, medir tempo.

## 7. Risco: timeout do envio

A materializacao cria o servico pedido a pedido, **~5-7 chamadas a Vuupt por pedido**, de forma sincrona no
"Confirmar e enviar" (sem barra de progresso). Rascunho com dezenas de pedidos pode estourar o timeout do waitress/Caddy.
Medir no 1o dia; se estourar: limite de pedidos por clique ou execucao em segundo plano (nao feito).

## 8. Plano de volta

- Trocar `planejamento.fonte_pool` para o valor anterior e reiniciar `painel-agentes` e `portal-cliente`.
- Pedido que ja esta sem servico: a materializacao decide por **id negativo, nao pela chave**; rascunho com id negativo
  continua enviando (cria o servico) em vez de mandar id sintetico a Vuupt. Com a chave desligada nao nascem novos
  pedidos sem servico: o pipeline volta a criar o servico na importacao e o `adotar_servico` troca o id.
- Dedupe da area nao atendida por codigo continua valendo (mudanca deliberada).

## 9. Itens adiados e avisos (de `.superpowers/sdd/.../progress.md`)

AVISO AO HUGO
- Dedupe da area nao atendida por codigo vale tambem com a chave desligada: servico recriado com o mesmo codigo nao reavisa.

Ruling
- Subir em homologacao exige push do ramo: nao feito; depende do Hugo.

Minor (deferred)
- `fonte_pool` desconhecida devolve a string em vez de cair para `vuupt`; `eh_sem_servico(None)` sem teste.
- Import lazy de `adotar_servico` em `pedidos.py`; dedupe do plano_original ignora lote_id; pedido com id real X recebendo real Y via upsert nao passa por `adotar`.
- E-mail de `cancelar_cancelados` mostra id negativo e assunto "cancelado(s) na Vuupt"; `ROTULOS["gravado_nucleo"]` sem uso; `conferir_pool` Enviado sem prova so avisa.
- Materializacao: espera de ate 30 s por trava velha; janela de duplo-envio; motivos duplicados no card; excecao fora de `materializar` encerra o "enviar todas"; sem barra de progresso; se o DELETE da trava falha a conexao pode ficar em transacao.
- `adicionar_em_rota_enviada` usa o 1o `res.ok` (lote de 1).
- Cancelar com id negativo deixa a parada em rascunho OFERTADA ate o envio; falha parcial de `tirar_dos_rascunhos` pula `remover_parada`/tratativa; `agendamentos_origem` guarda id negativo; se re-salvar o fingerprint base falha (best-effort) a regressao volta para o pedido; edicao de data sem rodada do pipeline antes do envio causa 1 PUT.
- Regra unica de marca de edicao local (feriado/confirmados x dia fixo/data automatica) a triar; `_sem_servico_do_nucleo` le o pool inteiro por pagina; `_conferir_agrupamento` faz 1 query por pedido.
- Fase 4 do spec (desligar o sync de descoberta da Vuupt) nao feita.
