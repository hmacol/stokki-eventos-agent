# Revisão: /equipe/cliente esconde membros de grupo (05/11)

Diff: portal_cliente/app.py, auth_cliente.py, templates/equipe.html, test_grupos.py (17 linhas).

- Rota continua exigindo `g.equipe` (login do painel); nada muda no acesso.
- `embarcadores_para_equipe` só lê `portal_grupos` + `interno` (SQL fixo, sem parâmetro do usuário) e reduz a lista.
- Template: `e.nome` e `e.empresas_grupo` saem pelo autoescape do Jinja; `empresas_grupo` é int calculado no servidor.
- POST com CNPJ de membro continua aceito (`_cliente_da_equipe`), igual antes: a mudança é só de exibição, não de permissão. A equipe já enxergava qualquer embarcador.

Achados: nenhum ALTO/MÉDIO/BAIXO.
