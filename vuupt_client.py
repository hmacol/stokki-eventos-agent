"""
api_vuupt.py
Cliente da API REST do VUUPT — criação/atualização de serviços,
montagem de payload, lógica híbrida de atualização (objeto completo vs
customer_id leve).

NOTA: este módulo foi recriado após reinício do container desta sessão,
focado nas partes diretamente relevantes para a mudança recente
("lat/long como trigger de atualização"). As demais funções completas
do VuuptClient (carregar_skills, criar_servico, buscar_servico_por_code,
cancelar_servico, montar_payload_servico, skill_ids_por_nome) seguem a
mesma estrutura já validada em mensagens anteriores desta conversa.
"""

import logging

import requests

from http_retry import chamar_com_retry

logger = logging.getLogger(__name__)

BASE_URL = "https://app.vuupt.com/api/v1"


def aplicar_forma_leve_se_endereco_inalterado(payload: dict) -> dict:
    """
    Estratégia híbrida para evitar regeocodificação desnecessária:

    - Se o endereço (texto + latitude/longitude) NÃO mudou desde a
      última importação deste código, e já temos customer_id/sender_id
      salvos de uma criação/atualização anterior, substitui os objetos
      completos 'customer'/'sender' por 'customer_id'/'sender_id' — o
      VUUPT então usa o endereço já cadastrado no contato, sem
      reprocessar geocodificação.
    - Se o endereço OU a geocodificação mudaram (ou é a primeira vez,
      sem histórico), mantém o payload com os objetos completos — força
      a atualização do contato no VUUPT com o endereço/lat/long novos.
      Isso cobre o caso de o texto do endereço permanecer o mesmo, mas a
      geocodificação ter mudado (ex: Google atualizou a precisão) — essa
      mudança por si só já é suficiente para forçar o reenvio completo,
      incluindo a latitude/longitude nova.

    Deve ser chamada DEPOIS de montar_payload_servico e ANTES de enviar
    à API, só para pedidos que já existem (atualização) — pedidos novos
    (criação) sempre devem usar o objeto completo, já que ainda não há
    contato cadastrado para referenciar por ID.
    """
    from fingerprint_importacao import endereco_mudou, buscar_ids_salvos

    codigo = payload.get("code", "")
    if not codigo:
        return payload

    if endereco_mudou(codigo, payload):
        # Endereço em texto OU lat/long mudaram (ou é a 1ª vez) — mantém
        # o payload completo, COM latitude/longitude se já tiverem sido
        # preenchidas por montar_payload_servico. Diferente da versão
        # anterior, aqui não removemos mais lat/long incondicionalmente:
        # a própria mudança de coordenada é o motivo do reenvio.
        return payload

    customer_id, sender_id = buscar_ids_salvos(codigo)
    if customer_id is None:
        return payload  # sem ID salvo ainda — não há como usar a forma leve

    payload_leve = dict(payload)
    payload_leve.pop("customer", None)
    payload_leve["customer_id"] = customer_id
    if sender_id is not None and "sender_id" not in payload_leve:
        payload_leve["sender_id"] = sender_id
        payload_leve.pop("sender", None)

    return payload_leve


class VuuptAPIError(Exception):
    pass


class VuuptClient:
    STATUS_ATUALIZAVEL = "not_assigned"  # "Não atribuído" — único status seguro para sobrescrever

    def __init__(self, token: str):
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        })
        self._skills_cache: dict[str, int] | None = None

    def carregar_skills(self, forcar_recarga: bool = False) -> dict[str, int]:
        """Carrega o mapa {nome_skill: id} de todas as skills cadastradas na
        conta. Cacheado em memória — chamada repetida (ex: por pedido, via
        skill_ids_por_nome) não bate na API de novo, a menos que
        forcar_recarga=True."""
        if self._skills_cache is not None and not forcar_recarga:
            return self._skills_cache
        resp = chamar_com_retry(self.session.get, f"{BASE_URL}/skills", params={"per_page": 100}, timeout=15)
        resp.raise_for_status()
        dados = resp.json().get("data", [])
        mapa = {item["name"]: item["id"] for item in dados}
        logger.info(f"Skills carregadas da API: {len(mapa)}")
        self._skills_cache = mapa
        return mapa

    def skill_ids_por_nome(self, *nomes: str) -> list[int]:
        """Converte nome(s) de skill (ex: 'Congelado-3') em lista de IDs."""
        mapa = self.carregar_skills()
        ids = []
        for nome in nomes:
            nome = (nome or "").strip()
            if not nome:
                continue
            skill_id = mapa.get(nome)
            if skill_id:
                ids.append(skill_id)
            else:
                logger.warning(f"Skill '{nome}' não encontrada na conta VUUPT.")
        return ids

    @staticmethod
    def _montar_params_filtro(filtros: list[dict], extra: dict | None = None) -> dict:
        """Converte lista de {'field','operator','value'} no formato de query
        filter[N][field]=... esperado pela API (confirmado via
        debug/investigar_api_relatorio*.py, 28/07: operadores 'eq'/'gte'/'lte'
        aceitos com valor de data 'YYYY-MM-DD'; múltiplos filtros = AND)."""
        params = dict(extra or {})
        for i, f in enumerate(filtros):
            params[f"filter[{i}][field]"] = f["field"]
            params[f"filter[{i}][operator]"] = f["operator"]
            params[f"filter[{i}][value]"] = f["value"]
        return params

    def contar_servicos(self, filtros: list[dict]) -> int:
        """
        Retorna só a CONTAGEM total de serviços que casam com os filtros
        (meta.pagination.total), sem trazer os registros — rápido para
        relatórios que só precisam de números.
        """
        params = self._montar_params_filtro(filtros, {"per_page": 1})
        resp = chamar_com_retry(self.session.get, f"{BASE_URL}/services", params=params, timeout=20)
        resp.raise_for_status()
        return resp.json().get("meta", {}).get("pagination", {}).get("total", 0)

    def listar_servicos(self, filtros: list[dict], per_page: int = 100,
                        limite_paginas: int = 500) -> list[dict]:
        """
        Lista TODOS os serviços que casam com os filtros, paginando
        automaticamente via meta.pagination.total_pages até esgotar.

        limite_paginas: trava de segurança (500 páginas × per_page=100 =
        50.000 registros) — se atingir, loga aviso e para, em vez de
        entrar num loop indefinido por engano de filtro.
        """
        todos: list[dict] = []
        pagina = 1
        while True:
            params = self._montar_params_filtro(filtros, {"per_page": per_page, "page": pagina})
            resp = chamar_com_retry(self.session.get, f"{BASE_URL}/services", params=params, timeout=30)
            resp.raise_for_status()
            corpo = resp.json()
            dados = corpo.get("data", [])
            todos.extend(dados)

            paginacao = corpo.get("meta", {}).get("pagination", {})
            total_pages = paginacao.get("total_pages", 1)
            if not dados or pagina >= total_pages:
                break
            if pagina >= limite_paginas:
                logger.warning(
                    f"listar_servicos: limite de segurança de {limite_paginas} "
                    f"página(s) atingido — parando (pode haver mais registros)."
                )
                break
            pagina += 1
        return todos

    def buscar_servico_por_code(self, code: str) -> dict | None:
        """
        Busca um serviço existente pelo campo 'code'.

        Tenta o code EXATO como veio e também a variante com/sem '#' na
        frente. Confirmado em produção (28/07, investigação de
        duplicação real no VUUPT, caso PS-34669): existem serviços no
        VUUPT com o code armazenado com '#' (criados por outra via/
        projeto) e este pipeline sempre cria/busca SEM '#' — uma busca
        'eq' exata só acha um dos dois formatos, então o pipeline
        concluía "não existe" e criava um SEGUNDO serviço para o mesmo
        pedido. Retorna o serviço mais recente entre as duas buscas
        (ou None se nenhuma achar nada).
        """
        if not code:
            return None

        variantes = [code]
        if code.startswith("#"):
            variantes.append(code[1:])
        else:
            variantes.append(f"#{code}")

        encontrados = []
        for variante in variantes:
            resp = chamar_com_retry(
                self.session.get,
                f"{BASE_URL}/services",
                params={
                    "filter[0][field]": "code",
                    "filter[0][operator]": "eq",
                    "filter[0][value]": variante,
                    "per_page": 5,
                },
                timeout=15,
            )
            resp.raise_for_status()
            encontrados.extend(resp.json().get("data", []))

        if not encontrados:
            return None
        encontrados_ordenados = sorted(encontrados, key=lambda s: s.get("created_at", ""), reverse=True)
        return encontrados_ordenados[0]

    def buscar_servico_por_titulo_contendo(self, texto: str) -> dict | None:
        """Busca um serviço cujo campo 'title' contenha o texto informado (operador 'contains')."""
        resp = chamar_com_retry(
            self.session.get,
            f"{BASE_URL}/services",
            params={
                "filter[0][field]": "title",
                "filter[0][operator]": "contains",
                "filter[0][value]": texto,
                "per_page": 5,
            },
            timeout=15,
        )
        resp.raise_for_status()
        servicos = resp.json().get("data", [])
        if not servicos:
            return None
        servicos_ordenados = sorted(servicos, key=lambda s: s.get("created_at", ""), reverse=True)
        return servicos_ordenados[0]

    def criar_servico(self, payload: dict) -> dict:
        """Cria um serviço (pedido) no VUUPT. Retorna o JSON de resposta. Lança VuuptAPIError em falha."""
        resp = chamar_com_retry(self.session.post, f"{BASE_URL}/services", json=payload, timeout=20)
        if resp.status_code == 200:
            return resp.json()
        try:
            erro = resp.json()
        except Exception:
            erro = {"message": resp.text}
        raise VuuptAPIError(f"Status {resp.status_code}: {erro}")

    def atualizar_servico(self, service_id: int, payload: dict) -> dict:
        """
        Atualiza um serviço existente via PUT.

        O payload já chega aqui pronto, decidido por
        aplicar_forma_leve_se_endereco_inalterado: se o endereço/geo
        mudou, latitude/longitude novos estão presentes no objeto
        'customer' e SÃO enviados (a própria mudança de coordenada é o
        motivo do reenvio); se nada mudou, o payload já está na forma
        leve (por customer_id/sender_id), sem nenhum dado de endereço.

        Retorna o JSON de resposta. Lança VuuptAPIError em caso de falha.
        """
        resp = chamar_com_retry(self.session.put, f"{BASE_URL}/services/{service_id}", json=payload, timeout=20)
        if resp.status_code == 200:
            return resp.json()
        try:
            erro = resp.json()
        except Exception:
            erro = {"message": resp.text}
        raise VuuptAPIError(f"Status {resp.status_code}: {erro}")

    def cancelar_servico(self, service_id: int) -> dict:
        """Cancela um serviço existente via PUT, definindo status='canceled'."""
        resp = chamar_com_retry(
            self.session.put,
            f"{BASE_URL}/services/{service_id}",
            json={"status": "canceled"},
            timeout=20,
        )
        if resp.status_code == 200:
            return resp.json()
        try:
            erro = resp.json()
        except Exception:
            erro = {"message": resp.text}
        raise VuuptAPIError(f"Status {resp.status_code}: {erro}")

    def buscar_customer_por_id(self, customer_id: int) -> dict | None:
        """
        Busca um contato pelo ID (GET /customers/{id}) — usado quando só
        temos o customer_id (ex: vindo de um serviço) e precisamos dos
        dados completos do contato, como o campo 'code' (CNPJ/CPF).
        Retorna None se não encontrar (404) ou em qualquer outra falha.
        """
        try:
            resp = chamar_com_retry(self.session.get, f"{BASE_URL}/customers/{customer_id}", timeout=15)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.warning(f"Falha ao buscar customer {customer_id}: {e}")
            return None

    def buscar_customer_por_code(self, code: str) -> dict | None:
        """
        Busca um contato existente pelo campo 'code' (CNPJ/CPF do
        destinatário). Retorna o dict do contato mais recente com esse
        código, ou None se não existir.
        """
        resp = chamar_com_retry(
            self.session.get,
            f"{BASE_URL}/customers",
            params={
                "filter[0][field]": "code",
                "filter[0][operator]": "eq",
                "filter[0][value]": code,
                "per_page": 5,
            },
            timeout=15,
        )
        resp.raise_for_status()
        contatos = resp.json().get("data", [])
        if not contatos:
            return None
        contatos_ordenados = sorted(contatos, key=lambda c: c.get("created_at", ""), reverse=True)
        return contatos_ordenados[0]

    def atualizar_customer(self, customer_id: int, dados: dict) -> dict:
        """PUT direto em /customers/{id} — grava os campos enviados no contato existente."""
        resp = chamar_com_retry(self.session.put, f"{BASE_URL}/customers/{customer_id}", json=dados, timeout=20)
        if resp.status_code == 200:
            return resp.json()
        try:
            erro = resp.json()
        except Exception:
            erro = {"message": resp.text}
        raise VuuptAPIError(f"Status {resp.status_code}: {erro}")

    def criar_customer(self, dados: dict) -> dict:
        """POST em /customers — cadastra um contato novo com os dados já corretos desde a criação."""
        resp = chamar_com_retry(self.session.post, f"{BASE_URL}/customers", json=dados, timeout=20)
        if resp.status_code == 200:
            return resp.json()
        try:
            erro = resp.json()
        except Exception:
            erro = {"message": resp.text}
        raise VuuptAPIError(f"Status {resp.status_code}: {erro}")

    def resolver_customer_id(self, code: str, nome: str, endereco: str, telefone: str,
                              horario_inicio: str = "", horario_fim: str = "",
                              latitude: float | None = None,
                              longitude: float | None = None) -> int | None:
        """
        Garante que o contato (code=CNPJ/CPF do destinatário) existe no
        VUUPT com os dados CORRETOS já gravados — ANTES do serviço ser
        criado — e retorna o customer_id pronto para uso.

        Por quê: criar/atualizar o serviço com o contato embutido faz o
        VUUPT ignorar telefone e, em alguns casos, dados do contato já
        existente (confirmado via testes com webhook real) — o contato
        só reflete os dados corretos de forma confiável quando
        atualizado por uma chamada própria a /customers. Resolver isso
        ANTES de criar o serviço garante que o primeiro evento de webhook
        (ServiceCreatedEvent) já sai com o contato certo, sem depender
        de uma correção posterior que o webhook não capturaria a tempo.

        Latitude/longitude geocodificadas entram AQUI, no upsert do
        contato — este é o único caminho que chega de fato à API quando
        o customer_id é resolvido (o objeto 'customer' do payload do
        serviço é descartado nesse caso). Sem isso, as coordenadas
        geocodificadas morriam antes de chegar ao VUUPT.

        Se code estiver vazio, retorna None (deixa o serviço criar o
        contato embutido normalmente — sem CNPJ não há como buscar/re-
        utilizar um contato específico).
        """
        if not code:
            return None

        dados_customer = {"name": nome, "address": endereco, "code": code}
        if telefone:
            dados_customer["phone_number"] = telefone
        if horario_inicio:
            dados_customer["operating_hour_start"] = horario_inicio
        if horario_fim:
            dados_customer["operating_hour_end"] = horario_fim
        if latitude is not None and longitude is not None:
            dados_customer["latitude"]  = latitude
            dados_customer["longitude"] = longitude

        existente = self.buscar_customer_por_code(code)
        if existente:
            atualizado = self.atualizar_customer(existente["id"], dados_customer)
            customer = atualizado.get("customer", atualizado)
            return customer.get("id", existente["id"])
        else:
            criado = self.criar_customer(dados_customer)
            customer = criado.get("customer", criado)
            return customer.get("id")

    def criar_ou_atualizar_servico(self, payload: dict) -> tuple[dict | None, str]:
        """
        Verifica se já existe um serviço com o mesmo 'code':

          - Não existe              -> cria (POST), sempre com objeto completo
          - Existe e está "Não atribuído" (status=not_assigned):
              - Aplica a forma híbrida (ver aplicar_forma_leve_se_endereco_inalterado):
                se nada mudou (incluindo lat/long), usa customer_id/sender_id
                (forma leve); se algo mudou, usa objeto completo (com lat/long
                novos, se for o caso).
              - Se mesmo assim o conteúdo final não mudou desde a última
                importação (fingerprint geral), pula o reenvio.
          - Existe mas já foi atribuído a um agente/rota -> NÃO atualiza.

        ANTES de criar/atualizar o serviço, resolve o contato (destinatário)
        separadamente via resolver_customer_id — garante que o VUUPT já
        tem o contato com nome/endereço/telefone corretos ANTES do
        serviço nascer, para que o primeiro evento de webhook
        (ServiceCreatedEvent) já saia certo, sem depender de uma correção
        posterior que o webhook poderia não capturar a tempo. A decisão
        de forma completa vs. forma leve continua usando o objeto
        'customer' original (endereço/lat/long) para comparação — só na
        hora de ENVIAR à API o objeto é trocado por customer_id.

        Retorna (resposta_json ou None, acao: 'criado' | 'atualizado' |
        'pulado_atribuido' | 'pulado_sem_alteracao').
        """
        from fingerprint_importacao import houve_alteracao, salvar_fingerprint

        code = payload.get("code", "")
        customer_info = payload.get("customer", {})
        customer_code = customer_info.get("code", "")

        customer_id_resolvido = None
        if customer_code:
            try:
                customer_id_resolvido = self.resolver_customer_id(
                    customer_code,
                    customer_info.get("name", ""),
                    customer_info.get("address", ""),
                    customer_info.get("phone_number", ""),
                    customer_info.get("operating_hour_start", ""),
                    customer_info.get("operating_hour_end", ""),
                    latitude=customer_info.get("latitude"),
                    longitude=customer_info.get("longitude"),
                )
            except Exception as e:
                logger.warning(
                    f"Não foi possível resolver o contato '{customer_code}' antes do serviço "
                    f"'{code}': {e}. Seguindo com o objeto 'customer' embutido normalmente."
                )

        existente = self.buscar_servico_por_code(code) if code else None

        if not existente:
            payload_para_enviar = dict(payload)
            if customer_id_resolvido:
                payload_para_enviar.pop("customer", None)
                payload_para_enviar["customer_id"] = customer_id_resolvido

            resultado = self.criar_servico(payload_para_enviar)
            if code:
                service = resultado.get("service", resultado)
                salvar_fingerprint(
                    code, payload,
                    customer_id=service.get("customer_id"),
                    sender_id=service.get("sender_id"),
                )
            return resultado, "criado"

        status_atual = existente.get("status", "")
        if status_atual != self.STATUS_ATUALIZAVEL:
            logger.warning(
                f"Pedido '{code}' já existe com status='{status_atual}' (não é 'Não atribuído') "
                f"— pulando atualização para não interferir na entrega já planejada."
            )
            return None, "pulado_atribuido"

        if code and not houve_alteracao(code, payload):
            logger.info(
                f"Pedido '{code}' sem alteração desde a última importação — pulando reenvio."
            )
            return None, "pulado_sem_alteracao"

        # A decisão de forma completa vs. leve usa o payload ORIGINAL (com
        # 'customer' completo, incluindo lat/long) — é essa comparação que
        # detecta se o endereço mudou desde a última importação.
        payload_para_enviar = aplicar_forma_leve_se_endereco_inalterado(payload)
        if customer_id_resolvido and "customer" in payload_para_enviar:
            payload_para_enviar = dict(payload_para_enviar)
            payload_para_enviar.pop("customer", None)
            payload_para_enviar["customer_id"] = customer_id_resolvido

        resultado = self.atualizar_servico(existente["id"], payload_para_enviar)
        if code:
            service = resultado.get("service", resultado)
            salvar_fingerprint(
                code, payload,
                customer_id=service.get("customer_id"),
                sender_id=service.get("sender_id"),
            )
        return resultado, "atualizado"


def _str(v, default: str = "") -> str:
    if v is None:
        return default
    s = str(v).strip()
    return default if s.lower() == "nan" else s


def _int(v, default: int = 0) -> int:
    """Converte um valor para inteiro de forma resiliente — None, vazio, ou 'nan' retornam o default."""
    s = _str(v)
    if not s:
        return default
    try:
        return int(float(s))
    except ValueError:
        return default


def _converter_data_para_iso(valor: str) -> str:
    """
    Converte uma data do formato do template ("DD/MM/YYYY HH:MM" ou
    variações comuns) para o formato ISO8601 com offset explícito de
    horário de Brasília ("YYYY-MM-DDTHH:MM:SS-03:00"), conforme aceito
    pela API VUUPT.

    IMPORTANTE: o horário no template/Stokki é sempre horário local
    (Brasília). Sem o offset explícito, a API pode interpretar o valor
    como já estando em UTC/GMT (formato "YYYY-MM-DD HH:MM:SS" sem fuso
    é o padrão de retorno da própria API, em UTC) — isso adiantaria o
    agendamento em 3 horas no VUUPT. O Brasil não usa horário de verão
    desde 2019, então o offset -03:00 é fixo e seguro durante todo o ano.

    Retorna string vazia se o valor estiver vazio ou não puder ser
    interpretado como data — nesse caso o campo simplesmente não é
    incluído no payload, em vez de enviar um valor inválido que a API
    rejeitaria com erro 422.
    """
    from datetime import datetime

    if not valor or valor.lower() == "nan":
        return ""

    formatos_aceitos = [
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M",
        "%d/%m/%Y",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
    ]
    for formato in formatos_aceitos:
        try:
            dt = datetime.strptime(valor.strip(), formato)
            return dt.strftime("%Y-%m-%dT%H:%M:%S-03:00")
        except ValueError:
            continue

    logger.warning(f"Data de agendamento em formato não reconhecido: {valor!r} — campo omitido do payload.")
    return ""


def _normalizar_telefone_e164(valor: str) -> str:
    """
    Normaliza um número de telefone brasileiro para o formato E.164
    exigido pela API VUUPT (ex: "+5511999999999") — usado para SMS de
    notificação ao destinatário.

    Aceita formatos comuns vindos do relatório Stokki: com ou sem DDI
    (55), com ou sem parênteses/traços/espaços, com ou sem o nono dígito.
    Retorna string vazia se não for possível extrair um número válido
    (mínimo 10 dígitos: DDD + número), evitando enviar um phone_number
    malformado que a API rejeitaria.
    """
    if not valor:
        return ""

    digitos = "".join(c for c in str(valor) if c.isdigit())
    if not digitos:
        return ""

    # Remove DDI 55 duplicado/incorreto antes de validar o tamanho —
    # alguns relatórios já vêm com "55" na frente, outros não.
    if digitos.startswith("55") and len(digitos) in (12, 13):
        digitos = digitos[2:]

    # Telefone brasileiro válido: 10 dígitos (DDD + fixo, 8 dígitos) ou
    # 11 dígitos (DDD + celular, 9 dígitos, com o nono dígito).
    if len(digitos) not in (10, 11):
        return ""

    # NÃO tentar "corrigir" um celular de 10 dígitos prefixando o 9.
    # Testado com XMLs de NF-e reais (fonte da verdade) para PS-35089 e
    # PS-35088: em ambos os casos o número de 10 dígitos da Stokki é o
    # número real de 11 dígitos com o ÚLTIMO dígito CORTADO NO FIM — não
    # o nono dígito faltando no meio. Prefixar "9" (tentativa anterior,
    # revertida) produzia um número diferente do real e inexistente.
    # Sem o último dígito não há como reconstruir o número certo, então
    # o pedido segue SEM telefone (VUUPT não recebe número errado) em
    # vez de arriscar notificar o destinatário errado por SMS.
    if len(digitos) == 10 and digitos[2] in "6789":
        logger.warning(
            f"Telefone de 10 dígitos com prefixo de celular (possível "
            f"último dígito cortado na origem/Stokki): {valor!r} — "
            f"enviado SEM telefone para não arriscar notificar o número errado."
        )
        return ""

    return f"+55{digitos}"


def montar_payload_servico(linha: dict, skill_ids: list[int]) -> dict:
    """
    Monta o payload de criação/atualização de serviço a partir de uma
    linha do arquivo de importação (já no formato de template, com
    colunas "Destinatário - *", "Remetente - *", etc.).
    """
    endereco_completo = ", ".join(filter(None, [
        f"{_str(linha.get('Destinatário - Logradouro'))}, {_str(linha.get('Destinatário - Número '))}".strip(", "),
        _str(linha.get("Destinatário - Complemento ")),
        _str(linha.get("Destinatário - Bairro ")),
        _str(linha.get("Destinatário - Cidade ")),
        _str(linha.get("Destinatário - Estado ")),
        _str(linha.get("Destinatário - CEP ")),
        "Brasil",
    ]))

    horario_inicio = _str(linha.get("Destinatário - Horário de atendimento - início")) or "00:00"
    horario_fim    = _str(linha.get("Destinatário - Horário de atendimento - fim")) or "23:59"

    telefone_dest = _normalizar_telefone_e164(_str(linha.get("Destinatário - Telefone ")))

    payload = {
        "title": _str(linha.get("Serviço - Título ", "")),
        "code": _str(linha.get("Serviço - Código ", "")),
        "type": "delivery",
        "customer": {
            "name": _str(linha.get("Destinatário - Nome ", "")),
            "code": _str(linha.get("Destinatário - Código", "")),
            "address": endereco_completo,
            "operating_hour_start": horario_inicio,
            "operating_hour_end": horario_fim,
        },
        "sender": {
            "name": _str(linha.get("Remetente - Nome", "")),
            "code": _str(linha.get("Remetente - Código", "")),
        },
        "dimension_3": _int(linha.get("Dimensão 3")),
    }

    if telefone_dest:
        payload["customer"]["phone_number"] = telefone_dest
        # O serviço tem seu PRÓPRIO phone_number (campo "Contato para
        # notificações" no nível do serviço), independente do phone_number
        # do contato/customer — mesmo padrão da latitude/longitude, que
        # também existe nos dois níveis. Sem isto, as notificações ligadas
        # ao serviço (não ao contato) ficariam sem telefone.
        payload["phone_number"] = telefone_dest

    # sender_id: ID numérico do remetente já cadastrado no VUUPT
    # (cadastrado por embarcador no BD Interno).
    sender_id_str = _str(linha.get("Remetente - Sender ID"))
    if sender_id_str:
        try:
            payload["sender_id"] = int(float(sender_id_str))
        except ValueError:
            logger.warning(f"sender_id inválido para o pedido '{payload['code']}': {sender_id_str!r} — ignorando.")

    # Latitude/longitude geocodificadas (ver steps/geocodificacao.py) —
    # ajudam o VUUPT a não precisar geocodificar por conta própria. A
    # própria mudança dessas coordenadas (mesmo com endereço em texto
    # igual) é tratada como gatilho de atualização — ver
    # fingerprint_importacao.py e aplicar_forma_leve_se_endereco_inalterado.
    lat_dest_str = _str(linha.get("Destinatário - Latitude"))
    lng_dest_str = _str(linha.get("Destinatário - Longitude"))
    if lat_dest_str and lng_dest_str:
        try:
            payload["customer"]["latitude"] = float(lat_dest_str)
            payload["customer"]["longitude"] = float(lng_dest_str)
        except ValueError:
            pass

    # Agendamento (data + horário de entrega previsto). O template usa o
    # formato "DD/MM/YYYY HH:MM" (padrão brasileiro), mas a API REST
    # exige formato ISO ("YYYY-MM-DD HH:MM:SS") — sem essa conversão, a
    # API rejeita com "campo não é uma data válida" (erro 422).
    agendamento_inicio = _converter_data_para_iso(_str(linha.get("Agendamento - Início")))
    agendamento_fim    = _converter_data_para_iso(_str(linha.get("Agendamento - Fim")))
    if agendamento_inicio:
        payload["scheduled_start"] = agendamento_inicio
    if agendamento_fim:
        payload["scheduled_end"] = agendamento_fim

    if skill_ids:
        payload["skills"] = skill_ids

    return payload



