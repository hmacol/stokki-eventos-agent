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

export type Comprovante = { id: number; tipo: string; uuid: string; capturado_em: string | null };

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
  km_real: number | null;
  km_fonte: string | null;
  total_paradas: number;
  entregues: number;
  insucessos: number;
  iniciada_em: string | null;
  concluida_em: string | null;
  confirmacao: { status: string; respondido_em: string | null; motivo_recusa: string | null } | null;
  paradas: Parada[];
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
  sem_tarifa: boolean;
  valor: number | null;
};

export type Extrato = {
  linhas: LinhaExtrato[];
  por_dia: { data: string; rotas: number; valor: number; km: number; sem_tarifa: number; provisorio: boolean }[];
  total: number;
  rotas_sem_tarifa: number;
  valores_provisorios: boolean;
  tarifa: { nome_tarifa: string; valor_base: number; km_franquia: number; valor_km_adicional: number } | null;
};

export type Ajuste = { data: string; disponivel: boolean; motivo: string | null };
