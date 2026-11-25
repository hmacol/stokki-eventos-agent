// Upload multipart de um arquivo local. Nativo: pelo uploader do
// expo-file-system, que lê do disco sem passar pelo JS (ver nota em
// api.ts). Na web: envioArquivo.web.ts, com fetch + FormData.
import * as FileSystem from 'expo-file-system/legacy';

export type RespostaUpload = { status: number; body: string };

/** null = o arquivo não existe mais no aparelho. Erros de leitura/rede são lançados. */
export async function postarArquivo(
  url: string, uri: string, campo: string, parametros: Record<string, string>, headers: Record<string, string>,
): Promise<RespostaUpload | null> {
  const info = await FileSystem.getInfoAsync(uri);
  if (!info.exists) return null;
  const nome = uri.split('/').pop() ?? 'foto.jpg';
  const ext = (nome.split('.').pop() ?? 'jpg').toLowerCase();
  const r = await FileSystem.uploadAsync(url, uri, {
    httpMethod: 'POST',
    uploadType: FileSystem.FileSystemUploadType.MULTIPART,
    fieldName: campo,
    mimeType: ext === 'png' ? 'image/png' : 'image/jpeg',
    parameters: parametros,
    headers,
  });
  return { status: r.status, body: r.body };
}
