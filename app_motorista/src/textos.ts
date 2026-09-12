// Formatação SÓ de exibição (Hugo, 12/09): o cadastro chega em CAIXA ALTA
// com sufixo societário e o endereço da Vuupt vem com complemento ("- ATÉ
// 707/708"), CEP e "Brasil" -- nada disso ajuda o motorista a achar a porta,
// e tudo isso empurra o que importa pra fora da tela. O valor original
// continua intacto: é ele que vai pro Navegar e pro servidor.

const PEQUENAS = new Set(['de', 'da', 'do', 'das', 'dos', 'e', 'em', 'no', 'na', 'com']);
const SUFIXO_SOCIETARIO = /[\s,.-]+(ltda|me|epp|eireli|mei)\.?$/i;
const CEP = /^\d{5}-?\d{3}$/;
const PAIS = /^(brasil|brazil)$/i;
const VOGAIS = /[aeiouáàâãéêíóôõúü]/i;

/** "IXE SANDUÍCHES LTDA" -> "Ixe Sanduíches". Nome que já vem em caixa
 *  mista é devolvido como está -- quem cadastrou sabia o que queria. */
export function tituloCaso(texto: string): string {
  const limpo = texto.trim();
  if (!limpo || /[a-záàâãéêíóôõúüç]/.test(limpo)) return limpo;
  return limpo
    .split(/\s+/)
    .map((p, i) => {
      const baixo = p.toLocaleLowerCase('pt-BR');
      if (i > 0 && PEQUENAS.has(baixo)) return baixo;
      if (/\d/.test(p)) return p;                               // "24H", "3M"
      if (p.includes('/')) return p;                            // "S/A", "KM/H"
      if (p.length <= 4 && !VOGAIS.test(p)) return p;           // siglas: JBS, SBT
      return p.charAt(0) + baixo.slice(1);
    })
    .join(' ');
}

export function nomeExibicao(p: { destinatario_nome?: string | null; titulo?: string | null; codigo?: string | null }): string {
  let bruto = (p.destinatario_nome || p.titulo || p.codigo || 'Parada').trim();
  // "FULANO LTDA ME" tem dois sufixos: tira até parar de casar.
  for (let i = 0; i < 2 && SUFIXO_SOCIETARIO.test(bruto); i += 1) bruto = bruto.replace(SUFIXO_SOCIETARIO, '');
  return tituloCaso(bruto) || 'Parada';
}

export type PartesEndereco = {
  rua: string;        // "Rua Cotoxó 404" (sem o complemento "- ATÉ 707/708")
  resto: string;      // "Perdizes · São Paulo"
  bairro: string;     // "Perdizes" (primeiro trecho depois da rua)
  completo: string;   // o endereço como veio, pra quem precisa do detalhe
};

/** Nada é inventado: só saem CEP, país e o "- ATÉ nnn" do correio. Se o
 *  endereço vier num formato inesperado, cai pro texto original inteiro. */
export function partesEndereco(endereco: string | null | undefined): PartesEndereco {
  const completo = (endereco ?? '').trim();
  const partes = completo
    .split(',')
    .map((x) => x.trim())
    .filter((x) => x && !CEP.test(x) && !PAIS.test(x));
  if (partes.length === 0) return { rua: completo, resto: '', bairro: '', completo };

  const rua = tituloCaso(partes[0].replace(/\s*-\s*(at[ée]|de)\s+.*$/i, '').trim()) || completo;
  const demais = partes.slice(1).map((x, i, todas) => {
    // Só no último trecho a UF grudada ("SÃO PAULO - SP") vira ruído.
    const semUf = i === todas.length - 1 ? x.replace(/\s*-\s*[A-Za-z]{2}$/, '') : x;
    return tituloCaso(semUf.trim());
  }).filter(Boolean);

  return { rua, resto: demais.join(' · '), bairro: demais[0] ?? '', completo };
}

export type TomJanela = 'neutro' | 'alerta' | 'perigo' | 'ausente';
export type ResumoJanela = { texto: string; detalhe: string; tom: TomJanela };

function minutosAte(dataRota: string, hhmm: string, agora: number): number | null {
  const m = /^(\d{1,2}):(\d{2})/.exec(hhmm.trim());
  if (!m) return null;
  const alvo = new Date(`${dataRota}T${m[1].padStart(2, '0')}:${m[2]}:00`);
  const t = alvo.getTime();
  return Number.isNaN(t) ? null : Math.round((t - agora) / 60000);
}

/** Quanto falta pra janela fechar. É o dado que muda a decisão do motorista
 *  (qual parada fazer antes), por isso ganha faixa própria no cartão. */
export function resumoJanela(
  p: { janela_inicio: string | null; janela_fim: string | null },
  dataRota: string,
  agora: number,
): ResumoJanela {
  const inicio = (p.janela_inicio ?? '').trim();
  const fim = (p.janela_fim ?? '').trim();
  if (!inicio && !fim) return { texto: 'Sem janela definida', detalhe: 'entrega no horário da rota', tom: 'ausente' };
  if (!fim) return { texto: `a partir de ${inicio}`, detalhe: '', tom: 'neutro' };

  const texto = inicio ? `${inicio} – ${fim}` : `até ${fim}`;
  const min = minutosAte(dataRota, fim, agora);
  if (min === null) return { texto, detalhe: '', tom: 'neutro' };
  if (min <= 0) return { texto, detalhe: `fechou às ${fim}`, tom: 'perigo' };
  const detalhe = min < 60 ? `fecha em ${min} min` : `fecha em ${Math.floor(min / 60)}h${String(min % 60).padStart(2, '0')}`;
  return { texto, detalhe, tom: min <= 120 ? 'alerta' : 'neutro' };
}

/** "2026-09-12T10:42:31" ou "2026-09-12 10:42:31" -> "10:42". */
export function horaDe(iso: string | null | undefined): string {
  const t = (iso ?? '').slice(11, 16);
  return /^\d{2}:\d{2}$/.test(t) ? t : '';
}
