/**
 * Which way a document's text runs. Right-to-left is said only where it is known: from the
 * language's script, or (better) from the page itself. Left-to-right is never forced from a
 * language tag: tags are often a source's default ("en" on a Hebrew text), and a `dir="ltr"` on such
 * a page would undo what the browser gets right from the characters (`dir="auto"`).
 */

/** Scripts written right to left. */
const RTL_SCRIPTS: ReadonlySet<string> = new Set([
  'Arab',
  'Hebr',
  'Syrc',
  'Thaa',
  'Nkoo',
  'Adlm',
  'Rohg',
  'Aran',
  'Armi',
  'Mand',
  'Samr',
]);

/** Languages written right to left, for a tag the browser cannot place (and the old codes iw, ji). */
const RTL_LANGUAGES: ReadonlySet<string> = new Set([
  'ar',
  'arz',
  'azb',
  'ckb',
  'dv',
  'fa',
  'glk',
  'he',
  'iw',
  'ji',
  'ks',
  'mzn',
  'nqo',
  'pnb',
  'ps',
  'sd',
  'syr',
  'ug',
  'ur',
  'yi',
]);

/**
 * `'rtl'` for a language tag whose script runs right to left (`he`, `ar`, `fa`, `ku-Arab`,
 * `pa-Arab`; not `ug-Cyrl` or `ku-Latn`), else undefined: never `'ltr'`.
 */
export function dirOf(lang: string | null | undefined): 'rtl' | undefined {
  const tag = (lang ?? '').trim().replace(/_/g, '-');
  if (!tag) return undefined;
  try {
    // The script the tag names, or the one the language is usually written in.
    const script = new Intl.Locale(tag).maximize().script;
    if (script) return RTL_SCRIPTS.has(script) ? 'rtl' : undefined;
  } catch {
    /* not a tag the browser can read: the list decides */
  }
  return RTL_LANGUAGES.has(tag.split('-')[0]!.toLowerCase()) ? 'rtl' : undefined;
}

/**
 * A language tag that says nothing about the text: none, or the default a source gives everything
 * it holds (`en` for document collections, empty for two ZIM sources: trawl 2026-11-10,
 * data-quality-08). Each paragraph of such a plain text takes its direction from its own characters.
 */
export function isDefaultLanguage(lang: string | null | undefined): boolean {
  const primary = (lang ?? '').trim().split(/[-_]/)[0]!.toLowerCase();
  return primary === '' || primary === 'en' || primary === 'und' || primary === 'mul';
}

/** A page's own `dir` attribute as a value to keep, or undefined. */
export function ownDir(value: string | null | undefined): 'rtl' | 'ltr' | 'auto' | undefined {
  const v = (value ?? '').trim().toLowerCase();
  return v === 'rtl' || v === 'ltr' || v === 'auto' ? v : undefined;
}
