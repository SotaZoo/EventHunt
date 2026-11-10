import type { ReactNode } from 'react'

export const metadata = {
  title: 'Event Hunt',
  description: 'Discover events from multiple sources.',
}

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  )
}
