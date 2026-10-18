import {
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  DestroyRef,
  ElementRef,
  HostListener,
  OnInit,
  effect,
  inject,
  input,
  model,
  untracked,
} from '@angular/core';
import { NgTemplateOutlet } from '@angular/common';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { Subject, debounceTime, distinctUntilChanged, switchMap, tap } from 'rxjs';
import { FontAwesomeModule } from '@fortawesome/angular-fontawesome';
import { faMagnifyingGlass, faPlus, faXmark } from '@fortawesome/free-solid-svg-icons';
import {
  TwitchCategoriesService,
  TwitchCategory,
  categoryKey as key,
} from '../services/twitch-categories.service';

/**
 * Search box for picking the stream categories a live subscription records.
 * Focusing it opens a panel: the selected categories first (each removable),
 * then Twitch's categories — the most-watched ones, or the search results —
 * with their box art. A typed name that Twitch doesn't know (e.g. a Kick
 * category) can still be added as is.
 */
@Component({
  selector: 'app-category-picker',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [FontAwesomeModule, NgTemplateOutlet],
  template: `
    <div class="input-group">
      <span class="input-group-text"><fa-icon [icon]="faMagnifyingGlass" /></span>
      <input
        type="text"
        class="form-control"
        [placeholder]="placeholder()"
        [value]="query"
        [disabled]="disabled()"
        (focus)="open()"
        (click)="open()"
        (input)="onQuery($any($event.target).value)"
        (keydown)="onKeydown($event)"
        [attr.aria-label]="placeholder()"
        aria-autocomplete="list"
        [attr.aria-expanded]="isOpen"
      />
    </div>
    @if (!isOpen && categories().length) {
      <div class="d-flex flex-wrap gap-1 mt-1">
        @for (name of categories(); track name) {
          <span class="badge rounded-pill text-bg-light border d-inline-flex align-items-center gap-1">
            @if (thumbnail(name); as art) {
              <img [src]="art" alt="" class="picker-chip-art" loading="lazy" />
            }
            {{ label(name) }}
            <button
              type="button"
              class="btn-close picker-chip-remove"
              [disabled]="disabled()"
              (click)="remove(name)"
              [attr.aria-label]="'Remove ' + name"
            ></button>
          </span>
        }
      </div>
    }
    @if (isOpen) {
      <div class="picker-panel border rounded mt-1">
        @if (categories().length) {
          <div class="picker-heading">Selected ({{ categories().length }})</div>
          @for (name of categories(); track name) {
            <div class="picker-row picker-row-selected">
              <ng-container
                *ngTemplateOutlet="art; context: { $implicit: thumbnail(name) }"
              ></ng-container>
              <span class="flex-grow-1 text-break">{{ label(name) }}</span>
              <button
                type="button"
                class="btn btn-sm btn-link text-danger p-0"
                (click)="remove(name)"
                [attr.aria-label]="'Remove ' + name"
              >
                <fa-icon [icon]="faXmark" />
              </button>
            </div>
          }
        }
        <div class="picker-heading">{{ query.trim() ? 'Twitch results' : 'Popular on Twitch' }}</div>
        @if (loading) {
          <div class="picker-note">
            <span class="spinner-border spinner-border-sm me-1" role="status" aria-hidden="true"></span>
            Loading…
          </div>
        } @else if (error) {
          <div class="picker-note text-danger">{{ error }}</div>
        } @else if (available().length === 0) {
          <div class="picker-note">No other categories found.</div>
        }
        @for (cat of available(); track cat.name) {
          <button type="button" class="picker-row picker-row-action" (click)="add(cat.name)">
            <ng-container
              *ngTemplateOutlet="art; context: { $implicit: cat.box_art_url }"
            ></ng-container>
            <span class="flex-grow-1 text-break">{{ cat.display_name }}</span>
            @if (cat.viewers !== null) {
              <span class="text-body-secondary small text-nowrap">{{ viewerLabel(cat.viewers) }}</span>
            }
          </button>
        }
        @if (customCandidate(); as custom) {
          <button type="button" class="picker-row picker-row-action" (click)="add(custom)">
            <span class="picker-art picker-art-empty"><fa-icon [icon]="faPlus" /></span>
            <span class="flex-grow-1 text-break">Add "{{ custom }}" as a custom category</span>
          </button>
        }
        <div class="d-flex justify-content-end p-1 border-top">
          <button type="button" class="btn btn-sm btn-outline-secondary" (click)="close()">
            Done
          </button>
        </div>
      </div>
    }
    <ng-template #art let-url>
      @if (url) {
        <img [src]="url" alt="" class="picker-art" loading="lazy" />
      } @else {
        <span class="picker-art picker-art-empty"></span>
      }
    </ng-template>
  `,
  styles: `
    :host {
      display: block;
    }
    .picker-panel {
      max-height: 22rem;
      overflow-y: auto;
      background: var(--bs-body-bg);
    }
    .picker-heading {
      position: sticky;
      top: 0;
      z-index: 1;
      padding: 0.25rem 0.5rem;
      font-size: 0.75rem;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      color: var(--bs-secondary-color);
      background: var(--bs-tertiary-bg);
    }
    .picker-row {
      display: flex;
      align-items: center;
      gap: 0.5rem;
      width: 100%;
      padding: 0.25rem 0.5rem;
      border: 0;
      background: transparent;
      color: inherit;
      text-align: left;
    }
    .picker-row-action:hover,
    .picker-row-action:focus-visible {
      background: var(--bs-secondary-bg);
    }
    .picker-row-selected {
      background: var(--bs-primary-bg-subtle);
    }
    .picker-art {
      flex: none;
      width: 1.5rem;
      height: 2rem;
      object-fit: cover;
      border-radius: 0.125rem;
    }
    .picker-art-empty {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      background: var(--bs-secondary-bg);
      font-size: 0.7rem;
    }
    .picker-note {
      padding: 0.5rem;
      font-size: 0.875rem;
      color: var(--bs-secondary-color);
    }
    .picker-chip-art {
      width: 0.9rem;
      height: 1.2rem;
      object-fit: cover;
      border-radius: 0.1rem;
    }
    .picker-chip-remove {
      width: 0.5em;
      height: 0.5em;
      padding: 0.25em;
    }
  `,
})
export class CategoryPickerComponent implements OnInit {
  private svc = inject(TwitchCategoriesService);
  private cdr = inject(ChangeDetectorRef);
  private destroyRef = inject(DestroyRef);
  private host = inject(ElementRef<HTMLElement>);

  /** The selected category names, in the order they were added. */
  categories = model<string[]>([]);
  disabled = input(false);
  placeholder = input('Search Twitch categories');

  readonly faMagnifyingGlass = faMagnifyingGlass;
  readonly faPlus = faPlus;
  readonly faXmark = faXmark;

  isOpen = false;
  query = '';
  results: TwitchCategory[] = [];
  loading = false;
  error = '';

  private query$ = new Subject<string>();

  constructor() {
    effect(() => {
      const names = this.categories();
      untracked(() => this.svc.ensure(names));
    });
  }

  ngOnInit() {
    this.svc.changed
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe(() => this.cdr.markForCheck());
    this.query$
      .pipe(
        debounceTime(250),
        distinctUntilChanged(),
        tap(() => {
          this.loading = true;
          this.error = '';
          this.cdr.markForCheck();
        }),
        switchMap((q) => this.svc.search(q)),
        takeUntilDestroyed(this.destroyRef),
      )
      .subscribe((res) => {
        this.loading = false;
        this.error = res.error ?? '';
        this.results = res.categories;
        this.svc.remember(res.categories);
        this.cdr.markForCheck();
      });
  }

  @HostListener('document:mousedown', ['$event'])
  onDocumentMouseDown(event: MouseEvent) {
    if (this.isOpen && !this.host.nativeElement.contains(event.target as Node)) {
      this.close();
    }
  }

  open() {
    if (this.isOpen || this.disabled()) {
      return;
    }
    this.isOpen = true;
    this.query$.next(this.query.trim());
    this.cdr.markForCheck();
  }

  close() {
    this.isOpen = false;
    if (this.query) {
      this.query = '';
      this.query$.next('');
    }
    this.cdr.markForCheck();
  }

  onQuery(value: string) {
    this.query = value;
    this.isOpen = true;
    this.query$.next(value.trim());
    this.cdr.markForCheck();
  }

  onKeydown(event: KeyboardEvent) {
    if (event.key === 'Escape') {
      this.close();
    } else if (event.key === 'Enter') {
      // Never let Enter submit the surrounding download form.
      event.preventDefault();
      const first = this.available()[0];
      const pick = this.query.trim() ? (first?.name ?? this.customCandidate()) : null;
      if (pick) {
        this.add(pick);
      }
    }
  }

  add(name: string) {
    const trimmed = name.trim();
    if (!trimmed || this.isSelected(trimmed)) {
      return;
    }
    this.categories.set([...this.categories(), trimmed]);
    this.query = '';
    this.query$.next('');
    this.cdr.markForCheck();
  }

  remove(name: string) {
    this.categories.set(this.categories().filter((c) => key(c) !== key(name)));
    this.cdr.markForCheck();
  }

  isSelected(name: string): boolean {
    const k = key(name);
    return this.categories().some((c) => key(c) === k);
  }

  /** Twitch categories not already selected. */
  available(): TwitchCategory[] {
    return this.results.filter((cat) => !this.isSelected(cat.name));
  }

  /** The typed text, when adding it as a custom category makes sense. */
  customCandidate(): string | null {
    const q = this.query.trim();
    if (!q || this.isSelected(q) || this.results.some((cat) => key(cat.name) === key(q))) {
      return null;
    }
    return q;
  }

  thumbnail(name: string): string | null {
    return this.svc.get(name)?.box_art_url ?? null;
  }

  label(name: string): string {
    return this.svc.get(name)?.display_name ?? name;
  }

  viewerLabel(viewers: number): string {
    if (viewers >= 1_000_000) {
      return `${(viewers / 1_000_000).toFixed(1)}M viewers`;
    }
    if (viewers >= 1_000) {
      return `${(viewers / 1_000).toFixed(1)}K viewers`;
    }
    return `${viewers} viewers`;
  }
}
