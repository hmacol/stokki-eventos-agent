# Revisão de segurança — app motorista: troca de PIN, áudio na Ajuda (08/10/2026)

Commit revisado: `b6c7970` (base `e275c98`, em produção na VPS antes do deploy).
Escopo: `git diff e275c98 b6c7970 -- nucleo/api_motorista.py nucleo/auth_motorista.py
nucleo/chamados_motorista.py nucleo/motoristas_cli.py portal_cliente/chamados.py
painel_agentes/atendimento_chamados.py painel_agentes/templates/atendimento*.html
app_motorista/src/api.ts`. Estratégia FOCUSED (repo médio): leitura integral do diff,
callers dos símbolos alterados, histórico dos módulos (`1181053`, `f467248`, `e91ef88`).

## Resumo

| Sev. | Achado | Onde |
|------|--------|------|
| MÉDIO | PIN inicial igual pra todos (123456) + conta criada em lote = janela de acesso com só o CPF, até o motorista trocar | `motoristas_cli.importar-planilha --pin-inicial`, `auth.autenticar` |
| BAIXO | `trocar_pin=1` não é imposto pelo servidor: só o app prende o motorista na troca | `api_motorista.requer_motorista` |
| BAIXO | PIN novo sem regra de força (aceita 111111, 123123...) | `auth.trocar_pin` / `validar_pin_formato` |
| BAIXO | Áudio validado só pela extensão `.m4a`; conteúdo não é conferido | `chamados.guardar_anexos`, `chamados_motorista.atd_mensagem` |
| INFO | `.m4a` passa a ser aceito também no portal do cliente (módulo compartilhado) | `portal_cliente/chamados.py` |

Nenhum achado ALTO. Nada de validação removida; nenhum controle de acesso afrouxado.

## Detalhes

### 1. MÉDIO — PIN inicial conhecido por todos
Decisão do Hugo (08/10): todos começam com `123456` e o app obriga a troca no 1º acesso.
Até o motorista instalar e trocar, qualquer pessoa que saiba o CPF dele entra como ele:
vê rotas (endereços de clientes), extrato, aceita/recusa rota, lê o chat. Pior caso:
alguém troca o PIN antes do motorista e o bloqueia (precisa de `resetar-pin`).
A trava de 5 tentativas/15 min não ajuda, porque o PIN é conhecido. Mesmo padrão do
achado A1 da revisão do portal (06/10), aqui com CPF em vez de CNPJ (menos público).
**Mitigação operacional:** criar o login só de quem vai instalar agora e mandar a
mensagem do grupo no mesmo dia; conferir com `motoristas_cli.py listar` quem ainda não
fez login depois de alguns dias e desativar/resetar.
**Mitigação técnica (opcional, não feita):** com `trocar_pin=1`, a API responder 403 em
tudo exceto `/api/eu` e `/api/trocar-pin` — reduz o que vaza antes da troca (achado 2).

### 2. BAIXO — flag `trocar_pin` só no cliente
`POST /api/trocar-pin` exige token e confere o PIN atual pela mesma `autenticar`
(bloqueio de tentativas preservado), troca o salt/hash (derruba sessões antigas) e
devolve tokens novos: correto. Mas o servidor não recusa as outras rotas enquanto
`trocar_pin=1`; um cliente modificado opera com o PIN provisório. Risco depende do 1.

### 3. BAIXO — PIN fraco permitido
`validar_pin_formato` só exige 6 dígitos; o novo só precisa diferir do atual. Sugestão
barata: recusar sequências (123456, 654321) e repetição (111111). Decisão do Hugo.

### 4. BAIXO — áudio sem conferência de conteúdo
Qualquer arquivo até 10 MB chamado `*.m4a` é gravado em `dados/chamados/<id>/` e servido
inline como `audio/mp4` (`send_file` com `mimetype` explícito, `conditional=True`).
Content-Type fixo evita o navegador tratar como HTML; `caminho_anexo` já barra traversal
(inalterado). Mesmo nível de risco das fotos `.jpg` já aceitas. A regra "áudio só com a
logística" É imposta no servidor (409 com `COM_ASSISTENTE`, conferida antes de gravar).

### 5. INFO — portal do cliente
`EXTENSOES_ANEXO` é compartilhada: o cliente do portal também pode anexar `.m4a` (a
mensagem de erro ainda diz "foto, PDF ou planilha"). Sem impacto de segurança.

## Correções (08/10, tarde — Hugo escolheu "blindar o PIN")
- Achado 2 CORRIGIDO: `requer_motorista` responde 403 em tudo exceto `/api/eu` e
  `/api/trocar-pin` enquanto `trocar_pin=1`; o app não inicia fila nem push até trocar.
- Achado 3 CORRIGIDO: `auth.validar_pin_forte` recusa todos iguais, sequência
  crescente/decrescente e `123456` (só na troca pelo motorista; o PIN da operação
  continua livre). Mesma regra no app antes de chamar a API.
- Achado 1 fica mitigado: com o PIN provisório o invasor só consegue trocar o PIN
  (lockout do motorista → `resetar-pin`), sem ver rota, extrato ou chat.
- Teste `test_troca_de_pin_obrigatoria_no_primeiro_acesso` ampliado (403 antes, 200
  depois, 6 PINs fracos recusados).

## Cobertura de testes
- Coberto: `test_troca_de_pin_obrigatoria_no_primeiro_acesso` (401/400/200, token
  antigo morre, PIN novo entra), `test_audio_so_no_atendimento_humano` (409 com
  assistente, 200 com logística, download inline `audio/*`).
- Não coberto: regra de PIN fraco (não existe), `.m4a` pelo portal, templates do painel.

## Raio de impacto
- `criar_ou_atualizar_motorista`: 3 chamadas no CLI + testes; kwarg novo com default,
  compatível. `auth.publico`: login/eu/trocar-pin. `EXTENSOES_ANEXO`: 2 usos em
  chamados.py (upload do portal e e-mail). Templates do painel: só a bolha da conversa.
- Clientes do APK 1.0.0 (ainda instalado no celular do Hugo) continuam funcionando: a
  API só acrescenta campos/rotas.

## Limites desta revisão
Feita pela mesma sessão que escreveu o código, sem agente adversarial separado e sem
teste no aparelho. O app (`ajuda.tsx`, `trocar-pin.tsx`) foi lido, não executado.
