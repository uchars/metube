import { inject, Injectable } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable, Subject, of } from 'rxjs';
import { catchError, map } from 'rxjs/operators';

export interface TwitchCategory {
  name: string;
  display_name: string;
  box_art_url: string | null;
  viewers: number | null;
}

export const categoryKey = (name: string) => name.trim().toLocaleLowerCase();

export interface TwitchCategoryResult {
  categories: TwitchCategory[];
  error?: string;
}

interface CategoryResponse {
  status: string;
  msg?: string;
  categories?: TwitchCategory[];
}

/** Twitch category search for the live subscription category picker. */
@Injectable({
  providedIn: 'root',
})
export class TwitchCategoriesService {
  private http = inject(HttpClient);

  /** Emits whenever new category details arrive in the cache. */
  readonly changed = new Subject<void>();
  /** Known details by lower-cased name; null = asked, Twitch doesn't know it. */
  private known = new Map<string, TwitchCategory | null>();
  /** Names waiting for the next batched lookup, by lower-cased name. */
  private queued = new Map<string, string>();
  private flushTimer: ReturnType<typeof setTimeout> | null = null;

  /** Cached details for a category name, if any have been fetched. */
  get(name: string): TwitchCategory | null {
    return this.known.get(categoryKey(name)) ?? null;
  }

  /** Adds categories (e.g. from a search) to the cache. */
  remember(categories: TwitchCategory[]) {
    for (const cat of categories) {
      this.known.set(categoryKey(cat.name), cat);
    }
    if (categories.length) {
      this.changed.next();
    }
  }

  /**
   * Fetches details for names not cached yet. Requests from many components
   * in the same tick are batched into one lookup; `changed` fires when done.
   */
  ensure(names: string[]) {
    for (const name of names) {
      const k = categoryKey(name);
      if (k && !this.known.has(k) && !this.queued.has(k)) {
        this.queued.set(k, name.trim());
      }
    }
    if (!this.queued.size || this.flushTimer !== null) {
      return;
    }
    this.flushTimer = setTimeout(() => this.flush(), 50);
  }

  private flush() {
    this.flushTimer = null;
    const batch = [...this.queued].slice(0, 50);
    const names = batch.map(([, name]) => name);
    for (const [k] of batch) {
      this.queued.delete(k);
      // Marked as asked up front so a failed lookup isn't retried on every render.
      this.known.set(k, null);
    }
    this.lookup(names).subscribe((res) => {
      this.remember(res.categories);
      if (!res.categories.length) {
        this.changed.next();
      }
    });
    if (this.queued.size) {
      this.flushTimer = setTimeout(() => this.flush(), 50);
    }
  }

  /** Categories matching `query`, or the most-watched ones when it is empty. */
  search(query: string): Observable<TwitchCategoryResult> {
    return this.http
      .get<CategoryResponse>('twitch/categories', { params: { q: query } })
      .pipe(
        map((res) => this.toResult(res)),
        catchError(() => of({ categories: [], error: 'Twitch category search failed' })),
      );
  }

  /** Details (thumbnails) for exact category names; unknown names are left out. */
  lookup(names: string[]): Observable<TwitchCategoryResult> {
    if (!names.length) {
      return of({ categories: [] });
    }
    return this.http.post<CategoryResponse>('twitch/categories/lookup', { names }).pipe(
      map((res) => this.toResult(res)),
      catchError(() => of({ categories: [], error: 'Twitch category lookup failed' })),
    );
  }

  private toResult(res: CategoryResponse): TwitchCategoryResult {
    if (res?.status !== 'ok') {
      return { categories: [], error: res?.msg || 'Twitch category search failed' };
    }
    return { categories: res.categories ?? [] };
  }
}
