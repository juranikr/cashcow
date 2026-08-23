import { getChatGPTUser } from '@/app/chatgpt-auth';

export function isOwnerEmail(email: string | null | undefined) {
  if (process.env.NODE_ENV !== 'production') return true;
  const ownerEmail = process.env.OWNER_EMAIL?.trim().toLowerCase();
  return Boolean(email && ownerEmail && email.trim().toLowerCase() === ownerEmail);
}

export async function getActor() {
  const user = await getChatGPTUser();
  if (user) return { id: user.userId, displayName: user.displayName, email: user.email };
  if (process.env.NODE_ENV !== 'production') return { id: 'local-ceo', displayName: '대표님', email: 'local@cashcow.test' };
  return null;
}
