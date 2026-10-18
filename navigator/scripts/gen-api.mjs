#!/usr/bin/env node
/**
 * Generate src/api/generated.ts from docs/api/openapi.json (the API contract).
 *
 *   node scripts/gen-api.mjs           # write src/api/generated.ts
 *   node scripts/gen-api.mjs --check   # exit 1 if the committed file is stale
 *
 * Dependency-free on purpose: openapi-typescript needs the TypeScript 5
 * compiler API, and this project builds with TypeScript 7. The spec is
 * produced by pydantic/FastAPI and uses a small JSON Schema subset ($ref,
 * type, enum, const, anyOf/oneOf, items/prefixItems, properties/required,
 * additionalProperties); anything else fails loudly rather than silently
 * becoming `unknown`.
 */
import { readFileSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const SPEC = resolve(here, '../../docs/api/openapi.json');
const OUT = resolve(here, '../src/api/generated.ts');

const IDENT = /^[A-Za-z_$][\w$]*$/;
const KNOWN = new Set([
  '$ref',
  'type',
  'enum',
  'const',
  'anyOf',
  'oneOf',
  'discriminator',
  'items',
  'prefixItems',
  'properties',
  'required',
  'additionalProperties',
  'title',
  'description',
  'default',
  'format',
  'minLength',
  'maxLength',
  'pattern', // validation only: no TypeScript shape
  'minItems',
  'maxItems',
  'minimum',
  'maximum',
  'exclusiveMinimum',
  'exclusiveMaximum',
]);

const lit = (v) =>
  typeof v === 'string' ? `'${v.replace(/\\/g, '\\\\').replace(/'/g, "\\'")}'` : JSON.stringify(v);
const key = (k) => (IDENT.test(k) ? k : lit(k));
const union = (parts) => [...new Set(parts)].join(' | ');

function doc(text, indent) {
  if (!text) return '';
  const lines = text.trim().replace(/\*\//g, '*\\/').split('\n');
  if (lines.length === 1) return `${indent}/** ${lines[0]} */\n`;
  return `${indent}/**\n${lines.map((l) => `${indent} *${l ? ` ${l}` : ''}`).join('\n')}\n${indent} */\n`;
}

/** TypeScript type expression for a schema. */
function ts(s, indent = '') {
  if (s === true || s === undefined) return 'unknown';
  for (const k of Object.keys(s)) {
    if (!KNOWN.has(k)) throw new Error(`gen-api: unsupported JSON Schema keyword "${k}"`);
  }
  if (s.$ref) return s.$ref.replace('#/components/schemas/', '');
  if ('const' in s) return lit(s.const);
  if (s.enum) return union(s.enum.map(lit));
  if (s.anyOf || s.oneOf) return union((s.anyOf ?? s.oneOf).map((x) => ts(x, indent)));
  switch (s.type) {
    case undefined:
      return 'unknown';
    case 'null':
      return 'null';
    case 'boolean':
      return 'boolean';
    case 'integer':
    case 'number':
      return 'number';
    case 'string':
      return s.format === 'binary' ? 'Blob' : 'string';
    case 'array':
      if (s.prefixItems) return `[${s.prefixItems.map((x) => ts(x, indent)).join(', ')}]`;
      return `Array<${ts(s.items, indent)}>`;
    case 'object': {
      if (!s.properties) {
        const v = s.additionalProperties;
        return `Record<string, ${v === undefined || v === true ? 'unknown' : ts(v, indent)}>`;
      }
      return objectBody(s, indent);
    }
    default:
      throw new Error(`gen-api: unsupported type ${JSON.stringify(s.type)}`);
  }
}

function objectBody(s, indent) {
  const req = new Set(s.required ?? []);
  const inner = `${indent}  `;
  const fields = Object.entries(s.properties).map(
    ([k, v]) =>
      `${doc(v.description, inner)}${inner}${key(k)}${req.has(k) ? '' : '?'}: ${ts(v, inner)};`,
  );
  return `{\n${fields.join('\n')}\n${indent}}`;
}

function schemaDecl(name, s) {
  const head = doc(s.description, '');
  if (s.type === 'object' && s.properties)
    return `${head}export interface ${name} ${objectBody(s, '')}\n`;
  return `${head}export type ${name} = ${ts(s)};\n`;
}

function params(op, where) {
  const ps = (op.parameters ?? []).filter((p) => p.in === where);
  if (ps.length === 0) return 'Record<string, never>';
  // Optional parameters also accept `undefined` (omitted from the URL), for exactOptionalPropertyTypes.
  const fields = ps.map(
    (p) =>
      `${doc(p.description, '      ')}      ${key(p.name)}${p.required ? '' : '?'}: ${ts(p.schema, '      ')}${p.required ? '' : ' | undefined'};`,
  );
  return `{\n${fields.join('\n')}\n    }`;
}

function response(op) {
  const ok = op.responses['200'] ?? op.responses['204'];
  if (!ok?.content) return 'null';
  const [media, body] = Object.entries(ok.content)[0];
  const t = ts(body.schema ?? {});
  return media === 'text/event-stream' ? `EventStream<${t}>` : t;
}

export function generate(spec) {
  const out = [
    '/**',
    ` * Whisipedia API v${spec.info.version}: types generated from docs/api/openapi.json`,
    ' * by scripts/gen-api.mjs. Do not edit; run `npm run gen:api`.',
    ' */',
    '',
    `export const API_VERSION = ${lit(spec.info.version)};`,
    '',
    '/** Marks a `text/event-stream` response; `T` is the JSON in each `data:` line. */',
    'export type EventStream<T> = { readonly __events: T };',
    '',
  ];
  for (const [name, s] of Object.entries(spec.components.schemas)) out.push(schemaDecl(name, s));

  out.push(
    '/** Every operation: `"METHOD /path"` → path params, query params, body and 200 response. */',
  );
  out.push('export interface Routes {');
  for (const [path, ops] of Object.entries(spec.paths)) {
    for (const [method, op] of Object.entries(ops)) {
      const body = op.requestBody?.content?.['application/json']?.schema;
      out.push(`${doc(op.summary, '  ')}  ${lit(`${method.toUpperCase()} ${path}`)}: {`);
      out.push(`    path: ${params(op, 'path')};`);
      out.push(`    query: ${params(op, 'query')};`);
      out.push(`    body: ${body ? ts(body, '    ') : 'null'};`);
      out.push(`    response: ${response(op)};`);
      out.push('  };');
    }
  }
  out.push('}', '');
  return out.join('\n');
}

const spec = JSON.parse(readFileSync(SPEC, 'utf8'));
const text = generate(spec);
if (process.argv.includes('--check')) {
  let current = '';
  try {
    current = readFileSync(OUT, 'utf8');
  } catch {
    /* missing counts as stale */
  }
  if (current !== text) {
    console.error('src/api/generated.ts is stale: run `npm run gen:api` and commit the result.');
    process.exit(1);
  }
  console.log('API types are up to date with docs/api/openapi.json.');
} else {
  writeFileSync(OUT, text);
  console.log(`Wrote ${OUT}`);
}
