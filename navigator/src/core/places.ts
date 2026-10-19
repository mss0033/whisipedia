/**
 * The library's own places: rooms the Navigator draws itself rather than sites from the catalogue. The
 * Atlas reads the map sources, which are tiles and not documents, so it never appeared among the
 * sites; the owner had to type "maps" into the location bar to find it (2026-10-18). Every list of
 * where to go (the Go menu, the start page, the directory, the directory buttons) takes them from here.
 */
import { makeAddress } from './address.ts';

export interface Place {
  /** Short name for a menu. */
  label: string;
  /** What it is, for the start page and the directory. */
  blurb: string;
  address: string;
}

export const PLACES: readonly Place[] = [
  {
    label: 'Atlas',
    blurb: 'Maps of the whole world, a 3-D globe and terrain, and place search.',
    address: makeAddress('atlas'),
  },
  {
    label: 'Screening Room',
    blurb: 'Films, recordings and audiobooks in the library.',
    address: makeAddress('screening-room'),
  },
  {
    label: 'Exam Practice Hall',
    blurb: 'Question pools to drill: amateur radio and more.',
    address: makeAddress('practice'),
  },
  {
    label: 'Dictionary',
    blurb: 'Look up a word in every dictionary the library holds.',
    address: makeAddress('define'),
  },
];
