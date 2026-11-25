import { lerBlob } from './fotoLocal';

export type RespostaUpload = { status: number; body: string };

export async function postarArquivo(
  url: string, uri: string, campo: string, parametros: Record<string, string>, headers: Record<string, string>,
): Promise<RespostaUpload | null> {
  const blob = await lerBlob(uri);
  if (!blob) return null;
  const form = new FormData();
  for (const [k, v] of Object.entries(parametros)) form.append(k, v);
  form.append(campo, blob, blob.type === 'image/png' ? 'foto.png' : 'foto.jpg');
  const r = await fetch(url, { method: 'POST', body: form, headers });
  return { status: r.status, body: await r.text() };
}
