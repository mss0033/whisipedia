/** Formatting helpers (dates, sizes, numbers) in a fixed locale for stable output. */

const LOCALE = 'en-US';

const dateFmt = new Intl.DateTimeFormat(LOCALE, {
  year: 'numeric',
  month: 'short',
  day: 'numeric',
  timeZone: 'UTC',
});
const dateTimeFmt = new Intl.DateTimeFormat(LOCALE, {
  year: 'numeric',
  month: 'short',
  day: 'numeric',
  hour: '2-digit',
  minute: '2-digit',
  hour12: false,
  timeZone: 'UTC',
});
/** The reader's own clock: for their History, not for library dates (which are UTC). */
const localDateTimeFmt = new Intl.DateTimeFormat(LOCALE, {
  year: 'numeric',
  month: 'short',
  day: 'numeric',
  hour: '2-digit',
  minute: '2-digit',
  hour12: false,
});
const numFmt = new Intl.NumberFormat(LOCALE);

export function formatDate(ts: string | number): string {
  const d = new Date(ts);
  return Number.isNaN(d.getTime()) ? String(ts) : dateFmt.format(d);
}

export function formatDateTime(ts: string | number): string {
  const d = new Date(ts);
  return Number.isNaN(d.getTime()) ? String(ts) : `${dateTimeFmt.format(d)} UTC`;
}

const localDateFmt = new Intl.DateTimeFormat(LOCALE, {
  year: 'numeric',
  month: 'short',
  day: 'numeric',
});

/** A day on the reader's own clock: the History groups visits by the reader's days. */
export function formatLocalDate(ts: string | number): string {
  const d = new Date(ts);
  return Number.isNaN(d.getTime()) ? String(ts) : localDateFmt.format(d);
}

/** A moment on the reader's own clock ("Oct 12, 2026, 09:25"), e.g. when they visited a page. */
export function formatLocalDateTime(ts: string | number): string {
  const d = new Date(ts);
  return Number.isNaN(d.getTime()) ? String(ts) : localDateTimeFmt.format(d);
}

/** YYYY-MM-DD in UTC. */
export function isoDay(ts: string | number | Date): string {
  const d = ts instanceof Date ? ts : new Date(ts);
  return Number.isNaN(d.getTime()) ? '' : d.toISOString().slice(0, 10);
}

export function formatNumber(n: number): string {
  return numFmt.format(n);
}

export function formatBytes(n: number): string {
  if (!Number.isFinite(n) || n < 0) return '?';
  const units = ['bytes', 'KB', 'MB', 'GB', 'TB', 'PB'];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return i === 0 ? `${v} bytes` : `${v.toFixed(v < 10 ? 1 : 0)} ${units[i]}`;
}

export function formatDuration(seconds: number): string {
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (d > 0) return `${d}d ${h}h`;
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m ${Math.floor(seconds % 60)}s`;
}

/** Odometer-style counter: 42 -> "000042". */
export function odometer(n: number, digits = 6): string {
  return String(Math.max(0, Math.floor(n))).padStart(digits, '0');
}
