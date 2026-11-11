# Aula do portal do cliente (vídeos automáticos) — desenho

Data: 06/11/2026. Pedido do Hugo: vídeos de demonstração do portal do cliente
para os embarcadores, bem visuais, mostrando o fluxo de verdade.

## Intenção

O cliente (embarcador) precisa sair sabendo usar o portal sozinho. Público:
quem está entrando agora e quem já tem acesso e não usa (os da tela
`/adesao-portal`). Hoje não existe material de treinamento; tudo é explicado
por WhatsApp. A "Pedir alteração da entrega" está sendo liberada para os
clientes em 06/11, então precisa estar na aula desde o início.

## Objetivos

- Uma aula em `app.freshhub.com.br/cliente/aula`, aberta sem login, com
  vídeos curtos (1 a 3 min), um por tarefa.
- Vídeos produzidos 100% automaticamente: o navegador (Playwright) clica e
  digita sozinho no portal real, com legenda na tela, destaque visual no
  clique e narração gerada.
- Regravar um vídeo com um comando quando a tela mudar.

## Não objetivos

- Não toca a Stokki nem a Vuupt, em nenhum vídeo (decisão do Hugo, 06/11),
  nem para os lançamentos de envio e cancelamento.
- Não mostra o que está desligado ou em piloto: endereço de entrega na
  prévia, calculadora de frete dedicado, aba de pedidos de entrada.
- Sem editor de vídeo, sem trilha sonora, sem vídeo hospedado fora
  (YouTube etc.).

## Anti-objetivos (seria fracasso mesmo funcionando)

- Aparecer nome, CNPJ, endereço ou NF de cliente real em qualquer quadro.
- Um chamado, e-mail, WhatsApp ou pedido de teste chegar à equipe, a um
  cliente ou à operação.
- O vídeo de alteração da entrega dar a entender que o cliente muda a entrega
  sozinho. Ele **pede**; vira chamado na área Entrega e a equipe trata.
- Narração robótica (descartada a voz Maria do Windows).

## Decisões do Hugo

- Vários vídeos curtos montados numa aula num site, não um vídeo único.
- Cobrir basicamente tudo que o cliente usa, incluindo a alteração da entrega.
- Produção automática: cliques sozinhos, legendas e narração gerada.
- Voz: **Thalita** (`pt-BR-ThalitaMultilingualNeural`, edge-tts).
- Endereço: `cliente/aula`.
- Estados que dependem da entrega (em rota, entregue, canhoto, insucesso) e
  as ações que iriam à Stokki ou à Vuupt vêm de dados de teste montados só
  no navegador da gravação.
- Dependências instaladas no Python 3.11 da máquina do Hugo em 06/11:
  `edge-tts` e `imageio-ffmpeg` (ffmpeg embutido, sem instalar nada no sistema).

## Lista de vídeos (ordem da aula)

1. Primeiro acesso: CNPJ, PIN, troca do PIN padrão, completar cadastro.
2. Acompanhar o dia: status de cada pedido, rota, previsão, filtros.
3. Pedir alteração da entrega (endereço, data, horário) e o que acontece depois.
4. Baixar o canhoto e exportar a planilha.
5. Enviar pedidos: XML ou planilha, prévia, confirmar, corrigir erros.
6. Responder a um insucesso.
7. Cancelar um pedido.
8. Retomar um pedido em espera.
9. Chat e chamados com a Fresh.
10. Notificações: e-mails e resumo das 20h.
11. Regiões e dias de atendimento (página pública `/regioes`).

Saiu da lista o vídeo "Equipe: dar acesso a outras pessoas": o cliente não tem
essa função. `/equipe` e `/equipe/cliente` são da equipe Fresh Log (login do
painel escolhendo um cliente). Foi um erro meu na primeira lista.

Detalhes por vídeo (minhas escolhas, revisor apontou):
- **1. Primeiro acesso:** ensina o caminho que a Fresh usa hoje: o cliente
  recebe o PIN da Fresh, entra e o portal obriga a trocar; depois completa o
  cadastro. No fim, uma frase sobre "Esqueci o PIN". O PIN padrão **nunca**
  aparece digitado em claro, nem é falado, nem se diz que é igual para todos
  (risco ALTO da revisão de segurança; a aula é pública). O campo de PIN é
  mascarado e a narração diz "o PIN que você recebeu da Fresh".
- **3. Alteração da entrega:** só vai para a página depois que a chave
  `portal_cliente.alteracao_entrega` estiver ligada para todos em produção
  (o Hugo libera em 06/11). Até lá o vídeo fica fora da lista.
- **9. Chat:** mostra a triagem do assistente como o cliente vê, com falas
  roteirizadas (interceptando `/api/atendimento/*`, sem Claude na gravação,
  para a fala ser sempre a mesma e casar com a narração), o "Deixar chamado"
  e uma resposta da equipe chegando (também montada).
- **10. Notificações:** apresenta os 8 tipos da janela
  (`preferencias_notificacao.TIPOS`) como funcionando. Decisão do Hugo (06/11):
  ele vai ativar todos para os clientes. Pré-condição de publicação: o vídeo 10
  só entra na página depois que os 8 tipos estiverem chegando de verdade aos
  clientes (sem `forcar_destino` para hugo@). Ligar é do Hugo; a gravação não
  muda nada disso.

Cada vídeo abre com um cartão de título ("Como enviar pedidos") e fecha
dizendo qual é o próximo.

## Como é feito

**Portal da gravação.** O código do portal em produção (origin/master, não a
master local, que está atrás e tem edições de outras sessões), numa cópia
separada do repo (worktree), porque o portal lê o `config.yaml` de caminho fixo
na raiz do repo; o config de gravação é o `config.yaml` dessa cópia. Rodando na
máquina do Hugo numa porta livre (≠ 8070/8074), com:
- banco descartável criado do zero (não uma cópia do `dados/dados.db`, para não
  carregar nenhum dado real), só com o cliente fictício e o que cada vídeo
  precisa (envios, chamados, notificações), recriado antes de cada vídeo;
- config de gravação próprio, sem credenciais de envio (SMTP, WhatsApp,
  Vuupt, Stokki, Claude), com a chave `alteracao_entrega` ligada para o
  cliente fictício. Se uma rota tentar falar com fora, deve falhar, não enviar;
- nenhum worker rodando (`enviar_stokki`, `chamados ler`).

**Respostas montadas.** O Playwright intercepta (`page.route`) as chamadas
que dependem de Stokki/Vuupt e responde com dados de teste: `/api/dia`
(pedidos em rota, entregue, insucesso, em espera), `/api/canhoto/<id>`
(imagem de canhoto fictícia), `/api/atendimento/*` (vídeo 9) e as ações que a cópia do banco não resolve
sozinha. Interceptar só o necessário; o resto passa pelo portal real.

**Dados fictícios.** Cliente "Mercado Exemplo" (minha escolha), CNPJ do
cliente teste (`00.000.000/0001-91`), destinatários, endereços, NFs e
motoristas inventados. Datas relativas ao dia da gravação.

**Roteiro.** Cada vídeo é um arquivo de roteiro com passos: ação do
navegador + frase narrada + legenda. A narração de cada passo é gerada antes
(edge-tts, Thalita); o tempo do passo é o da fala, para áudio e tela
andarem juntos.

**Visual na gravação.** Viewport 1280x720 (desktop). Injetados na página:
legenda embaixo, círculo de destaque onde o robô vai clicar, cursor visível,
digitação em velocidade humana.

**Montagem.** ffmpeg junta vídeo (webm do Playwright) + áudio, corta o
início em branco e gera `mp4` (H.264 + AAC) e um quadro de capa por vídeo.

**Comando.** Um script com `--video N` (regrava um) e `--todos`, rodando
com `py -3.11`.

## A página `cliente/aula`

- Rota pública no `portal_cliente/app.py`, liberada no `before_request`
  como `/regioes` (entra em `_LIVRES_ANTES_DA_TROCA` e no que mais exigir).
- Lista dos 11 vídeos em ordem, cada um com título, uma linha do que ensina,
  duração e capa; player na página; pode assistir em qualquer ordem.
- Link no cabeçalho do portal (`base_cliente.html`, junto do botão de
  Regiões) e link na tela de login para quem ainda não entrou.
- Templates com `url_for()`, nunca caminho absoluto (ProxyFix com prefixo).
- Funciona no celular (muitos vão abrir pelo link do WhatsApp).
- Entra no `MAPA_DO_SISTEMA.txt` no mesmo commit.

## Deixado para quem construir

- Estrutura de pastas do gerador e formato do roteiro.
- Onde os mp4 ficam e como chegam à VPS (são gerados, ~50 MB no total; minha
  inclinação é não versionar no git e mandar por scp para uma pasta servida
  pelo portal, mas cabe ao plano decidir).
- Texto exato das narrações, desde que siga a lista acima e os anti-objetivos.
- Como montar a cópia do banco com o mínimo de dados.

## Verificação

- Assistir cada vídeo inteiro antes de publicar: fala alinhada à tela,
  nenhum dado real, nenhuma mensagem de erro aparecendo.
- Durante a gravação, nenhuma requisição saindo para Stokki, Vuupt, SMTP ou
  WhatsApp (registrar as requisições de rede do Playwright e conferir).
- Testes do portal continuam passando (`py -3.11 -m unittest` dos
  `portal_cliente/test_*.py`).
- Deploy: `/cliente/aula` abre sem login em produção e os vídeos tocam.
