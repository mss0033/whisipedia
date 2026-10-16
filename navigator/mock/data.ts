/**
 * Fixture library for the mock API. All prose is written for this project
 * (or is public-domain text) so the fixtures are license-clean. The handler
 * (mock/handler.ts) turns these into responses typed by the generated spec.
 */
import type { DocKind, DocumentLink, OriginalFile, SourceHealth } from '../src/api/types.ts';

/** Generation ids are the sealing date (PLAN §7). */
export const GENERATION = '2026-10-01';

/** Registry facts plus what the vault and catalog would report. */
export interface FixtureSource {
  id: string;
  name: string;
  publisher: string;
  kind: DocKind;
  domain: string[];
  license: string;
  tier: 'T1' | 'T2' | 'T3';
  description: string;
  doc_count: number;
  bytes: number;
  as_of: string;
  health: SourceHealth;
  home_doc_id?: string;
  related: string[];
}

export interface FixtureVersion {
  revision: string;
  timestamp: string;
  html?: string;
  text?: string;
}

export interface FixtureDoc {
  source_id: string;
  native_id: string;
  title: string;
  kind: DocKind;
  lang: string;
  license: string;
  url_native: string;
  format: 'html' | 'text';
  /** Newest first; [0] is current. */
  versions: FixtureVersion[];
  links: DocumentLink[];
  entities: string[];
  /** Extra search keywords (aliases). */
  aliases?: string[];
  /** Edition status and reader warning (document collections, PLAN D13). */
  currency?: string;
  banner?: string;
  /** The stored file a collection document was refined from. */
  original?: OriginalFile;
}

const T = (d: string) => `${d}T00:00:00Z`;

export const SOURCES: FixtureSource[] = [
  {
    id: 'wikipedia-en-zim',
    name: 'Wikipedia (English)',
    publisher: 'Wikimedia Foundation',
    kind: 'encyclopedia',
    domain: ['encyclopedia'],
    license: 'CC-BY-SA-4.0',
    tier: 'T1',
    description: 'The free encyclopedia: millions of articles, mirrored monthly.',
    doc_count: 6_912_404,
    bytes: 127.4e9,
    as_of: T('2026-10-01'),
    health: 'ok',
    home_doc_id: 'wsp:wikipedia-en-zim/Main_Page',
    related: ['wiktionary-en', 'wikidata', 'wikisource-en'],
  },
  {
    id: 'wiktionary-en',
    name: 'Wiktionary (English)',
    publisher: 'Wikimedia Foundation',
    kind: 'dictionary',
    domain: ['dictionary'],
    license: 'CC-BY-SA-4.0',
    tier: 'T1',
    description: 'A free dictionary of words in every language, defined in English.',
    doc_count: 8_421_337,
    bytes: 1.6e9,
    as_of: T('2026-10-01'),
    health: 'ok',
    related: ['wikipedia-en-zim', 'wikidata'],
  },
  {
    id: 'wikidata',
    name: 'Wikidata',
    publisher: 'Wikimedia Foundation',
    kind: 'data',
    domain: ['knowledge-graph'],
    license: 'CC0-1.0',
    tier: 'T1',
    description: 'Structured facts about 110 million things, the glue between all sites.',
    doc_count: 112_004_561,
    bytes: 103.3e9,
    as_of: T('2026-09-28'),
    health: 'ok',
    related: ['wikipedia-en-zim', 'osm-planet'],
  },
  {
    id: 'wikisource-en',
    name: 'Wikisource (English)',
    publisher: 'Wikimedia Foundation',
    kind: 'books',
    domain: ['books', 'primary-sources'],
    license: 'CC-BY-SA-4.0',
    tier: 'T1',
    description: 'Transcribed primary sources: constitutions, speeches, letters.',
    doc_count: 1_003_225,
    bytes: 3.4e9,
    as_of: T('2026-10-01'),
    health: 'ok',
    related: ['gutenberg', 'wikipedia-en-zim'],
  },
  {
    id: 'gutenberg',
    name: 'Project Gutenberg',
    publisher: 'Project Gutenberg Literary Archive Foundation',
    kind: 'books',
    domain: ['books'],
    license: 'Public domain (US)',
    tier: 'T1',
    description: 'Over 75,000 free eBooks: the great literature of the public domain.',
    doc_count: 76_102,
    bytes: 71.9e9,
    as_of: T('2026-09-30'),
    health: 'ok',
    related: ['wikisource-en', 'standard-ebooks'],
  },
  {
    id: 'standard-ebooks',
    name: 'Standard Ebooks',
    publisher: 'Standard Ebooks',
    kind: 'books',
    domain: ['books'],
    license: 'CC0-1.0',
    tier: 'T1',
    description: 'Carefully produced, typographically excellent public-domain ebooks.',
    doc_count: 1_204,
    bytes: 2.1e9,
    as_of: T('2026-09-15'),
    health: 'stale',
    related: ['gutenberg'],
  },
  {
    id: 'open-library',
    name: 'Open Library',
    publisher: 'Internet Archive',
    kind: 'data',
    domain: ['catalog'],
    license: 'Public domain (catalog data)',
    tier: 'T2',
    description: 'Catalog records of books: works, editions and authors.',
    doc_count: 3,
    bytes: 0.2e9,
    as_of: T('2026-10-10'),
    health: 'ok',
    related: ['gutenberg'],
  },
  {
    id: 'prelinger-archives',
    name: 'Prelinger Archives',
    publisher: 'Internet Archive',
    kind: 'video',
    domain: ['film'],
    license: 'Public domain',
    tier: 'T2',
    description: 'Ephemeral, industrial and educational films, 1903 to the 1980s.',
    doc_count: 1,
    bytes: 0.4e9,
    as_of: T('2026-10-01'),
    health: 'ok',
    related: ['rfc'],
  },
  {
    id: 'librivox',
    name: 'LibriVox',
    publisher: 'LibriVox volunteers',
    kind: 'audio',
    domain: ['audiobooks'],
    license: 'Public domain',
    tier: 'T2',
    description: 'Public-domain books read aloud by volunteers.',
    doc_count: 1,
    bytes: 0.1e9,
    as_of: T('2026-10-01'),
    health: 'ok',
    related: ['gutenberg'],
  },
  {
    id: 'ncvec-question-pools',
    name: 'NCVEC amateur radio question pools',
    publisher: 'National Conference of Volunteer Examiner Coordinators',
    kind: 'other',
    domain: ['howto'],
    license: 'Public domain',
    tier: 'T2',
    description: 'The question pools every US amateur radio licence exam is drawn from.',
    doc_count: 7,
    bytes: 2.1e6,
    as_of: T('2026-02-19'),
    health: 'ok',
    related: ['rfc'],
  },
  {
    id: 'rfc',
    name: 'IETF RFC Editor',
    publisher: 'IETF / RFC Editor',
    kind: 'papers',
    domain: ['standards'],
    license: 'IETF Trust',
    tier: 'T1',
    description: 'Every Request for Comments: the documents that define the Internet.',
    doc_count: 9_812,
    bytes: 0.6e9,
    as_of: T('2026-10-07'),
    health: 'ok',
    related: ['stackexchange', 'wikipedia-en-zim'],
  },
  {
    id: 'arxiv',
    name: 'arXiv (metadata + OA PDFs)',
    publisher: 'Cornell University',
    kind: 'papers',
    domain: ['papers'],
    license: 'Various (per paper)',
    tier: 'T1',
    description: 'Preprints in physics, mathematics, computer science and more.',
    doc_count: 2_590_118,
    bytes: 1.9e12,
    as_of: T('2026-10-01'),
    health: 'ok',
    related: ['rfc', 'openstax'],
  },
  {
    id: 'stackexchange',
    name: 'Stack Exchange',
    publisher: 'Stack Exchange, Inc.',
    kind: 'qa',
    domain: ['q-and-a'],
    license: 'CC-BY-SA-4.0',
    tier: 'T1',
    description: 'Questions and answers from 180 communities of experts.',
    doc_count: 61_337_902,
    bytes: 98.2e9,
    as_of: T('2026-09-30'),
    health: 'ok',
    related: ['rfc', 'wikipedia-en-zim'],
  },
  {
    id: 'kiwix-xkcd-com-en-all',
    name: 'xkcd (Kiwix ZIM)',
    publisher: 'Randall Munroe',
    kind: 'other',
    domain: ['comics'],
    license: 'CC-BY-NC-2.5',
    tier: 'T2',
    description: 'A webcomic of romance, sarcasm, math, and language.',
    doc_count: 3_000,
    bytes: 451e6,
    as_of: T('2026-08-07'),
    health: 'ok',
    related: [],
  },
  {
    id: 'kiwix-www-mankier-com-en-all',
    name: 'ManKier man pages (Kiwix ZIM)',
    publisher: 'ManKier',
    kind: 'other',
    domain: ['computing'],
    license: 'LicenseRef-per-item',
    tier: 'T2',
    description: 'Linux manual pages with examples.',
    doc_count: 60_000,
    bytes: 1.2e9,
    as_of: T('2026-07-01'),
    health: 'ok',
    related: [],
  },
  {
    id: 'openstax',
    name: 'OpenStax Textbooks',
    publisher: 'Rice University',
    kind: 'books',
    domain: ['textbooks'],
    license: 'CC-BY-4.0',
    tier: 'T1',
    description: 'Peer-reviewed, openly licensed college textbooks.',
    doc_count: 72,
    bytes: 4.2e9,
    as_of: T('2026-09-01'),
    health: 'ok',
    related: ['medlineplus', 'arxiv'],
  },
  {
    id: 'medlineplus',
    name: 'MedlinePlus',
    publisher: 'U.S. National Library of Medicine',
    kind: 'medicine',
    domain: ['medical'],
    license: 'Public domain (US Gov)',
    tier: 'T1',
    description: 'Trusted health information in plain language.',
    doc_count: 4_310,
    bytes: 0.4e9,
    as_of: T('2026-10-02'),
    health: 'ok',
    related: ['openstax', 'wikipedia-en-zim'],
  },
  {
    id: 'osm-planet',
    name: 'OpenStreetMap',
    publisher: 'OpenStreetMap Foundation',
    kind: 'maps',
    domain: ['maps'],
    license: 'ODbL-1.0',
    tier: 'T1',
    description: 'The free map of the world, with local vector tiles.',
    doc_count: 10_204_118_330,
    bytes: 86.2e9,
    as_of: T('2026-10-06'),
    health: 'ok',
    related: ['wikidata', 'wikipedia-en-zim'],
  },
  {
    id: 'us-code',
    name: 'United States Code',
    publisher: 'Office of the Law Revision Counsel',
    kind: 'law',
    domain: ['law'],
    license: 'Public domain (US Gov)',
    tier: 'T1',
    description: 'The codification of the general and permanent laws of the United States.',
    doc_count: 54,
    bytes: 0.9e9,
    as_of: T('2026-08-20'),
    health: 'degraded',
    related: ['wikisource-en'],
  },
];

const turingV3 = `
<p><b>Alan Mathison Turing</b> (1912&ndash;1954) was an English mathematician, computer scientist, logician and cryptanalyst. He is widely regarded as a founder of theoretical computer science and artificial intelligence.</p>
<p>In 1936 Turing described an abstract device, now called the <a href="./Turing_machine">Turing machine</a>, which formalised the notions of algorithm and computation. During the Second World War he worked at <a href="./Bletchley_Park">Bletchley Park</a> on breaking the German <a href="./Enigma_machine">Enigma machine</a> ciphers.</p>
<h2 id="Legacy">Legacy</h2>
<p>The structured record for Turing is held in <a href="wsp:wikidata/Q7251">Wikidata (Q7251)</a>. The essay that introduced the imitation game remains widely read; see the <a href="https://www.turing.org.uk/">Turing Digital Archive</a> (not held locally).</p>
<p>Modern networked computers speak protocols such as <a href="./Hypertext_Transfer_Protocol">HTTP</a>, the descendants of ideas Turing helped make possible.</p>
<script>alert('this must never run')</script>
<p class="noprint" onclick="steal()">Fixture: this paragraph had an onclick handler, removed by the sanitizer (the API already drops inline styles; tests/links.test.ts covers the sanitizer dropping them too).</p>
<img src="https://upload.example.org/turing.jpg" alt="Portrait of Alan Turing">
`;

const turingV2 = `
<p><b>Alan Mathison Turing</b> (1912&ndash;1954) was an English mathematician, computer scientist, logician and cryptanalyst.</p>
<p>In 1936 Turing described an abstract device, now called the <a href="./Turing_machine">Turing machine</a>. During the Second World War he worked at <a href="./Bletchley_Park">Bletchley Park</a> on the <a href="./Enigma_machine">Enigma machine</a>.</p>
<h2 id="Legacy">Legacy</h2>
<p>See <a href="wsp:wikidata/Q7251">Wikidata (Q7251)</a>.</p>
`;

const turingV1 = `
<p><b>Alan Turing</b> (1912&ndash;1954) was an English mathematician.</p>
<p>He described the <a href="./Turing_machine">Turing machine</a> in 1936.</p>
`;

export const DOCS: FixtureDoc[] = [
  {
    source_id: 'wikipedia-en-zim',
    native_id: 'Main_Page',
    title: 'Wikipedia, the free encyclopedia',
    kind: 'encyclopedia',
    lang: 'en',
    license: 'CC-BY-SA-4.0',
    url_native: 'https://en.wikipedia.org/wiki/Main_Page',
    format: 'html',
    versions: [
      {
        revision: '1250000001',
        timestamp: T('2026-10-01'),
        html: `<p>Welcome to the offline copy of <b>Wikipedia</b>, the free encyclopedia that anyone can edit.</p>
<h2>Selected articles</h2>
<ul><li><a href="./Alan_Turing">Alan Turing</a></li><li><a href="./Vaccine">Vaccine</a></li><li><a href="./Immune_system">Immune system</a></li><li><a href="./Lisbon">Lisbon</a></li><li><a href="./Hypertext_Transfer_Protocol">Hypertext Transfer Protocol</a></li><li><a href="./Mary_Shelley">Mary Shelley</a></li><li><a href="./World_Wide_Web">World Wide Web</a></li></ul>`,
      },
    ],
    links: [],
    entities: [],
  },
  {
    source_id: 'wikipedia-en-zim',
    native_id: 'Alan_Turing',
    title: 'Alan Turing',
    kind: 'encyclopedia',
    lang: 'en',
    license: 'CC-BY-SA-4.0',
    url_native: 'https://en.wikipedia.org/wiki/Alan_Turing',
    format: 'html',
    versions: [
      { revision: '1249900120', timestamp: T('2026-09-27'), html: turingV3 },
      { revision: '1231000044', timestamp: T('2026-06-02'), html: turingV2 },
      { revision: '1201234567', timestamp: T('2025-12-30'), html: turingV1 },
    ],
    links: [
      { href: './Bletchley_Park', target: null },
      { href: 'https://www.turing.org.uk/', target: null },
    ],
    entities: ['Q7251'],
    aliases: ['turing', 'father of computer science', 'enigma codebreaker'],
  },
  {
    source_id: 'wikipedia-en-zim',
    native_id: 'Turing_machine',
    title: 'Turing machine',
    kind: 'encyclopedia',
    lang: 'en',
    license: 'CC-BY-SA-4.0',
    url_native: 'https://en.wikipedia.org/wiki/Turing_machine',
    format: 'html',
    versions: [
      {
        revision: '1248000001',
        timestamp: T('2026-09-12'),
        html: `<p>A <b>Turing machine</b> is a mathematical model of computation describing an abstract machine that manipulates symbols on a strip of tape according to a table of rules. It was introduced by <a href="./Alan_Turing">Alan Turing</a> in 1936.</p>
<p>Despite its simplicity, the model can implement any computer algorithm, which makes it the standard yardstick for what is computable.</p>`,
      },
    ],
    links: [],
    entities: ['Q163310', 'Q7251'],
  },
  {
    source_id: 'wikipedia-en-zim',
    native_id: 'Enigma_machine',
    title: 'Enigma machine',
    kind: 'encyclopedia',
    lang: 'en',
    license: 'CC-BY-SA-4.0',
    url_native: 'https://en.wikipedia.org/wiki/Enigma_machine',
    format: 'html',
    versions: [
      {
        revision: '1247000001',
        timestamp: T('2026-09-03'),
        html: `<p>The <b>Enigma machine</b> is a cipher device used in the early-to-mid 20th century, notably by German military communications during the Second World War. Its rotors scrambled each keypress into a different letter.</p>
<p>Polish and later British cryptanalysts, including <a href="./Alan_Turing">Alan Turing</a>, developed methods and machines to read its traffic.</p>`,
      },
    ],
    links: [],
    entities: ['Q48244'],
  },
  {
    source_id: 'wikipedia-en-zim',
    native_id: 'Vaccine',
    title: 'Vaccine',
    kind: 'encyclopedia',
    lang: 'en',
    license: 'CC-BY-SA-4.0',
    url_native: 'https://en.wikipedia.org/wiki/Vaccine',
    format: 'html',
    versions: [
      {
        revision: '1249800000',
        timestamp: T('2026-09-25'),
        html: `<p>A <b>vaccine</b> is a biological preparation that trains the <a href="./Immune_system">immune system</a> to recognise a pathogen without causing the disease. It typically contains a weakened or inactivated form of the microbe, one of its surface proteins, or genetic instructions for making such a protein.</p>
<h2 id="How_vaccines_work">How vaccines work</h2>
<p>After vaccination, antigen-presenting cells show fragments of the antigen to T cells, and B cells begin producing antibodies. Some of these cells become long-lived <i>memory cells</i>, so a later encounter with the real pathogen triggers a fast, strong response.</p>
<p>For a plain-language overview see the <a href="wsp:medlineplus/vaccines">MedlinePlus page on vaccines</a>; for the textbook treatment see <a href="wsp:openstax/microbiology-18-1">OpenStax Microbiology &sect;18.1</a>.</p>`,
      },
    ],
    links: [],
    entities: ['Q134808'],
    aliases: ['vaccines', 'vaccination', 'immunization'],
  },
  {
    source_id: 'wikipedia-en-zim',
    native_id: 'Immune_system',
    title: 'Immune system',
    kind: 'encyclopedia',
    lang: 'en',
    license: 'CC-BY-SA-4.0',
    url_native: 'https://en.wikipedia.org/wiki/Immune_system',
    format: 'html',
    versions: [
      {
        revision: '1249700000',
        timestamp: T('2026-09-20'),
        html: `<p>The <b>immune system</b> is a network of biological systems that protects an organism from disease. It has two broad layers: the <i>innate</i> immune system, which responds quickly and generically, and the <i>adaptive</i> immune system, which learns to recognise specific pathogens and remembers them.</p>
<p>Adaptive immunity is what <a href="./Vaccine">vaccines</a> exploit: they let the body form memory B and T cells safely.</p>`,
      },
    ],
    links: [],
    entities: ['Q1059'],
  },
  {
    source_id: 'wikipedia-en-zim',
    native_id: 'Lisbon',
    title: 'Lisbon',
    kind: 'encyclopedia',
    lang: 'en',
    license: 'CC-BY-SA-4.0',
    url_native: 'https://en.wikipedia.org/wiki/Lisbon',
    format: 'html',
    versions: [
      {
        revision: '1249600000',
        timestamp: T('2026-09-18'),
        html: `<p><b>Lisbon</b> is the capital and largest city of Portugal, on the estuary of the Tagus river. It is one of the oldest cities in Western Europe.</p>
<p>The city was largely rebuilt after the great earthquake of 1755. See the <a href="wsp:osm-planet/relation/5400890">local map of Lisbon</a>.</p>`,
      },
    ],
    links: [],
    entities: ['Q597'],
    aliases: ['lisboa', 'capital of portugal'],
  },
  {
    source_id: 'wikipedia-en-zim',
    native_id: 'Hypertext_Transfer_Protocol',
    title: 'Hypertext Transfer Protocol',
    kind: 'encyclopedia',
    lang: 'en',
    license: 'CC-BY-SA-4.0',
    url_native: 'https://en.wikipedia.org/wiki/HTTP',
    format: 'html',
    versions: [
      {
        revision: '1249500000',
        timestamp: T('2026-09-16'),
        html: `<p>The <b>Hypertext Transfer Protocol</b> (<b>HTTP</b>) is an application-layer protocol for distributed hypermedia information systems and the foundation of data communication on the <a href="./World_Wide_Web">World Wide Web</a>.</p>
<h2 id="Caching">Caching</h2>
<p>HTTP caching lets clients and intermediaries reuse stored responses. Its rules are specified in <a href="https://www.rfc-editor.org/rfc/rfc9111">RFC 9111</a>, while the core semantics are in <a href="https://www.rfc-editor.org/rfc/rfc9110">RFC 9110</a>.</p>`,
      },
    ],
    links: [
      { href: 'https://www.rfc-editor.org/rfc/rfc9111', target: 'wsp:rfc/9111' },
      { href: 'https://www.rfc-editor.org/rfc/rfc9110', target: 'wsp:rfc/9110' },
    ],
    entities: ['Q8777'],
    aliases: ['http'],
  },
  {
    source_id: 'wikipedia-en-zim',
    native_id: 'World_Wide_Web',
    title: 'World Wide Web',
    kind: 'encyclopedia',
    lang: 'en',
    license: 'CC-BY-SA-4.0',
    url_native: 'https://en.wikipedia.org/wiki/World_Wide_Web',
    format: 'html',
    versions: [
      {
        revision: '1249400000',
        timestamp: T('2026-09-14'),
        html: `<p>The <b>World Wide Web</b> is an information system that enables documents to be accessed over the Internet. Pages are linked by hypertext and transferred with <a href="./Hypertext_Transfer_Protocol">HTTP</a>.</p>
<p>Early graphical browsers such as Mosaic (1993) and Netscape Navigator (1994) brought the web to the general public; this offline web is styled after them.</p>`,
      },
    ],
    links: [],
    entities: ['Q466'],
    aliases: ['www', 'the web'],
  },
  {
    source_id: 'wikipedia-en-zim',
    native_id: 'Mary_Shelley',
    title: 'Mary Shelley',
    kind: 'encyclopedia',
    lang: 'en',
    license: 'CC-BY-SA-4.0',
    url_native: 'https://en.wikipedia.org/wiki/Mary_Shelley',
    format: 'html',
    versions: [
      {
        revision: '1249300000',
        timestamp: T('2026-09-11'),
        html: `<p><b>Mary Wollstonecraft Shelley</b> (1797&ndash;1851) was an English novelist, best known for the Gothic novel <a href="wsp:gutenberg/84"><i>Frankenstein; or, The Modern Prometheus</i></a> (1818).</p>
<p>Her other books include <i>The Last Man</i> (1826), an early work of apocalyptic fiction.</p>`,
      },
    ],
    links: [],
    entities: ['Q47152'],
    aliases: ['books by mary shelley', 'frankenstein author'],
  },
  {
    source_id: 'wiktionary-en',
    native_id: 'sesquipedalian',
    title: 'sesquipedalian',
    kind: 'dictionary',
    lang: 'en',
    license: 'CC-BY-SA-4.0',
    url_native: 'https://en.wiktionary.org/wiki/sesquipedalian',
    format: 'html',
    versions: [
      {
        revision: '84000001',
        timestamp: T('2026-08-30'),
        html: `<h2>English</h2><h3>Etymology</h3><p>From Latin <i>sesquipedalis</i> (&ldquo;a foot and a half long&rdquo;).</p>
<h3>Adjective</h3><ol><li>Given to using long words.</li><li>Of a word: long, having many syllables.</li></ol>
<h3>Noun</h3><ol><li>A long word.</li></ol>`,
      },
    ],
    links: [],
    entities: [],
  },
  {
    source_id: 'wikidata',
    native_id: 'Q7251',
    title: 'Alan Turing (Q7251)',
    kind: 'data',
    lang: 'en',
    license: 'CC0-1.0',
    url_native: 'https://www.wikidata.org/wiki/Q7251',
    format: 'html',
    versions: [
      {
        revision: '2200000001',
        timestamp: T('2026-09-28'),
        html: `<p>English computer scientist (1912&ndash;1954)</p>
<table><thead><tr><th>Property</th><th>Value</th></tr></thead><tbody>
<tr><td>instance of</td><td>human</td></tr>
<tr><td>date of birth</td><td>23 June 1912</td></tr>
<tr><td>place of birth</td><td>Maida Vale, London</td></tr>
<tr><td>date of death</td><td>7 June 1954</td></tr>
<tr><td>field of work</td><td>mathematics, cryptanalysis, computer science</td></tr>
<tr><td>Wikipedia article</td><td><a href="wsp:wikipedia-en-zim/Alan_Turing">Alan Turing</a></td></tr>
</tbody></table>`,
      },
    ],
    links: [],
    entities: ['Q7251'],
  },
  {
    source_id: 'gutenberg',
    native_id: '84',
    title: 'Frankenstein; Or, The Modern Prometheus by Mary Wollstonecraft Shelley',
    kind: 'books',
    lang: 'en',
    license: 'Public domain (US)',
    url_native: 'https://www.gutenberg.org/ebooks/84',
    format: 'text',
    versions: [
      {
        revision: '2024-11-01',
        timestamp: T('2026-09-30'),
        text: `FRANKENSTEIN;

OR, THE MODERN PROMETHEUS.

by Mary Wollstonecraft (Godwin) Shelley


LETTER 1

To Mrs. Saville, England.

St. Petersburgh, Dec. 11th, 17--.

You will rejoice to hear that no disaster has accompanied the
commencement of an enterprise which you have regarded with such evil
forebodings. I arrived here yesterday, and my first task is to assure
my dear sister of my welfare and increasing confidence in the success
of my undertaking.

I am already far north of London, and as I walk in the streets of
Petersburgh, I feel a cold northern breeze play upon my cheeks, which
braces my nerves and fills me with delight.

LETTER 2

To Mrs. Saville, England.

How slowly the time passes here, encompassed as I am by frost and snow!
Yet a second step is taken towards my enterprise.

CHAPTER I

I am by birth a Genevese, and my family is one of the most
distinguished of that republic. My ancestors had been for many years
counsellors and syndics.

CHAPTER II

We were brought up together; there was not quite a year difference in
our ages. I need not say that we were strangers to any species of
disunion or dispute.
`,
      },
    ],
    links: [],
    entities: ['Q150827', 'Q47152'],
    aliases: ['books by mary shelley', 'frankenstein'],
  },
  {
    source_id: 'gutenberg',
    native_id: '1342',
    title: 'Pride and Prejudice by Jane Austen',
    kind: 'books',
    lang: 'en',
    license: 'Public domain (US)',
    url_native: 'https://www.gutenberg.org/ebooks/1342',
    format: 'text',
    versions: [
      {
        revision: '2025-02-10',
        timestamp: T('2026-09-30'),
        text: `PRIDE AND PREJUDICE

By Jane Austen


CHAPTER I.

It is a truth universally acknowledged, that a single man in possession
of a good fortune, must be in want of a wife.

However little known the feelings or views of such a man may be on his
first entering a neighbourhood, this truth is so well fixed in the minds
of the surrounding families, that he is considered as the rightful
property of some one or other of their daughters.
`,
      },
    ],
    links: [],
    entities: ['Q170583'],
    aliases: ['jane austen'],
  },
  {
    source_id: 'open-library',
    native_id: 'works/OL450063W',
    title: 'Frankenstein or The Modern Prometheus',
    kind: 'data',
    lang: 'en',
    license: 'Public domain (catalog data)',
    url_native: 'https://openlibrary.org/works/OL450063W',
    format: 'text',
    versions: [
      {
        revision: '12',
        timestamp: T('2026-10-10'),
        text: 'Frankenstein or The Modern Prometheus\nOpen Library catalog record: work.\nBy: Mary Shelley\nFirst published: 1818\nAlso at: Project Gutenberg #84\nSubjects: Science fiction',
      },
    ],
    links: [{ href: 'wsp:gutenberg/84', target: 'wsp:gutenberg/84' }],
    entities: [],
  },
  {
    source_id: 'open-library',
    native_id: 'works/OL66554W',
    title: 'Pride and Prejudice',
    kind: 'data',
    lang: 'en',
    license: 'Public domain (catalog data)',
    url_native: 'https://openlibrary.org/works/OL66554W',
    format: 'text',
    versions: [
      {
        revision: '9',
        timestamp: T('2026-10-10'),
        text: 'Pride and Prejudice\nOpen Library catalog record: work.\nBy: Jane Austen\nFirst published: 1813\nSubjects: Courtship',
      },
    ],
    links: [],
    entities: [],
  },
  {
    source_id: 'open-library',
    native_id: 'books/OL22856696M',
    title: 'The Quiet Orchard',
    kind: 'data',
    lang: 'en',
    license: 'Public domain (catalog data)',
    url_native: 'https://openlibrary.org/books/OL22856696M',
    format: 'text',
    versions: [
      {
        revision: '3',
        timestamp: T('2026-10-10'),
        text: 'The Quiet Orchard\nOpen Library catalog record: edition.\nBy: A. Novelist\nPublished: Small Press, 1998\nISBN: 9780000000099\nWork: works/OL1W',
      },
    ],
    links: [],
    entities: [],
  },
  {
    source_id: 'prelinger-archives',
    native_id: 'DuckandC1951',
    title: 'Duck and Cover (1951)',
    kind: 'video',
    lang: 'en',
    license: 'Public domain',
    url_native: 'https://archive.org/details/DuckandC1951',
    format: 'text',
    versions: [
      {
        revision: '1',
        timestamp: T('2026-10-01'),
        text: 'Duck and Cover (1951)\nFederal Civil Defense Administration film with Bert the Turtle. 9 minutes.',
      },
    ],
    links: [],
    entities: [],
    original: {
      url: '/v1/files/wsp%3Aprelinger-archives%2FDuckandC1951',
      name: 'DuckandC1951.webm',
      media_type: 'video/webm',
      inline: true,
      preview_url: null,
    },
  },
  {
    source_id: 'librivox',
    native_id: 'moby-dick-01',
    title: 'Moby Dick, chapter 1: Loomings',
    kind: 'audio',
    lang: 'en',
    license: 'Public domain',
    url_native: 'https://librivox.org/moby-dick-by-herman-melville/',
    format: 'text',
    versions: [
      {
        revision: '1',
        timestamp: T('2026-10-01'),
        text: 'Moby Dick, chapter 1: Loomings\nRead by a LibriVox volunteer. 22 minutes.',
      },
    ],
    links: [],
    entities: [],
    original: {
      url: '/v1/files/wsp%3Alibrivox%2Fmoby-dick-01',
      name: 'mobydick_01.ogg',
      media_type: 'audio/ogg',
      inline: true,
      preview_url: null,
    },
  },
  {
    // A pool a newer release replaced: the hall labels it and lists it after the current one.
    source_id: 'ncvec-question-pools',
    native_id: 'ncvec:2022-2026-Technician-Pool-Errata:2025',
    title: '2022-2026 Technician Question Pool Dec 18 2025.pdf',
    kind: 'other',
    lang: 'en',
    license: 'Public domain',
    url_native: 'https://ncvec.org/downloads/2022-2026%20Technician%20Pool.pdf',
    format: 'text',
    currency: 'superseded',
    versions: [
      {
        revision: '2025',
        timestamp: T('2025-12-18'),
        text: `2022-2026 Technician Question Pool Dec 18 2025.pdf
Issuer: NCVEC Question Pool Committee

T1A01 (C) [97.1]
Which of the following is part of the Basis and Purpose of the Amateur Radio Service?
A. Providing personal radio communications for as many citizens as possible
B. Providing communications for international contesting
C. Advancing skills in the technical and communication phases of the radio art
D. All these choices are correct
~~`,
      },
    ],
    links: [],
    entities: [],
  },
  {
    source_id: 'ncvec-question-pools',
    native_id: 'ncvec:2026-2030-Technician-Pool-and-Syllabus-Public-Release:2026',
    title: '2026-2030 Technician Pool and Syllabus Public Release Feb 19 2026.pdf',
    kind: 'other',
    lang: 'en',
    license: 'Public domain',
    url_native: 'https://ncvec.org/downloads/2026-2030%20Technician%20Pool.pdf',
    format: 'text',
    versions: [
      {
        revision: '2026',
        timestamp: T('2026-02-19'),
        text: `2026-2030 Technician Pool and Syllabus Public Release Feb 19 2026.pdf
Issuer: NCVEC Question Pool Committee
Edition: 2026

SUBELEMENT T1 - COMMISSION’S RULES [2 Exam Questions - 2 Groups] 3 Questions
T1A Purpose and permissible use of the Amateur Radio Service; Operator/primary station license grant
T1B Frequency allocations; Emission modes
SUBELEMENT T2 - OPERATING PROCEDURES [1 Exam Question - 1 Group] 1 Question
T2A Station operation

T1A01 (C) [97.1]
Which of the following is part of the Basis and Purpose of the Amateur Radio Service?
A. Providing personal radio communications for as many citizens as possible
B. Providing communications for international contesting
C. Advancing skills in the technical and communication phases of the radio art
D. All these choices are correct
~~
T1A02 (C) [97.1]
Which agency regulates and enforces the rules for the Amateur Radio Service in the United States?
A. ARRL
B. Homeland Security
C. The FCC
D. All these choices are correct
~~
T1B01 (B) [97.301]
Which frequency is in the 6 meter band?
A. 49.00 MHz
B. 52.525 MHz
C. 28.50 MHz
D. 222.1 MHz
~~
T2A01 (B) [97.119]
How often must an amateur station transmit its assigned call sign?
A. At least every 5 minutes
B. At least every 10 minutes
C. At least every 15 minutes
D. Only at the end of a contact
~~
`,
      },
    ],
    links: [],
    entities: [],
    aliases: ['question pool', 'technician'],
  },
  {
    source_id: 'rfc',
    native_id: '9110',
    title: 'RFC 9110: HTTP Semantics',
    kind: 'papers',
    lang: 'en',
    license: 'IETF Trust',
    url_native: 'https://www.rfc-editor.org/rfc/rfc9110',
    format: 'text',
    versions: [
      {
        revision: 'rfc9110',
        timestamp: T('2022-06-01'),
        text: `Internet Engineering Task Force (IETF)                  R. Fielding, Ed.
Request for Comments: 9110                                         Adobe
STD: 97                                               M. Nottingham, Ed.
Obsoletes: 2818, 7230, 7231, 7232, 7233, 7235,                    Fastly
           7538, 7615, 7694                                  J. Reschke, Ed.
Category: Standards Track                                     greenbytes
ISSN: 2070-1721                                                June 2022


                             HTTP Semantics

Abstract

   [Fixture summary] This document describes the overall architecture
   of HTTP, establishes common terminology, and defines aspects of the
   protocol that are shared by all versions.  Caching is defined
   separately in RFC 9111; see also RFC 1149 for an alternative
   transport.

1.  Introduction

   Each Hypertext Transfer Protocol message is either a request or a
   response.  A server listens on a connection for a request, parses
   each message received, interprets the message semantics in relation
   to the identified target resource, and responds to that request.
`,
      },
    ],
    links: [],
    entities: ['Q8777'],
    aliases: ['http semantics', 'rfc about http'],
  },
  {
    source_id: 'rfc',
    native_id: '9111',
    title: 'RFC 9111: HTTP Caching',
    kind: 'papers',
    lang: 'en',
    license: 'IETF Trust',
    url_native: 'https://www.rfc-editor.org/rfc/rfc9111',
    format: 'text',
    versions: [
      {
        revision: 'rfc9111',
        timestamp: T('2022-06-01'),
        text: `Internet Engineering Task Force (IETF)                  R. Fielding, Ed.
Request for Comments: 9111                                         Adobe
STD: 98                                               M. Nottingham, Ed.
Category: Standards Track                                         Fastly
                                                         J. Reschke, Ed.
                                                              greenbytes
                                                               June 2022


                              HTTP Caching

Abstract

   [Fixture summary] The Hypertext Transfer Protocol (HTTP) is a
   stateless application-level protocol for distributed, collaborative,
   hypertext information systems.  This document defines HTTP caches and
   the associated header fields that control cache behavior or indicate
   cacheable response messages.  Semantics are in RFC 9110.

1.  Introduction

   An HTTP cache is a local store of response messages and the subsystem
   that controls storage, retrieval, and deletion of messages in it.
   Freshness, validation with ETag and Last-Modified, and the
   Cache-Control header field are described below.
`,
      },
    ],
    links: [],
    entities: [],
    aliases: ['http caching', 'cache-control', 'etag'],
  },
  {
    source_id: 'rfc',
    native_id: '1149',
    title: 'RFC 1149: A Standard for the Transmission of IP Datagrams on Avian Carriers',
    kind: 'papers',
    lang: 'en',
    license: 'IETF Trust',
    url_native: 'https://www.rfc-editor.org/rfc/rfc1149',
    format: 'text',
    versions: [
      {
        revision: 'rfc1149',
        timestamp: T('1990-04-01'),
        text: `Network Working Group                                        D. Waitzman
Request for Comments: 1149                                       BBN STC
                                                            1 April 1990


   A Standard for the Transmission of IP Datagrams on Avian Carriers

Status of this Memo

   [Fixture summary] This memo describes an experimental method for the
   encapsulation of IP datagrams in avian carriers.  It is a famous
   April Fools' Day RFC.  Distribution of this memo is unlimited.
`,
      },
    ],
    links: [],
    entities: [],
    aliases: ['carrier pigeon', 'avian carriers', 'april fools rfc'],
  },
  {
    source_id: 'kiwix-xkcd-com-en-all',
    native_id: 'xkcd.com/936/',
    title: 'xkcd: Password Strength',
    kind: 'other',
    lang: 'en',
    license: 'CC-BY-NC-2.5',
    url_native: 'https://xkcd.com/936/',
    format: 'html',
    versions: [
      {
        revision: 'xkcd.com_en_all_2026-08',
        timestamp: T('2026-08-07'),
        html: `<div id="middleContainer" class="box"><div id="ctitle">Password Strength</div>
<ul class="comicNav"><li><a href="wsp://kiwix-xkcd-com-en-all/xkcd.com/1">|&lt;</a></li>
<li><a rel="prev" href="wsp://kiwix-xkcd-com-en-all/xkcd.com/935" accesskey="p">&lt; Prev</a></li>
<li><a href="https://c.xkcd.com/random/comic/">Random</a></li>
<li><a rel="next" href="wsp://kiwix-xkcd-com-en-all/xkcd.com/937" accesskey="n">Next &gt;</a></li></ul>
<div id="comic"><img src="/v1/assets/kiwix-xkcd-com-en-all/2026-08/imgs.xkcd.com/comics/password_strength.png" title="To anyone who understands information theory and security, I sincerely apologize." alt="Password Strength" srcset="/v1/assets/kiwix-xkcd-com-en-all/2026-08/imgs.xkcd.com/comics/password_strength_2x.png 2x" /></div>
<div id="transcript">((The comic illustrates the relative strength of passwords.))</div></div>`,
      },
    ],
    links: [],
    entities: [],
    aliases: ['correct horse battery staple'],
  },
  {
    source_id: 'kiwix-www-mankier-com-en-all',
    native_id: 'www.mankier.com/1/rsync',
    title: 'rsync: a fast, versatile, remote (and local) file-copying tool',
    kind: 'other',
    lang: 'en',
    license: 'LicenseRef-per-item',
    url_native: 'https://www.mankier.com/1/rsync',
    format: 'html',
    versions: [
      {
        revision: 'www.mankier.com_en_all_2026-07',
        timestamp: T('2026-07-01'),
        html: `<div class="burger"></div><div id="menu"><form action="/" id="ta"><input id="ta-input" name="q"></form>
<a href="#" class="menu-heading">rsync</a><nav id="menu-scroller"><ul class="nav-h2">
<li><a href="#Synopsis">Synopsis</a></li><li><a href="#Options">Options</a></li></ul></nav></div>
<main><section id="Synopsis"><a href="#Synopsis"><h2>Synopsis</h2></a><pre>rsync [OPTION...] SRC... [DEST]</pre></section>
<section id="Options"><a href="#Options"><h2>Options</h2></a><dl><dt><a id="--help" href="#--help">--help</a></dt><dd>Show help.</dd></dl></section></main>
<footer><ul><li><a href="wsp://kiwix-www-mankier-com-en-all/www.mankier.com/blog">Blog</a></li></ul></footer>`,
      },
    ],
    links: [],
    entities: [],
    aliases: [],
  },
  {
    source_id: 'stackexchange',
    native_id: 'webmasters/1007',
    title: 'What is the difference between ETag and Last-Modified for caching?',
    kind: 'qa',
    lang: 'en',
    license: 'CC-BY-SA-4.0',
    url_native: 'https://webmasters.stackexchange.com/questions/1007',
    format: 'html',
    versions: [
      {
        revision: 'a-2026-09-01',
        timestamp: T('2026-09-01'),
        html: `<header class="s-topbar js-top-bar"><ol><li><a href="wsp://stackexchange/webmasters/questions">Questions</a></li><li><a href="wsp://stackexchange/webmasters/tags">Tags</a></li></ol></header>
<div id="content"><div id="mainbar">
<div id="question" class="question" data-questionid="1007" data-score="37">
<div class="post-layout"><div class="votecell"><div class="js-voting-container">37</div></div>
<div class="postcell"><div class="s-prose js-post-body"><p>My server sends both <code>ETag</code> and <code>Last-Modified</code>. Which one does a browser use to revalidate a cached response, and should I send both?</p></div>
<div class="post-taglist"><a href="wsp://stackexchange/webmasters/questions/tagged/http" class="post-tag" rel="tag">http</a> <a href="wsp://stackexchange/webmasters/questions/tagged/caching" class="post-tag" rel="tag">caching</a></div></div></div></div>
<div id="answers"><div id="answers-header"><h2>3 Answers</h2></div>
<div id="answer-1010" class="answer" data-answerid="1010" data-score="5"><div class="post-layout"><div class="votecell"></div><div class="answercell"><div class="s-prose js-post-body"><p>Send both; they cost nothing.</p></div></div></div></div>
<div id="answer-1008" class="answer accepted-answer" data-answerid="1008" data-score="212"><div class="post-layout"><div class="votecell"></div><div class="answercell"><div class="s-prose js-post-body"><p>Both are <em>validators</em>. A cache that holds a stale response sends <code>If-None-Match</code> with the ETag and <code>If-Modified-Since</code> with the date; the origin answers 304 when either still matches.</p></div></div></div></div>
<div id="answer-1012" class="answer" data-answerid="1012" data-score="40"><div class="post-layout"><div class="votecell"></div><div class="answercell"><div class="s-prose js-post-body"><p>Prefer the ETag when the content can change within a second.</p></div></div></div></div>
</div></div><div id="left-sidebar">nav</div></div><footer>Site footer</footer>`,
      },
    ],
    links: [],
    entities: [],
    aliases: ['http caching', 'etag', 'last-modified'],
  },
  {
    source_id: 'openstax',
    native_id: 'microbiology-18-1',
    title: 'Microbiology 18.1: Overview of Specific Adaptive Immunity',
    kind: 'books',
    lang: 'en',
    license: 'CC-BY-4.0',
    url_native: 'https://openstax.org/books/microbiology/pages/18-1',
    format: 'html',
    versions: [
      {
        revision: '2026.1',
        timestamp: T('2026-09-01'),
        html: `<h2>Learning objectives</h2><ul><li>Explain how vaccination produces immunological memory.</li><li>Compare primary and secondary immune responses.</li></ul>
<p>[Fixture summary] The primary response to a new antigen takes days to peak. Memory B and T cells generated during that response allow a much faster and larger secondary response. Vaccines deliberately trigger a primary response so the body is prepared for the real pathogen.</p>`,
      },
    ],
    links: [],
    entities: [],
    aliases: ['immune memory', 'adaptive immunity', 'how do vaccines train the immune system'],
  },
  {
    source_id: 'medlineplus',
    native_id: 'vaccines',
    title: 'Vaccines (Immunizations)',
    kind: 'medicine',
    lang: 'en',
    license: 'Public domain (US Gov)',
    url_native: 'https://medlineplus.gov/vaccines.html',
    format: 'html',
    versions: [
      {
        revision: '2026-10-02',
        timestamp: T('2026-10-02'),
        html: `<p>[Fixture summary] Vaccines help your immune system learn to fight specific germs. They contain a harmless part or version of the germ, so your body can practice making antibodies. Later, if the real germ attacks, your immune system recognizes it and responds quickly.</p>`,
      },
    ],
    links: [],
    entities: ['Q134808'],
    aliases: ['immunizations', 'shots'],
  },
  {
    source_id: 'osm-planet',
    native_id: 'relation/5400890',
    title: 'Lisbon (OpenStreetMap relation)',
    kind: 'maps',
    lang: 'pt',
    license: 'ODbL-1.0',
    url_native: 'https://www.openstreetmap.org/relation/5400890',
    format: 'html',
    versions: [
      {
        revision: 'v42',
        timestamp: T('2026-10-06'),
        html: `<p>Administrative boundary: <b>Lisboa</b>, Portugal.</p><table><tbody><tr><th>Centre</th><td>38.7223 N, 9.1393 W</td></tr><tr><th>admin_level</th><td>7</td></tr><tr><th>wikidata</th><td><a href="wsp:wikidata/Q597">Q597</a></td></tr></tbody></table><p>Interactive vector map: available when the map viewer is installed.</p>`,
      },
    ],
    links: [{ href: 'wsp:wikidata/Q597', target: null }],
    entities: ['Q597'],
    aliases: ['map of lisbon', 'lisbon map'],
  },
];

export const DIRECTORY: Record<
  string,
  {
    name: string;
    children: string[];
    entries: string[];
    crosslinks?: string[];
    /** Pages in the category when `entries` lists only the best known. */
    total?: number;
  }
> = {
  '': {
    name: 'Top',
    children: [
      'Arts',
      'Science',
      'Computers_and_Internet',
      'Health',
      'Regional',
      'Reference',
      'Society',
    ],
    entries: [],
  },
  Arts: { name: 'Arts', children: ['Arts/Literature'], entries: [] },
  'Arts/Literature': {
    name: 'Literature',
    children: ['Arts/Literature/Novels', 'Arts/Literature/Poetry'],
    entries: ['wsp:wikipedia-en-zim/Mary_Shelley'],
  },
  'Arts/Literature/Novels': {
    name: 'Novels',
    children: [],
    entries: ['wsp:gutenberg/84', 'wsp:gutenberg/1342'],
    total: 7121,
  },
  'Arts/Literature/Poetry': { name: 'Poetry', children: [], entries: [] },
  Science: {
    name: 'Science',
    children: ['Science/Biology', 'Science/Mathematics'],
    crosslinks: ['Computers_and_Internet'],
    entries: [],
  },
  'Science/Biology': {
    name: 'Biology',
    children: ['Science/Biology/Immunology'],
    entries: [],
  },
  'Science/Biology/Immunology': {
    name: 'Immunology',
    children: [],
    entries: [
      'wsp:wikipedia-en-zim/Immune_system',
      'wsp:wikipedia-en-zim/Vaccine',
      'wsp:openstax/microbiology-18-1',
    ],
    crosslinks: ['Health'],
  },
  'Science/Mathematics': {
    name: 'Mathematics',
    children: [],
    entries: ['wsp:wikipedia-en-zim/Turing_machine'],
  },
  Computers_and_Internet: {
    name: 'Computers and Internet',
    children: ['Computers_and_Internet/Internet', 'Computers_and_Internet/History'],
    entries: [],
  },
  'Computers_and_Internet/Internet': {
    name: 'Internet',
    children: [],
    entries: [
      'wsp:wikipedia-en-zim/World_Wide_Web',
      'wsp:wikipedia-en-zim/Hypertext_Transfer_Protocol',
      'wsp:rfc/9110',
      'wsp:rfc/9111',
      'wsp:rfc/1149',
      'wsp:stackexchange/webmasters/1007',
    ],
  },
  'Computers_and_Internet/History': {
    name: 'History',
    children: [],
    entries: ['wsp:wikipedia-en-zim/Alan_Turing', 'wsp:wikipedia-en-zim/Enigma_machine'],
  },
  Health: {
    name: 'Health',
    children: [],
    entries: ['wsp:medlineplus/vaccines', 'wsp:wikipedia-en-zim/Vaccine'],
  },
  Regional: { name: 'Regional', children: ['Regional/Europe'], entries: [] },
  'Regional/Europe': { name: 'Europe', children: ['Regional/Europe/Portugal'], entries: [] },
  'Regional/Europe/Portugal': {
    name: 'Portugal',
    children: [],
    entries: ['wsp:wikipedia-en-zim/Lisbon', 'wsp:osm-planet/relation/5400890'],
  },
  Reference: {
    name: 'Reference',
    children: [],
    entries: ['wsp:wiktionary-en/sesquipedalian', 'wsp:wikidata/Q7251'],
  },
  Society: { name: 'Society', children: [], entries: [] },
};

export const DESCRIPTIONS: Record<string, string> = {
  'wsp:wikipedia-en-zim/Mary_Shelley': 'English novelist, author of Frankenstein.',
  'wsp:gutenberg/84': 'The 1818 Gothic novel, full text.',
  'wsp:gutenberg/1342': "Jane Austen's 1813 novel of manners, full text.",
  'wsp:wikipedia-en-zim/Immune_system': 'How the body defends itself.',
  'wsp:wikipedia-en-zim/Vaccine': 'Preparations that train the immune system.',
  'wsp:openstax/microbiology-18-1': 'Textbook chapter on adaptive immunity.',
  'wsp:wikipedia-en-zim/Turing_machine': 'The abstract model of computation.',
  'wsp:wikipedia-en-zim/World_Wide_Web': 'The hypertext system you are imitating right now.',
  'wsp:wikipedia-en-zim/Hypertext_Transfer_Protocol': 'The protocol of the web.',
  'wsp:rfc/9110': 'HTTP Semantics, the core spec.',
  'wsp:rfc/9111': 'HTTP Caching.',
  'wsp:rfc/1149': 'IP over Avian Carriers. Cool site!',
  'wsp:stackexchange/webmasters/1007': 'ETag vs Last-Modified, answered.',
  'wsp:wikipedia-en-zim/Alan_Turing': 'Mathematician, codebreaker, computing pioneer.',
  'wsp:wikipedia-en-zim/Enigma_machine': 'The WWII cipher machine.',
  'wsp:medlineplus/vaccines': 'Plain-language guide to immunizations.',
  'wsp:wikipedia-en-zim/Lisbon': 'Capital of Portugal.',
  'wsp:osm-planet/relation/5400890': 'Map boundary of Lisbon.',
  'wsp:wiktionary-en/sesquipedalian': 'A word about long words.',
  'wsp:wikidata/Q7251': 'Structured facts about Alan Turing.',
};
