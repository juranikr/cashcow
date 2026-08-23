export const BOARD_SIZE = 256;
export const BOARD_PIXELS = BOARD_SIZE * BOARD_SIZE;
export const BOARD_PALETTE = ['#f9f4df', '#ef7657', '#e4b34f', '#55a47c', '#4d8fb8', '#745f9d', '#26394f'] as const;

export type BoardTextBlock = {
  id: string;
  x: number;
  y: number;
  width: number;
  height: number;
  text: string;
  color: string;
  background: string;
  fontSize: number;
  authorType?: 'user' | 'agent' | 'system';
  authorId?: string;
  authorName?: string;
  createdAt?: string;
  updatedAt?: string;
  lastEditorId?: string;
  lastEditorName?: string;
};

export type BoardRevisionMeta = {
  version: number;
  authorType: 'user' | 'agent' | 'system';
  authorId: string;
  authorName: string;
  changeSummary: string;
  createdAt: string;
  contentAvailable?: boolean;
};

export type WhiteboardDocument = {
  title: string;
  width: 256;
  height: 256;
  palette: string[];
  pixelsBase64: string;
  textBlocks: BoardTextBlock[];
  version: number;
  authorType: 'user' | 'agent' | 'system';
  authorId: string;
  authorName: string;
  updatedAt: string;
  history: BoardRevisionMeta[];
};

export function createBlankBoardPixels(fill = 0): Uint8Array {
  return new Uint8Array(BOARD_PIXELS).fill(fill);
}

export function bytesToBase64(bytes: Uint8Array): string {
  let binary = '';
  const chunkSize = 0x8000;
  for (let offset = 0; offset < bytes.length; offset += chunkSize) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + chunkSize));
  }
  return btoa(binary);
}

export function base64ToBytes(value: string): Uint8Array {
  const binary = atob(value);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
  return bytes;
}

export function isValidBoardPixels(value: string, paletteLength: number): boolean {
  try {
    const bytes = base64ToBytes(value);
    return bytes.length === BOARD_PIXELS && bytes.every((pixel) => pixel < paletteLength);
  } catch {
    return false;
  }
}

export function linePoints(from: { x: number; y: number }, to: { x: number; y: number }): Array<{ x: number; y: number }> {
  const points: Array<{ x: number; y: number }> = [];
  let x0 = Math.round(from.x);
  let y0 = Math.round(from.y);
  const x1 = Math.round(to.x);
  const y1 = Math.round(to.y);
  const dx = Math.abs(x1 - x0);
  const sx = x0 < x1 ? 1 : -1;
  const dy = -Math.abs(y1 - y0);
  const sy = y0 < y1 ? 1 : -1;
  let error = dx + dy;
  while (true) {
    points.push({ x: x0, y: y0 });
    if (x0 === x1 && y0 === y1) break;
    const twice = 2 * error;
    if (twice >= dy) { error += dy; x0 += sx; }
    if (twice <= dx) { error += dx; y0 += sy; }
  }
  return points;
}
