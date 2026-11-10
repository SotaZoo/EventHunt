import { render, screen } from '@testing-library/react'
import Page from '../app/page'
import { metadata } from '../app/layout'

describe('home page', () => {
  it('renders the product surface', () => {
    render(<Page />)
    expect(screen.getByRole('heading', { name: /event hunt/i })).toBeInTheDocument()
  })

  it('uses the product name in browser metadata', () => {
    expect(metadata.title).toBe('Event Hunt')
  })
})
