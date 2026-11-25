---
name: pendencias-prompts
description: Use quando o Hugo pedir pra listar o que esta incompleto em todas as sessoes/worktrees, "atualizar a situacao", refazer a pagina "Prompts de Pendencias" ou gerar prompts pra finalizar, atualizar ou encerrar cada pendencia.
---

# Pendencias das sessoes: levantar, classificar e republicar a pagina de prompts

Mantem o artefato **Prompts de Pendencias** (https://claude.ai/artifact/TTuMrL21GM9noRRUZKJTMW). Cada pendencia vira um cartao com prompt pronto pra colar numa sessao nova (modos Finalizar, So atualizar, Encerrar; da pra juntar varios). O botao **Atualizar situacao** da pagina copia o prompt que chama esta skill.

Arquivos desta pasta:
- `modelo.html`: a pagina. Nao tem dados; carrega `pendencias.json` por fetch. So mexa nele pra mudar visual ou comportamento.
- `pendencias.json`: os dados. E o que esta skill atualiza.

## Regras

- **So leitura.** Nada de commit, push, deploy, `git worktree remove`, apagar ramo, mexer na VPS. A pagina so propoe; quem executa e uma sessao nova com OK do Hugo.
- VPS so por `py -3.11 .claude/scripts/vps_ler.py` (ver skill `vps`).
- Nao pegar valores do `config.yaml` nem colar token em prompt ou json.
- Worktrees de outras sessoes: nao reverter nem limpar nada.

## Passos

1. **Levantar o estado real** (a memoria envelhece; o git nao):
   - `git worktree list`; pra cada worktree, `git -C <wt> status --short` (sujo = trabalho que se perde) e `git log --oneline origin/master..HEAD` (commits so locais).
   - Ramos: `git for-each-ref --format='%(refname:short) %(upstream:track)' refs/heads` e `git branch --merged origin/master` (mergeado e limpo = candidato a encerrar).
   - A master local: `git status --short` e quantos commits atras/a frente do origin.
   - Merge ou rebase em andamento em algum worktree (`UU` no status) = prioridade.
   - Worktrees fora de `.claude/worktrees` tambem entram (`C:/wt_*`, `C:/agente_stokki_eventos-*`, os do scratchpad em Temp).
2. **Ler a memoria:** `MEMORY.md` da pasta de memoria do projeto, secao "Em andamento / esperando o Hugo", e as notas ligadas aos ramos achados. Marcacoes como DESLIGADO, SEM commit, falta teste, espera o Hugo viram o texto do cartao.
3. **Na VPS (leitura):** `vps_ler.py` pra `git status` em `/opt/stokki-eventos` (arquivo solto de outra sessao), `timers` e `falhas` quando uma pendencia disser "falta ver rodar".
4. **Comparar com `pendencias.json` atual** e decidir, por pendencia:
   - saiu (ramo mergeado, limpo, ou nota marcada concluida) → passa pra grupo `enc` ou sai da lista;
   - entrou (worktree novo sujo, ramo novo, nota nova) → cria cartao;
   - mudou de situacao → atualiza `meta`, `tag` e `fin`.
5. **Classificar** em `g`: `prio` (risco, conflito aberto, seguranca, trava deploy), `sujo` (codigo sem commit), `cont` (falta teste, decisao ou dado externo do Hugo), `enc` (concluido ou so sobrou o worktree). Na duvida entre grupos, escolha o mais urgente.
6. **Escrever `pendencias.json`** (atualizar `atualizado_em` com a data de hoje, AAAA-MM-DD). Cada item:
   ```
   {"id":"slug-unico","g":"prio|sujo|cont|enc","t":"titulo curto",
    "tag":["rotulo curto","red|amber|green|gray"],
    "meta":"1 frase com o fato verificado (ex.: 4 arquivos sem commit)",
    "ref":"ramo ou worktree, ou -", "mem":"nota(s) de memoria (opcional)",
    "fin":"prompt do modo Finalizar (itens prio/sujo/cont)",
    "enc":"prompt do modo Encerrar (so itens do grupo enc)"}
   ```
   O texto de `fin`/`enc` e uma ordem direta a outra sessao: o que ler, o que rodar (`py_compile`, testes do modulo), o que NAO fazer (commit, deploy, apagar) e quando parar e perguntar. Nunca prometa no `meta` o que nao conferiu; diga o que e fato do git e o que veio so da nota.
7. **Publicar:** `Artifact` com `action: "read"` na URL acima (obrigatorio antes de republicar), depois `publish` com `url` da pagina, `file_path` = `.claude/skills/pendencias-prompts/modelo.html` e `files: {"pendencias.json": ".claude/skills/pendencias-prompts/pendencias.json"}`. Se a URL nao abrir mais, publique como artefato novo e troque a URL aqui e em `URL_ARTEFATO` no `modelo.html`.
8. **Responder ao Hugo** com a diferenca desde a ultima versao: o que entrou, o que saiu, o que mudou de grupo, e os 3 a 5 itens de maior prioridade. Sem repetir a lista inteira.

## Armadilhas

- `git worktree list` mostra nomes de caminho com acento escapados; use as aspas e `git -C`.
- Ramo "behind 400" nao quer dizer abandonado: confira commits unicos (`origin/master..ramo`) antes de sugerir encerrar.
- Pasta `docs/revisoes/*.md` solta (untracked) nao e trabalho de codigo perdido, mas some se o worktree for removido: cite no cartao de encerrar.
- Nao escrever em `dados/` nem em `STATUS_*.md`.
