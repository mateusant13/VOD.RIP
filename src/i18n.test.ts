import { describe, it, expect, afterEach } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import {
  DICTS,
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
    // Smoke test only. It cannot see a MISSING key: five hand-picked keys stay
    // translated no matter how much of the UI is left English. The real
    // key-set checks are the "locale parity" block below.
    for (const key of ['Language', 'Close', 'Retry search', 'Play', 'Clip length']) {
      setLanguage('pt-BR')
      const pt = t(key)
      setLanguage('es')
      const esv = t(key)
      expect(pt).not.toBe(key)
      expect(esv).not.toBe(key)
    }
  })

  it('every crash-boundary string is translated in pt-BR and es', () => {
    // The crash panel is the ONLY thing the user sees when the app dies, so a
    // missing key here means the one diagnostic that exists renders English to
    // a Brazilian or Spanish user - or, for a key missing everywhere, an empty
    // label. Asserted non-identity as well as present.
    const keys = [
      'crash.title',
      'crash.body',
      'crash.reload',
      'crash.dismiss',
      'crash.errorLabel',
      'crash.componentStackLabel',
      'crash.noComponentStack',
      'crash.previousLabel',
      'crash.persistedNote',
      'crash.unknownError',
    ];
    for (const key of keys) {
      expect(DICTS.en[key], `en is missing "${key}"`).toBeTruthy();
      for (const lang of ['pt-BR', 'es'] as const) {
        expect(DICTS[lang][key], `${lang} is missing "${key}"`).toBeTruthy();
        expect(DICTS[lang][key], `${lang} left "${key}" in English`).not.toBe(key);
      }
    }
  });

  it('the Shorts/Clips filter labels are translated in pt-BR and es', () => {
    // The new filter reuses existing keys, but only because they were already
    // translated; a rename would silently drop one locale back to English.
    for (const key of ['Shorts', 'Clips', 'No shorts', 'No clips']) {
      for (const lang of ['pt-BR', 'es'] as const) {
        expect(DICTS[lang][key], `${lang} is missing "${key}"`).toBeTruthy();
      }
    }
  });

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

/**
 * Locale parity — the check that can actually catch an untranslated surface.
 *
 * Why this block exists: the `en` dictionary is IMPLICIT. Flat keys *are* the
 * English source strings, so a flat key missing from pt-BR/es does not show up
 * as a key-set difference between `en` and the others — `en` simply has no
 * entry to be missing. Comparing dictionaries can therefore never catch the
 * defect that actually happens in this repo: a component calls t('...') and
 * nobody adds the string to pt-BR/es, so a Brazilian or Spanish user silently
 * reads English. The only way to see it is to read the call sites, which is
 * what PARITY_COVERED_SURFACES below is for.
 */

/** Surfaces whose every t() literal must exist in BOTH pt-BR and es.
 *  Extend this when a surface is localized. As of this writing a repo-wide
 *  scan of src/ finds further untranslated literals in App.tsx,
 *  ChannelExplorePopup.tsx, LivePlayerPopup.tsx, NotificationsPanel.tsx,
 *  PreviewMiniEditor.tsx and TwitchClipPopup.tsx — those belong to other
 *  owners and are tracked as a backlog, not hidden behind an allowlist here. */
const PARITY_COVERED_SURFACES = [
  'components/RateGovernorSection.tsx',
  'components/CookieBridgeSection.tsx',
  'components/ErrorBoundary.tsx',
]

/** Every t('...') / t("...") literal in a source file, plus the total number of
 *  standalone `t(` call sites seen.
 *
 *  `t(` is only matched as a standalone call — the lookbehind rejects a
 *  preceding identifier character, `.` or `$`, so `i18n.t(...)` and `useT(...)`
 *  are not false positives. This repo uses no `t(\`...\`)` template-literal
 *  form and no qualified `i18n.t(` call, so a plain-literal scan is complete
 *  here; if either appears, the caller count no longer equals the literal
 *  count and the test below says so out loud instead of quietly
 *  under-reporting a surface as fully translated. */
function tLiterals(relPath: string): { literals: string[]; callSites: number } {
  const text = readFileSync(fileURLToPath(new URL(relPath, import.meta.url)), 'utf8')
  const callSites = [...text.matchAll(/(?<![A-Za-z0-9_$.])t\(/g)].length
  const re = /(?<![A-Za-z0-9_$.])t\(\s*(['"])((?:\\.|(?!\1)[^\\])*)\1/g
  const literals = [...text.matchAll(re)].map((m) => m[2].replace(/\\(['"])/g, '$1'))
  return { literals, callSites }
}

describe('locale parity', () => {
  it('pt-BR and es carry exactly the same key set', () => {
    const pt = new Set(Object.keys(DICTS['pt-BR']))
    const es = new Set(Object.keys(DICTS.es))
    expect([...pt].filter((k) => !es.has(k))).toEqual([])
    expect([...es].filter((k) => !pt.has(k))).toEqual([])
  })

  it('every namespaced en key exists in pt-BR and es', () => {
    // Namespaced keys (botGate.*, cookieAuto.*, cookieBridge.*, captionsPark.*,
    // progress.*) are the only ones with a real `en` entry, so they are the only
    // ones a dictionary-vs-dictionary check can see.
    for (const key of Object.keys(DICTS.en)) {
      expect(DICTS['pt-BR'][key], `pt-BR is missing "${key}"`).toBeTruthy()
      expect(DICTS.es[key], `es is missing "${key}"`).toBeTruthy()
    }
  })

  for (const relPath of PARITY_COVERED_SURFACES) {
    it(`every t() literal in ${relPath} is translated in pt-BR and es`, () => {
      const { literals, callSites } = tLiterals(relPath)
      expect(literals.length).toBeGreaterThan(0)
      // Guards the guard: if the scanner ever stops reading a call site (a
      // t(variable) or a template literal), the surface would look translated
      // while silently rendering English. Every call site must be a literal we
      // actually read, so that case fails here instead.
      expect(
        callSites,
        `${relPath} has a t() call whose argument is not a plain string literal the scanner can read`,
      ).toBe(literals.length)
      for (const key of literals) {
        expect(DICTS['pt-BR'][key], `pt-BR is missing "${key}" (${relPath})`).toBeTruthy()
        expect(DICTS.es[key], `es is missing "${key}" (${relPath})`).toBeTruthy()
      }
    })
  }

  it('the rate governor reserve invariant is translated, not passed through in English', () => {
    // The USER pool is the owner's reserve: it gets the whole ceiling and AUTO
    // can only ever spend its own share. A translation that read as "the
    // automatic pool may also raise/claim this" would be a functional
    // regression, so these two strings are asserted present AND non-identity —
    // an entry added as a copy of the key would render English and fail here.
    const reserve =
      'Gets all {rpm} req/min. Background work can never draw from this reserve.'
    const share =
      'May use {pct}% of the ceiling — never more, so a background storm cannot spend your share.'
    for (const lang of ['pt-BR', 'es'] as const) {
      for (const key of [reserve, share]) {
        expect(DICTS[lang][key], `${lang} is missing "${key}"`).toBeTruthy()
        expect(DICTS[lang][key], `${lang} left "${key}" in English`).not.toBe(key)
      }
    }
  })

  it('the age-gate capture recipe is translated in pt-BR and es', () => {
    // This is the instruction the owner follows to release a parked video, so
    // the steps that keep the exported session valid must all be present: the
    // private window, the immediate close, and keeping the bridge enabled.
    const recipe = [
      'Age-restricted video? Capture the YouTube session so it lasts',
      'Open a private/incognito window and sign in to YouTube. Use a throwaway account if you can — a personal account risks a YouTube ban.',
      'In that same window, go to https://www.youtube.com/robots.txt',
      'Export the youtube.com cookies from this window, then close the window immediately — a closed window is never rotated.',
      'Keep the Cookie Bridge enabled so the session reaches this app. Do NOT point an exporter at a normal tab: those cookies rotate within hours.',
      'Then retry the failed job — it picks up on its own.',
    ]
    for (const lang of ['pt-BR', 'es'] as const) {
      for (const key of recipe) {
        expect(DICTS[lang][key], `${lang} is missing "${key.slice(0, 48)}…"`).toBeTruthy()
      }
    }
  })
})
