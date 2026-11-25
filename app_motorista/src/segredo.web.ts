// PWA: o navegador não tem cofre; o token fica no localStorage do próprio
// app (mesma origem do servidor, sem terceiros). localStorage pode lançar
// em modo privado/bloqueado: nesse caso a sessão só vale até fechar o app.
const memoria = new Map<string, string>();

export async function ler(chave: string): Promise<string | null> {
  try {
    return window.localStorage.getItem(chave) ?? memoria.get(chave) ?? null;
  } catch {
    return memoria.get(chave) ?? null;
  }
}

export async function gravar(chave: string, valor: string): Promise<void> {
  memoria.set(chave, valor);
  try {
    window.localStorage.setItem(chave, valor);
  } catch {
    // fica só na memória
  }
}

export async function apagar(chave: string): Promise<void> {
  memoria.delete(chave);
  try {
    window.localStorage.removeItem(chave);
  } catch {
    // nada a limpar
  }
}
