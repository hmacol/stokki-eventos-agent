// Opções do auto-cadastro e de "Meus dados". Espelham o servidor
// (nucleo/cadastro_motorista.py: ZONAS/DIAS; regras/tipo_veiculo.py).
// Mudou lá, muda aqui.
export const TIPOS_VEICULO = [
  { codigo: 'FIORINO', nome: 'Fiorino / utilitário pequeno' },
  { codigo: 'VAN_HR', nome: 'Van / HR' },
  { codigo: 'VUC', nome: 'VUC' },
  { codigo: 'TRES_QUARTOS', nome: '3/4' },
  { codigo: 'TRUCK', nome: 'Truck' },
];

export const ZONAS = [
  'ZONA NORTE', 'ZONA SUL', 'ZONA LESTE', 'ZONA OESTE', 'CENTRO', 'GUARULHOS', 'ABCD',
  'OSASCO-BARUERI-ALPHAVILLE', 'COTIA-EMBU-TABOAO',
];

export const DIAS: { codigo: string; rotulo: string }[] = [
  { codigo: 'SEGUNDA', rotulo: 'Seg' }, { codigo: 'TERCA', rotulo: 'Ter' }, { codigo: 'QUARTA', rotulo: 'Qua' },
  { codigo: 'QUINTA', rotulo: 'Qui' }, { codigo: 'SEXTA', rotulo: 'Sex' }, { codigo: 'SABADO', rotulo: 'Sáb' },
  { codigo: 'DOMINGO', rotulo: 'Dom' },
];

export const mascaraCpf = (v: string): string => {
  const d = v.replace(/\D/g, '').slice(0, 11);
  return d.replace(/(\d{3})(\d)/, '$1.$2').replace(/(\d{3})(\d)/, '$1.$2').replace(/(\d{3})(\d{1,2})$/, '$1-$2');
};

export const mascaraTelefone = (v: string): string => {
  const d = v.replace(/\D/g, '').slice(0, 11);
  if (d.length <= 2) return d;
  if (d.length <= 6) return `(${d.slice(0, 2)}) ${d.slice(2)}`;
  if (d.length <= 10) return `(${d.slice(0, 2)}) ${d.slice(2, 6)}-${d.slice(6)}`;
  return `(${d.slice(0, 2)}) ${d.slice(2, 7)}-${d.slice(7)}`;
};

export const placaValida = (v: string): boolean => /^[A-Z]{3}\d[A-Z0-9]\d{2}$/.test(v.replace(/[\s-]/g, '').toUpperCase());
