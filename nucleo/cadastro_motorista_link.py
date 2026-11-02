# -*- coding: utf-8 -*-
"""
nucleo/cadastro_motorista_link.py

Depois do "Enviar" do cadastro pelo link (Hugo, 28/10): cria o agente e o
veículo na Vuupt, grava a linha na bd_motoristas (pro roteirizador alocar),
avisa o grupo e manda as boas-vindas ao motorista.

Cada agente novo é uma licença paga: duplicar é a pior falha. Por isso
cada etapa guarda o resultado em `motoristas_cadastros` assim que sai e
pula quando já foi feita; o e-mail é gravado ANTES do POST, e uma
retentativa procura esse e-mail na Vuupt antes de criar outro (a resposta
pode ter se perdido depois de a Vuupt criar). A trava `processando_em`
impede duas rodadas no mesmo cadastro ao mesmo tempo.

COMO USAR (timer a cada 15 min na VPS):
    venv/bin/python nucleo/cadastro_motorista_link.py --retentar
    venv/bin/python nucleo/cadastro_motorista_link.py --id 12
    venv/bin/python nucleo/cadastro_motorista_link.py --retentar --modo-teste   # Vuupt e WhatsApp de mentira, nada gravado
"""
import argparse
import itertools
import logging
import re
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

_RAIZ = Path(__file__).resolve().parent.parent
if str(_RAIZ) not in sys.path:
    sys.path.insert(0, str(_RAIZ))

from nucleo import banco  # noqa: E402
from nucleo.cadastro_motorista import ORIGEM_LINK, STATUS_RECUSADO, CadastroInvalido, buscar_cadastro  # noqa: E402
from nucleo.vuupt_agentes import (  # noqa: E402
    ErroVuupt, VuuptAgentes, base_operacional_mais_comum, gerar_email, mesmo_telefone,
)
from regras import motoristas_banco  # noqa: E402

logger = logging.getLogger("nucleo.cadastro_motorista_link")

LINK_APP_VUUPT = "https://play.google.com/store/apps/details?id=com.vuupt.vuuptdriver"
ORIGEM_GRUPO = "cadastro_motorista"
TRAVA_MINUTOS = 10
MAX_TENTATIVAS = 20
MAX_EMAILS = 9
# nao_enviado = canal em pausa ou teto do dia: o motorista ainda não recebeu o login
BOAS_VINDAS_DE_NOVO = ("falhou", "indeterminado", "nao_enviado")
AVISO_GRUPO_DE_NOVO = ("falhou", "nao_enviado")
# Quem vem pelo link entra como esporádico; o "Fixo" pedido fica em
# motoristas_cadastros.posicao e a equipe promove pelo Editar (decisão de 02/11).
POSICAO_TABELA = "ESPORADICO"
MOTIVO_DESFEITO = "desfeito no painel"
ROTULOS = {"conflito": "conferência do telefone e da placa na Vuupt", "agente": "agente na Vuupt",
           "veiculo": "veículo na Vuupt", "tabela": "linha na tabela de motoristas"}
_FMT = "%Y-%m-%d %H:%M:%S"


class _PendenciaVuupt(ErroVuupt):
    """Erro que a equipe precisa conferir: a mensagem vai inteira como pendência pro grupo."""


def _avisar_grupo_padrao(config, tipo, texto, assinatura, modo_teste=False):
    import notificar_whatsapp
    return notificar_whatsapp.despachar(config, ORIGEM_GRUPO, tipo, texto, assinatura, modo_teste=modo_teste)


def _avisar_motorista_padrao(cadastro_id, telefone, texto, config, modo_teste=False):
    import notificar_whatsapp
    return notificar_whatsapp.avisar_motorista_boas_vindas(cadastro_id, telefone, texto, config,
                                                           modo_teste=modo_teste)


def _gravar(conn, cadastro_id: int, **campos) -> None:
    conn.execute(f"UPDATE motoristas_cadastros SET {', '.join(f'{k} = ?' for k in campos)} WHERE id = ?",
                 (*campos.values(), cadastro_id))
    conn.commit()


def _email(valor) -> str:
    return str(valor or "").strip().lower()


def _placa(valor) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(valor or "").upper())


def _url_ficha(config: dict) -> str:
    cfg = (config.get("portal_cliente") or {}).get("chamados") or {}
    return (cfg.get("url_painel") or "https://app.freshhub.com.br/painel").rstrip("/") + "/motoristas"


def _motivo_curto(exc: Exception) -> str:
    if isinstance(exc, ErroVuupt):
        return f"HTTP {exc.status}" if exc.status else "sem resposta"
    return "erro inesperado"


# ── Etapas ────────────────────────────────────────────────────────────────────

def _dono_do_telefone(conn, agentes: list, c: dict):
    """agent_id de quem já usa o telefone (Vuupt ou tabela), ou None. Ignora
    agente apagado e o agente com o nosso próprio e-mail (POST anterior cuja
    resposta se perdeu)."""
    nosso = _email(c.get("vuupt_email"))
    for a in agentes:
        if a.get("deleted_at") or (nosso and _email(a.get("email")) == nosso):
            continue
        if a.get("phone_number") and mesmo_telefone(a["phone_number"], c["telefone"]):
            return int(a["id"])
    for linha in motoristas_banco.listar(conn):
        # linha sem agente não tem a quem apontar o conflito (e não roteiriza)
        if linha["AGENT_ID_VUUPT"] is not None and linha.get("TELEFONE_MOTORISTA") and mesmo_telefone(str(linha["TELEFONE_MOTORISTA"]), c["telefone"]):
            return linha["AGENT_ID_VUUPT"]
    return None


def _criar_agente(conn, vuupt, agentes: list, c: dict) -> int:
    nosso = _email(c.get("vuupt_email"))
    if nosso:
        achado = next((a for a in agentes if not a.get("deleted_at") and _email(a.get("email")) == nosso), None)
        if achado:
            logger.info("cadastro #%s: agente %s já existia na Vuupt com %s", c["id"], achado["id"], nosso)
            # e-mail gravado por este cadastro antes do POST: o agente é nosso
            _gravar(conn, c["id"], agent_id=int(achado["id"]), agente_criado=1)
            return int(achado["id"])
    # e-mail de agente apagado continua ocupado (a Vuupt não deixa reusar)
    ocupados = {_email(a.get("email")) for a in agentes if a.get("email")}
    ocupados |= {_email(r[0]) for r in conn.execute(
        "SELECT vuupt_email FROM motoristas_cadastros WHERE id != ? AND vuupt_email IS NOT NULL", (c["id"],))}
    ocupados |= {_email(r["EMAIL_MOTORISTA"]) for r in motoristas_banco.listar(conn) if r.get("EMAIL_MOTORISTA")}
    digitos = c.get("vuupt_senha_digitos") or 6
    anterior = nosso   # e-mail de uma tentativa anterior: a Vuupt pode ter criado sem a lista mostrar
    for _ in range(MAX_EMAILS):
        email = gerar_email(c["nome"], ocupados)
        # gravado antes do POST: é por ele que a retentativa acha o agente se a resposta se perder
        _gravar(conn, c["id"], vuupt_email=email, vuupt_senha_digitos=digitos)
        c["vuupt_email"], c["vuupt_senha_digitos"] = email, digitos
        try:
            agent_id = vuupt.criar_agente(c["nome"], email, c["cpf"][:digitos], c["telefone"])
        except ErroVuupt as e:
            corpo = str(e.corpo or "").lower()
            if e.status == 422 and "password" in corpo and digitos == 6:
                digitos = 8
                continue
            if e.status == 422 and "email" in corpo:
                if anterior and _email(email) == anterior:
                    # pode ser o nosso agente de um POST cuja resposta se perdeu: outro e-mail = licença dobrada
                    raise _PendenciaVuupt(f"e-mail {email} já existe na Vuupt: confira se o agente foi criado",
                                          422, e.corpo)
                ocupados.add(email)
                continue
            raise
        _gravar(conn, c["id"], agent_id=agent_id, agente_criado=1)
        logger.info("cadastro #%s: agente %s criado na Vuupt (%s)", c["id"], agent_id, email)
        return agent_id
    raise ErroVuupt(f"Vuupt recusou {MAX_EMAILS} tentativas de criar o agente", 422, "")


def _veiculo_da_placa(veiculos: list, c: dict):
    return next((v for v in veiculos if _placa(v.get("license_plate")) == _placa(c["placa"])), None)


def _dono_da_placa(veiculos: list, agentes: list, c: dict):
    """Outro agente (não apagado) que já está com o veículo da placa, ou None.
    A resolução no painel vale só pro agente que estava em conflito."""
    existente = _veiculo_da_placa(veiculos, c)
    if not existente:
        return None
    for a in agentes:
        if (not a.get("deleted_at") and a.get("vehicle_id") is not None and int(a["vehicle_id"]) == int(existente["id"])
                and int(a["id"]) != c.get("agent_id")):
            dono = int(a["id"])
            return None if (c["conflito_resolvido"] and c["conflito_agent_id"] == dono) else dono
    return None


def _veiculo(conn, vuupt, c: dict):
    """Cria ou reaproveita o veículo e liga ao agente. Devolve o agent_id de
    quem já tem o veículo da placa (conflito: nada é ligado) ou None.
    A placa já foi conferida antes de criar o agente; aqui confere de novo
    (pode ter sido ligada a outro agente entre as etapas)."""
    if not c.get("vehicle_id"):
        veiculos = vuupt.listar_veiculos()
        existente = _veiculo_da_placa(veiculos, c)
        if existente:
            dono = _dono_da_placa(veiculos, vuupt.listar_agentes(), c)
            if dono is not None:
                return dono
        vehicle_id = (int(existente["id"]) if existente else
                      vuupt.criar_veiculo(c["placa"], base_operacional_mais_comum(veiculos), c["agent_id"]))
        # vehicle_criado: só o veículo criado aqui pode ser apagado no "Desfazer"
        _gravar(conn, c["id"], vehicle_id=vehicle_id, vehicle_criado=0 if existente else 1)
        c["vehicle_id"] = vehicle_id
        logger.info("cadastro #%s: veículo %s (%s)", c["id"], vehicle_id, "reaproveitado" if existente else "criado")
    # PUT idempotente: repete enquanto a tabela não foi gravada (ligar pode ter falhado antes)
    if not c.get("bd_motorista_id"):
        vuupt.ligar_veiculo(c["agent_id"], c["vehicle_id"])
    return None


def _linha_da_tabela(conn, agent_id: int):
    ids = [r["id"] for r in motoristas_banco.listar(conn) if r["AGENT_ID_VUUPT"] == agent_id]
    return ids[0] if ids else None


def _tabela(conn, config: dict, c: dict) -> None:
    bd_id = _linha_da_tabela(conn, c["agent_id"])
    if bd_id is not None and c.get("vehicle_id"):
        # motorista que volta: a linha antiga passa a apontar pro veículo deste cadastro
        linha = next(r for r in motoristas_banco.listar(conn) if r["id"] == bd_id)
        if linha.get("VEHICLE_ID_VUUPT") != c["vehicle_id"]:
            motoristas_banco.atualizar(conn, bd_id, {"VEHICLE_ID_VUUPT": c["vehicle_id"]}, ORIGEM_LINK)
    if bd_id is None:
        from regras.cadastro_motoristas import cadastrar_motorista
        cadastrar_motorista(config, {
            "agent_id": c["agent_id"], "nome": c["nome"], "ativo": True, "max_rotas_dia": 1,
            "aceita_viagens": False, "dias_disponiveis": c["dias"], "zonas_preferidas": c["zonas"],
            "tipo_veiculo": c["tipo_veiculo"], "telefone": c["telefone"], "email": c["vuupt_email"],
            "placa": c["placa"], "cpf": c["cpf"], "vehicle_id": c["vehicle_id"],
            "apenas_carga_seca": not c.get("refrigerado"), "sem_app": c.get("celular") == "IPHONE",
            "posicao": POSICAO_TABELA,
        }, origem=ORIGEM_LINK, conn=conn)
        bd_id = _linha_da_tabela(conn, c["agent_id"])
    _gravar(conn, c["id"], bd_motorista_id=bd_id)
    c["bd_motorista_id"] = bd_id


# ── Processamento ─────────────────────────────────────────────────────────────

def processar(conn: sqlite3.Connection, config: dict, cadastro_id: int, *, vuupt=None, avisar_grupo=None,
              avisar_motorista=None, agora: datetime | None = None) -> dict:
    """Leva um cadastro enviado até o fim (ou até onde der). Devolve
    {"situacao": concluido | pendente | conflito | ocupado | ignorado, "erro": str | None}.
    `ignorado` = não é do link, não foi enviado ou já concluiu (nada é feito)."""
    agora = agora or datetime.now()
    if _ignorar(buscar_cadastro(conn, cadastro_id)):
        return {"situacao": "ignorado", "erro": None}
    trava = agora.strftime(_FMT)
    cur = conn.execute("UPDATE motoristas_cadastros SET processando_em = ? WHERE id = ? "
                       "AND (processando_em IS NULL OR processando_em < ?)",
                       (trava, cadastro_id, (agora - timedelta(minutes=TRAVA_MINUTOS)).strftime(_FMT)))
    conn.commit()
    if cur.rowcount == 0:
        return {"situacao": "ocupado", "erro": None}
    try:
        return _processar(conn, config, cadastro_id, vuupt or VuuptAgentes(config["vuupt_api"]["token"]),
                          avisar_grupo or _avisar_grupo_padrao, avisar_motorista or _avisar_motorista_padrao, agora)
    finally:
        # só solta a própria trava (outra rodada pode ter assumido a vencida)
        conn.execute("UPDATE motoristas_cadastros SET processando_em = NULL WHERE id = ? AND processando_em = ?",
                     (cadastro_id, trava))
        conn.commit()


def _pendente(conn, cadastro_id: int, erro: str) -> dict:
    """Etapa final (aviso) que precisa de outra rodada: conta tentativa."""
    conn.execute("UPDATE motoristas_cadastros SET ultimo_erro = ?, tentativas = tentativas + 1 WHERE id = ?",
                 (erro, cadastro_id))
    conn.commit()
    return {"situacao": "pendente", "erro": erro}


def _ignorar(c) -> bool:
    return not c or c.get("origem") != ORIGEM_LINK or not c.get("enviado_em") or bool(c.get("concluido_em"))


def _registrar_conflito(conn, cadastro_id: int, dono: int, tipo: str) -> None:
    _gravar(conn, cadastro_id, conflito_agent_id=dono, conflito_resolvido=0, conflito_tipo=tipo)


def _processar(conn, config, cadastro_id, vuupt, avisar_grupo, avisar_motorista, agora) -> dict:
    import notificar_whatsapp as nw
    c = buscar_cadastro(conn, cadastro_id)
    if _ignorar(c):   # concluído por outra rodada entre a leitura e a trava
        return {"situacao": "ignorado", "erro": None}
    situacao, pendencia, erro, etapa = None, None, None, "conflito"
    try:
        agentes = None
        if not c["agent_id"]:
            agentes = vuupt.listar_agentes()
            dono = None if c["conflito_resolvido"] else _dono_do_telefone(conn, agentes, c)
            if dono is not None:
                _registrar_conflito(conn, cadastro_id, dono, "telefone")
                situacao, pendencia = "conflito", f"telefone já é do agente {dono} — resolver no painel"
                logger.warning("cadastro #%s: telefone já é do agente %s", cadastro_id, dono)
            else:
                # placa antes do POST: cada agente é uma licença paga
                dono = _dono_da_placa(vuupt.listar_veiculos(), agentes, c)
                if dono is not None:
                    _registrar_conflito(conn, cadastro_id, dono, "placa")
                    situacao = "conflito"
                    pendencia = f"placa {c['placa']} já é do veículo do agente {dono} — resolver no painel"
                    logger.warning("cadastro #%s: placa %s já é do agente %s", cadastro_id, c["placa"], dono)
        if situacao is None:
            etapa = "agente"
            if not c["agent_id"]:
                c["agent_id"] = _criar_agente(conn, vuupt, agentes, c)
            etapa = "veiculo"
            dono = _veiculo(conn, vuupt, c)
            if dono is not None:
                _registrar_conflito(conn, cadastro_id, dono, "placa")
                situacao = "conflito"
                pendencia = f"placa {c['placa']} já é do veículo do agente {dono} — resolver no painel"
                logger.warning("cadastro #%s: placa %s já é do agente %s", cadastro_id, c["placa"], dono)
        if situacao is None:
            etapa = "tabela"
            if not c["bd_motorista_id"]:
                _tabela(conn, config, c)
    except Exception as e:
        if isinstance(e, ErroVuupt):
            logger.warning("cadastro #%s: %s falhou: %s", cadastro_id, etapa, e)
        else:
            logger.exception("cadastro #%s: %s falhou", cadastro_id, etapa)
        erro = f"{etapa}: {e}"[:500]
        conn.execute("UPDATE motoristas_cadastros SET ultimo_erro = ?, tentativas = tentativas + 1 WHERE id = ?",
                     (erro, cadastro_id))
        conn.commit()
        situacao = "pendente"
        pendencia = (str(e) if isinstance(e, _PendenciaVuupt) else
                     f"{ROTULOS[etapa]} pendente ({_motivo_curto(e)}); nova tentativa em 15 min")

    cad_texto = {**c, "iphone": c.get("celular") == "IPHONE"}
    aviso = None
    if not c["aviso_grupo_em"]:
        texto = nw.texto_cadastro_link(cad_texto, _url_ficha(config), pendencia, agora=agora)
        aviso = avisar_grupo(config, "cadastro_link", texto, f"cadastro_link:{cadastro_id}:inicio")
        if aviso not in AVISO_GRUPO_DE_NOVO:
            # sem pendência o próprio aviso já diz que deu tudo certo: não há "concluído" a mandar
            _gravar(conn, cadastro_id, aviso_grupo_em=agora.strftime(_FMT),
                    **({} if pendencia else {"aviso_concluido_em": agora.strftime(_FMT)}))
            c["aviso_grupo_em"] = agora.strftime(_FMT)
            if not pendencia:
                c["aviso_concluido_em"] = c["aviso_grupo_em"]
    if situacao:
        return {"situacao": situacao, "erro": erro}

    bv = c["boas_vindas_situacao"]
    if not bv or bv in BOAS_VINDAS_DE_NOVO:
        digitos = c["vuupt_senha_digitos"] or 6
        link_app = (config.get("cadastro_motorista_link") or {}).get("link_app_vuupt") or LINK_APP_VUUPT
        texto = nw.texto_boas_vindas_motorista(c["nome"].split(" ")[0], c["vuupt_email"], c["cpf"][:digitos],
                                               link_app, cad_texto["iphone"])
        bv = avisar_motorista(cadastro_id, c["telefone"], texto, config)
        _gravar(conn, cadastro_id, boas_vindas_situacao=bv)
        if bv in BOAS_VINDAS_DE_NOVO:
            return _pendente(conn, cadastro_id, f"boas-vindas: {bv}")
    if aviso in AVISO_GRUPO_DE_NOVO:
        # canal em pausa, teto do dia ou gateway fora: sem isso o aviso se perderia ao concluir
        return _pendente(conn, cadastro_id, f"aviso do grupo: {aviso}")

    if c["aviso_grupo_em"] and not c["aviso_concluido_em"]:
        texto = nw.texto_cadastro_link_concluido(cad_texto, agora=agora)
        aviso = avisar_grupo(config, "cadastro_link_concluido", texto, f"cadastro_link:{cadastro_id}:concluido")
        if aviso in AVISO_GRUPO_DE_NOVO:
            return _pendente(conn, cadastro_id, f"aviso de concluído do grupo: {aviso}")
        _gravar(conn, cadastro_id, aviso_concluido_em=agora.strftime(_FMT))
    _gravar(conn, cadastro_id, concluido_em=agora.strftime(_FMT), ultimo_erro=None)
    logger.info("cadastro #%s concluído (agente %s, veículo %s)", cadastro_id, c["agent_id"], c["vehicle_id"])
    return {"situacao": "concluido", "erro": None}


def _agente_ativo(agentes: list, agent_id: int):
    """O agente (não apagado) com esse id na lista da Vuupt, ou None."""
    return next((a for a in agentes if a.get("id") is not None and int(a["id"]) == int(agent_id)
                 and not a.get("deleted_at")), None)


def agente_da_vuupt(config: dict, agent_id: int, *, vuupt=None) -> dict | None:
    """{id, nome, telefone} do agente da Vuupt (confirmação do "usar agente"
    no painel), ou None se não existe ou foi apagado."""
    vuupt = vuupt or VuuptAgentes(config["vuupt_api"]["token"])
    a = _agente_ativo(vuupt.listar_agentes(), agent_id)
    return {"id": int(a["id"]), "nome": a.get("name"), "telefone": a.get("phone_number")} if a else None


def _limite_trava(agora: datetime | None) -> str:
    return ((agora or datetime.now()) - timedelta(minutes=TRAVA_MINUTOS)).strftime(_FMT)


def resolver_conflito(conn: sqlite3.Connection, config: dict, cadastro_id: int, usar_agent_id: int | None,
                      usuario: str, *, vuupt=None, **kw) -> dict:
    """Decisão da equipe no painel: `usar_agent_id` = é a mesma pessoa (usa esse
    agente, sem criar outro nem mandar login); None = é outra pessoa (cria agente
    novo). Serve também ao e-mail que já existe na Vuupt (pendente sem conflito).
    Depois chama `processar` e devolve o resultado dele."""
    from regras.cadastro_motoristas import _colunas_do_formulario, _espelhar
    c = buscar_cadastro(conn, cadastro_id)
    if not c or c.get("origem") != ORIGEM_LINK or not c.get("enviado_em"):
        raise CadastroInvalido("Cadastro não encontrado.", 404)
    if c.get("concluido_em"):
        raise CadastroInvalido("Cadastro já concluído.", 409)
    campos = {"conflito_resolvido": 1, "revisado_por": usuario, "revisado_em": banco.agora()}
    if usar_agent_id is None:
        _gravar(conn, cadastro_id, **campos)
    else:
        usar_agent_id = int(usar_agent_id)
        if c.get("agent_id") and int(c["agent_id"]) != usar_agent_id:
            raise CadastroInvalido(f"Este cadastro já tem agente na Vuupt ({c['agent_id']}).", 409)
        vuupt = vuupt or VuuptAgentes(config["vuupt_api"]["token"])
        achado = _agente_ativo(vuupt.listar_agentes(), usar_agent_id)
        if not achado:
            raise CadastroInvalido(f"Agente {usar_agent_id} não existe na Vuupt (ou foi apagado).", 400)
        # agente_criado = 0: agente que já existia nunca é apagado pelo "Desfazer"
        campos.update(agent_id=usar_agent_id, agente_criado=0, ultimo_erro=None, boas_vindas_situacao="agente_existente")
        if achado.get("email"):
            campos["vuupt_email"] = _email(achado["email"])
        # condicional: uma rodada do processar em andamento pode estar criando o agente agora
        cur = conn.execute(
            f"UPDATE motoristas_cadastros SET {', '.join(f'{k} = ?' for k in campos)} WHERE id = ? "
            "AND (agent_id IS NULL OR agent_id = ?) AND (processando_em IS NULL OR processando_em < ?)",
            (*campos.values(), cadastro_id, usar_agent_id, _limite_trava(kw.get("agora"))))
        conn.commit()
        if cur.rowcount == 0:
            raise CadastroInvalido("Cadastro em processamento, tente de novo em instantes.", 409)
        linha_id = _linha_da_tabela(conn, usar_agent_id)
        if linha_id is not None:
            # motorista que volta: a linha antiga vira a deste cadastro (processar só grava o vínculo)
            atual = next(r for r in motoristas_banco.listar(conn) if r["id"] == linha_id)
            novos = _colunas_do_formulario({
                "ativo": True, "dias_disponiveis": c["dias"], "zonas_preferidas": c["zonas"],
                "tipo_veiculo": c["tipo_veiculo"], "telefone": c["telefone"], "placa": c["placa"],
                "posicao": POSICAO_TABELA, "apenas_carga_seca": not c.get("refrigerado"),
                "sem_app": c.get("celular") == "IPHONE"})
            if not atual.get("CPF_MOTORISTA"):
                novos["CPF_MOTORISTA"] = c["cpf"]
            motoristas_banco.atualizar(conn, linha_id, novos, f"link:{usuario}")
            _espelhar(conn)
    logger.info("cadastro #%s: conflito resolvido por %s (%s)", cadastro_id, usuario,
                f"usar agente {usar_agent_id}" if usar_agent_id is not None else "agente novo")
    return processar(conn, config, cadastro_id, vuupt=vuupt, **kw)


def desfazer_cadastro_link(conn: sqlite3.Connection, config: dict, cadastro_id: int, usuario: str, *,
                           apagar_na_vuupt: bool, vuupt=None, agora: datetime | None = None) -> dict:
    """Cadastro falso ou de teste (botão do painel): inativa a linha da
    bd_motoristas, libera o CPF pra um cadastro novo e encerra o cadastro
    (RECUSADO, sai do retentar). Com `apagar_na_vuupt`, apaga o veículo e o
    agente só se foram criados por este cadastro (agente vindo de "usar
    agente" nunca). Erro da Vuupt volta em "erro" sem desfazer o resto.
    Pode rodar de novo (apaga na Vuupt o que ficou)."""
    from regras.cadastro_motoristas import _espelhar
    c = buscar_cadastro(conn, cadastro_id)
    if not c or c.get("origem") != ORIGEM_LINK:
        raise CadastroInvalido("Cadastro não encontrado.", 404)
    agora = agora or datetime.now()
    cur = conn.execute(
        "UPDATE motoristas_cadastros SET status = ?, motivo = ?, revisado_por = ?, revisado_em = ?, "
        "concluido_em = COALESCE(concluido_em, ?) WHERE id = ? AND (processando_em IS NULL OR processando_em < ?)",
        (STATUS_RECUSADO, MOTIVO_DESFEITO, usuario, banco.agora(), agora.strftime(_FMT), cadastro_id,
         _limite_trava(agora)))
    conn.commit()
    if cur.rowcount == 0:
        raise CadastroInvalido("Cadastro em processamento, tente de novo em instantes.", 409)
    origem = f"link-desfeito:{usuario}"
    linha = next((r for r in motoristas_banco.listar(conn) if r["id"] == c.get("bd_motorista_id")), None)
    if linha:
        motoristas_banco.atualizar(conn, linha["id"], {"ATIVO": "NAO"}, origem)
        # só o CPF deste cadastro: linha reaproveitada pode ter o CPF de outra pessoa
        if re.sub(r"\D", "", str(linha.get("CPF_MOTORISTA") or "")) == c["cpf"]:
            motoristas_banco.liberar_cpf(conn, linha["id"], origem)
        _espelhar(conn)
    apagados, erros = [], []
    if apagar_na_vuupt and (c.get("vehicle_criado") or c.get("agente_criado")):
        vuupt = vuupt or VuuptAgentes(config["vuupt_api"]["token"])
        for rotulo, chave, coluna, apagar in (("veículo", "vehicle_id", "vehicle_criado", vuupt.apagar_veiculo),
                                              ("agente", "agent_id", "agente_criado", vuupt.apagar_agente)):
            if not (c.get(coluna) and c.get(chave)):
                continue
            try:
                apagar(int(c[chave]))
            except Exception as e:
                logger.warning("cadastro #%s: apagar %s %s na Vuupt falhou: %s", cadastro_id, rotulo, c[chave], e)
                erros.append(f"{rotulo} {c[chave]}: {e}")
                continue
            _gravar(conn, cadastro_id, **{coluna: 0})
            apagados.append(f"{rotulo} {c[chave]}")
    logger.info("cadastro #%s desfeito por %s (apagados na Vuupt: %s)", cadastro_id, usuario, ", ".join(apagados) or "nada")
    return {"situacao": "desfeito", "erro": "; ".join(erros)[:500] or None, "apagados": apagados}


def retentar(conn: sqlite3.Connection, config: dict, **kw) -> list[dict]:
    """Todos os cadastros do link enviados e não concluídos, sem conflito em
    aberto e com menos de MAX_TENTATIVAS. Cada item = {"id", "situacao", "erro"}."""
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM motoristas_cadastros WHERE origem = ? AND enviado_em IS NOT NULL AND concluido_em IS NULL "
        "AND tentativas < ? AND NOT (conflito_agent_id IS NOT NULL AND conflito_resolvido = 0) ORDER BY id",
        (ORIGEM_LINK, MAX_TENTATIVAS))]
    resultados = []
    for i in ids:
        try:
            resultados.append({"id": i, **processar(conn, config, i, **kw)})
        except Exception as e:
            # um cadastro com defeito não pode travar os de id maior
            logger.exception("cadastro #%s: erro fora das etapas", i)
            erro = f"inesperado: {e}"[:500]
            try:
                conn.rollback()
                conn.execute("UPDATE motoristas_cadastros SET ultimo_erro = ?, tentativas = tentativas + 1 WHERE id = ?",
                             (erro, i))
                conn.commit()
            except Exception:
                logger.exception("cadastro #%s: nem o erro deu pra gravar", i)
            resultados.append({"id": i, "situacao": "pendente", "erro": erro})
    return resultados


# ── Linha de comando ──────────────────────────────────────────────────────────

class _VuuptDeTeste:
    """--modo-teste: nada vai pra Vuupt; ids falsos crescentes e log."""

    def __init__(self):
        self._ids = itertools.count(900_000_001)

    def listar_agentes(self):
        return []

    def listar_veiculos(self):
        return []

    def criar_agente(self, nome, email, senha, telefone):
        novo = next(self._ids)
        logger.info("[TESTE] criaria o agente %s (%s) -> id falso %s", nome, email, novo)
        return novo

    def criar_veiculo(self, placa, base_operacional_id, agent_id):
        novo = next(self._ids)
        logger.info("[TESTE] criaria o veículo %s (base %s) -> id falso %s", placa, base_operacional_id, novo)
        return novo

    def ligar_veiculo(self, agent_id, vehicle_id):
        logger.info("[TESTE] ligaria o veículo %s ao agente %s", vehicle_id, agent_id)


def _carregar_config() -> dict:
    import yaml
    caminho = next((c for c in (_RAIZ / "config.yaml", Path.cwd() / "config.yaml") if c.exists()), None)
    return (yaml.safe_load(caminho.read_text(encoding="utf-8")) or {}) if caminho else {}


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Cria agente/veículo na Vuupt e a linha na tabela pro cadastro pelo link.")
    parser.add_argument("--retentar", action="store_true", help="todos os enviados e não concluídos")
    parser.add_argument("--id", type=int, help="só este cadastro")
    parser.add_argument("--modo-teste", action="store_true", help="Vuupt e WhatsApp de mentira; nada é gravado")
    args = parser.parse_args(argv)
    if not args.retentar and not args.id:
        parser.error("use --retentar ou --id N")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s - %(message)s")

    config = _carregar_config()
    conn = banco.conectar()
    espelho_original = motoristas_banco.escrever_espelho
    try:
        if args.modo_teste:
            # roda numa cópia em memória: os ids falsos não chegam no banco nem na planilha-espelho
            copia = sqlite3.connect(":memory:")
            conn.backup(copia)
            conn.close()
            conn = copia
            conn.row_factory = sqlite3.Row
            motoristas_banco.escrever_espelho = lambda *a, **k: True
            kw = {"vuupt": _VuuptDeTeste(),
                  "avisar_grupo": lambda cfg, t, tx, a: _avisar_grupo_padrao(cfg, t, tx, a, modo_teste=True),
                  "avisar_motorista": lambda i, tel, tx, cfg: _avisar_motorista_padrao(i, tel, tx, cfg, modo_teste=True)}
        else:
            if not (config.get("vuupt_api") or {}).get("token"):
                logger.error("vuupt_api.token ausente no config.yaml.")
                return 1
            kw = {"vuupt": VuuptAgentes(config["vuupt_api"]["token"])}
        resultados = ([{"id": args.id, **processar(conn, config, args.id, **kw)}] if args.id
                      else retentar(conn, config, **kw))
    finally:
        motoristas_banco.escrever_espelho = espelho_original
        conn.close()
    for r in resultados:
        logger.info("%scadastro #%s: %s%s", "[TESTE] " if args.modo_teste else "", r["id"], r["situacao"],
                    f" ({r['erro']})" if r["erro"] else "")
    logger.info("%s cadastro(s) processado(s).", len(resultados))
    return 0


if __name__ == "__main__":
    sys.exit(main())
