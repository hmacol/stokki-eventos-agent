# Fluxo Cross — registro de decisões da implementação

Cópia do ledger da execução (25-26/10/2026). Linhas `Ruling:` são decisões tomadas fora do plano; `Final:` são da revisão final do ramo.

    # SDD ledger — plan: docs/superpowers/plans/2026-10-25-importacao-fluxo-cross.md
    
    Spec: docs/superpowers/specs/2026-10-24-importacao-fluxo-cross-design.md
    Repos: principal = worktree C:\agente_stokki_eventos\.claude\worktrees\fluxo-cross (ramo fluxo-cross, base 189bb63);
           importador = worktree C:\agente_importacao_stokki\.claude\worktrees\fluxo-cross (ramo fluxo-cross, base 549a7c4).
    
    Setup: Ruling: commits locais por tarefa no ramo fluxo-cross, sem push — Hugo aprovou o plano (que traz os commits) escolhendo "Native"; a pergunta explicita sobre commit ficou sem resposta — custo se errado: commits locais num ramo, reversiveis.
    Setup: Ruling: importador em worktree proprio em vez de checkout do ramo no diretorio principal — nao mexer no master que outras sessoes usam — custo se errado: nenhum.
    Setup: Ruling: atribuicao dos commits = Claude Opus 5.5 (lembrete do sistema substituiu a anterior) — plano ajustado.
    
    Pre-flight (interfaces compartilhadas):
    - T2 produz TRANSFORMACOES["cross"](origem, destino, codigo_xd, nome_item) / processar_xml(nome, transformar_fn) <- T7 (px.TRANSFORMACOES["cross"](..., codigo_xd=, nome_item=)) e T8 (partial): batem.
    - T3 produz fluxos_do_embarcador(conn, cnpj=None, stkkc=None) -> {codigo_xd, nome_item, erro_xd, fluxos,...} <- T6 (por cnpj), T7 (por cnpj), T8 (por stkkc), T9: batem. ROTULOS/FLUXOS <- T6: batem.
    - T4 produz trocar_nf(sessao, id_stokki, caminho_xml_original, numero_nf, client_id), localizar_pedido_por_nf(sessao, numero_nf, client_id), contar_itens <- T5, T9: batem.
    - T5 produz enfileirar(conn, *, cnpj_embarcador, client_id, numero_nf, chave_nfe, caminho_xml_original, origem, envio_id, id_stokki, codigo_pedido) <- T7, T8, T9: batem. espelhar_em_portal_envios depende das colunas da T6 (T5 vem antes): a T5 checa PRAGMA antes de gravar, conforme plano.
    - T6 produz colunas portal_envios.fluxo/id_stokki/troca_nf_* <- T5 (espelho), T7 (_marcar troca_nf_status): T7 vem depois da T6, ok.
    - T1 produz MODO_CONFERENCIA <- T4.
    Setup: Ruling: titulos "### Tarefa N" -> "### Task N" no plano — os scripts task-start/task-done procuram "Task N" — custo: nenhum.
    Task 1: Ruling: sonda de producao NAO rodada — leitura em producao negada pelo classificador em 25/10 e o Hugo nao esta disponivel pra autorizar (sessao autonoma) — em vez de MODO_CONFERENCIA fixo, trocar_nf detecta em tempo de execucao: tenta baixar o XML por /pt-br/xml/nfe/<b64 padrao com '='> (formato confirmado em documentos_pedido/test_coletar_xml_nfe._href); se baixar, confere chave+itens; se nao baixar, cai pra "data-url da linha mudou"; idempotencia (ja_estava) so quando o download funciona — custo se errado: conferencia mais fraca e POST repetido (inofensivo, a fila nao reabre FEITA); a sonda vira item obrigatorio do roteiro de piloto (Task 10).
    Task 1: complete (sem commits: sonda adiada por falta de autorizacao; decisao acima)
    Task 2: complete (importador, commits 549a7c4..a241503, tests: py -3.11 -m unittest etapa2_processamento_xml.test_processar_xml test_relatorio_email → OK 8 + 14)
    Task 2: Ruling: linhas de ledger das tarefas do importador escritas a mao — task-done usa o git do repo principal — custo: nenhum.
    Task 2: fix de seguranca (revisao automatica): qVol inf/negativo — test_infinito_negativo_e_lixo_nao_contam RED→GREEN, commit 459a47b, suite OK 9 + 14
    Task 3: complete (commits bfc24b2..a646f1b, tests: py -3.11 -m unittest nucleo.test_fluxo_embarcador portal_cliente.test_gerenciar_fluxos portal_cliente.test_checar_cliente → OK)
    Task 3: Ruling: teste extra portal_cliente/test_gerenciar_fluxos.py pra CLI (plano previa so smoke run no banco local) — TDD exige teste do codigo de producao e evita tocar o dados.db local — custo: nenhum.
    Task 4: complete (commits a646f1b..d217980, tests: py -3.11 -m unittest stokki.test_trocar_nf stokki.test_cancelar → OK)
    Task 4: Ruling: sem MODO_CONFERENCIA fixo; conferencia decidida em tempo de execucao (ver Task 1) e devolvida em r["conferencia"] — custo: ver Task 1.
    Task 4: Ruling: conferencia compara chave + LISTA DE cProd (nao so a contagem de <det>) — XML convertido (1 item XD) e original de 1 item so teriam a mesma contagem e a mesma chave, e a troca seria dada como feita sem POST — custo se errado: nenhum (mais estrito que o plano).
    Task 4: Ruling: assinatura ganhou codigos_dos_itens(conteudo) e o resultado ganhou "conferencia"; localizar usa status="" como _buscar_codigos_na_listagem — custo: se "" nao listar todos os status, pedido em status raro nao e achado e a troca fica PENDENTE ate ERRO com aviso.
    Task 5: complete (commits d217980..d59b07d, tests: py -3.11 -m unittest portal_cliente.test_trocas_nf portal_cliente.test_cancelamento → OK)
    Task 5: Ruling: enfileirar NAO reabre ERRO sozinho; so com reabrir=True (o script da virada usa) — o importador reenvia os mesmos XMLs 4x/dia e reabrir mandaria o mesmo e-mail de erro toda rodada — custo se errado: troca em ERRO precisa de reabertura manual/virada.
    Task 5: Ruling: processar_pendentes roda DEPOIS dos lotes no ciclo (plano dizia depois dos cancelamentos) — pedido criado no ciclo ja tem a troca tentada no mesmo ciclo — custo: nenhum.
    Task 5: Ruling: falha ao abrir sessao da Stokki adia a fila sem gastar tentativa; injecao ganhou `localizar` — Stokki fora do ar nao deve virar ERRO + e-mail — custo: nenhum.
    Task 5: Ruling: enfileirar faz commit — chamadores de dois repos; evita linha perdida por falta de commit — custo: nenhum.
    Task 6: complete (commits d59b07d..379d814, tests: py -3.11 -m unittest portal_cliente.test_fluxo_envio portal_cliente.test_fora_dia_fixo_envio portal_cliente.test_bloqueio_area → OK)
    Task 6: Ruling: regra "quais fluxos valem agora" virou envio_pedidos.fluxos_para_envio (testavel) e MSG_XD_AUSENTE mora la; app.py so chama — importar o app exige o config local, ausente no worktree — custo: nenhum.
    Task 6: Ruling: escolher_fluxo ganhou `rotulo`; planilha descarta Cross e, sobrando um fluxo, usa ele sem perguntar; confirmar_envios com lista VAZIA recusa (MSG_XD_AUSENTE) em vez de cair em armazenagem — evita pedido Cross forjado entrar com os produtos reais — custo: nenhum.
    Task 6: Ruling: coluna Fluxo da previa entra como 2a coluna (nao no fim) — o handler do agendamento usa td:last-child — custo: nenhum.
    Task 6: Ruling: prova manual no navegador trocada por prova das rotas com Flask test_client (scratchpad/prova_rotas_fluxo.py: config falso em memoria, banco temporario, e-mail interceptado) + node --check do JS — worktree sem config/dados e sem o Hugo pra login — 8/8 verificacoes OK (previa com 3 fluxos, recusa sem fluxo, grava cross/full_cross, lista, Cross some sem XD + e-mail, so-Cross sem XD recusa, confirmacao forjada recusa, pagina renderiza) — custo se errado: layout da coluna Fluxo nao foi visto num navegador; fica pro piloto.
    Task 7: Ruling: helper _enfileirar_troca concentra o enfileiramento (criado e duplicado) e grava troca_nf_status=PENDENTE; XD resolvido uma vez por lote so quando ha envio Cross; Cross sem XD no worker vira ERRO pelo caminho de preparacao (aviso ao cliente com copia do atendimento, rede de seguranca da spec 6.2) — custo: nenhum.
    Task 7: complete (commits 379d814..e04a216, tests: py -3.11 -m unittest portal_cliente.test_enviar_stokki_cross portal_cliente.test_trocas_nf portal_cliente.test_cancelamento portal_cliente.test_reconciliar_codigos → OK)
    Task 7: Ruling: test_trocas_nf deixou de criar as colunas troca_nf_* a mao — Task 6 passou a cria-las no conectar (duplicate column) — custo: nenhum.
    Task 6: fix de seguranca (revisao automatica, e-mail sem limite por requisicao do cliente) — test_primeiro_aviso_do_dia_por_embarcador RED→GREEN, commit aec5368, suite test_fluxo_envio OK; prova das rotas 8/8 (passo 6 sem e-mail repetido)
    Task 8: complete (importador, commits 459a47b..29deb89, tests: py -3.11 -m unittest test_main_cross test_avisos_cliente test_relatorio_email etapa2_processamento_xml.test_processar_xml → OK 36; fumaca entre repos (scratchpad/fumaca_importador_repos.py) com codigo real do repo principal: fluxo+XD lidos, troca enfileirada, duplicado nao duplica linha)
    Task 8: Ruling: repo principal inacessivel => segue pela regra do nome do cliente (nao RuntimeError como o plano dizia) — sem o repo nao da pra saber se o cliente e Cross; na VPS o repo sempre existe — custo se errado: Quatro Estrelas sai com XD (regra antiga, agora com volumes) mas sem troca de NF, e o log avisa.
    Task 8: Ruling: copia do aviso de XD ausente = notificacao.destinatarios do config do cliente (o importador nao tem email_atendimento); envio real so com email.forcar_destino "" no config do cliente, sem a chave vai so pro Hugo — custo: nenhum.
    Task 8: Ruling: XD ausente roda a etapa 1 (baixa os e-mails) e para antes da etapa 2; os ZIPs ficam guardados e entram quando o XD voltar, desde que ainda estejam nos ultimos N e-mails — custo se errado: pedidos de mais de 4 e-mails atras precisam de reprocesso manual.
    Task 8: Ruling: avisos_cliente.py foi escrito antes do teste; apaguei, escrevi test_avisos_cliente.py, vi falhar (ModuleNotFoundError) e reescrevi — regra de TDD — custo: nenhum.
    Task 5: fix de higiene — test_trocas_nf chamava sessao_uso.em_uso() real e criava dados/dados.db no worktree; agora mockado; residuo (tabelas vazias + 1 linha de ajustes_complexidade_cliente da prova das rotas) apagado.
    Task 3: Ruling (pos Task 8): resolver_codigo_xd casa por apelido OU nome_remetente e nome_item usa o mais curto — em producao os dois campos aparecem trocados (Dourado: apelido=razao social, nome_remetente=DOURADO) — tests NomesTrocados RED→GREEN, commit 29d3dcf — custo: nenhum.
    Task 9: complete (commits 29d3dcf..f7c9105, tests: py -3.11 -m unittest portal_cliente.test_enfileirar_trocas_virada portal_cliente.test_trocas_nf →    TROCA MANUAL (sem XML original): PS-30 referencia 300)
    Task 9: Ruling: NAO rodei o script localmente (plano previa --modo-teste local) — sem cookies no worktree a StokkiSession faria login real e derrubaria a sessao de producao — prova so pelos testes; docstring e mapa dizem "so na VPS" — custo: nenhum.
    Task 9: Ruling: numero da NF do pedido aberto = referencia da linha da listagem (pedido criado por XML usa a NF; visto PS-42542 "152754") e o original e casado por NF + CNPJ do emitente — custo se errado: pedidos com referencia diferente da NF caem na lista de troca manual.
    Task 9: Ruling: virada usa reabrir=True (troca em ERRO volta a PENDENTE) — e uma acao manual e deliberada — custo: nenhum.
    Task 9: fix de seguranca (revisao automatica: XML de outro embarcador/regex fragil na virada) — tests test_xml_do_importador_exige_emitente_e_chave_do_embarcador + test_mesma_nf_com_chaves_diferentes_vira_troca_manual RED→GREEN, commit 86f9e65, suite 7/7.
    Task 9: Ruling: nao restringi a pasta do importador por cliente (sugestao da revisao) — o CNPJ embutido na chave + emit/CNPJ ja garantem que a NF e do embarcador — custo se errado: nenhum pratico (NF de outro emitente nunca passa).
    Task 10: suites completas — stokki 95 OK; nucleo 316 com 1 falha (test_acoplamento_vuupt) que tambem falha no master b17398d e cuja lista no ramo e subconjunto da do master; portal 266 com 8 erros todos por falta de config no worktree — os 7 modulos afetados rodados com config falso em memoria: ramo 87/88 e master 189bb63 87/88, mesmo erro (test_tela_inicial_tem_o_botao_notificacoes, ambiente) — sem regressao. Importador 36 OK.
    Task 10: Ruling: passo 2 (worker --simular com Cliente Teste no banco local) trocado por test_simular_nao_enfileira + prova das rotas — worktree sem config/dados e o --simular le o config real — custo: nenhum.
    Final review: self? nao — revisor independente (modelo fable) leu o ramo inteiro dos 2 repos: "With fixes".
    Final: fixed I-1 transacao aberta durante HTTP na fila — test_commit_antes_das_chamadas_http RED→GREEN, commit 0086a91
    Final: fixed I-2 sem teto e sem freio quando a Stokki cai — test_teto_por_passada + test_sessao_caiu_interrompe_sem_gastar_tentativas RED→GREEN, commit 0086a91
    Final: fixed M-1 (regraduado Important: duplicado do portal de NF ja trocada pelo e-mail ficaria "NF a trocar" pra sempre, caso frequente do Quatro Estrelas) — test_duplicado_de_nf_ja_trocada_mostra_feita RED→GREEN, commit 0086a91
    Final: fixed I-4 download so com padding — test_download_sem_padding_tambem_serve RED→GREEN, commit 0086a91; suite stokki.test_trocar_nf+test_cancelar 21/21, portal test_trocas_nf+test_enviar_stokki_cross 25/25
    Final: fixed I-5 roteiro apontava pra sonda inexistente (o comando que a criaria em 25/10 foi negado inteiro) e dizia que interno.fluxos nascia sozinha — sonda versionada em stokki/sondar_trocar_nf.py, roteiro e mapa corrigidos, commit 5e514ad
    Final: Ruling: a sonda nao tem teste unitario — ferramenta operacional so leitura que chama as proprias funcoes ja testadas de trocar_nf; compila (py_compile) — custo se errado: a sonda falha na VPS e precisa de ajuste, sem efeito em producao.
    Final: fixed I-3 fallback silencioso do importador (Ruling da Task 8 revertido em parte: repo AUSENTE ainda segue pela regra do nome, so local; repo PRESENTE e cadastro ilegivel = RuntimeError; troca nao enfileirada = rodada em erro) — test_cadastro_ilegivel_com_repo_presente_para_com_erro + test_troca_que_nao_entra_na_fila_marca_erro RED→GREEN, commit e29ee71 (importador), suite 38/38, fumaca entre repos OK
    Final: suite apos a rodada de correcoes — principal 165 + stokki 96 OK; importador 38 OK.
    Final: minor (deferred): M-2 Armazenagem+Cross com XD ausente: pedidos vao em Armazenagem com aviso so no titulo da previa (conforme spec 6.3).
    Final: minor (deferred): M-3 aviso de XD ausente sai mesmo em lote so de planilha (limitado a 1/dia).
    Final: minor (deferred): M-4 importador avisa o cliente de XD ausente a cada rodada (ate 4x/dia) quando forcar_destino estiver vazio; portal limita a 1/dia.
    Final: minor (deferred): M-5 stkkc duplicado em interno (Gazzino/Magazzino) escolhe a linha por ordem de CNPJ sem aviso.
    Final: minor (deferred): M-6 duas linhas longas com espacos onde havia continuacao (enviar_stokki.py condicao do log do laco; test_trocas_nf.py).
    Final: minor (deferred): M-7 importador usa o nome do arquivo como chave da fila em vez do Id="NFe..." do XML.
