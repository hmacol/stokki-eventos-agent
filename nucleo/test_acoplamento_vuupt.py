# -*- coding: utf-8 -*-
"""
test_acoplamento_vuupt.py

Trava branda contra dependência NOVA da Vuupt (Etapa 0 do
DOC_EXECUCAO_CLAUDE_SAIDA_VUUPT.md). Em 18 dias (25/08 -> 12/09) o número
de arquivos que falam com a Vuupt cresceu mais rápido do que a saída dela
avançava: cada funcionalidade nova ainda nascia chamando a API direto.

O teste lista os arquivos .py que chamam a Vuupt DIRETO (URL vuupt.com ou
import de vuupt_client / rotas_client / otimizacao_client) e compara com a
lista congelada em 15/09:

  - arquivo NOVO chamando a Vuupt -> falha. O caminho preferido é gravar e
    ler no núcleo (nucleo/*). Se a chamada for mesmo necessária (ex.: a
    ponte núcleo -> Vuupt, que o Hugo exigiu até a virada de chave), entra
    na lista ABAIXO com o motivo no comentário.
  - arquivo da lista que PAROU de chamar a Vuupt -> falha também, pedindo
    pra tirar da lista: a lista é o que ainda falta migrar, e só encolhe.

Rodar (raiz do repo):  python -m unittest nucleo.test_acoplamento_vuupt -v
"""
import re
import unittest
from pathlib import Path

_RAIZ = Path(__file__).resolve().parent.parent

_IGNORAR = {"debug", "investigacao", "venv", ".venv", "node_modules", "app_motorista", "__pycache__",
            ".claude", ".git", "saida_investigacao_stokki"}
# A URL vai com esquema de propósito: só "vuupt\.com" casava dentro de
# `comparar_vuupt.comparar_dia(...)` e acusava arquivo que não chama nada.
_PADRAO = re.compile(r"https?://[\w.-]*vuupt\.com|^\s*(?:from|import)\s+(?:vuupt_client|rotas_client|otimizacao_client)\b",
                     re.M)

# Congelada em 15/09/2026 (50 arquivos). Só encolhe -- ou cresce COM motivo.
PERMITIDOS = {
    "atualizar_agendamentos_confirmados.py",
    "coleta_emporio_quatro_estrelas/lancar_coleta.py",
    "dashboard_embarcadores/atualizar_dashboard.py",
    "dashboard_embarcadores/dashboard_dados.py",
    "documentos_pedido/processar_documentos.py",
    "expedir_pedidos.py",
    "insucesso_entrega/aplicar_resposta_insucesso.py",
    "insucesso_entrega/expedir_pedidos.py",
    "lalamove_integracao.py",
    "ler_planilha_entregas_nuu.py",
    "nucleo/comparar_vuupt.py",             # 15/09: compara os dois lados; some quando a Vuupt sair
    "nucleo/exportar_historico_vuupt.py",   # 16/09: tira o historico da Vuupt pro nosso bucket (Etapa 1)
    "nucleo/relatorios_financeiro.py",      # 16/09: --atualizar-cadastros puxa usuario/veiculo (nome e placa)
    "nucleo/sincronizar_servicos_vuupt.py",  # 16/09: espelha o pedido fora da rota (pool, retirada, reentrega)
    "nucleo/sincronizar_vuupt.py",          # a ponte Vuupt -> núcleo (morre na virada)
    "painel_agentes/expedicao.py",
    "painel_agentes/laboratorio_rotas.py",
    "painel_agentes/mapa_rotas.py",
    "painel_agentes/pedidos_parados_triagem.py",
    "painel_agentes/planejamento_rotas.py",
    "painel_agentes/rascunhos_rota.py",
    "painel_agentes/test_editar_endereco_pedido.py",
    "painel_agentes/test_editar_endereco_pedidos_lote.py",
    "painel_agentes/test_reagendar_pedidos_lote.py",
    "painel_agentes/torre_controle.py",
    "pipeline.py",
    "portal_cliente/dados_cliente.py",
    "reconciliar_pedidos_retirada.py",
    "regras/cadastro_motoristas.py",
    "relatorio_operacional.py",
    "retiradas/acompanhar_retiradas.py",
    "retiradas/criar_agente_retirada.py",
    "revisar_complexidade_entrega.py",
    "roteirizacao/avisar_motoristas_rotas.py",
    "roteirizacao/benchmark_modelos.py",
    "roteirizacao/cancelar_rotas_sem_motorista.py",
    "roteirizacao/criar_rotas_diarias.py",
    "roteirizacao/documentacao_rota.py",
    "roteirizacao/incrementar_rotas.py",
    "roteirizacao/otimizacao_client.py",
    "roteirizacao/reprocessar_rotas.py",
    "roteirizacao/rotas_client.py",
    # roteirizacao/roteirizacao_dados.py saiu da lista: só cita a Vuupt em
    # comentário, recebe o serviço pronto de quem chamou (não é acoplamento).
    "roteirizacao/roteirizar.py",
    "sincronizar_cpf_motoristas.py",
    "sincronizar_placas_motoristas.py",
    "test_vuupt_client_buscar_servico_por_id.py",
    "testar_fluxo_agente_vuupt.py",
    "testar_lalamove_e2e.py",
    "validacao_checklists/validar_checklists.py",
    "verificar_entregues_nao_expedidos.py",
    "verificar_pedidos_duplicados_vuupt.py",
    "vincular_veiculos_motoristas.py",
    "vuupt_client.py",
}


def arquivos_que_chamam_vuupt(raiz: Path = _RAIZ) -> set[str]:
    achados = set()
    este_arquivo = Path(__file__).resolve()
    for caminho in raiz.rglob("*.py"):
        relativo = caminho.relative_to(raiz)
        if any(parte in _IGNORAR for parte in relativo.parts) or caminho.resolve() == este_arquivo:
            continue
        try:
            texto = caminho.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if _PADRAO.search(texto):
            achados.add(relativo.as_posix())
    return achados


class TestAcoplamentoVuupt(unittest.TestCase):
    def test_nenhum_arquivo_novo_chama_a_vuupt_direto(self):
        novos = sorted(arquivos_que_chamam_vuupt() - PERMITIDOS)
        self.assertEqual(novos, [], (
            "Arquivo(s) novo(s) chamando a Vuupt direto. Grave/leia no núcleo (nucleo/*) -- ver "
            "DOC_EXECUCAO_CLAUDE_SAIDA_VUUPT.md. Se for inevitável, acrescente em PERMITIDOS com o motivo."
        ))

    def test_lista_so_encolhe(self):
        migrados = sorted(PERMITIDOS - arquivos_que_chamam_vuupt())
        self.assertEqual(migrados, [], "Arquivo(s) não chamam mais a Vuupt (ou sumiram): tire de PERMITIDOS.")

    def test_padrao_pega_os_jeitos_de_chamar(self):
        for trecho in ('URL = "https://api.vuupt.com/api/v1"', "from vuupt_client import VuuptClient",
                       "    from rotas_client import listar_rotas", "import otimizacao_client"):
            self.assertRegex(trecho, _PADRAO)
        for trecho in ("# a VUUPT não manda nela", "vuupt_route_id = 1", "from nucleo import sincronizar_vuupt",
                       "comparar_vuupt.comparar_dia(dia, rotas, conn)"):
            self.assertNotRegex(trecho, _PADRAO)


if __name__ == "__main__":
    unittest.main()
