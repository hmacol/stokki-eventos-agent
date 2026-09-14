// Tipos espelhando o JSON da API (nucleo/api_motorista.py).

export type Motorista = {
  cpf: string;
  nome: string;
  agent_id: number | null;
  vehicle_id: number | null;
  telefone: string | null;
  email: string | null;
  tipo_veiculo: string | null;
  perfil: 'MOTORISTA' | 'TESTE';
};

export type SituacaoParada = 'PENDENTE' | 'EM_DESLOCAMENTO' | 'EM_ROTA' | 'ENTREGUE' | 'PARCIAL' | 'INSUCESSO' | 'CANCELADA';
export type StatusRota = 'PLANEJADA' | 'ACEITA' | 'EM_ROTA' | 'CONCLUIDA' | 'CANCELADA';

export type Comprovante = { id: number; tipo: string; uuid: string; capturado_em: string | null; validacao?: string | null };

// Validação automática da foto (nucleo/validacao_fotos.py, Hugo 12/09).
// pode_seguir=false trava a conclusão da parada; depois de max_tentativas
// o servidor devolve pode_seguir=true e revisao_humana=true.
export type ResultadoValidacao = {
  resultado: 'APROVADO' | 'REPROVADO' | 'NAO_VERIFICADO';
  motivo: string | null;
  nitidez: number | null;
  pode_seguir: boolean;
  revisao_humana: boolean;
  tentativa: number;
  tentativas_restantes: number;
  sha256?: string;
  nf_lidas?: string[];
  nf_confere?: boolean | null;
  avisos?: string[];
  valor_lido?: number | null;
};

export type Parada = {
  id: number;
  rota_id: number;
  ordem: number;
  codigo: string | null;
  titulo: string | null;
  destinatario_nome: string | null;
  endereco: string | null;
  latitude: number | null;
  longitude: number | null;
  remetente_nome: string | null;
  nivel_dificuldade: number | null;
  volume_caixas: number | null;
  janela_inicio: string | null;
  janela_fim: string | null;
  telefone?: string | null;
  situacao: SituacaoParada;
  motivo_texto: string | null;
  started_at: string | null;
  arrived_at: string | null;
  completed_at: string | null;
  reagendado_para: string | null;   // "YYYY-MM-DD HH:MM" ou "FIM" (depois das outras)
  tentativas: number;
  comprovantes: Comprovante[];
  // NFs do pedido (documentos_processados): um canhoto por NF (Hugo, 12/09).
  // Vazio = pedido sem NF conhecida, um canhoto só e sem cobrar número.
  nfs?: string[];
};

// Pedágio da rota (Hugo, 11/09): valor + foto do recibo, um por recibo;
// PENDENTE até o painel aprovar. Só o aprovado entra no extrato.
// CANCELADO = o motorista desistiu do envio pelo app (só de PENDENTE).
export type Pedagio = {
  id: number;
  uuid: string;
  valor_informado: number;
  status: 'PENDENTE' | 'APROVADO' | 'REJEITADO' | 'CANCELADO';
  valor_aprovado: number | null;
  capturado_em: string | null;
  enviado_em: string;
  observacao_revisao: string | null;
  tem_foto: boolean;
  validacao?: string | null;
};

export type Rota = {
  id: number;
  data_rota: string;
  nome: string | null;
  provedor: 'VUUPT' | 'APP';
  editavel: boolean;
  status: StatusRota;
  start_at: string | null;
  km_estimado: number | null;
  km_volta_estimado?: number | null;
  km_real: number | null;
  km_fonte: string | null;
  total_paradas: number;
  entregues: number;
  insucessos: number;
  iniciada_em: string | null;
  concluida_em: string | null;
  confirmacao: { status: string; respondido_em: string | null; motivo_recusa: string | null } | null;
  paradas: Parada[];
  pedagios?: Pedagio[];
};

export type CampoChecklist = {
  chave: string;
  rotulo: string;
  tipo: 'TEXTO' | 'SELECAO' | 'FOTO' | 'DOCUMENTO' | 'NUMERO';
  obrigatorio: boolean;
  opcoes: string[] | null;
  aviso: string | null;
};

export type Checklist = {
  fluxos: Record<string, CampoChecklist[]>;
  motivos: { id: number; motivo_texto: string; categoria: string }[];
  // Conferência automática das fotos; ativo=false (padrão) = tudo como antes.
  validacao_fotos?: { ativo: boolean; max_tentativas: number; exigir_nf: boolean };
};

export type Oferta = {
  rascunho_id: number;
  data_alvo: string;
  status: string;
  aplicada?: boolean;
  resumo: Record<string, unknown>;
};

export type LinhaExtrato = {
  rota_id: number;
  data_rota: string;
  nome: string | null;
  status: StatusRota;
  total_paradas: number;
  entregues: number;
  insucessos: number;
  km: number | null;
  km_fonte: string | null;
  km_provisorio: boolean;
  // Regra do km (regras/km_cobrado.py): a volta ao CD só conta com
  // insucesso/parcial ou parada fora da Grande SP.
  km_detalhe?: { volta_conta: boolean; motivo_volta: 'INSUCESSO' | 'PARCIAL' | 'FORA_GRANDE_SP' | null; km_volta: number | null; volta_estimada: boolean } | null;
  sem_tarifa: boolean;
  valor: number | null;
  pedagio_aprovado?: number;
  pedagio_pendente?: number;
  pedagio_rejeitado?: number;
  pedagios?: number;
  valor_com_pedagio?: number | null;
};

export type Extrato = {
  linhas: LinhaExtrato[];
  por_dia: { data: string; rotas: number; valor: number; km: number; sem_tarifa: number; provisorio: boolean; pedagio_aprovado?: number; pedagio_pendente?: number }[];
  total: number;
  total_rotas?: number;
  total_pedagio?: number;
  pedagio_pendente?: number;
  rotas_sem_tarifa: number;
  valores_provisorios: boolean;
  tarifa: { nome_tarifa: string; valor_base: number; km_franquia: number; valor_km_adicional: number } | null;
};

export type Ajuste = { data: string; disponivel: boolean; motivo: string | null };

// ── Atendimento (aba Ajuda) ────────────────────────────────────────────────
// Chat do motorista com o assistente e com a logística. Mesmo chamado que a
// equipe vê na tela /atendimento do painel (portal_cliente/chamados.py).

export type StatusChamado = 'COM_ASSISTENTE' | 'NA_FILA' | 'EM_ATENDIMENTO' | 'AGUARDANDO_FL' | 'RESPONDIDO' | 'RESOLVIDO';

export type PedidoOpcao = {
  ordem: number;
  nome: string;
  codigo: string;
  bairro: string;
  caixas: number | null;
  situacao: string;
  situacao_rotulo: string;
  janela: string;
};

export type OpcaoMensagem = {
  rotulo: string;
  valor?: string;
  acao: 'chip' | 'atendente' | 'resolvido';
  estilo?: 'principal' | 'linha';
  pedido?: PedidoOpcao;   // "qual pedido?": o app mostra como cartão
};

export type AnexoChamado = { nome: string; arquivo: string; tamanho: number; tipo: string };

export type MensagemChamado = {
  id: number;
  origem: 'cliente' | 'equipe' | 'assistente' | 'sistema';
  autor: string | null;
  canal: string;
  texto: string;
  anexos: AnexoChamado[];
  opcoes: OpcaoMensagem[];
  hora: string;
  quando: string;
};

export type Chamado = {
  id: number;
  assunto: string | null;
  area: string | null;
  area_rotulo: string | null;
  pedido_ref: string | null;
  status: StatusChamado;
  status_rotulo: string;
  origem: string;
  etapa_assistente: string | null;
  atendente: string | null;
  criado_em: string;
  ultima_msg_em: string | null;
  resolvido_em: string | null;
  quando: string;
  aberto: boolean;
  nao_lidas?: number;
  ultima_texto?: string | null;
  ultima_origem_msg?: string | null;
  rota_id: number | null;
};

export type SituacaoAtendimento = {
  estado: 'online' | 'ausente' | 'almoco' | 'fechado';
  texto: string;
  dentro_horario: boolean;
  volta_em: string;
  horario: string;
  atendentes_online: number;
  nomes_online: string[];
};

export type EstadoAtendimento = {
  situacao: SituacaoAtendimento;
  ativo: { chamado: Chamado; mensagens: MensagemChamado[] } | null;
  chamados: Chamado[];
  nao_lidas: number;
  areas: { valor: string; rotulo: string }[];
  agora: string;
};

export type RespostaChamado = {
  chamado: Chamado;
  mensagens: MensagemChamado[];
  situacao?: SituacaoAtendimento;
};
