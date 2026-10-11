/**
 * What to search for and open on each target. The mock fixtures live in
 * mock/data.ts; the live values are pages a full library is expected to hold.
 */
import type { Target } from './navigator.ts';

export interface TargetData {
  /** A plain-language query with more than ten hits and at least two kinds. */
  question: string;
  /** A query whose results include `result`, an HTML document with internal links. */
  docQuery: string;
  result: RegExp;
  /** Address of that document. */
  docAddress: RegExp;
  /** A document that can be bookmarked (title matches `docTitle`). */
  doc: string;
  docTitle: RegExp;
  /** A second document, for history. */
  doc2: string;
  doc2Title: RegExp;
  /** Text that yields search-as-you-type suggestions. */
  suggest: string;
  /** A misspelled query that earns a "Did you mean" line. */
  misspelled: string;
}

export const DATA: Record<Target, TargetData> = {
  mock: {
    question: 'how do vaccines train the immune system',
    docQuery: 'alan turing',
    result: /^Alan Turing$/,
    docAddress: /^wsp:\/\/wikipedia-en-zim\/Alan_Turing$/,
    doc: 'wsp://rfc/9110',
    docTitle: /RFC 9110/,
    doc2: 'wsp://gutenberg/84',
    doc2Title: /Frankenstein/,
    suggest: 'turing',
    misspelled: 'vacine immune',
  },
  live: {
    question: 'how do vaccines train the immune system',
    docQuery: 'immune system',
    result: /^Immune system$/,
    docAddress: /^wsp:\/\/mdwiki\/Immune_system$/,
    doc: 'wsp://wikipedia-en-zim/Alan_Turing',
    docTitle: /Alan Turing/,
    doc2: 'wsp://wikipedia-en-zim/Turing_machine',
    doc2Title: /Turing machine/,
    suggest: 'turing',
    misspelled: 'diabetis insulen',
  },
};
