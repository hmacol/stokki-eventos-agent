// Assinatura na tela (react-native-signature-canvas, roda num WebView).
// Devolve o PNG em base64 (data URL) -- a tela da parada grava num
// arquivo e manda como comprovante tipo ASSINATURA.
import React, { useRef } from 'react';
import { Modal, StyleSheet, Text, View } from 'react-native';
import SignatureScreen, { SignatureViewRef } from 'react-native-signature-canvas';
import { Botao } from './componentes';
import { cores } from './tema';

export function ModalAssinatura({ visivel, onFechar, onAssinou }: { visivel: boolean; onFechar: () => void; onAssinou: (dataUrl: string) => void }) {
  const ref = useRef<SignatureViewRef>(null);
  return (
    <Modal visible={visivel} animationType="slide" onRequestClose={onFechar}>
      <View style={s.tela}>
        <Text style={s.titulo}>Assinatura de quem recebeu</Text>
        <View style={s.area}>
          <SignatureScreen
            ref={ref}
            onOK={(sig) => onAssinou(sig)}
            onEmpty={() => undefined}
            descriptionText=""
            webStyle=".m-signature-pad--footer {display:none} .m-signature-pad {box-shadow:none;border:0} body,html{background:#fff}"
            backgroundColor="#fff"
            penColor={cores.texto}
            imageType="image/png"
          />
        </View>
        <View style={s.botoes}>
          <Botao titulo="Limpar" tipo="secundario" onPress={() => ref.current?.clearSignature()} estilo={{ flex: 1 }} />
          <Botao titulo="Cancelar" tipo="secundario" onPress={onFechar} estilo={{ flex: 1 }} />
          <Botao titulo="Confirmar" onPress={() => ref.current?.readSignature()} estilo={{ flex: 1 }} />
        </View>
      </View>
    </Modal>
  );
}

const s = StyleSheet.create({
  tela: { flex: 1, backgroundColor: cores.fundo, padding: 16, paddingTop: 48 },
  titulo: { fontSize: 18, fontWeight: '800', color: cores.texto, marginBottom: 12 },
  area: { flex: 1, borderWidth: 2, borderColor: cores.borda, borderRadius: 12, overflow: 'hidden', backgroundColor: '#fff' },
  botoes: { flexDirection: 'row', gap: 8, marginTop: 12 },
});
