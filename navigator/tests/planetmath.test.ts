// PlanetMath: link targets printed as text and NNexus guess clusters are removed; the term stays.
import { describe, expect, it } from 'vitest';
import { looksLikePlanetMath, planetMathView } from '../src/pages/planetmath.ts';

const PAGE = `<p class="ltx_p">Every contraction has a unique <span class="ltx_text ltx_font_typewriter">http://planetmath.org/node/2777</span><a class="nnexus_concepts" href="javascript:void(0)">fixed point</a><sup><a class="nnexus_concept" href="http://planetmath.org/closuremap"><img alt="Planetmath"></a></sup>. See <span class="ltx_text ltx_font_typewriter">ls -l</span>.</p>`;

describe('PlanetMath pages', () => {
  it('reads as prose', () => {
    expect(looksLikePlanetMath(PAGE)).toBe(true);
    expect(looksLikePlanetMath('<p>plain</p>')).toBe(false);
    const body = new DOMParser().parseFromString(PAGE, 'text/html').body;
    expect(planetMathView(body)).toBe(3);
    expect(body.textContent).toBe('Every contraction has a unique fixed point. See ls -l.');
    expect(body.querySelector('a')).toBeNull();
  });
});
