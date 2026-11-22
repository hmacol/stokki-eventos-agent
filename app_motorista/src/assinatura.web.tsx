// PWA: assinatura em <canvas> (a versão nativa usa um WebView, que não
// existe na web). Mesmo contrato: devolve o PNG como data URL.
import React, { useRef, useState } from 'react';
import { Modal, StyleSheet, Text, View } from 'react-native';
import { Botao } from './componentes';
import { cores } from './tema';

export function ModalAssinatura({ visivel, onFechar, onAssinou }: { visivel: boolean; onFechar: () => void; onAssinou: (dataUrl: string) => void }) {
  const canvas = useRef<HTMLCanvasElement | null>(null);
  const desenhando = useRef(false);
  const [desenhou, setDesenhou] = useState(false);

  const preparar = (el: HTMLCanvasElement | null) => {
    canvas.current = el;
    if (!el) return;
    const caixa = el.parentElement?.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    el.width = Math.max(1, Math.floor((caixa?.width ?? 300) * dpr));
    el.height = Math.max(1, Math.floor((caixa?.height ?? 300) * dpr));
    const c = el.getContext('2d');
    if (c) {
      c.scale(dpr, dpr);
      c.lineWidth = 2.5;
      c.lineCap = 'round';
      c.lineJoin = 'round';
      c.strokeStyle = cores.texto;
    }
  };
  const ponto = (e: React.PointerEvent<HTMLCanvasElement>) => {
    const r = e.currentTarget.getBoundingClientRect();
    return { x: e.clientX - r.left, y: e.clientY - r.top };
  };
  const limpar = () => {
    const el = canvas.current;
    el?.getContext('2d')?.clearRect(0, 0, el.width, el.height);
    setDesenhou(false);
  };
  const confirmar = () => {
    if (!canvas.current || !desenhou) return;
    onAssinou(canvas.current.toDataURL('image/png'));
  };

  return (
    <Modal visible={visivel} animationType="slide" onRequestClose={onFechar}>
      <View style={s.tela}>
        <Text style={s.titulo}>Assinatura de quem recebeu</Text>
        <View style={s.area}>
          <canvas
            ref={preparar}
            style={{ width: '100%', height: '100%', touchAction: 'none', display: 'block' }}
            onPointerDown={(e) => {
              e.currentTarget.setPointerCapture(e.pointerId);
              desenhando.current = true;
              const { x, y } = ponto(e);
              const c = e.currentTarget.getContext('2d');
              c?.beginPath();
              c?.moveTo(x, y);
            }}
            onPointerMove={(e) => {
              if (!desenhando.current) return;
              const { x, y } = ponto(e);
              const c = e.currentTarget.getContext('2d');
              c?.lineTo(x, y);
              c?.stroke();
              if (!desenhou) setDesenhou(true);
            }}
            onPointerUp={() => { desenhando.current = false; }}
            onPointerCancel={() => { desenhando.current = false; }}
          />
        </View>
        <View style={s.botoes}>
          <Botao titulo="Limpar" tipo="secundario" onPress={limpar} estilo={{ flex: 1 }} />
          <Botao titulo="Cancelar" tipo="secundario" onPress={onFechar} estilo={{ flex: 1 }} />
          <Botao titulo="Confirmar" onPress={confirmar} desabilitado={!desenhou} estilo={{ flex: 1 }} />
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
