import {
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  DestroyRef,
  OnInit,
  effect,
  inject,
  input,
  untracked,
} from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { TwitchCategoriesService } from '../services/twitch-categories.service';

/** Read-only list of category names with their Twitch box art, where known. */
@Component({
  selector: 'app-category-chips',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @for (name of names(); track name) {
      <span class="category-chip" [title]="label(name)">
        @if (thumbnail(name); as art) {
          <img [src]="art" alt="" loading="lazy" />
        } @else {
          <span class="category-chip-empty"></span>
        }
        <span class="text-break">{{ label(name) }}</span>
      </span>
    }
  `,
  styles: `
    :host {
      display: inline-flex;
      flex-wrap: wrap;
      gap: 0.25rem;
    }
    .category-chip {
      display: inline-flex;
      align-items: center;
      gap: 0.3rem;
      padding: 0.1rem 0.45rem 0.1rem 0.1rem;
      border: 1px solid var(--bs-border-color);
      border-radius: 0.375rem;
      background: var(--bs-body-bg);
      font-size: 0.8rem;
    }
    img,
    .category-chip-empty {
      flex: none;
      width: 1.25rem;
      height: 1.65rem;
      object-fit: cover;
      border-radius: 0.2rem;
    }
    .category-chip-empty {
      background: var(--bs-secondary-bg);
    }
  `,
})
export class CategoryChipsComponent implements OnInit {
  private svc = inject(TwitchCategoriesService);
  private cdr = inject(ChangeDetectorRef);
  private destroyRef = inject(DestroyRef);

  names = input<string[]>([]);

  constructor() {
    effect(() => {
      const names = this.names();
      untracked(() => this.svc.ensure(names));
    });
  }

  ngOnInit() {
    this.svc.changed
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe(() => this.cdr.markForCheck());
  }

  thumbnail(name: string): string | null {
    return this.svc.get(name)?.box_art_url ?? null;
  }

  label(name: string): string {
    return this.svc.get(name)?.display_name ?? name;
  }
}
