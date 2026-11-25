# Revisão de segurança: entrada da equipe no portal pelo botão Cotação da Torre

- Data: 16/11/2026
- Commit revisado: f4780be (ramo `cotacao-torre`), sobre cb8b764 (em produção)
- Módulos expostos tocados: `portal_cliente/app.py`, `portal_cliente/entrada_equipe.py`, templates `equipe.html` e `base_cliente.html`
- Lado interno: `painel_agentes/painel_agentes.py` (`/torre/cotacao`)

## O que mudou

O painel, com a pessoa logada, assina um passe (itsdangerous, segredo do portal, salt próprio, 60 s, número único) e redireciona para `/cliente/equipe/entrar?t=...&proximo=/cotacao`. O portal valida, abre a sessão de equipe e manda para a escolha do cliente.

## Resultado

Nenhum achado ALTO ou MÉDIO em aberto.

| # | Nível | Achado | Situação |
|---|-------|--------|----------|
| 1 | MÉDIO | Passe na URL podia ser reutilizado por 60 s por quem lesse a URL (log do Caddy, histórico) | CORRIGIDO antes do commit: número único, o portal recusa a segunda vez (`_passe_ja_usado`), com teste |
| 2 | BAIXO | A lista de passes usados fica em memória: um restart do `portal-cliente` reabre a janela para um passe usado nos últimos 60 s | Aceito; exige ler a URL e acertar o restart dentro de 1 min |
| 3 | BAIXO | Login por GET: alguém da equipe com acesso ao portal pode mandar um link com o próprio passe e fazer outra pessoa abrir o portal logada como ele (derruba a sessão que ela tinha) | Aceito; exige alguém de dentro, tem 60 s e as ações ficam no nome de quem gerou o passe |
| 4 | BAIXO | Sessão de equipe dos logins compartilhados do config dura 14 dias e não cai com "Derrubar todas as sessões" | Já era assim no login `/equipe` por senha; não mudou |

## Conferido e sem problema

- **Quem entra:** o passe só prova quem está logado no painel. O portal decide de novo: login nominal precisa estar ativo, sem senha provisória e com "Portal do cliente" marcado (entra como operador, reconferido a cada request); par fixo só total/operador/leitura e o nome tem que bater com o do config para aquele nível. Nível "total" forjado num passe de login nominal não tem efeito (teste). Conta nominal apagada depois do login não entra.
- **Forjar o passe:** exige `portal_cliente.secret_key`. Painel sem essa chave assina com segredo vazio e o portal recusa.
- **Redirecionamento aberto:** `proximo` só é usado por `_proximo_seguro` (caminho relativo dentro do app); `//site` cai no início (teste).
- **XSS:** `proximo` entra em `url_for` e em atributo com autoescape do Jinja.
- **Vazamento por Referer:** a URL com o passe responde com redirect; na recusa o passe já está queimado ou inválido.
- **Memória:** só passe com assinatura válida entra na lista, que é limpa a cada uso.
- **Quem gera passe:** `/torre/cotacao` exige login no painel com os mesmos níveis da Torre (e respeita telas personalizadas pelo padrão `^/torre`).
