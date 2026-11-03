"""Cliente da Vuupt pra criar agente (motorista) e veiculo: POST /users e /vehicles."""
import re
import unicodedata
from collections import Counter

import requests

DOMINIO_EMAIL = "@freshhub.com.br"
_LIGACOES = {"da", "de", "do", "das", "dos", "e"}


class ErroVuupt(Exception):
    def __init__(self, mensagem, status=None, corpo=""):
        super().__init__(mensagem)
        self.status = status
        self.corpo = corpo


def _sem_acento(texto: str) -> str:
    return unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()


def gerar_email(nome: str, ocupados: set) -> str:
    """Iniciais dos nomes (sem da/de/do/das/dos/e); colisao vira jps2, jps3..."""
    palavras = [p for p in re.split(r"\s+", _sem_acento(nome).lower().strip())
                if p and p not in _LIGACOES]
    base = "".join(p[0] for p in palavras if p[0].isalnum())
    ocupados = {o.lower() for o in ocupados}
    candidato, n = f"{base}{DOMINIO_EMAIL}", 1
    while candidato in ocupados:
        n += 1
        candidato = f"{base}{n}{DOMINIO_EMAIL}"
    return candidato


def _digitos(telefone: str) -> str:
    return re.sub(r"\D", "", telefone or "")


def _nacional(telefone: str) -> str:
    d = _digitos(telefone)
    if len(d) > 11 and d.startswith("55"):
        d = d[2:]
    return d[-11:]


def telefone_e164(telefone: str) -> str:
    return "+55" + _nacional(telefone)


def mesmo_telefone(a: str, b: str) -> bool:
    na, nb = _nacional(a), _nacional(b)
    return bool(na) and na == nb


def base_operacional_mais_comum(veiculos: list):
    contagem = Counter(v["operational_base_id"] for v in veiculos
                       if v.get("operational_base_id") is not None)
    return contagem.most_common(1)[0][0] if contagem else None


class VuuptAgentes:
    def __init__(self, token: str, base: str = "https://api.vuupt.com/api/v1", sessao=None):
        self.base = base.rstrip("/")
        self.sessao = sessao or requests
        self.headers = {"Authorization": f"Bearer {token}",
                        "Accept": "application/json",
                        "Content-Type": "application/json"}

    def _chamar(self, metodo: str, caminho: str, **kw):
        try:
            resp = getattr(self.sessao, metodo)(f"{self.base}{caminho}", headers=self.headers,
                                                timeout=30, **kw)
        except requests.RequestException as e:
            raise ErroVuupt(f"{metodo.upper()} {caminho}: {e}", None, str(e))
        if resp.status_code >= 400:
            raise ErroVuupt(f"{metodo.upper()} {caminho}: HTTP {resp.status_code}",
                            resp.status_code, resp.text)
        return resp

    def _listar(self, caminho: str) -> list:
        todos, pagina = [], 1
        while True:
            corpo = self._chamar("get", caminho, params={"per_page": 100, "page": pagina}).json()
            todos.extend(corpo.get("data", []))
            total = corpo.get("meta", {}).get("pagination", {}).get("total_pages", pagina)
            if pagina >= total:
                return todos
            pagina += 1

    def listar_agentes(self) -> list:
        return self._listar("/agents")

    def listar_veiculos(self) -> list:
        return self._listar("/vehicles")

    def _id_criado(self, resp, chave: str) -> int:
        corpo = resp.json()
        obj = corpo.get(chave) or corpo.get("data") or corpo
        if not isinstance(obj, dict) or obj.get("id") is None:
            raise ErroVuupt(f"Resposta sem id ({chave})", resp.status_code, resp.text)
        return int(obj["id"])

    def criar_agente(self, nome: str, email: str, senha: str, telefone: str) -> int:
        resp = self._chamar("post", "/users", json={
            "name": nome, "email": email, "password": senha,
            "phone_number": telefone_e164(telefone),
            "is_driver": "Y", "is_user": "N", "is_user_readonly": "N", "is_admin": "N"})
        return self._id_criado(resp, "user")

    def criar_veiculo(self, placa: str, base_operacional_id, agent_id: int) -> int:
        payload = {"license_plate": placa, "code": placa, "agentsAllowed": [agent_id]}
        if base_operacional_id is not None:
            payload["operational_base_id"] = base_operacional_id
        return self._id_criado(self._chamar("post", "/vehicles", json=payload), "vehicle")

    def ligar_veiculo(self, agent_id: int, vehicle_id: int) -> None:
        self._chamar("put", f"/users/{agent_id}", json={"vehicle_id": vehicle_id})

    def apagar_agente(self, agent_id: int) -> None:
        self._chamar("delete", f"/users/{agent_id}")

    def apagar_veiculo(self, vehicle_id: int) -> None:
        self._chamar("delete", f"/vehicles/{vehicle_id}")
