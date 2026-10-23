# Recebimento de retornos (insucessos que voltam ao galpão)

Data: 23/10/2026 · Pedido do Hugo · Status: design aprovado em conversa, aguardando leitura do documento

## 1. Por que

Hoje, quando o motorista volta ao galpão com pedidos que não entregou, ele descarrega num
pallet, o operador recebedor acompanha, confere a carga, aponta divergências e lança tudo no
Fresh Hub do Lovable. Esse lançamento não tem ligação nenhuma com o nosso fluxo: ninguém no
painel sabe se a caixa de um insucesso voltou de fato, se voltou inteira, ou se o motorista
nem passou no galpão.

O Hugo quer trazer esse recebimento para o nosso painel, com um desenho melhor e pensado para
o celular, e que ele seja **automático**: os retornos aparecem sozinhos a partir das baixas
de insucesso, sem ninguém precisar digitar o pedido.

## 2. Objetivos

1. Todo pedido em **insucesso** (qualquer origem da baixa) ou **entrega parcial**, exceto
   **"Não coletado"**, aparece
   sozinho numa lista de retornos a receber, com **motorista responsável**, **quantidade de
   volumes** e **motivo do retorno**.
2. O recebedor confere, pelo celular, pedido por pedido: caixas recebidas, avaria (com foto),
   motivo (confirma ou corrige), observação. Tira **foto da carga** (pallet). O **motorista
   assina** na tela.
3. **Divergência** gera alerta no grupo **Alertas** do /atendimento (Atendimento, Hugo e
   Logística leem lá).
4. Retorno **não confirmado até 07h do próximo dia útil** depois do insucesso gera alerta no
   mesmo grupo, para a Logística acompanhar com o motorista.
5. **Parcial recebida** gera alerta no grupo para a tratativa: avisar o cliente e solicitar a
   entrada no estoque do produto que voltou.
6. Pedido confirmado aparece como **"no galpão"** na Torre e na Consulta.

## 3. Fora do escopo (não fazer)

- **Não segura nem libera a reentrega.** A confirmação só registra e avisa (decisão do Hugo:
  "a princípio podemos só registrar e avisar"). A reentrega (-R1) continua no fluxo de hoje
  (`expedir_pedidos.py`, resposta do embarcador / 12h).
- **Sem WhatsApp e sem e-mail** nesta versão. O aviso vai só para o grupo Alertas do
  /atendimento (decisão do Hugo: "por enquanto só o grupo alertas"; quer tudo no mesmo lugar).
- Não mexe na Stokki (nenhum ajuste de estoque, nenhuma movimentação).
- Não desliga nem integra com o Lovable. A tela do Lovable continua no ar até o Hugo decidir.
- Retornos de Lalamove ficam de fora (só rotas Vuupt e app próprio).

## 4. Anti-objetivos (seria fracasso mesmo funcionando)

- Recebedor ter que digitar código de pedido ou procurar pedido que o sistema já sabe que
  voltou.
- Tela que só funciona bem no computador. O uso real é em pé, no galpão, com uma mão, ao lado
  do pallet.
- Alerta que vira ruído: uma mensagem por pedido, ou alerta repetido do mesmo retorno todo dia.
- Qualquer efeito colateral na reentrega, na Vuupt ou na Stokki.

## 5. Decisões do Hugo

| Tema | Decisão |
|---|---|
| Quais pedidos entram | Tudo que for insucesso, de qualquer origem (motorista, Logística, Torre, finalização em massa), menos "Não coletado"; e entregas parciais |
| Parcial | Entra; o recebimento de parcial precisa ser informado para tratativa da Logística: avisar o cliente e pedir a entrada do produto no estoque |
| Motorista guardar o retorno | Aceito até a manhã seguinte (o comum é voltar no mesmo dia); passou das 07h do próximo dia útil, precisa ficar de olho |
| O que mostra | Motorista responsável, quantidade de volumes, motivo do retorno |
| Motivo | Recebedor confirma e pode corrigir se necessário |
| Fotos | Foto da carga + foto de avaria |
| Assinatura | Campo para assinatura do motorista |
| Efeito no fluxo | Só registra e avisa (não segura reentrega) |
| Quem é avisado de divergência | Atendimento e Hugo (repassam ao cliente) e Logística (entende com o motorista) |
| Canal | Só um grupo "Alertas" criado na aba Grupos do /atendimento |
| Retorno que não chega | Alerta para Logística acompanhar, às 07h do **próximo dia útil** após o insucesso (sempre dia útil) |
| Tela | Mobile first, no nosso painel |

## 6. Como funciona

### 6.1 De onde vêm os retornos

Fonte: `nucleo_paradas` + `nucleo_rotas` (já espelham as rotas da Vuupt a cada 30 min via
`nucleo/sincronizar_vuupt.py` e são fonte de verdade das rotas `provedor=APP`).

Um pedido é **retorno esperado** quando a parada está em insucesso
(`situacao='INSUCESSO'` ou `status_done_provedor='failed'`) **ou em entrega parcial**
(`situacao='PARCIAL'`), e o motivo **não** é "Não coletado". Não importa quem fechou a parada
(motorista, Logística na Vuupt, Torre, finalização em massa): decisão do Hugo, "tudo o que for
insucesso e diferente de não coletado pode entrar". Exclusão de "Não coletado":
- Vuupt: `failed_reason_id = 5744` (ver `insucesso_entrega/motivos_falha.py`).
- App próprio: o motivo equivalente em `motivos_ocorrencia` (o builder identifica o id/texto;
  comparar por texto normalizado "nao coletado" se não houver id fixo).

**Parcial:** o núcleo não guarda quantas caixas foram entregues. Para parcial, a tela mostra
"Parcial" no lugar das caixas esperadas e o recebedor informa quantas caixas voltaram (campo
obrigatório, sem pré-preenchimento). Se o builder achar a quantidade devolvida em `dados_json`
ou nos eventos do app, usa como esperado.

Dados mostrados por pedido: `codigo`, `destinatario_nome`, `remetente_nome` (embarcador),
`volume_caixas` (caixas esperadas), `motivo_texto`, `completed_at` (hora da baixa), e da rota:
`motorista_nome`, `motorista_cpf`, `data_rota`, `nome` da rota.

Consequência conhecida: o retorno pode levar até ~30 min para aparecer depois da baixa na
Vuupt (ritmo do espelho). Aceitável, porque o motorista leva mais que isso para voltar.

A lista é calculada na leitura (consulta), cruzando com a tabela de recebimentos (6.4) para
saber o que já foi recebido. Janela: insucessos dos últimos 7 dias ainda não recebidos
(chamada do builder; ver 9).

### 6.2 A tela (`/painel/retornos`, mobile first)

**Lista (tela inicial).**
- Cartões **por motorista**: nome, quantidade de pedidos pendentes, total de caixas
  esperadas, data da rota. Pendentes de dias anteriores ficam marcados com destaque
  ("desde ontem").
- Aba ou segmento **Recebidos** (histórico): descargas já confirmadas, com data, recebedor,
  motorista, divergências, e acesso às fotos e à assinatura.

**Conferência de uma descarga** (ao tocar num motorista):
1. Lista dos pedidos pendentes daquele motorista. Cada pedido tem uma caixa de seleção
   "veio nesta descarga" (marcada por padrão). Pedido desmarcado continua pendente.
2. Para cada pedido marcado:
   - **Caixas recebidas**: número, pré-preenchido com as esperadas, com botões −/+.
   - **Avaria**: sim/não. Se sim, **foto da avaria obrigatória** (uma ou mais) para aquele
     pedido.
   - **Motivo**: mostra o motivo do motorista; o recebedor confirma ou troca por outro da
     lista de motivos (mesma lista de `motivos_falha.MOTIVOS_FALHA`).
   - **Observação**: texto livre opcional.
3. **Foto da carga** (o pallet): obrigatória, pelo menos uma por descarga.
4. **Assinatura do motorista**: campo de desenho com o dedo (canvas), com botão "limpar".
   Obrigatória.
5. Nome de quem recebeu: obrigatório (o login pode ser compartilhado por nível, igual ao
   /atendimento; lembrar o nome no aparelho).
6. Botão **Confirmar recebimento**. Grava tudo de uma vez.

Fotos: usar `<input type="file" accept="image/*" capture="environment">` (abre a câmera no
celular). Reduzir no navegador antes de enviar (ex.: lado maior 1600 px, JPEG) para não
pesar no 4G do galpão.

### 6.3 O que conta como divergência

Um pedido da descarga é divergente se qualquer um:
- caixas recebidas ≠ caixas esperadas (não se aplica a parcial sem esperado conhecido);
- avaria = sim;
- motivo corrigido (diferente do informado pelo motorista).

Parcial recebida **sempre** entra no alerta (ver 6.5), mesmo sem divergência.

Observação sozinha **não** é divergência (vai junto na mensagem se houver divergência).

### 6.4 Gravação

Tabelas novas em `dados/dados.db` (criadas de forma idempotente, no padrão do repo):

- `retornos_descargas`: id, motorista_nome, motorista_cpf, recebedor (nome), usuario (login),
  recebido_em, fotos_carga (lista de caminhos), assinatura (caminho), tem_divergencia.
- `retornos_recebidos`: id, descarga_id, codigo, parada_id, rota_id, service_id,
  caixas_esperadas, caixas_recebidas, avaria (0/1), fotos_avaria (lista), motivo_original,
  motivo_confirmado, motivo_corrigido (0/1), observacao, divergente (0/1).
  Um pedido (por parada) só pode ser recebido uma vez.
- `retornos_alertas_atraso`: parada_id (ou codigo+rota), alertado_em. Garante que o alerta das
  07h sai uma vez por retorno.

Fotos e assinatura vão para o **GCS**, pelo mesmo cliente de `documentos_pedido/storage_gcs.py`
(caminho por pedido/descarga a critério do builder). O painel serve as imagens por rota
autenticada, nunca por URL pública.

### 6.5 Alertas no grupo "Alertas" do /atendimento

Usa a infraestrutura da aba Grupos (`painel_agentes/grupos_equipe.py`, `postar`). O grupo
"Alertas" é criado automaticamente se não existir (nome exato "Alertas"). Autor da mensagem:
"Sistema" (ou "Recebimento de retornos"). Os códigos PS-… já viram link para a Consulta.

**Divergência** — sai na hora da confirmação, **uma mensagem por descarga** com divergência:
motorista, recebedor, e uma linha por pedido divergente (código, embarcador, esperado ×
recebido, avaria, motivo corrigido "de → para", observação) e o link para a descarga no
histórico.

**Parcial recebida** — entra na mesma mensagem da descarga (ou numa mensagem própria se a
descarga não tiver divergência), com a indicação "PARCIAL: avisar o cliente e solicitar a
entrada no estoque", código, embarcador e caixas que voltaram. O aviso ao cliente e a entrada
na Stokki são feitos pela equipe (nada automático para o cliente nem para a Stokki).

**Não chegou** — às **07h de dia útil** (usar `regras/feriados.py`; em fim de semana e feriado
o script não faz nada), uma mensagem com todos os retornos esperados cujo insucesso foi num
dia anterior a hoje e que não foram recebidos nem alertados antes. Assim, insucesso de sexta
ou sábado só é cobrado às 07h de segunda (ou do próximo dia útil). Motorista guardar o retorno
até a manhã seguinte é normal, por isso o corte é 07h; agrupados por motorista
(código, embarcador, caixas, motivo). Se não houver nenhum, não manda nada. Cada retorno é
alertado uma única vez (`retornos_alertas_atraso`). Roda por **timer systemd** novo na VPS
(`infra/retornos-alerta-atraso.{service,timer}`, 07h00), script em lote com `--modo-teste`
(no modo teste imprime a mensagem e não posta no grupo).

### 6.6 "No galpão" na Torre e na Consulta

Pedido com linha em `retornos_recebidos` mostra o selo **"No galpão"** (com data/hora e
recebedor) na Torre de Controle e na Consulta de pedido. Se divergente, o selo indica
divergência. Só leitura; nenhum botão novo nessas telas.

### 6.7 Acesso

- Conferir/confirmar: níveis `total`, `operador`, `logistica`, `galpao`, `expedicao`.
- Ver lista e histórico: também `atendimento` e `leitura`.
- Seguir o padrão do painel: `@requer_auth(niveis=...)` na rota, `GRUPOS_MENU`,
  `_menu_lateral_nav.html`, `acesso_telas.TELAS` (tela /acessos) e o redirecionamento de
  login do nível `galpao` (hoje vai direto pra /wms; o galpão precisa achar a tela nova no
  menu dele).
- POST de confirmação com `@exige_mesma_origem`.

## 7. Restrições do projeto

- Flask do `painel_agentes/`, templates com `url_for()` (nunca caminho absoluto; o painel vive
  atrás de `app.freshhub.com.br/painel`).
- Português em código, comentários e logs; testes `unittest` rodando de dentro de
  `painel_agentes/`.
- Testar local em porta diferente da 8070 (ex.: 8099). Banco local é cópia congelada.
- Contador do menu (se houver badge de pendentes) não pode fazer consulta cara dentro do
  request (ver `contadores_menu.py`).
- Atualizar `MAPA_DO_SISTEMA.txt` no mesmo commit (tela, tabelas, timer).

## 8. Escolhas do Claude (não decididas pelo Hugo)

- Endereço `/painel/retornos`; lista agrupada por motorista; assinatura **por descarga**
  (não por pedido); foto da carga **por descarga**; foto de avaria **por pedido**.
- Os 4 tipos de campo (caixas, avaria, motivo, observação) e a regra de divergência (6.3).
- Fonte = núcleo (não a Vuupt direto); atraso de até 30 min aceito.
- Uma mensagem por descarga; alerta de atraso uma única vez por retorno.
- Níveis de acesso (6.7). Fotos/assinatura no GCS.
- Lalamove fora.
- Caixa que volta sem o sistema esperar (ex.: Vuupt marcou entregue) fica fora desta versão.
- Nível `expedicao` também confirma (não se sabe se o recebedor usa o login do galpão ou da
  expedição).
- Parcial sem quantidade conhecida: recebedor informa as caixas.

## 9. Deixado para o builder

- Janela de busca de insucessos pendentes (sugestão 7 dias) e se retornos muito antigos
  somem da lista ou ficam em "pendências antigas".
- Como identificar "Não coletado" no app próprio (`motivos_ocorrencia`).
- Tratamento da armadilha conhecida: reentrega com o mesmo código sobrescreve a linha no
  núcleo (`project_nucleo_perde_falha_reentrega_mesmo_codigo`); chavear o recebimento por
  parada (rota + service_id), não só pelo código.
- Visual (seguir o estilo das telas mobile existentes do painel, como /atendimento/mobile e
  /wms).
- Formato exato do caminho no GCS e compressão das fotos.
