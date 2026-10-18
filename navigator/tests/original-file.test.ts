import { describe, expect, it } from 'vitest';
import { originalPanel } from '../src/pages/document.ts';

const base = (path: string) => `http://lib.test${path}`;

describe('original file panel', () => {
  it('frames a PDF and offers open and download', () => {
    const el = originalPanel(
      {
        title: 'Coast Pilot 1',
        kind: 'other',
        original: {
          url: '/v1/files/wsp%3Anoaa%2Fcp1',
          name: 'CP1.pdf',
          media_type: 'application/pdf',
          inline: true,
          preview_url: null,
        },
      },
      base,
    )!;
    const frame = el.querySelector('iframe')!;
    expect(frame.getAttribute('src')).toBe('http://lib.test/v1/files/wsp%3Anoaa%2Fcp1');
    expect(frame.getAttribute('title')).toContain('Coast Pilot 1');
    expect(el.textContent).toContain('PDF document');
    expect(el.querySelector('a[download="CP1.pdf"]')).not.toBeNull();
    expect(el.querySelector('a[target="_blank"]')).not.toBeNull();
  });

  it('opens a chapter of a long PDF at its first page', () => {
    const el = originalPanel(
      {
        title: 'Closed-loop control — Lessons In Industrial Instrumentation',
        kind: 'other',
        url_native: 'https://ibiblio.org/kuphaldt/socratic/sinst/book/liii.pdf#page=2370',
        original: {
          url: '/v1/files/liii',
          name: 'liii.pdf',
          media_type: 'application/pdf',
          inline: true,
          preview_url: null,
        },
      },
      base,
    )!;
    expect(el.querySelector('iframe')!.getAttribute('src')).toBe(
      'http://lib.test/v1/files/liii#page=2370',
    );
    expect(el.querySelector('a[target="_blank"]')!.getAttribute('href')).toBe(
      'http://lib.test/v1/files/liii#page=2370',
    );
    expect(el.querySelector('a[download]')!.getAttribute('href')).toBe(
      'http://lib.test/v1/files/liii',
    );
  });

  it('shows a map scan through its preview', () => {
    const el = originalPanel({
      title: 'Boston South 1:24,000',
      kind: 'maps',
      original: {
        url: '/v1/files/x',
        name: 'MA_Boston_South.tif',
        media_type: 'image/tiff',
        inline: false,
        preview_url: '/v1/files/x/preview',
      },
    })!;
    expect(el.querySelector('iframe')).toBeNull();
    expect(el.querySelector('img')!.getAttribute('src')).toBe('/v1/files/x/preview');
    expect(el.querySelector('a.original-preview')!.getAttribute('href')).toBe('/v1/files/x');
    expect(el.querySelector('img')!.getAttribute('alt')).toContain('Boston South');
    expect(el.textContent).toContain('TIFF image');
  });

  it('offers only a download for other files, and nothing without one', () => {
    const el = originalPanel({
      title: 'Data',
      kind: 'data',
      original: {
        url: '/v1/files/z',
        name: 'data.zip',
        media_type: 'application/zip',
        inline: false,
        preview_url: null,
      },
    })!;
    expect(el.querySelector('a[target="_blank"]')).toBeNull();
    expect(el.textContent).toContain('download it to open it');
    expect(originalPanel({ title: 'x', kind: 'other', original: null })).toBeNull();
  });

  it('does not say a text file cannot be shown (it is, below)', () => {
    const el = originalPanel({
      title: 'Internet Protocol',
      kind: 'papers',
      original: {
        url: '/v1/files/r',
        name: 'RFC-791.txt',
        media_type: 'text/plain',
        inline: false,
        preview_url: null,
      },
    })!;
    expect(el.textContent).not.toContain('cannot show');
    expect(el.querySelector('h2')?.textContent).toBe('Text of the file');
  });

  it('shows a map PDF as its rendered sheet, not a viewer', () => {
    const el = originalPanel({
      title: 'RI East Greenwich 1942',
      kind: 'maps',
      original: {
        url: '/v1/files/m',
        name: 'East-Greenwich.pdf',
        media_type: 'application/pdf',
        inline: true,
        preview_url: '/v1/files/m/preview',
      },
    })!;
    expect(el.querySelector('iframe')).toBeNull();
    expect(el.querySelector('img')!.getAttribute('src')).toBe('/v1/files/m/preview');
  });

  it('opens a long PDF from a maps source in the viewer (a Coast Pilot is a book)', () => {
    const el = originalPanel({
      title: 'Coast Pilot 8',
      kind: 'maps',
      text: 'Chapter 1. General information. '.repeat(200),
      original: {
        url: '/v1/files/cp8',
        name: 'COAST-PILOT-8.pdf',
        media_type: 'application/pdf',
        inline: true,
        preview_url: '/v1/files/cp8/preview',
      },
    })!;
    expect(el.querySelector('iframe')?.getAttribute('src')).toBe('/v1/files/cp8');
    expect(el.querySelector('img')).toBeNull();
  });

  it('still shows a sheet whose only text is its catalog line', () => {
    const el = originalPanel({
      title: 'HI Puako 1928',
      kind: 'maps',
      text: 'HI Puako 349899 1928 62500 geo. US Geological Survey, scale 1:62,500.',
      original: {
        url: '/v1/files/p',
        name: 'HI_Puako.pdf',
        media_type: 'application/pdf',
        inline: true,
        preview_url: '/v1/files/p/preview',
      },
    })!;
    expect(el.querySelector('iframe')).toBeNull();
    expect(el.querySelector('img')!.getAttribute('src')).toBe('/v1/files/p/preview');
  });
});

describe('links of a text document (audit 2026-10-12 F6)', () => {
  it('lists held pages first and the rest as broken links', async () => {
    const { linksList } = await import('../src/pages/document.ts');
    const el = linksList({
      format: 'text',
      html: null,
      links: [
        { href: 'https://doi.org/10.1/x', target: null },
        { href: 'wsp:gutenberg-zim/Alice.11', target: 'wsp:gutenberg-zim/Alice.11' },
      ],
    })!;
    const anchors = [...el.querySelectorAll('a')];
    expect(anchors[0]!.dataset.wsp).toBe('wsp://gutenberg-zim/Alice.11');
    expect(anchors[1]!.className).toBe('broken-link');
    expect(el.querySelector('h2')!.textContent).toBe('Also in the library');
    expect(
      linksList({ format: 'html', html: '<p>x</p>', links: [{ href: 'a', target: null }] }),
    ).toBeNull();
    expect(linksList({ format: 'text', html: null, links: [] })).toBeNull();
  });
});

describe('View Source Data names a borrowed page (audit 2026-10-12 F16)', () => {
  it('shows where the page on screen comes from', async () => {
    const { sourceDataDialog } = await import('../src/chrome/dialogs.ts');
    const { mockFetch } = await import('./helpers.ts');
    const { WspClient } = await import('../src/api/client.ts');
    const api = new WspClient({ fetch: mockFetch() });
    const doc = await api.document('wsp:gutenberg/84');
    const el = sourceDataDialog({
      ...doc,
      rendition: {
        source_id: 'wikipedia-en-zim',
        generation_id: '2026-08',
        doc_id: 'wsp:wikipedia-en-zim/Dog',
      },
    });
    expect(el.textContent).toContain('Page shown from');
    expect(el.textContent).toContain('wsp:wikipedia-en-zim/Dog');
  });
});

describe('catalog cards (Open Library)', () => {
  it('say they are not the book and point to it', async () => {
    const { catalogNotice } = await import('../src/pages/document.ts');
    const el = catalogNotice(
      {
        title: 'Coast Pilot 1 : Atlantic Coast',
        source_id: 'open-library',
        text: 'Open Library catalog record: edition.\nWork: works/OL14903344W\n',
      },
      { domain: ['catalog'] },
    )!;
    const links = [...el.querySelectorAll('a')].map((a) => a.dataset.wsp);
    expect(links[0]).toBe('wsp://search?q=Coast+Pilot+1&kind=books');
    expect(links[1]).toBe('wsp://open-library/works/OL14903344W');
    expect(el.textContent).toContain('not the book');
    expect(
      catalogNotice({ title: 'x', source_id: 'gutenberg', text: '' }, { domain: ['books'] }),
    ).toBeNull();
  });
});

describe('text documents', () => {
  it('do not repeat the title under the heading', async () => {
    const { withoutTitleLine } = await import('../src/pages/document.ts');
    expect(
      withoutTitleLine(
        'Coast Pilot 1 : Atlantic Coast\nOpen Library catalog record',
        'Coast Pilot 1 : Atlantic Coast',
      ),
    ).toBe('Open Library catalog record');
    expect(withoutTitleLine('\n  Moby  Dick \nCall me Ishmael.', 'Moby Dick')).toBe(
      'Call me Ishmael.',
    );
    expect(withoutTitleLine('Chapter 1\nIt was', 'Moby Dick')).toBe('Chapter 1\nIt was');
  });
});

describe('an empty text document', () => {
  it('says so instead of showing a blank page', async () => {
    const { renderBody } = await import('../src/pages/document.ts');
    const { mockFetch } = await import('./helpers.ts');
    const { WspClient } = await import('../src/api/client.ts');
    const doc = await new WspClient({ fetch: mockFetch() }).document('wsp:gutenberg/84');
    const { node } = renderBody({ ...doc, format: 'text', html: null, text: '', original: null });
    expect(node.textContent).toContain('no text of its own');
    expect(node.querySelector('a')?.dataset.wsp).toContain('wsp://search?q=');
  });
});

describe('a front page with no way in (2026-10-17 field exam)', () => {
  it('points to the site’s shelves when it links to almost none of its pages', async () => {
    const { deadEndNotice } = await import('../src/pages/document.ts');
    const source = {
      id: 'aops',
      home_doc_id: 'wsp:aops/A/Main_Page',
      domain: ['education'],
      doc_count: 11529,
    };
    const doc = { doc_id: 'wsp:aops/A/Main_Page', source_id: 'aops' };
    const bare = document.createElement('div');
    bare.innerHTML = '<p>Popular Topics</p><a data-wsp="wsp://aops/A/One">one</a>';
    const note = deadEndNotice(doc, source, bare);
    expect(note?.textContent).toContain('few of the site’s 11,529 pages');
    expect(note?.querySelector('a')?.dataset.wsp).toBe('wsp://directory/education/aops');
    const linked = document.createElement('div');
    linked.innerHTML = ['A', 'B', 'C']
      .map((x) => `<a data-wsp="wsp://aops/A/${x}">${x}</a>`)
      .join('');
    expect(deadEndNotice(doc, source, linked)).toBeNull();
    expect(deadEndNotice({ ...doc, doc_id: 'wsp:aops/A/Other' }, source, bare)).toBeNull();
  });
});
