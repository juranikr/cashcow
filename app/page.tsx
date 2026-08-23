import OfficeClient from './office-client';
import { getChatGPTUser } from './chatgpt-auth';

export const dynamic = 'force-dynamic';

export default async function Home() {
  const user = await getChatGPTUser();
  return <OfficeClient viewer={user ? { displayName: user.displayName, email: user.email } : null} initialNow={new Date().toISOString()} />;
}
