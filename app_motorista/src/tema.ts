// Paleta da marca FreshLog -- a MESMA do painel (painel_agentes/templates/
// _paleta_cores.html) e da página de confirmação de rota
// (confirmacao_motoristas/templates/*.html): navy do logo como cor
// primária (cabeçalhos, chips selecionados), verde-água do logo como
// acento (botões de ação, sucesso). Alto contraste, botões grandes (uso
// ao sol, com luva).
export const cores = {
  // marca
  primaria: '#141428',        // --primaria (navy do logo)
  primariaClara: '#1F1F3D',   // --primaria-clara
  primariaEscura: '#0B0B1A',
  acento: '#0EA575',          // --acento (verde do logo, tom sólido)
  acentoLogo: '#00C896',      // verde-água do gradiente do logo (detalhes)
  acentoBg: '#E5F7EF',        // --sucesso-bg
  // superfícies e texto
  fundo: '#F3F4F6',           // --fundo
  cartao: '#FFFFFF',          // --superficie
  borda: '#E5E7EB',           // --borda
  texto: '#1F2937',           // --texto
  textoSuave: '#6B7280',      // --texto-suave
  textoSobrePrimaria: '#FFFFFF',
  textoSuaveSobrePrimaria: '#C7C7E0',
  // estados
  sucesso: '#0EA575',         // --sucesso
  perigo: '#E1483D',          // --erro
  perigoBg: '#FDEBEA',        // --erro-bg
  alerta: '#E8A33D',          // --rodando
  alertaBg: '#FDF3E1',        // --rodando-bg
  info: '#3B6FD6',
};

export const situacaoCor: Record<string, string> = {
  PENDENTE: '#9CA3AF',
  EM_ROTA: cores.info,
  ENTREGUE: cores.sucesso,
  PARCIAL: cores.alerta,
  INSUCESSO: cores.perigo,
  CANCELADA: '#6B7280',
};

export const situacaoRotulo: Record<string, string> = {
  PENDENTE: 'Pendente',
  EM_ROTA: 'No local',
  ENTREGUE: 'Entregue',
  PARCIAL: 'Parcial',
  INSUCESSO: 'Insucesso',
  CANCELADA: 'Cancelada',
};

export const statusRotaRotulo: Record<string, string> = {
  PLANEJADA: 'Aguardando aceite',
  ACEITA: 'Aceita',
  EM_ROTA: 'Em rota',
  CONCLUIDA: 'Concluída',
  CANCELADA: 'Cancelada',
};

export const statusRotaCor: Record<string, string> = {
  PLANEJADA: cores.alerta,
  ACEITA: cores.info,
  EM_ROTA: cores.acento,
  CONCLUIDA: cores.textoSuave,
  CANCELADA: '#6B7280',
};

export function formatarReal(v: number | null | undefined): string {
  if (v === null || v === undefined) return 'a definir';
  return `R$ ${v.toFixed(2).replace('.', ',')}`;
}

export function formatarData(iso: string): string {
  const [a, m, d] = iso.slice(0, 10).split('-');
  return `${d}/${m}/${a}`;
}

export function hoje(): string {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

export function somarDias(iso: string, n: number): string {
  const d = new Date(`${iso}T12:00:00`);
  d.setDate(d.getDate() + n);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}
