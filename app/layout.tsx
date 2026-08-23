import type { Metadata } from 'next';
import './globals.css';

export const metadata: Metadata = {
  metadataBase: new URL(process.env.SITE_ORIGIN ?? 'http://localhost:3000'),
  title: 'Cashcow HQ · 에이전트 오피스',
  description: '사람과 AI 에이전트가 같은 공간에서 협업하는 픽셀 오피스',
  openGraph: {
    title: 'CASHCOW HQ',
    description: '사람과 AI가 함께 일하는 픽셀 오피스',
    images: [{ url: '/og.png', width: 1200, height: 630, alt: 'Cashcow HQ 픽셀 에이전트 오피스' }],
  },
  twitter: {
    card: 'summary_large_image',
    title: 'CASHCOW HQ',
    description: '사람과 AI가 함께 일하는 픽셀 오피스',
    images: ['/og.png'],
  },
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="ko"><body>{children}</body></html>;
}
