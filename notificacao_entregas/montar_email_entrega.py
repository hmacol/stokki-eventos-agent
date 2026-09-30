# -*- coding: utf-8 -*-
"""
notificacao_entregas/montar_email_entrega.py

Assunto e HTML do e-mail de entrega concluída (1 por pedido, pro
embarcador). Puro: recebe o serviço da Vuupt + a linha do embarcador e
devolve texto. Ver DOC_EXECUCAO_CLAUDE_NOTIFICACAO_ENTREGAS.md.

Decisões do Hugo (17/09): motorista NÃO aparece; na falha o e-mail só
informa (os botões de reenvio continuam no e-mail de insucesso).
"""
import re
import sys
from html import escape
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
if str(_RAIZ) not in sys.path:
    sys.path.insert(0, str(_RAIZ))
# append (não insert): insucesso_entrega/ tem um expedir_pedidos.py próprio
# que não pode sombrear o da raiz.
if str(_RAIZ / "insucesso_entrega") not in sys.path:
    sys.path.append(str(_RAIZ / "insucesso_entrega"))

from email_utils import (  # noqa: E402
    COR_ACENTO, COR_BORDA, COR_DESTAQUE, COR_ERRO, COR_FUNDO, COR_PRIMARIA, COR_PRIMARIA_CLARA,
    COR_TEXTO, COR_TEXTO_SUAVE, envelope_html,
)
from motivos_falha import MOTIVOS_FALHA  # noqa: E402

from notificacao_entregas.regras_entrega import codigo_limpo, concluido_em_local, volumes_reais  # noqa: E402
from retiradas.regras_retirada import dados_da_nota, eh_servico_retirada  # noqa: E402

COR_ERRO_CLARA = "#FEF2F2"
MOTIVO_NAO_INFORMADO = "Motivo não informado"
_PADRAO_CEP = re.compile(r"^\d{5}-?\d{3}$")


def _desembrulhar(obj: dict, *chaves: str) -> dict:
    """include=customer/failedReason vem como {"data": {...}} ou direto."""
    for chave in chaves:
        v = (obj or {}).get(chave)
        if isinstance(v, dict):
            return v["data"] if isinstance(v.get("data"), dict) else v
    return {}


def _endereco(servico: dict) -> str:
    """Sem CEP/país e sem parte repetida (mesma limpeza do portal), com
    o complemento no fim."""
    partes: list[str] = []
    for p in (servico.get("address") or "").split(","):
        p = p.strip()
        if not p or p.lower() in ("brasil", "brazil") or _PADRAO_CEP.match(p):
            continue
        if partes and (p.lower() == partes[-1].lower() or partes[-1].lower().endswith(p.lower())):
            continue
        partes.append(p)
    endereco = ", ".join(partes)
    complemento = (servico.get("address_complement") or "").strip()
    return f"{endereco} · {complemento}" if complemento else endereco


# Rotulos da planilha BD_TRANSPORTADORAS (tipo RETIRADA) que nao sao uma
# empresa de verdade, mais o fallback "cliente" de montar_payload_retirada.
# Pra esses o e-mail nao diz "retirado por X": nao sabemos quem foi.
_QUEM_RETIRA_GENERICO = re.compile(
    r"^\s*(cliente(\s+retira)?|retirada\s+pessoal|coleta\s+f[aá]brica|transportadora\s+coleta)\s*$", re.IGNORECASE)


def motivo_para_cliente(servico: dict) -> str:
    """Texto do motivo pra quem é de FORA: de-para manual -> descrição
    oficial da Vuupt -> genérico. Não usa motivos_falha.texto_do_motivo
    porque ele acrescenta avisos internos ("novo -- sem regra de
    duplicação", "ainda não cadastrado no de-para")."""
    info = MOTIVOS_FALHA.get(servico.get("failed_reason_id"))
    if info:
        return info["texto"]
    descricao = (_desembrulhar(servico, "failedReason", "failed_reason").get("description") or "").strip()
    return descricao or MOTIVO_NAO_INFORMADO


def dados_do_servico(servico: dict, embarcador: dict, nf: str, com_canhoto: bool) -> dict:
    sucesso = servico.get("status_done") != "failed"
    codigo = codigo_limpo(servico.get("code"))
    cliente = _desembrulhar(servico, "customer")
    if eh_servico_retirada(servico):
        # Na retirada o customer/endereco do servico sao o PROPRIO galpao e
        # a nota e texto interno ("nao roteirizar..."): o que interessa ao
        # embarcador (quem retirou, destinatario final) vem carimbado na nota.
        nota = dados_da_nota(servico)
        return {
            "tipo": "retirada", "codigo": codigo, "sucesso": True, "reentrega": False,
            "embarcador": (embarcador or {}).get("nome") or "",
            "destinatario": nota["destinatario"],
            "quem_retira": "" if _QUEM_RETIRA_GENERICO.match(nota["quem_retira"]) else nota["quem_retira"],
            "endereco": "", "observacoes": "", "motivo": "", "com_canhoto": False,
            "concluido_em": concluido_em_local(servico.get("completed_at")),
            "nf": (nf or "").strip(),
            "volumes": volumes_reais(servico.get("dimension_3"), (embarcador or {}).get("fator_ponderado")),
        }
    return {
        "tipo": "entrega", "quem_retira": "",
        "codigo": codigo,
        "sucesso": sucesso,
        "reentrega": bool(re.search(r"-R\d+$", codigo)),
        "embarcador": (embarcador or {}).get("nome") or "",
        "destinatario": (cliente.get("name") or servico.get("title") or "").strip()[:120],
        "endereco": _endereco(servico),
        "concluido_em": concluido_em_local(servico.get("completed_at")),
        "nf": (nf or "").strip(),
        "volumes": volumes_reais(servico.get("dimension_3"), (embarcador or {}).get("fator_ponderado")),
        "observacoes": (servico.get("note") or "").strip(),
        "motivo": "" if sucesso else motivo_para_cliente(servico),
        "com_canhoto": bool(com_canhoto) and sucesso,
    }


def montar_assunto(dados: dict) -> str:
    if dados["tipo"] == "retirada":
        complemento = dados["quem_retira"] or dados["destinatario"]
        cabeca = f"Pedido {dados['codigo']} retirado"
    elif dados["sucesso"]:
        complemento = dados["destinatario"]
        cabeca = f"Pedido {dados['codigo']} entregue"
    else:
        complemento = dados["motivo"]
        cabeca = f"Pedido {dados['codigo']} não entregue"
    complemento = " ".join(complemento.split())[:70]
    return f"{cabeca} · {complemento}" if complemento else cabeca


# ── HTML ───────────────────────────────────────────────────────────────────────

def _pilula(texto: str, cor_fundo: str) -> str:
    return (f'<span style="display:inline-block;padding:5px 14px;border-radius:14px;font-size:12px;'
            f'font-weight:800;letter-spacing:.6px;color:#FFFFFF;background:{cor_fundo};">{texto}</span>')


def _linha(rotulo: str, valor) -> str:
    if valor in (None, ""):
        return ""
    return f"""
        <tr>
          <td width="32%" style="padding:10px 14px;border-top:1px solid {COR_BORDA};font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.4px;color:{COR_TEXTO_SUAVE};vertical-align:top;">{rotulo}</td>
          <td style="padding:10px 14px;border-top:1px solid {COR_BORDA};font-size:14px;line-height:1.5;color:{COR_TEXTO};vertical-align:top;">{valor}</td>
        </tr>"""


def _bloco(texto_html: str, cor_borda: str, cor_fundo: str) -> str:
    return (f'<div style="margin:0 0 20px 0;padding:14px 16px;border-left:4px solid {cor_borda};'
            f'background:{cor_fundo};border-radius:6px;font-size:14px;line-height:1.6;color:{COR_TEXTO};">'
            f'{texto_html}</div>')


def montar_html(dados: dict, portal_url: str, promete_email_reenvio: bool = False, aviso_topo: str = "") -> str:
    """promete_email_reenvio: só True quando o e-mail de insucesso com
    botões está LIGADO (notificacoes_automaticas.ativo) -- senão o texto
    prometeria um e-mail que não vai chegar. aviso_topo: faixa tracejada
    do modo teste / forcar_destino ("iria para X")."""
    sucesso = dados["sucesso"]
    codigo = escape(dados["codigo"])
    codigo_forte = f'<strong style="white-space:nowrap;">{codigo}</strong>'

    retirada = dados["tipo"] == "retirada"
    if retirada:
        pilula = _pilula("RETIRADO", COR_ACENTO)
        titulo = f"Pedido {codigo} foi retirado"
        por = f" por <strong>{escape(dados['quem_retira'])}</strong>" if dados["quem_retira"] else ""
        abertura = (f"Olá! O pedido {codigo_forte} foi retirado no nosso galpão{por}"
                    f"{', com saída registrada em ' + escape(dados['concluido_em']) if dados['concluido_em'] else ''}.")
        destaque = ""
    elif sucesso:
        pilula = _pilula("ENTREGUE", COR_ACENTO)
        titulo = f"Pedido {codigo} foi entregue"
        abertura = (f"Olá! A entrega do pedido {codigo_forte} foi concluída"
                    f"{' em ' + escape(dados['concluido_em']) if dados['concluido_em'] else ''}.")
        if dados["com_canhoto"]:
            destaque = _bloco("O <strong>canhoto assinado</strong> segue em anexo neste e-mail (PDF).",
                              COR_ACENTO, COR_PRIMARIA_CLARA)
        else:
            destaque = _bloco("O canhoto desta entrega <strong>ainda não foi processado</strong>. "
                              "Assim que estiver disponível, ele aparece no portal do cliente.",
                              COR_DESTAQUE, "#FFFBEB")
    else:
        pilula = _pilula("NÃO ENTREGUE", COR_ERRO)
        titulo = f"Pedido {codigo} não foi entregue"
        abertura = (f"Olá! A tentativa de entrega do pedido {codigo_forte}"
                    f"{' em ' + escape(dados['concluido_em']) if dados['concluido_em'] else ''}"
                    f" <strong>não foi concluída</strong>.")
        proximo = ("Você receberá em seguida um e-mail para decidir sobre o reenvio."
                   if promete_email_reenvio else
                   "Nossa equipe vai tratar a próxima tentativa. Se quiser orientar o reenvio, responda este e-mail.")
        destaque = _bloco(f'<span style="font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.4px;'
                          f'color:{COR_ERRO};">Motivo</span><br>'
                          f'<strong style="font-size:16px;">{escape(dados["motivo"])}</strong><br>{proximo}',
                          COR_ERRO, COR_ERRO_CLARA)

    if dados["reentrega"]:
        pilula += " " + _pilula("REENTREGA", COR_PRIMARIA)

    linhas = "".join([
        _linha("Pedido", codigo),
        _linha("Nota fiscal", escape(dados["nf"])),
        _linha("Destinatário final" if retirada else "Destinatário", escape(dados["destinatario"])),
        _linha("Retirado por", escape(dados["quem_retira"])),
        _linha("Endereço", escape(dados["endereco"])),
        _linha("Saída registrada em" if retirada else "Concluído em" if sucesso else "Tentativa em",
               escape(dados["concluido_em"])),
        _linha("Volumes", dados["volumes"]),
        _linha("Observações", escape(dados["observacoes"]).replace("\n", "<br>")),
    ])

    topo = ""
    if aviso_topo:
        topo = (f'<div style="margin:0 0 20px 0;padding:12px 14px;border:2px dashed #b45309;border-radius:8px;'
                f'background:#fffbeb;font-size:12px;color:#78350f;line-height:1.6;">{escape(aviso_topo)}</div>')

    conteudo = f"""{topo}
      <p style="margin:0 0 8px 0;font-size:11px;font-weight:700;letter-spacing:.8px;text-transform:uppercase;color:{COR_TEXTO_SUAVE};">Acompanhamento de entrega{' · ' + escape(dados['embarcador']) if dados['embarcador'] else ''}</p>
      <p style="margin:0 0 10px 0;font-size:22px;font-weight:800;color:{COR_PRIMARIA};line-height:1.2;">{titulo}</p>
      <p style="margin:0 0 18px 0;">{pilula}</p>
      <p style="margin:0 0 18px 0;font-size:15px;color:{COR_TEXTO};line-height:1.6;">{abertura}</p>
      {destaque}
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
             style="border:1px solid {COR_BORDA};border-radius:8px;border-collapse:separate;overflow:hidden;">
        <tr style="background:{COR_FUNDO};">
          <th colspan="2" align="left" style="padding:8px 14px;font-size:11px;text-transform:uppercase;letter-spacing:.5px;color:{COR_TEXTO_SUAVE};">Dados do pedido</th>
        </tr>{linhas}
      </table>
      <p style="margin:22px 0 0 0;">
        <a href="{escape(portal_url, quote=True)}" style="display:inline-block;padding:11px 20px;border-radius:8px;background:{COR_PRIMARIA};color:#FFFFFF;font-size:14px;font-weight:700;text-decoration:none;">Acompanhar no portal do cliente</a>
      </p>
      <p style="margin:20px 0 0 0;font-size:14px;color:{COR_TEXTO};line-height:1.6;">
        Dúvidas? É só responder este e-mail.<br><strong>Freshlog Logística</strong>
      </p>"""

    return envelope_html(
        conteudo, cor_acento=COR_ACENTO if sucesso else COR_ERRO,
        rodape="Você recebe este aviso a cada pedido concluído porque é o contato de logística cadastrado "
               "na Freshlog. Para alterar quem recebe, responda este e-mail.",
    )
