// PWA: Alert.alert (confirmações, "tentar de novo ou seguir", escolha de
// câmera/galeria) vira um modal. Ao importar este arquivo o Alert já é
// trocado, então as telas continuam chamando Alert.alert como sempre.
import React, { useEffect, useState } from 'react';
import { Alert, AlertButton, AlertOptions, Modal, StyleSheet, Text, View } from 'react-native';
import { Botao } from './componentes';
import { cores } from './tema';

type Pedido = { titulo: string; mensagem?: string; botoes: AlertButton[]; opcoes?: AlertOptions };

let mostrar: ((p: Pedido) => void) | null = null;
const esperando: Pedido[] = [];

(Alert as unknown as { alert: (t: string, m?: string, b?: AlertButton[], o?: AlertOptions) => void }).alert = (titulo, mensagem, botoes, opcoes) => {
  const p: Pedido = { titulo, mensagem, botoes: botoes && botoes.length ? botoes : [{ text: 'OK' }], opcoes };
  if (mostrar) mostrar(p);
  else esperando.push(p);
};

export function AlertaHost(): React.JSX.Element | null {
  const [fila, setFila] = useState<Pedido[]>([]);

  useEffect(() => {
    mostrar = (p) => setFila((f) => [...f, p]);
    if (esperando.length) setFila(esperando.splice(0));
    return () => {
      mostrar = null;
    };
  }, []);

  const atual = fila[0];
  if (!atual) return null;
  const fechar = (b?: AlertButton) => {
    setFila((f) => f.slice(1));
    b?.onPress?.();
  };
  const cancelavel = atual.opcoes?.cancelable !== false;
  return (
    <Modal transparent animationType="fade" visible onRequestClose={() => { if (cancelavel) { atual.opcoes?.onDismiss?.(); fechar(); } }}>
      <View style={s.fundo}>
        <View style={s.caixa}>
          <Text style={s.titulo}>{atual.titulo}</Text>
          {atual.mensagem ? <Text style={s.mensagem}>{atual.mensagem}</Text> : null}
          <View style={s.botoes}>
            {atual.botoes.map((b, i) => (
              <Botao
                key={`${b.text}-${i}`}
                titulo={b.text ?? 'OK'}
                tipo={b.style === 'cancel' ? 'secundario' : b.style === 'destructive' ? 'perigo' : 'primario'}
                onPress={() => fechar(b)}
              />
            ))}
          </View>
        </View>
      </View>
    </Modal>
  );
}

const s = StyleSheet.create({
  fundo: { flex: 1, backgroundColor: 'rgba(0,0,0,0.5)', justifyContent: 'center', padding: 24 },
  caixa: { backgroundColor: cores.cartao, borderRadius: 14, padding: 20, maxWidth: 480, width: '100%', alignSelf: 'center' },
  titulo: { fontSize: 18, fontWeight: '800', color: cores.texto, marginBottom: 8 },
  mensagem: { fontSize: 15, color: cores.texto, marginBottom: 16, lineHeight: 21 },
  botoes: { gap: 8 },
});
