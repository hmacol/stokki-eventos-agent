// Paleta e utilitários visuais do app -- simples, alto contraste (uso
// ao sol, com luva), botões grandes.
export const cores = {
  fundo: '#F4F6F5',
  cartao: '#FFFFFF',
  texto: '#1B2621',
  textoSuave: '#5F6B66',
  borda: '#DDE3E0',
  primaria: '#1E8E5A',
  primariaEscura: '#166B44',
  perigo: '#C9372C',
  alerta: '#D98B12',
  info: '#2F6FB0',
  sucesso: '#1E8E5A',
};

export const situacaoCor: Record<string, string> = {
  PENDENTE: '#9AA6A0',
  EM_ROTA: cores.info,
  ENTREGUE: cores.sucesso,
  PARCIAL: cores.alerta,
  INSUCESSO: cores.perigo,
  CANCELADA: '#777',
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
