/**
 * Menu bar (File/Edit/View/Go/Bookmarks/Options/Help) following the WAI-ARIA
 * menubar pattern: roving tabindex, arrow keys, Escape, typeahead by first
 * letter, mouse hover switching once a menu is open.
 */
import { h } from '../core/dom.ts';

export type MenuItem =
  | { type: 'item'; label: string; shortcut?: string; disabled?: boolean; action: () => void }
  | { type: 'check'; label: string; checked: boolean; shortcut?: string; action: () => void }
  | { type: 'radio'; label: string; checked: boolean; action: () => void }
  | { type: 'header'; label: string }
  | { type: 'sep' };

export interface MenuDef {
  label: string;
  /** Built at open time so dynamic menus (Go, Bookmarks) stay current. */
  items: () => MenuItem[];
}

export class MenuBar {
  readonly el: HTMLElement;
  private readonly tops: HTMLButtonElement[] = [];
  private readonly popups: HTMLUListElement[] = [];
  private openIndex = -1;
  private readonly defs: MenuDef[];
  private onClose: (() => void) | undefined;

  constructor(defs: MenuDef[], onClose?: () => void) {
    this.defs = defs;
    this.onClose = onClose;
    const ul = h('ul', { class: 'menubar', role: 'menubar', 'aria-label': 'Main menu' });
    defs.forEach((def, i) => {
      const id = `menu-${def.label.toLowerCase()}`;
      const btn = h(
        'button',
        {
          type: 'button',
          class: 'menubar-item',
          role: 'menuitem',
          id: `${id}-btn`,
          'aria-haspopup': 'menu',
          'aria-expanded': 'false',
          'aria-controls': id,
          tabindex: i === 0 ? '0' : '-1',
        },
        h('span', { class: 'mnemonic' }, def.label[0]!),
        def.label.slice(1),
      );
      const pop = h('ul', {
        class: 'menu',
        role: 'menu',
        id,
        'aria-labelledby': `${id}-btn`,
        hidden: true,
      });
      btn.addEventListener('click', () =>
        this.openIndex === i ? this.close(true) : this.open(i, false),
      );
      btn.addEventListener('mouseenter', () => {
        if (this.openIndex !== -1 && this.openIndex !== i) this.open(i, false);
      });
      btn.addEventListener('keydown', (ev) => this.onTopKey(ev, i));
      pop.addEventListener('keydown', (ev) => this.onMenuKey(ev, i));
      this.tops.push(btn);
      this.popups.push(pop);
      ul.append(h('li', { role: 'none', class: 'menubar-li' }, btn, pop));
    });
    this.el = h('nav', { class: 'menubar-nav', 'aria-label': 'Menu bar' }, ul);
    document.addEventListener('pointerdown', (ev) => {
      if (this.openIndex !== -1 && !this.el.contains(ev.target as Node)) this.close(false);
    });
  }

  get isOpen(): boolean {
    return this.openIndex !== -1;
  }

  focusFirst(): void {
    this.tops[0]?.focus();
  }

  private items(i: number): HTMLElement[] {
    return Array.from(
      this.popups[i]!.querySelectorAll<HTMLElement>(
        '[role^="menuitem"]:not([aria-disabled="true"])',
      ),
    );
  }

  open(i: number, focusFirst: boolean): void {
    if (this.openIndex !== -1 && this.openIndex !== i) this.hide(this.openIndex);
    this.openIndex = i;
    this.build(i);
    this.popups[i]!.hidden = false;
    this.tops[i]!.setAttribute('aria-expanded', 'true');
    this.setRoving(i);
    if (focusFirst) this.items(i)[0]?.focus();
    else this.tops[i]!.focus();
  }

  private hide(i: number): void {
    this.popups[i]!.hidden = true;
    this.tops[i]!.setAttribute('aria-expanded', 'false');
  }

  close(refocusTop: boolean): void {
    if (this.openIndex === -1) return;
    const i = this.openIndex;
    this.hide(i);
    this.openIndex = -1;
    if (refocusTop) this.tops[i]!.focus();
    else this.onClose?.();
  }

  private setRoving(i: number): void {
    this.tops.forEach((t, j) => {
      t.setAttribute('tabindex', j === i ? '0' : '-1');
    });
  }

  private build(i: number): void {
    const pop = this.popups[i]!;
    const items = this.defs[i]!.items();
    pop.replaceChildren(
      ...items.map((it) => {
        if (it.type === 'sep') return h('li', { role: 'separator', class: 'menu-sep' });
        if (it.type === 'header')
          return h('li', { role: 'presentation', class: 'menu-header' }, it.label);
        const role =
          it.type === 'item'
            ? 'menuitem'
            : it.type === 'check'
              ? 'menuitemcheckbox'
              : 'menuitemradio';
        const disabled = it.type === 'item' && it.disabled;
        const li = h(
          'li',
          {
            role,
            class: `menu-item ${disabled ? 'is-disabled' : ''}`,
            tabindex: '-1',
            'aria-disabled': disabled ? 'true' : undefined,
            'aria-checked': it.type === 'item' ? undefined : String(it.checked),
            'aria-keyshortcuts':
              'shortcut' in it && it.shortcut ? it.shortcut.replace(/Ctrl/g, 'Control') : undefined,
          },
          h(
            'span',
            { class: 'menu-check', 'aria-hidden': 'true' },
            it.type === 'check'
              ? it.checked
                ? '✓'
                : ''
              : it.type === 'radio'
                ? it.checked
                  ? '•'
                  : ''
                : '',
          ),
          h('span', { class: 'menu-label' }, it.label),
          'shortcut' in it && it.shortcut
            ? h('span', { class: 'menu-shortcut', 'aria-hidden': 'true' }, it.shortcut)
            : null,
        );
        if (!disabled) {
          li.addEventListener('click', () => {
            this.close(false);
            it.action();
          });
          li.addEventListener('mouseenter', () => li.focus());
        }
        return li;
      }),
    );
  }

  private onTopKey(ev: KeyboardEvent, i: number): void {
    const n = this.tops.length;
    switch (ev.key) {
      case 'ArrowRight':
      case 'ArrowLeft': {
        ev.preventDefault();
        const j = (i + (ev.key === 'ArrowRight' ? 1 : -1) + n) % n;
        if (this.openIndex !== -1) this.open(j, false);
        else {
          this.setRoving(j);
          this.tops[j]!.focus();
        }
        break;
      }
      case 'ArrowDown':
      case 'Enter':
      case ' ':
        ev.preventDefault();
        this.open(i, true);
        break;
      case 'ArrowUp': {
        ev.preventDefault();
        this.open(i, false);
        const items = this.items(i);
        items[items.length - 1]?.focus();
        break;
      }
      case 'Escape':
        ev.preventDefault();
        if (this.openIndex !== -1) this.close(true);
        else this.onClose?.();
        break;
      case 'Home':
        ev.preventDefault();
        this.tops[0]!.focus();
        this.setRoving(0);
        break;
      case 'End':
        ev.preventDefault();
        this.tops[n - 1]!.focus();
        this.setRoving(n - 1);
        break;
    }
  }

  private onMenuKey(ev: KeyboardEvent, i: number): void {
    const items = this.items(i);
    const cur = items.indexOf(document.activeElement as HTMLElement);
    const move = (j: number) => items[(j + items.length) % items.length]?.focus();
    switch (ev.key) {
      case 'ArrowDown':
        ev.preventDefault();
        move(cur + 1);
        break;
      case 'ArrowUp':
        ev.preventDefault();
        move(cur - 1);
        break;
      case 'Home':
        ev.preventDefault();
        move(0);
        break;
      case 'End':
        ev.preventDefault();
        move(items.length - 1);
        break;
      case 'ArrowRight':
      case 'ArrowLeft': {
        ev.preventDefault();
        const n = this.tops.length;
        this.open((i + (ev.key === 'ArrowRight' ? 1 : -1) + n) % n, true);
        break;
      }
      case 'Escape':
        ev.preventDefault();
        ev.stopPropagation();
        this.close(true);
        break;
      case 'Tab':
        this.close(false);
        break;
      case 'Enter':
      case ' ':
        ev.preventDefault();
        (document.activeElement as HTMLElement | null)?.click();
        break;
      default:
        if (ev.key.length === 1 && /\S/.test(ev.key)) {
          const k = ev.key.toLowerCase();
          const start = cur + 1;
          for (let off = 0; off < items.length; off += 1) {
            const it = items[(start + off) % items.length]!;
            if (it.querySelector('.menu-label')?.textContent?.toLowerCase().startsWith(k)) {
              it.focus();
              break;
            }
          }
        }
    }
  }
}
