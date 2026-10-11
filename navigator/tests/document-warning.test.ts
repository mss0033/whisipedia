import { describe, expect, it } from 'vitest';
import { documentWarning } from '../src/pages/document.ts';

describe('document warning banner (PLAN D13)', () => {
  it('shows the server banner for a historical document', () => {
    const el = documentWarning({
      banner: 'Historical document — methods may be outdated or unsafe.',
      currency: 'historical',
    });
    expect(el).not.toBeNull();
    expect(el!.getAttribute('role')).toBe('note');
    expect(el!.className).toContain('doc-warning-historical');
    expect(el!.textContent).toContain('methods may be outdated or unsafe');
  });

  it('renders nothing for a current document', () => {
    expect(documentWarning({ banner: null, currency: 'current' })).toBeNull();
    expect(documentWarning({ banner: null, currency: null })).toBeNull();
  });
});
