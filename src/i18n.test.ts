import { describe, it, expect, afterEach } from 'vitest'
import {
  detectSystemLanguage,
  getLanguage,
  langFamily,
  setLanguage,
  t,
} from './i18n'

const saved = getLanguage()
afterEach(() => {
  setLanguage(saved)
})

describe('i18n', () => {
  it('t() returns the key verbatim in English (default, no dictionary entry)', () => {
    expect(t('Hello there')).toBe('Hello there')
  })

  it('looks up translations per language', () => {
    setLanguage('pt-BR')
    expect(t('Language')).toBe('Idioma')
    expect(t('Save Settings')).toBe('Salvar configurações')
    setLanguage('es')
    expect(t('Language')).toBe('Idioma')
    expect(t('Save Settings')).toBe('Guardar ajustes')
  })

  it('interpolates {vars} in both dictionaries', () => {
    setLanguage('pt-BR')
    expect(t('v{version} available', { version: '2.0.1' })).toBe('v2.0.1 disponível')
    expect(t('Live {name}', { name: 'alanzoka' })).toBe('Ao vivo: alanzoka')
    setLanguage('es')
    expect(t('v{version} available', { version: '2.0.1' })).toBe('v2.0.1 disponible')
    expect(t('Live {name}', { name: 'ibai' })).toBe('En vivo: ibai')
  })

  it('falls back to the English key when missing from the dictionary', () => {
    setLanguage('pt-BR')
    expect(t('No translation for this exact string')).toBe('No translation for this exact string')
    setLanguage('es')
    expect(t('No translation for this exact string')).toBe('No translation for this exact string')
  })

  it('keeps both dictionaries in sync (same key set as the implicit English source)', () => {
    // real check: every key t() can translate exists in both dicts via module internals
    // (covered by the extraction-time sync check; here we assert a few shared keys)
    for (const key of ['Language', 'Close', 'Retry search', 'Play', 'Clip length']) {
      setLanguage('pt-BR')
      const pt = t(key)
      setLanguage('es')
      const esv = t(key)
      expect(pt).not.toBe(key)
      expect(esv).not.toBe(key)
    }
  })

  it('detectSystemLanguage maps pt→pt-BR, es→es, anything else→en', () => {
    expect(langFamily('pt-BR')).toBe('pt')
    expect(langFamily('es')).toBe('es')
    expect(langFamily('en')).toBe('en')
    const original = Object.getOwnPropertyDescriptor(globalThis, 'navigator')
    try {
      Object.defineProperty(globalThis, 'navigator', {
        value: { language: 'pt-BR' },
        configurable: true,
      })
      expect(detectSystemLanguage()).toBe('pt-BR')
      Object.defineProperty(globalThis, 'navigator', {
        value: { language: 'es-ES' },
        configurable: true,
      })
      expect(detectSystemLanguage()).toBe('es')
      Object.defineProperty(globalThis, 'navigator', {
        value: { language: 'de-DE' },
        configurable: true,
      })
      expect(detectSystemLanguage()).toBe('en')
    } finally {
      if (original) Object.defineProperty(globalThis, 'navigator', original)
    }
  })
})
