import { describe, expect, it } from 'vitest';
import {
  addressToDocId,
  docIdToAddress,
  formatAddress,
  hashToAddress,
  interpretInput,
  makeAddress,
  normalizeAddress,
  parseAddress,
  shortcutFor,
} from '../src/core/address.ts';

describe('parseAddress / formatAddress', () => {
  it('parses host, path, params and fragment', () => {
    expect(parseAddress('wsp://rfc/9110?at=2024-01-01#section-1')).toEqual({
      host: 'rfc',
      path: '9110',
      params: { at: '2024-01-01' },
      fragment: 'section-1',
    });
  });

  it('round-trips native ids with a trailing slash or a question mark', () => {
    for (const id of [
      'wsp:kiwix-xkcd-com-en-all/xkcd.com/936/',
      'wsp:kiwix-gotquestions-org-en-all/printer.gotquestions.net/GeneratePF?articleId=3547',
      'wsp:s/a b#c%d//',
    ]) {
      const address = docIdToAddress(id);
      expect(addressToDocId(parseAddress(address)!), address).toBe(id);
    }
    expect(docIdToAddress('wsp:kiwix-xkcd-com-en-all/xkcd.com/936/')).toBe(
      'wsp://kiwix-xkcd-com-en-all/xkcd.com/936%2F',
    );
  });

  it('accepts doc_id form, aliases, case and trailing slashes', () => {
    expect(normalizeAddress('wsp:rfc/9110')).toBe('wsp://rfc/9110');
    expect(normalizeAddress('WSP://Wikipedia.en/Alan_Turing/')).toBe(
      'wsp://wikipedia-en-zim/Alan_Turing',
    );
    expect(normalizeAddress('wsp://')).toBe('wsp://home');
    expect(normalizeAddress('wsp://osm/relation/1')).toBe('wsp://osm-planet/relation/1');
    expect(normalizeAddress('wsp://maps/@1,2,3z')).toBe('wsp://atlas/@1,2,3z');
  });

  it('keeps native ids with slashes and encodes only what breaks parsing', () => {
    const a = makeAddress('wikipedia-en-zim', 'AC/DC? 100%');
    expect(a).toBe('wsp://wikipedia-en-zim/AC/DC%3F%20100%25');
    expect(parseAddress(a)?.path).toBe('AC/DC? 100%');
  });

  it('rejects non-wsp and malformed hosts', () => {
    expect(parseAddress('http://example.com')).toBeNull();
    expect(parseAddress('wsp://bad host/x')).toBeNull();
  });

  it('round-trips', () => {
    for (const s of ['wsp://home', 'wsp://search?q=a+b&page=2', 'wsp://gutenberg/84']) {
      expect(formatAddress(parseAddress(s)!)).toBe(s);
    }
  });
});

describe('doc_id mapping', () => {
  it('maps both ways', () => {
    expect(docIdToAddress('wsp:stackexchange/webmasters/1007')).toBe(
      'wsp://stackexchange/webmasters/1007',
    );
    expect(addressToDocId(parseAddress('wsp://rfc/9110')!)).toBe('wsp:rfc/9110');
    expect(addressToDocId(parseAddress('wsp://search?q=x')!)).toBeNull();
    expect(addressToDocId(parseAddress('wsp://rfc')!)).toBeNull();
  });
});

describe('location bar input', () => {
  it('navigational shortcuts', () => {
    expect(shortcutFor('rfc 9110')).toBe('wsp://rfc/9110');
    expect(shortcutFor('RFC-0791')).toBe('wsp://rfc/791');
    expect(shortcutFor('gutenberg #84')).toBe('wsp://gutenberg/84');
    expect(shortcutFor('Q42')).toBe('wsp://wikidata/Q42');
    expect(shortcutFor('arxiv:2101.00001')).toBe('wsp://arxiv/2101.00001');
    expect(shortcutFor('rfc about caching')).toBeNull();
  });

  it('classifies addresses, searches and external URLs', () => {
    expect(interpretInput('wsp://rfc/9110')).toEqual({
      type: 'address',
      address: 'wsp://rfc/9110',
    });
    expect(interpretInput('how do vaccines work')).toEqual({
      type: 'search',
      query: 'how do vaccines work',
    });
    expect(interpretInput('https://example.com/a')?.type).toBe('external');
    expect(interpretInput('www.example.com')?.type).toBe('external');
    expect(interpretInput('about:')).toEqual({ type: 'address', address: 'wsp://about' });
    expect(interpretInput('   ')).toBeNull();
  });

  it('reads addresses back from the URL fragment', () => {
    expect(hashToAddress('#wsp://rfc/9110')).toBe('wsp://rfc/9110');
    expect(hashToAddress('#wsp://search?q=a%20b')).toBe('wsp://search?q=a+b');
    expect(hashToAddress('')).toBeNull();
  });
});

describe('shortcuts and autolinks follow the readable sites (audit 2026-10-12 F20)', () => {
  it('searches instead of jumping into a site the library does not hold', async () => {
    const { interpretInput, setReadableHosts } = await import('../src/core/address.ts');
    const { linkify } = await import('../src/core/text.ts');
    try {
      setReadableHosts(['gutenberg']);
      expect(interpretInput('rfc 9110')).toEqual({ type: 'search', query: 'rfc 9110' });
      expect(interpretInput('pg 84')).toEqual({ type: 'address', address: 'wsp://gutenberg/84' });
      expect(linkify('see RFC 9110').querySelector('a')).toBeNull();
      setReadableHosts(['rfc']);
      expect(linkify('see RFC 9110').querySelector('a')?.dataset.wsp).toBe('wsp://rfc/9110');
    } finally {
      setReadableHosts(null);
    }
    expect(interpretInput('rfc 9110')).toEqual({ type: 'address', address: 'wsp://rfc/9110' });
  });
});
