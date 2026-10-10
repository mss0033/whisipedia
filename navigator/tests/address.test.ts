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

  it('accepts doc_id form, aliases, case and trailing slashes', () => {
    expect(normalizeAddress('wsp:rfc/9110')).toBe('wsp://rfc/9110');
    expect(normalizeAddress('WSP://Wikipedia.en/Alan_Turing/')).toBe(
      'wsp://wikipedia-en/Alan_Turing',
    );
    expect(normalizeAddress('wsp://')).toBe('wsp://home');
    expect(normalizeAddress('wsp://maps/relation/1')).toBe('wsp://osm-planet/relation/1');
  });

  it('keeps native ids with slashes and encodes only what breaks parsing', () => {
    const a = makeAddress('wikipedia-en', 'AC/DC? 100%');
    expect(a).toBe('wsp://wikipedia-en/AC/DC%3F%20100%25');
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
