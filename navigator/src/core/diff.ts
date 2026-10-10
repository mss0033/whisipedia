/**
 * Line diff for the time machine. Plain LCS over lines after trimming the
 * common prefix/suffix; bounded so a huge document cannot freeze the UI.
 */

export type DiffOp = { op: 'eq' | 'add' | 'del'; text: string };

export const MAX_DIFF_CELLS = 4_000_000;

export function diffLines(oldText: string, newText: string): DiffOp[] | null {
  const a = oldText.split('\n');
  const b = newText.split('\n');
  let start = 0;
  while (start < a.length && start < b.length && a[start] === b[start]) start += 1;
  let endA = a.length;
  let endB = b.length;
  while (endA > start && endB > start && a[endA - 1] === b[endB - 1]) {
    endA -= 1;
    endB -= 1;
  }
  const midA = a.slice(start, endA);
  const midB = b.slice(start, endB);
  const n = midA.length;
  const m = midB.length;
  if (n * m > MAX_DIFF_CELLS) return null;

  // lcs[i][j] = LCS length of midA[i..] and midB[j..], flattened
  const w = m + 1;
  const lcs = new Uint32Array((n + 1) * w);
  for (let i = n - 1; i >= 0; i -= 1) {
    for (let j = m - 1; j >= 0; j -= 1) {
      lcs[i * w + j] =
        midA[i] === midB[j]
          ? lcs[(i + 1) * w + j + 1]! + 1
          : Math.max(lcs[(i + 1) * w + j]!, lcs[i * w + j + 1]!);
    }
  }
  const out: DiffOp[] = a.slice(0, start).map((text) => ({ op: 'eq', text }));
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (midA[i] === midB[j]) {
      out.push({ op: 'eq', text: midA[i]! });
      i += 1;
      j += 1;
    } else if (lcs[(i + 1) * w + j]! >= lcs[i * w + j + 1]!) {
      out.push({ op: 'del', text: midA[i]! });
      i += 1;
    } else {
      out.push({ op: 'add', text: midB[j]! });
      j += 1;
    }
  }
  while (i < n) out.push({ op: 'del', text: midA[i++]! });
  while (j < m) out.push({ op: 'add', text: midB[j++]! });
  for (const text of a.slice(endA)) out.push({ op: 'eq', text });
  return out;
}

export function diffStats(ops: DiffOp[]): { added: number; removed: number } {
  let added = 0;
  let removed = 0;
  for (const o of ops) {
    if (o.op === 'add') added += 1;
    else if (o.op === 'del') removed += 1;
  }
  return { added, removed };
}
