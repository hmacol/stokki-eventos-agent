# -*- coding: utf-8 -*-
"""
Remessa Itau (Pix): estados dos pagamentos, remessas, cancelamento e retorno, sobre o banco.
O layout do arquivo e a chave Pix ficam em remessa_itau.py (puro). Reaproveita as tabelas de
relatorio_esporadicos (pagamentos_esporadicos / pagamentos_esporadicos_rotas).

Situacoes de pagamentos_esporadicos.situacao: PAGO (inclui "marcar como pago" manual e o que o retorno
do banco efetivou), ENVIADO (em remessa, ainda sem retorno) e REJEITADO (volta a pendente, com o motivo).
"""
import hashlib
import sqlite3
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from nucleo import banco
import relatorio_esporadicos as rel
import remessa_itau as ri


class ErroRemessa(ValueError):
    """Mensagem pronta pra tela (o JSON devolve em `erro`)."""


def garantir_tabelas(conn) -> None:
    """Idempotente: colunas novas de pagamentos_esporadicos e a tabela remessas_itau."""
    rel._garantir_tabelas(conn)


def elegibilidade(motorista: dict):
    """(ChavePix, None) se o motorista pode entrar na remessa; (None, motivo) caso contrario.
    `motorista` e o dict de relatorio_esporadicos.gerar."""
    if not motorista.get("pode_pagar") or float(motorista.get("total_pendente") or 0) <= 0:
        return None, "nada pendente com valor definido"
    r = ri.classificar_chave(motorista.get("pix"), motorista.get("cpf"))
    if isinstance(r, str):
        return None, r
    return r, None


def _abrir(conn):
    return (conn, False) if conn is not None else (banco.conectar(), True)


def _soltar_rotas(conn, pagamento_id: int) -> None:
    conn.execute("DELETE FROM pagamentos_esporadicos_rotas WHERE pagamento_id = ?", (pagamento_id,))


def _proximo_nome(conn, dia: date) -> str:
    prefixo = f"PIX_{dia.strftime('%Y%m%d')}_"
    n = conn.execute("SELECT COUNT(*) FROM remessas_itau WHERE arquivo_nome LIKE ?", (prefixo + "%",)).fetchone()[0]
    return f"{prefixo}{n + 1:02d}.REM"


def criar_remessa(chaves: list, data_inicio: str, data_fim: str, data_pagamento: date, usuario: str,
                  conta: ri.ContaPagadora, pasta: Path, conn=None, agora: datetime | None = None) -> dict:
    """Uma remessa com UM pagamento ENVIADO por motorista (tudo o que esta pendente dele no periodo).
    Transacao unica: qualquer falha desfaz tudo e apaga o arquivo parcial."""
    agora = agora or datetime.now()
    conn, fechar = _abrir(conn)
    arquivo = None
    try:
        garantir_tabelas(conn)
        if not chaves:
            raise ErroRemessa("Nenhum motorista selecionado.")
        if data_pagamento < agora.date():
            raise ErroRemessa("A data de pagamento não pode ser anterior a hoje.")
        pendentes = {m["chave"]: m for m in rel.gerar(data_inicio, data_fim, "pendentes", conn=conn)["motoristas"]}
        problemas, escolhidos = [], []
        for chave in chaves:
            m = pendentes.get(chave)
            if m is None:
                problemas.append(f"{chave}: sem rota pendente no período (já enviada, paga ou fora do período)")
                continue
            pix, motivo = elegibilidade(m)
            if motivo:
                problemas.append(f"{m['nome']}: {motivo}")
            else:
                escolhidos.append((m, pix))
        if problemas:
            raise ErroRemessa("Não foi possível gerar a remessa. " + "; ".join(problemas))

        agora_iso = agora.strftime("%Y-%m-%dT%H:%M:%S")
        try:
            cur = conn.execute(
                "INSERT INTO remessas_itau (gerada_em, gerada_por, data_pagamento, qtd, valor_total) VALUES (?, ?, ?, 0, 0)",
                (agora_iso, usuario, data_pagamento.isoformat()))
            remessa_id = cur.lastrowid
            itens, total = [], 0.0
            for m, pix in escolhidos:
                pagaveis = [l for l in m["linhas"] if l["valor"] is not None and not l["pago"] and not l["enviado"]]
                valor = round(sum(rel._valor_linha(l) for l in pagaveis), 2)
                cp = conn.execute(
                    "INSERT INTO pagamentos_esporadicos (chave, nome, cpf, valor, periodo_ini, periodo_fim, observacao, "
                    "pago_em, pago_por, situacao, remessa_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ENVIADO', ?)",
                    (m["chave"], m["nome"], m["cpf"], valor, data_inicio, data_fim,
                     f"remessa Itaú #{remessa_id}", agora_iso, usuario, remessa_id))
                conn.executemany(
                    "INSERT INTO pagamentos_esporadicos_rotas (pagamento_id, rota_id, valor_rota, pedagio_aprovado) "
                    "VALUES (?, ?, ?, ?)", [(cp.lastrowid, l["rota_id"], l["valor"], l["pedagio_aprovado"]) for l in pagaveis])
                itens.append(ri.ItemRemessa(cp.lastrowid, m["nome"], Decimal(str(valor)), pix))
                total += valor
            try:
                texto = ri.montar_arquivo(conta, itens, data_pagamento, agora)
                dados = texto.encode("ascii")
            except (ValueError, UnicodeEncodeError) as e:
                raise ErroRemessa(f"Não foi possível montar o arquivo: {e}")
            nome = _proximo_nome(conn, agora.date())
            pasta = Path(pasta)
            pasta.mkdir(parents=True, exist_ok=True)
            arquivo = pasta / nome
            arquivo.write_bytes(dados)
            conn.execute("UPDATE remessas_itau SET qtd = ?, valor_total = ?, arquivo_nome = ?, arquivo_sha256 = ? WHERE id = ?",
                         (len(itens), round(total, 2), nome, hashlib.sha256(dados).hexdigest(), remessa_id))
            conn.commit()
        except sqlite3.IntegrityError:
            raise ErroRemessa("Alguma dessas rotas acabou de entrar em outra remessa ou pagamento. Recarregue a tela.")
        return {"remessa_id": remessa_id, "arquivo_nome": nome, "qtd": len(itens), "valor_total": round(total, 2)}
    except BaseException:
        conn.rollback()
        if arquivo is not None and arquivo.exists():
            arquivo.unlink()
        raise
    finally:
        if fechar:
            conn.close()


def _remessa(conn, remessa_id: int):
    r = conn.execute("SELECT * FROM remessas_itau WHERE id = ?", (remessa_id,)).fetchone()
    if r is None:
        raise ErroRemessa("Remessa não encontrada.")
    return r


def ler_arquivo(remessa_id: int, pasta: Path, usuario: str, conn=None) -> bytes:
    """Bytes do arquivo da remessa (confere o SHA-256 gravado). Marca `baixada_em` na primeira leitura:
    depois disso a remessa nao pode mais ser cancelada (o banco ainda pode receber o arquivo)."""
    conn, fechar = _abrir(conn)
    try:
        garantir_tabelas(conn)
        r = _remessa(conn, remessa_id)
        caminho = Path(pasta) / (r["arquivo_nome"] or "")
        if not r["arquivo_nome"] or not caminho.is_file():
            raise ErroRemessa("Arquivo da remessa não encontrado no servidor.")
        dados = caminho.read_bytes()
        if hashlib.sha256(dados).hexdigest() != r["arquivo_sha256"]:
            raise ErroRemessa("O arquivo no servidor não confere com o gerado (SHA-256 diferente). Não envie ao banco.")
        conn.execute("UPDATE remessas_itau SET baixada_em = COALESCE(baixada_em, ?), baixada_por = COALESCE(baixada_por, ?) "
                     "WHERE id = ?", (datetime.now().strftime("%Y-%m-%dT%H:%M:%S"), usuario, remessa_id))
        conn.commit()
        return dados
    finally:
        if fechar:
            conn.close()


def cancelar_remessa(remessa_id: int, usuario: str, conn=None) -> None:
    """So enquanto o arquivo nunca foi baixado. Os pagamentos viram REJEITADO ('remessa cancelada')
    e as rotas voltam a pendentes."""
    conn, fechar = _abrir(conn)
    try:
        garantir_tabelas(conn)
        r = _remessa(conn, remessa_id)
        if r["baixada_em"]:
            raise ErroRemessa("Esta remessa já foi baixada: o banco ainda pode receber o arquivo. Use o retorno do Itaú "
                              "ou confirme/rejeite cada pagamento depois de conferir no Itaú.")
        if r["retorno_em"]:
            raise ErroRemessa("Esta remessa já tem retorno do banco.")
        ids = [x[0] for x in conn.execute(
            "SELECT id FROM pagamentos_esporadicos WHERE remessa_id = ? AND situacao = 'ENVIADO'", (remessa_id,))]
        if not ids:
            raise ErroRemessa("Não há pagamentos enviados nesta remessa.")
        for pid in ids:
            conn.execute("UPDATE pagamentos_esporadicos SET situacao = 'REJEITADO', motivo = ? WHERE id = ?",
                         (f"remessa cancelada por {usuario}", pid))
            _soltar_rotas(conn, pid)
        conn.commit()
    finally:
        if fechar:
            conn.close()


def _enviado(conn, pagamento_id: int):
    p = conn.execute("SELECT * FROM pagamentos_esporadicos WHERE id = ?", (pagamento_id,)).fetchone()
    if p is None or p["situacao"] != "ENVIADO":
        raise ErroRemessa("Pagamento não está em remessa (já foi pago, rejeitado ou não existe).")
    return p


def confirmar_pago_manual(pagamento_id: int, usuario: str, conn=None, agora: datetime | None = None) -> None:
    """Pagamento ENVIADO vira PAGO por confirmacao humana (quando o retorno do banco nao veio)."""
    conn, fechar = _abrir(conn)
    try:
        garantir_tabelas(conn)
        _enviado(conn, pagamento_id)
        conn.execute("UPDATE pagamentos_esporadicos SET situacao = 'PAGO', pago_em = ?, pago_por = ?, "
                     "motivo = 'confirmado manualmente' WHERE id = ?",
                     ((agora or datetime.now()).strftime("%Y-%m-%dT%H:%M:%S"), usuario, pagamento_id))
        conn.commit()
    finally:
        if fechar:
            conn.close()


def rejeitar_manual(pagamento_id: int, motivo: str, usuario: str, conn=None) -> None:
    """Pagamento ENVIADO vira REJEITADO por decisao humana; a rota volta a pendente."""
    conn, fechar = _abrir(conn)
    try:
        garantir_tabelas(conn)
        if not (motivo or "").strip():
            raise ErroRemessa("Informe o motivo da rejeição.")
        _enviado(conn, pagamento_id)
        conn.execute("UPDATE pagamentos_esporadicos SET situacao = 'REJEITADO', motivo = ? WHERE id = ?",
                     (f"{motivo.strip()[:280]} (por {usuario})", pagamento_id))
        _soltar_rotas(conn, pagamento_id)
        conn.commit()
    finally:
        if fechar:
            conn.close()


def aplicar_retorno(texto: str, remessa_id: int, usuario: str, conn=None, agora: datetime | None = None) -> dict:
    """Aplica o arquivo de retorno do Itau a uma remessa. So mexe em pagamentos ENVIADO DESTA remessa.
    `00` vira PAGO (com a data efetiva); rejeicao conhecida vira REJEITADO (rota solta, motivo do manual);
    so informativa (ex.: BD agendado) mantem ENVIADO; qualquer outra coisa (ocorrencia desconhecida, sem
    ocorrencia, valor diferente, pagamento de outra remessa) fica em `ignorados` e NAO altera estado."""
    conn, fechar = _abrir(conn)
    try:
        garantir_tabelas(conn)
        remessa = _remessa(conn, remessa_id)
        sha = hashlib.sha256(str(texto).encode("utf-8")).hexdigest()
        res = {"pagos": 0, "rejeitados": 0, "agendados": 0, "ignorados": [], "repetido": False, "sem_retorno": 0}
        if remessa["retorno_sha256"] == sha:
            return {**res, "repetido": True}
        try:
            regs = ri.ler_retorno(texto)
            lote = ri.ocorrencias_de_lote(texto)
        except ValueError as e:
            raise ErroRemessa(f"Arquivo de retorno ilegível: {e}")
        if not regs and not lote:
            raise ErroRemessa("O arquivo não tem nenhum pagamento (segmento A) nem ocorrência de lote.")
        for c in lote:
            res["ignorados"].append(f"lote/arquivo: {c} - {ri.OCORRENCIAS.get(c, 'ocorrência desconhecida')}")
        agora_iso = (agora or datetime.now()).strftime("%Y-%m-%dT%H:%M:%S")
        vistos = set()
        for reg in regs:
            p = conn.execute("SELECT * FROM pagamentos_esporadicos WHERE id = ?", (reg.pagamento_id,)).fetchone() \
                if reg.pagamento_id is not None else None
            if p is None or p["remessa_id"] != remessa_id:
                res["ignorados"].append(f"pagamento {reg.pagamento_id}: não pertence à remessa #{remessa_id}")
                continue
            vistos.add(p["id"])
            if p["situacao"] != "ENVIADO":
                res["ignorados"].append(f"pagamento {p['id']}: já está {p['situacao']}")
                continue
            oc = reg.ocorrencias
            if not oc:
                res["ignorados"].append(f"pagamento {p['id']}: retorno sem ocorrência")
                continue
            desconhecidas = [c for c in oc if c not in ri.OCORRENCIAS]
            if desconhecidas:
                res["ignorados"].append(f"pagamento {p['id']}: ocorrência desconhecida {', '.join(desconhecidas)}")
                continue
            rejeicoes = [c for c in oc if c != "00" and c not in ri.INFORMATIVAS]
            if rejeicoes:
                conn.execute("UPDATE pagamentos_esporadicos SET situacao = 'REJEITADO', ocorrencia = ?, motivo = ? WHERE id = ?",
                             (",".join(rejeicoes), "; ".join(ri.OCORRENCIAS[c] for c in rejeicoes), p["id"]))
                _soltar_rotas(conn, p["id"])
                res["rejeitados"] += 1
            elif "00" in oc:
                previsto = Decimal(str(p["valor"])).quantize(Decimal("0.01"))
                if reg.valor_efetivo is None or reg.valor_efetivo != previsto:
                    res["ignorados"].append(f"pagamento {p['id']}: valor efetivo ({reg.valor_efetivo}) diferente do previsto ({previsto})")
                    continue
                quando = reg.data_efetiva.strftime("%Y-%m-%dT00:00:00") if reg.data_efetiva else agora_iso
                conn.execute("UPDATE pagamentos_esporadicos SET situacao = 'PAGO', pago_em = ?, pago_por = ?, ocorrencia = '00', "
                             "motivo = NULL WHERE id = ?", (quando, usuario, p["id"]))
                res["pagos"] += 1
            else:
                conn.execute("UPDATE pagamentos_esporadicos SET ocorrencia = ? WHERE id = ?", (",".join(oc), p["id"]))
                res["agendados"] += 1
        res["sem_retorno"] = sum(1 for (pid,) in conn.execute(
            "SELECT id FROM pagamentos_esporadicos WHERE remessa_id = ? AND situacao = 'ENVIADO'", (remessa_id,))
            if pid not in vistos)
        conn.execute("UPDATE remessas_itau SET retorno_em = ?, retorno_por = ?, retorno_sha256 = ? WHERE id = ?",
                     (agora_iso, usuario, sha, remessa_id))
        conn.commit()
        return res
    except BaseException:
        conn.rollback()
        raise
    finally:
        if fechar:
            conn.close()


def nome_arquivo(remessa_id: int, conn=None) -> str:
    conn, fechar = _abrir(conn)
    try:
        garantir_tabelas(conn)
        return _remessa(conn, remessa_id)["arquivo_nome"] or ""
    finally:
        if fechar:
            conn.close()


def listar_remessas(limite: int = 20, conn=None) -> list:
    """Remessas mais recentes primeiro, com a contagem de pagamentos por situacao."""
    conn, fechar = _abrir(conn)
    try:
        garantir_tabelas(conn)
        lista = []
        for r in conn.execute("SELECT * FROM remessas_itau ORDER BY id DESC LIMIT ?", (limite,)).fetchall():
            d = dict(r)
            d["situacoes"] = {s: n for s, n in conn.execute(
                "SELECT situacao, COUNT(*) FROM pagamentos_esporadicos WHERE remessa_id = ? GROUP BY situacao", (r["id"],))}
            lista.append(d)
        return lista
    finally:
        if fechar:
            conn.close()
