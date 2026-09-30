# Verificador automático de botões

Clica em todo botão visível das telas do painel interno e confere se o clique
dispara a ação certa, **sem que nada rode no servidor**. Fase 1 do plano de
16/09/2026: "clique e dispara somente". A fase 2 (`--consertar`, correção
automática por agente a partir do JSON) vem depois que o detector rodar limpo
por alguns dias.

## Como roda

```
py -3.11 -m verificador_botoes.verificar_botoes                 # todas as telas
py -3.11 -m verificador_botoes.verificar_botoes --tela /torre    # uma tela (repetível)
py -3.11 -m verificador_botoes.verificar_botoes --sem-subir --porta 8099
py -3.11 -m verificador_botoes.verificar_botoes --visivel        # Chromium com janela
```

Rode pelo PowerShell ou `cmd`: o Git Bash converte `/torre` em caminho do Windows.

1. Sobe o `painel_agentes` na porta 8099 com o banco local (nunca a 8070, que pode ter instância real).
2. Chromium headless loga como nível **total** (usuário/senha de `painel_agentes` no `config.yaml`).
3. Descobre as telas pelo `url_map` do Flask: rotas GET sem parâmetro fora de `/api/`.
4. Para cada botão: recarrega a tela, limpa localStorage, clica, espera 1,5 s, registra.
5. Botões que só aparecem depois de um clique (modal, menu) entram numa segunda passada (`--profundidade 1`, até `--max-filhos` por pai).
6. Botões iguais repetidos em lista (um por pedido) são clicados uma vez só; o JSON guarda `repeticoes`.

**Trava de rede:** chegam ao servidor só a carga da página (GET de navegação), arquivos
estáticos e **GET de `/api/*`** (leituras do banco local congelado, pra tela ter dados reais;
sem isso os botões que dependem de dados davam erro falso de `undefined.filter`). Todo POST,
todo `fetch` fora de `/api/` e as rotas GET cuja view menciona a Stokki (hoje só
`/api/torre/stokki`, login concorrente derruba a sessão da VPS) são respondidos no próprio
navegador com `{"ok": true, "falso_verificador": true}` e registrados. Nada escreve na Vuupt,
nenhum agente é disparado. Leituras da Vuupt pela Torre e pelos pedidos parados acontecem
(são só leitura). `--api-falsa` volta a falsificar tudo.

Nunca aponte para o painel de produção.

## Status

| Status | Significa |
|---|---|
| OK | chamou rota existente, ou mexeu na tela (DOM mudou, `window.open`, `print`) |
| LINK_OK | `<a href>` interno bate com uma rota do Flask (não é clicado) |
| NAVEGOU | mudou de página sem chamada de rede |
| ERRO_JS | exceção de JavaScript **antes** de qualquer chamada de rede (função inexistente etc.) |
| ROTA_INEXISTENTE | chamou caminho ou método que não existe no `url_map` |
| LINK_QUEBRADO | `<a href>` interno sem rota correspondente |
| PRECISA_DADOS | nada aconteceu, mas o form tinha campo obrigatório vazio ou há campo de texto vazio ao lado (não é falha) |
| SEM_EFEITO | clicou e nada aconteceu em 1,5 s |
| NAO_ENCONTRADO / NAO_CLICAVEL | sumiu após o reload, ou ficou coberto |
| DESABILITADO | `disabled`; não é clicado |

Erro de JavaScript **depois** da primeira chamada de rede não conta como falha: quase sempre
é o código engasgando na resposta falsa. Vai no detalhe como aviso.

Falhas = ERRO_JS + ROTA_INEXISTENTE + LINK_QUEBRADO + SEM_EFEITO. Código de saída 1 se houver alguma.

## Saída

`verificador_botoes/relatorios/relatorio_<data>_<hora>.md` (leitura) e `.json` (fase 2). A pasta é ignorada pelo git.

## Testes

```
py -3.11 -m unittest verificador_botoes.test_classificador verificador_botoes.test_relatorio
```

## Limitações conhecidas

- Tela `/wms` pede PIN de operador dentro da página: só a camada de fora é verificada.
- Controles do Leaflet (marcadores, popup, zoom) são pulados: são da biblioteca.
- Rodada completa leva ~30 min (o planejamento sozinho ~20). Divida com `--tela`/`--exceto` em dois processos com `--porta` diferentes.
- Rode pelo PowerShell: no Git Bash, `/torre` vira `C:/Program Files/Git/torre`.
- Botão em estado que depende de dados (ex.: só aparece com rota selecionada) pode não ser alcançado.
- Portal do cliente, atendimento e confirmação de motoristas ainda não entram (cada um tem seu login).
