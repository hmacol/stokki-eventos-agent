// Guia de instalação do app no iPhone (camada 1 do tour, 22/11).
// Aparece em tela cheia quando o app é aberto no navegador do iPhone/iPad e AINDA NÃO está instalado na
// tela inicial. Instalado, o app funciona sem sinal e o iPhone não apaga os dados do motorista (o Safari
// limpa o armazenamento de sites não instalados depois de alguns dias sem uso).
//   - Dentro de outro app (navegador interno do WhatsApp, etc.) o passo 1 manda abrir no Safari.
//   - "Já instalei" só lembra de abrir pelo ícone; "Continuar mesmo assim" dispensa por 7 dias.
//   - Aberto pelo ícone (standalone), no computador ou no Android: nunca aparece.
import React, { useEffect, useState } from 'react';
import { Image, Modal, Pressable, ScrollView, StyleSheet, Text, View } from 'react-native';
import { Ionicons } from '@expo/vector-icons';
import { Botao } from './componentes';
import { cores } from './tema';

const CHAVE_DISPENSA = 'motorista.guia_instalacao.dispensado_ate';
const DIAS_DISPENSA = 7;

type Ambiente = { ios: boolean; instalado: boolean; safari: boolean };

function ambiente(): Ambiente {
  const ua = navigator.userAgent;
  const ios = /iPhone|iPad|iPod/.test(ua) || (/Macintosh/.test(ua) && navigator.maxTouchPoints > 1);
  const instalado = (navigator as unknown as { standalone?: boolean }).standalone === true
    || (typeof window.matchMedia === 'function' && window.matchMedia('(display-mode: standalone)').matches);
  // Safari de verdade. Navegadores internos (WhatsApp, Instagram...) e outros navegadores não oferecem o caminho certo.
  const safari = /Safari\//.test(ua) && !/CriOS|FxiOS|EdgiOS|OPiOS|FBAN|FBAV|Instagram|Line\/|GSA|Twitter|MicroMessenger/.test(ua);
  return { ios, instalado, safari };
}

function dispensado(): boolean {
  try {
    const ate = Number(window.localStorage.getItem(CHAVE_DISPENSA) || 0);
    return Date.now() < ate;
  } catch {
    return false;
  }
}

function dispensar(): void {
  try {
    window.localStorage.setItem(CHAVE_DISPENSA, String(Date.now() + DIAS_DISPENSA * 86400000));
  } catch {
    // sem armazenamento: o guia volta na próxima abertura, não é erro
  }
}

function Passo({ n, icone, titulo, texto, destaque }: { n: number; icone: React.ComponentProps<typeof Ionicons>['name']; titulo: string; texto?: string; destaque?: boolean }) {
  return (
    <View style={[s.passo, destaque && s.passoDestaque]}>
      <View style={s.numero}><Text style={s.numeroTexto}>{n}</Text></View>
      <View style={{ flex: 1 }}>
        <Text style={s.passoTitulo}>{titulo}</Text>
        {texto ? <Text style={s.passoTexto}>{texto}</Text> : null}
      </View>
      <Ionicons name={icone} size={34} color={cores.primaria} />
    </View>
  );
}

export function GuiaInstalacao(): React.JSX.Element | null {
  const [env, setEnv] = useState<Ambiente | null>(null);
  const [aberto, setAberto] = useState(false);
  const [jaInstalei, setJaInstalei] = useState(false);
  const [copiado, setCopiado] = useState<boolean | null>(null);

  useEffect(() => {
    const e = ambiente();
    setEnv(e);
    setAberto(e.ios && !e.instalado && !dispensado());
  }, []);

  if (!aberto || !env) return null;

  const copiar = async () => {
    try {
      await navigator.clipboard.writeText(window.location.origin);
      setCopiado(true);
    } catch {
      setCopiado(false);
    }
  };
  const continuar = () => {
    dispensar();
    setAberto(false);
  };

  // Numeração segue o que o motorista de fato precisa fazer (no Safari o passo "abrir no Safari" não existe).
  let n = 0;
  return (
    <Modal visible animationType="fade" onRequestClose={continuar}>
      <ScrollView style={{ flex: 1, backgroundColor: cores.fundo }} contentContainerStyle={s.tela}>
        <Image source={require('../assets/logo_freshlog.png')} style={s.logo} resizeMode="contain" accessibilityLabel="FreshLog" />
        <Text style={s.titulo}>Instale o app no seu iPhone</Text>
        <Text style={s.subtitulo}>
          Leva 30 segundos. Instalado, o app funciona mesmo sem sinal e não perde os seus dados.
        </Text>

        {!env.safari ? (
          <>
            <Passo
              n={++n} icone="compass-outline" destaque
              titulo="Abra este link no Safari"
              texto="Você está num navegador dentro de outro app. Toque em ⋯ (ou no ícone da bússola) e escolha Abrir no Safari."
            />
            <Pressable onPress={() => void copiar()} style={s.copiar} accessibilityRole="button">
              <Ionicons name="link-outline" size={20} color={cores.primaria} />
              <Text style={s.copiarTexto}>
                {copiado === true ? 'Link copiado. Cole no Safari.' : copiado === false ? 'Não deu pra copiar. Selecione o endereço na barra.' : 'Copiar o link'}
              </Text>
            </Pressable>
          </>
        ) : null}
        <Passo
          n={++n} icone="share-outline"
          titulo="Toque em Compartilhar"
          texto="É o quadrado com a seta para cima, na barra do Safari. Em alguns iPhones fica dentro do botão ⋯."
        />
        <Passo
          n={++n} icone="add-circle-outline"
          titulo="Toque em Adicionar à Tela de Início"
          texto="Role a lista até achar. Depois toque em Adicionar, no canto da tela."
        />
        <Passo
          n={++n} icone="phone-portrait-outline"
          titulo="Abra o ícone FreshLog"
          texto="Feche o Safari e abra o ícone novo na tela do seu iPhone. É por ele que você usa o app."
        />

        {jaInstalei ? (
          <View style={s.lembrete}>
            <Ionicons name="checkmark-circle" size={22} color={cores.acento} />
            <Text style={s.lembreteTexto}>Ótimo! Agora feche o Safari e abra o ícone FreshLog na sua tela. É lá que o app funciona.</Text>
          </View>
        ) : (
          <Botao titulo="Já instalei" onPress={() => setJaInstalei(true)} estilo={{ marginTop: 20 }} />
        )}
        <Pressable onPress={continuar} style={s.continuar} accessibilityRole="button">
          <Text style={s.continuarTexto}>Continuar mesmo assim</Text>
        </Pressable>
        <Text style={s.aviso}>
          Sem instalar, o iPhone pode apagar os seus dados depois de alguns dias sem uso, e o app não abre sem sinal.
        </Text>
      </ScrollView>
    </Modal>
  );
}

const s = StyleSheet.create({
  tela: { padding: 20, paddingTop: 40, paddingBottom: 48, maxWidth: 520, width: '100%', alignSelf: 'center' },
  logo: { width: 150, height: 44, alignSelf: 'center', marginBottom: 20 },
  titulo: { fontSize: 24, fontWeight: '800', color: cores.texto, textAlign: 'center' },
  subtitulo: { fontSize: 16, color: cores.textoSuave, textAlign: 'center', lineHeight: 23, marginTop: 8, marginBottom: 22 },
  passo: { flexDirection: 'row', alignItems: 'center', gap: 12, backgroundColor: cores.cartao, borderRadius: 14, borderWidth: 1, borderColor: cores.borda, padding: 14, marginBottom: 12 },
  passoDestaque: { borderColor: cores.alerta, backgroundColor: cores.alertaBg },
  numero: { width: 32, height: 32, borderRadius: 16, backgroundColor: cores.primaria, alignItems: 'center', justifyContent: 'center' },
  numeroTexto: { color: '#fff', fontWeight: '800', fontSize: 16 },
  passoTitulo: { fontSize: 17, fontWeight: '800', color: cores.texto },
  passoTexto: { fontSize: 14, color: cores.textoSuave, lineHeight: 20, marginTop: 3 },
  copiar: { flexDirection: 'row', alignItems: 'center', justifyContent: 'center', gap: 8, paddingVertical: 12, marginBottom: 12 },
  copiarTexto: { color: cores.primaria, fontWeight: '700', fontSize: 15 },
  lembrete: { flexDirection: 'row', gap: 10, alignItems: 'flex-start', backgroundColor: cores.acentoBg, borderRadius: 14, padding: 14, marginTop: 20 },
  lembreteTexto: { flex: 1, color: cores.texto, fontSize: 15, lineHeight: 21, fontWeight: '600' },
  continuar: { alignItems: 'center', paddingVertical: 16, marginTop: 6 },
  continuarTexto: { color: cores.textoSuave, fontSize: 15, textDecorationLine: 'underline' },
  aviso: { color: cores.textoSuave, fontSize: 13, textAlign: 'center', lineHeight: 19 },
});
