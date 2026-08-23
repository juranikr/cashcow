import { getChatGPTUser } from '@/app/chatgpt-auth';

export async function getActor() {
  const user = await getChatGPTUser();
  if (user) return { id: user.userId, displayName: user.displayName, email: user.email };
  if (process.env.NODE_ENV !== 'production') return { id: 'local-ceo', displayName: '대표님', email: 'local@cashcow.test' };
  return null;
}
