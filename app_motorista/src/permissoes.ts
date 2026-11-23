// Permissões do aparelho (câmera, microfone). O pedido em si é o do sistema
// (request...PermissionsAsync); aqui só o que fazer quando ele nega.
// Hugo, 08/10: se o Android parou de perguntar ("não perguntar de novo"),
// oferecer o atalho pras Configurações do app em vez de só avisar.
import { Alert, Linking } from 'react-native';

type Resultado = { granted: boolean; canAskAgain?: boolean };

/** Devolve true se liberado. Negado: avisa; bloqueado de vez: avisa com botão "Abrir configurações". */
export function conferirPermissao(r: Resultado, titulo: string, texto: string): boolean {
  if (r.granted) return true;
  if (r.canAskAgain === false) {
    Alert.alert(titulo, `${texto}\n\nO Android parou de perguntar. Abra as configurações do app, ligue a permissão e volte.`, [
      { text: 'Agora não', style: 'cancel' },
      { text: 'Abrir configurações', onPress: () => void Linking.openSettings() },
    ]);
  } else {
    Alert.alert(titulo, texto);
  }
  return false;
}
