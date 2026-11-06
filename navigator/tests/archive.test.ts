import { describe, expect, it } from 'vitest';
import { ApiRequestError } from '../src/api/client.ts';
import type { ArchiveListing, ArchiveView } from '../src/api/types.ts';
import { append } from '../src/core/dom.ts';
import {
  type ArchiveApi,
  archiveBrowser,
  archiveContents,
  parseDelimited,
  renderView,
} from '../src/pages/archive.ts';
import { originalPanel } from '../src/pages/document.ts';
import { until } from './helpers.ts';

const DOC = 'wsp:nrcs-gssurgo/gssurgo:GNATSGO';
const BASE = '/v1/files/wsp%3Anrcs-gssurgo%2Fgssurgo%3AGNATSGO/archive';

function member(path: string, size: number, view: 'text' | 'metadata' | null = null) {
  return {
    path,
    name: path.slice(path.lastIndexOf('/') + 1),
    size,
    packed: size,
    modified: '2026-10-01T08:24:46',
    view,
  };
}

function listing(over: Partial<ArchiveListing> = {}): ArchiveListing {
  return {
    name: 'GNATSGO.zip',
    folder: '',
    members: 4,
    bytes: 7_751_344_732 + 3855 + 7_049_440 + 100,
    packed: 7_700_000_000,
    folders: [
      { path: '', count: 0, bytes: 0, total_count: 4, total_bytes: 1, files: [] },
      { path: 'gNATSGO/', count: 0, bytes: 0, total_count: 4, total_bytes: 1, files: [] },
      {
        path: 'gNATSGO/Geopackages/',
        count: 1,
        bytes: 7_049_440,
        total_count: 4,
        total_bytes: 1,
        files: [member('gNATSGO/Geopackages/ssurgo_iso19115_3.xml', 7_049_440, 'metadata')],
      },
      {
        path: 'gNATSGO/Geopackages/Archive/FY19/',
        count: 0,
        bytes: 0,
        total_count: 0,
        total_bytes: 0,
        files: [],
      },
      {
        path: 'gNATSGO/Geopackages/US/',
        count: 3,
        bytes: 7_751_344_832,
        total_count: 3,
        total_bytes: 7_751_344_832,
        files: [
          member('gNATSGO/Geopackages/US/01_ReadMe.txt', 3855, 'text'),
          member('gNATSGO/Geopackages/US/gNATSGO_10_01_2026.7z', 7_751_340_877),
        ],
      },
    ],
    truncated: true,
    member_url: `${BASE}/member`,
    view_url: `${BASE}/view`,
    ...over,
  };
}

const META: ArchiveView = {
  path: 'gNATSGO/Geopackages/ssurgo_iso19115_3.xml',
  name: 'ssurgo_iso19115_3.xml',
  size: 7_049_440,
  text: null,
  truncated: false,
  metadata: {
    standard: 'ISO 19115-3',
    title: 'Soil Survey Geographic Database (SSURGO)',
    abstract: 'The most detailed level of soil geographic data.',
    sections: [
      {
        heading: 'Identification',
        fields: [{ label: 'Citation › Title', value: 'Soil Survey Geographic Database (SSURGO)' }],
        more: 0,
      },
      { heading: 'Metadata date', fields: [{ label: '', value: '2025-03-05' }], more: 2 },
    ],
    dictionary: [
      {
        name: 'chaashto',
        label: 'Horizon AASHTO',
        definition: 'The Horizon AASHTO table.',
        attributes: [
          {
            name: 'aashtocl',
            definition: 'AASHTO class.',
            type: 'Choice',
            unit: null,
            values: ['A-1 (1): Granular materials.'],
            more_values: 15,
          },
        ],
        more_attributes: 0,
      },
    ],
    more_entities: 0,
  },
};

function fakeApi(log: string[], view: ArchiveView | Error = META): ArchiveApi {
  return {
    archive: async (_id, folder) => {
      log.push(`archive ${folder ?? ''}`);
      return listing(folder ? { folder, truncated: false } : {});
    },
    archiveView: async (_id, path) => {
      log.push(`view ${path}`);
      if (view instanceof Error) throw view;
      return view;
    },
    url: (path) => `http://lib.test${path}`,
  };
}

describe('archive browser', () => {
  it('lists files by folder with sizes and a download each', () => {
    const shown: string[] = [];
    const box = document.createElement('div');
    append(
      box,
      archiveContents(listing(), { api: fakeApi([]), docId: DOC }, (f) => shown.push(f)),
    );
    expect(box.querySelector('.archive-summary')!.textContent).toContain(
      '4 files, 7.2 GB unpacked',
    );
    // Folders that only hold folders are not repeated as empty boxes; an empty folder still shows.
    const names = [...box.querySelectorAll('.archive-folder-name')].map((n) => n.textContent);
    expect(names).toEqual([
      'gNATSGO/Geopackages/',
      'gNATSGO/Geopackages/Archive/FY19/',
      'gNATSGO/Geopackages/US/',
    ]);
    const us = box.querySelectorAll('details.archive-folder')[2]!;
    expect(us.className).toContain('archive-depth-2');
    expect(us.querySelector('.archive-folder-size')!.textContent).toBe('3 files, 7.2 GB');
    const link = us.querySelector<HTMLAnchorElement>('a.archive-download')!;
    expect(link.getAttribute('href')).toBe(
      `http://lib.test${BASE}/member?path=gNATSGO%2FGeopackages%2FUS%2F01_ReadMe.txt`,
    );
    expect(link.getAttribute('download')).toBe('01_ReadMe.txt');
    // The read-me and the metadata are offered first; a .7z has only its download.
    const first = box.querySelector('.archive-first')!;
    expect(first.textContent).toContain('ssurgo_iso19115_3.xml');
    expect(first.textContent).toContain('01_ReadMe.txt');
    expect(first.querySelector('button.archive-read')!.textContent).toBe('Read the metadata');
    const rows = us.querySelectorAll('tbody tr:not(.archive-view-row)');
    expect(rows[1]!.querySelector('button.archive-read')).toBeNull();
    // Cut short: the folder offers the rest of itself.
    expect(box.querySelector('.archive-truncated')).not.toBeNull();
    us.querySelector<HTMLButtonElement>('button.archive-show-folder')!.click();
    expect(shown).toEqual(['gNATSGO/Geopackages/US/']);
  });

  it('loads, narrows to a folder and back, and reads metadata in place', async () => {
    const log: string[] = [];
    const host = document.createElement('div');
    host.append(archiveBrowser({ api: fakeApi(log), docId: DOC }));
    await until(() => host.querySelector('.archive-summary') !== null);
    expect(host.querySelector('.archive')!.hasAttribute('aria-busy')).toBe(false);
    host.querySelector<HTMLButtonElement>('button.archive-show-folder')!.click();
    await until(() => log.length === 2);
    expect(log[1]).toBe('archive gNATSGO/Geopackages/US/');
    await until(() => host.querySelector('button.archive-all') !== null);
    expect(host.querySelector('h2')!.textContent).toBe('In the folder gNATSGO/Geopackages/US/');
    host.querySelector<HTMLButtonElement>('button.archive-all')!.click();
    await until(() => log.length === 3 && host.querySelector('.archive-first') !== null);

    const read = host.querySelector<HTMLButtonElement>('.archive-first button.archive-read')!;
    read.click();
    await until(() => host.querySelector('.archive-metadata') !== null);
    expect(log.at(-1)).toBe('view gNATSGO/Geopackages/ssurgo_iso19115_3.xml');
    expect(read.textContent).toBe('Hide');
    expect(read.getAttribute('aria-expanded')).toBe('true');
    const meta = host.querySelector('.archive-metadata')!;
    expect(meta.textContent).toContain('Metadata (ISO 19115-3)');
    expect(meta.querySelector('h3')!.textContent).toBe('Soil Survey Geographic Database (SSURGO)');
    const dts = [...meta.querySelectorAll('dt')].map((d) => d.textContent);
    expect(dts).toEqual(['Citation › Title', 'Metadata date']); // a bare value takes its heading
    expect(meta.textContent).toContain('2 more fields not shown.');
    expect(meta.querySelector('.archive-dictionary summary')!.textContent).toBe(
      'Data dictionary: 1 table',
    );
    expect(meta.querySelector('.archive-columns')!.textContent).toContain('and 15 more');
    // Hidden and shown again without asking the library twice.
    read.click();
    read.click();
    expect(log.filter((l) => l.startsWith('view')).length).toBe(1);
  });

  it('says what went wrong and tries again when asked again', async () => {
    const log: string[] = [];
    const host = document.createElement('div');
    const busy = new ApiRequestError(503, 'busy', 'busy');
    host.append(archiveBrowser({ api: fakeApi(log, busy), docId: DOC }));
    await until(() => host.querySelector('.archive-first') !== null);
    const read = host.querySelector<HTMLButtonElement>('.archive-first button.archive-read')!;
    read.click();
    await until(() => host.querySelector('.archive-first .notice') !== null);
    expect(host.querySelector('.archive-first .notice')!.textContent).toContain('busy');
    read.click();
    read.click();
    await until(() => log.filter((l) => l.startsWith('view')).length === 2);
  });

  it('shows a listing it cannot read as a notice', async () => {
    const host = document.createElement('div');
    const api: ArchiveApi = {
      archive: async () => {
        throw new ApiRequestError(413, 'archive_too_large', 'the archive holds 300,000 files');
      },
      archiveView: async () => META,
      url: (p) => p,
    };
    host.append(archiveBrowser({ api, docId: DOC }));
    await until(() => host.querySelector('.notice') !== null);
    expect(host.textContent).toContain('the archive holds 300,000 files');
  });
});

describe('reading a file in an archive', () => {
  it('shows a CSV as a table and says when it is only the start', () => {
    const el = renderView({
      path: 'a/t.csv',
      name: 't.csv',
      size: 9_000_000,
      text: 'mukey,name\n1,"Loam, silty"\n2,"say ""hi"""\n3,partial row',
      truncated: true,
      metadata: null,
    });
    const cells = [...el.querySelectorAll('tbody tr')].map((r) =>
      [...r.querySelectorAll('td')].map((c) => c.textContent),
    );
    expect(cells).toEqual([
      ['1', 'Loam, silty'],
      ['2', 'say "hi"'],
    ]); // the cut-off last row is left out
    expect(el.textContent).toContain('Only the start of the file is shown');
  });

  it('shows other text as it is', () => {
    const el = renderView({
      path: 'r.txt',
      name: 'r.txt',
      size: 10,
      text: '<b>not html</b>',
      truncated: false,
      metadata: null,
    });
    expect(el.querySelector('pre')!.textContent).toBe('<b>not html</b>');
    expect(el.querySelector('b')).toBeNull();
  });

  it('parses delimited text with quotes, CRLF and a limit', () => {
    expect(parseDelimited('a\tb\r\n"x\ny"\tz\r\n', '\t', 10)).toEqual([
      ['a', 'b'],
      ['x\ny', 'z'],
    ]);
    expect(parseDelimited('a\nb\nc\n', ',', 2)).toEqual([['a'], ['b']]);
    expect(parseDelimited('a,b', ',', 5)).toEqual([['a', 'b']]);
  });
});

describe('original file panel for an archive', () => {
  it('browses the archive in place instead of saying the browser cannot show it', async () => {
    const log: string[] = [];
    const el = originalPanel(
      {
        title: 'gNATSGO',
        kind: 'other',
        original: {
          url: '/v1/files/x',
          name: 'GNATSGO.zip',
          media_type: 'application/zip',
          inline: false,
          preview_url: null,
          archive_url: '/v1/files/x/archive',
        },
      },
      (p) => p,
      undefined,
      { api: fakeApi(log), docId: DOC },
    )!;
    expect(el.querySelector('section.archive')).not.toBeNull();
    expect(el.textContent).not.toContain('cannot show');
    expect(el.querySelector('.original-text-heading')!.textContent).toBe(
      'Text the library read from the archive',
    );
    expect(el.querySelector('a[download="GNATSGO.zip"]')).not.toBeNull();
    await until(() => log.length === 1);
  });
});
