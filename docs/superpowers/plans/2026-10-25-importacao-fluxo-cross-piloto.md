# Fluxo Cross — roteiro de piloto e virada

Escrito em 26/10/2026 para quem for colocar o fluxo Cross em produção (o Hugo, ou uma sessão do Claude com ele acompanhando). Spec: `docs/superpowers/specs/2026-10-24-importacao-fluxo-cross-design.md`. Plano: `docs/superpowers/plans/2026-10-25-importacao-fluxo-cross.md`.

Nada aqui roda sem o Hugo pedir. Cada passo tem a prova que o fecha; não siga para o próximo sem ela.

## O que vai pra produção

- **Repo principal** (ramo `fluxo-cross`): `nucleo/fluxo_embarcador.py`, `stokki/trocar_nf.py`, `portal_cliente/trocas_nf.py`, `portal_cliente/enfileirar_trocas_virada.py`, mudanças em `portal_cliente/envio_pedidos.py`, `app.py`, `enviar_stokki.py`, `gerenciar_clientes.py`, `templates/_envios.html`.
- **Importador** (ramo `fluxo-cross` do `agente_importacao_stokki`): `etapa2_processamento_xml/processar_xml.py`, `main.py`, `relatorio_email.py`, `avisos_cliente.py`.
- **Banco de produção** ganha sozinho, na primeira conexão do portal: colunas `fluxo`, `id_stokki`, `troca_nf_status`, `troca_nf_em`, `troca_nf_erro` em `portal_envios`; tabelas `trocas_nf_pendentes` e `avisos_xd_ausente`. A coluna `interno.fluxos` **não** nasce sozinha: só a carga do Passo 3 a cria, e até lá todo embarcador continua em Armazenagem.

## Mudanças que a operação sente

- **Quatro Estrelas** passa a sair com o item XD na quantidade de volumes da nota (antes era 1 fixo), pelo e-mail e pelo portal.
- **Fruta Fina e Fazenda Burin** (Armazenagem + Cross) passam a escolher o fluxo de cada pedido na tela de envio do portal. Avise os dois antes de ligar. Eles não podem mais usar importação por e-mail.
- **Muai, Maíz, Vidaveg, Khappy** (Full cross) e os de Armazenagem: nada muda.
- Pedido Cross aparece na lista do portal com os selos "Cross" e "NF a trocar", depois "NF trocada".

## Passo 0 — Sonda que ficou pendente (depois do Passo 2; precisa de autorização de leitura em produção)

A troca de NF confere o resultado baixando o XML do pedido. Não foi possível provar esse download por HTTP na área admin (leitura em produção negada em 25/10). Sem ele, a conferência cai para "o caminho da NF mudou", mais fraca.

```bash
ssh -i ~/.ssh/atendimento_vps root@187.127.52.197 "cd /opt/stokki-eventos && sudo -u www-data venv/bin/python stokki/sondar_trocar_nf.py"
```

A sonda vem com o deploy do Passo 2 (rode depois dele), é só leitura e usa a sessão já salva, sem login. Esperado: `_token ... True` e `download OK, N item(ns), chave ok`, terminando em "Conclusao: conferencia por itens". Se terminar em "caminho", a troca funciona do mesmo jeito, mas a idempotência (não repostar em pedido já trocado) depende do download: registre e avise o Hugo antes da virada.

## Passo 1 — Conferir o saldo do XD (equipe)

Para cada embarcador Cross, o item XD consome a quantidade de volumes de cada pedido. A equipe confere na Stokki que há saldo do XD de: Quatro Estrelas (98), Dourado (18), Facilita (10), Jatobá (15), Jersey Vale (79), Dansken (85), De Tommaso/CIAO (87), Morota (77), Fruta Fina (94), Fazenda Burin (12).

## Passo 2 — Deploy do repo principal

Siga a skill `deploy-vps`. Resumo:

```bash
ssh -i ~/.ssh/atendimento_vps root@187.127.52.197 "cd /opt/stokki-eventos && git status --short && git log --oneline -3"
# merge do ramo fluxo-cross no master + push (só com o Hugo pedindo), depois na VPS:
ssh -i ~/.ssh/atendimento_vps root@187.127.52.197 "cd /opt/stokki-eventos && git pull --no-rebase && chown -R www-data:www-data /opt/stokki-eventos && systemctl restart portal-cliente portal-cliente-envios"
```

Prova: `systemctl show -p ActiveState --value portal-cliente-envios` = `active`, e o journal do worker sem traceback no primeiro ciclo.

## Passo 3 — Carga dos fluxos

Exporte as marcações da página "Fluxo por embarcador" (https://claude.ai/artifact/BWYxiXrDfVhiD37z3SSbER) com `ArtifactData list` da coleção `fluxos`, num arquivo JSON com a lista de documentos (`[{"id": ..., "data": {...}}, ...]`). Depois:

```bash
scp -i ~/.ssh/atendimento_vps fluxos.json root@187.127.52.197:/tmp/fluxos.json
ssh -i ~/.ssh/atendimento_vps root@187.127.52.197 "chmod a+r /tmp/fluxos.json && cd /opt/stokki-eventos && sudo -u www-data venv/bin/python portal_cliente/gerenciar_clientes.py fluxos --carga /tmp/fluxos.json"
```

Prova: a saída lista cada embarcador Cross com `XD <código>`. Qualquer `XD NÃO ENCONTRADO` precisa ser resolvido (produto XD inativo, ou sincronização de produtos atrasada) antes do piloto. O Cliente Teste fica com os três fluxos de propósito.

## Passo 4 — Piloto com um XML do Quatro Estrelas, pelo portal

1. O Hugo entra em `/cliente/equipe`, escolhe o Quatro Estrelas e envia **um** XML de NF-e real.
2. O worker cria o pedido. Conferir na Stokki: item `XD...` com quantidade igual aos volumes da nota.
3. No ciclo seguinte (até 20 s), a troca de NF. Conferir na lista do portal o selo "NF trocada" e, na Stokki, a DANFE do pedido com os produtos reais.
4. Banco, só leitura: `py -3.11 .claude/scripts/vps_ler.py sql "SELECT numero_nf, status, tentativas, erro FROM trocas_nf_pendentes ORDER BY id DESC LIMIT 5"`.

Se a troca falhar, o erro fica na linha e o worker tenta de novo em 10 min; na terceira falha, e-mail para o atendimento. Pare o piloto e investigue antes de seguir.

## Passo 5 — Virada dos pedidos já abertos

```bash
ssh -i ~/.ssh/atendimento_vps root@187.127.52.197 "cd /opt/stokki-eventos && sudo -u www-data venv/bin/python portal_cliente/enfileirar_trocas_virada.py --modo-teste"
```

Confira a lista: quantos pedidos com XML original, quantos para troca manual. Com o Hugo de acordo:

```bash
ssh -i ~/.ssh/atendimento_vps root@187.127.52.197 "cd /opt/stokki-eventos && sudo -u www-data venv/bin/python portal_cliente/enfileirar_trocas_virada.py --executar"
```

Os de "troca manual" são feitos à mão na Stokki (Pedidos de Saída, Opções, Informar Nota Fiscal).

## Passo 6 — Importador por e-mail

O deploy do importador é por `scp`, não `git pull`. Faça backup antes:

```bash
ssh -i ~/.ssh/atendimento_vps root@187.127.52.197 "cd /opt/agente-importacao-stokki && for f in etapa2_processamento_xml/processar_xml.py main.py relatorio_email.py; do cp \$f /root/\$(basename \$f).bak-fluxo-cross; done"
scp -i ~/.ssh/atendimento_vps etapa2_processamento_xml/processar_xml.py root@187.127.52.197:/opt/agente-importacao-stokki/etapa2_processamento_xml/
scp -i ~/.ssh/atendimento_vps main.py relatorio_email.py avisos_cliente.py root@187.127.52.197:/opt/agente-importacao-stokki/
ssh -i ~/.ssh/atendimento_vps root@187.127.52.197 "chown -R www-data:www-data /opt/agente-importacao-stokki && cd /opt/agente-importacao-stokki && sudo -u www-data /opt/stokki-eventos/venv/bin/python main.py 'Empório Quatro Estrelas' --modo-teste"
```

Prova do `--modo-teste`: termina sem traceback e o log mostra a etapa 2 com a regra cross (não manda e-mail ao cliente, não cria pedido). O aviso de XD ausente ao cliente só sai de verdade com `email.forcar_destino: ""` no config do cliente; sem a chave, vai para o Hugo.

Depois, conferir a primeira rodada real (16:20) com a skill `conferir-rodada`: relatório com o selo "NF p/ trocar" e as linhas novas em `trocas_nf_pendentes` com origem `email`.
