// Guarda o token do login. No celular nativo vai pro cofre do sistema;
// na web (PWA) existe segredo.web.ts com o mesmo contrato.
import * as SecureStore from 'expo-secure-store';

export const ler = (chave: string): Promise<string | null> => SecureStore.getItemAsync(chave);
export const gravar = (chave: string, valor: string): Promise<void> => SecureStore.setItemAsync(chave, valor);
export const apagar = (chave: string): Promise<void> => SecureStore.deleteItemAsync(chave);
