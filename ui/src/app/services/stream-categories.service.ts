import { inject, Injectable } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable, Subject, of } from 'rxjs';
import { catchError, map } from 'rxjs/operators';

/** A site whose category list MeTube can browse. */
export type CategoryPlatform = 'twitch' | 'kick';

export const PLATFORM_LABELS: Record<CategoryPlatform, string> = {
  twitch: 'Twitch',
  kick: 'Kick',
};

export interface StreamCategory {
  name: string;
  display_name: string;
  box_art_url: string | null;
  viewers: number | null;
}

export interface StreamCategoryResult {
  categories: StreamCategory[];
  error?: string;
}

interface CategoryResponse {
  status: string;
  msg?: string;
  categories?: StreamCategory[];
}

export const categoryKey = (name: string) => name.trim().toLocaleLowerCase();

/** The category platform of a channel URL, by hostname; null for other sites. */
export function categoryPlatformForUrl(url: string | null | undefined): CategoryPlatform | null {
  let host: string;
  try {
    host = new URL((url || '').trim()).hostname.toLowerCase();
  } catch {
    return null;
  }
  const matches = (domain: string) => host === domain || host.endsWith('.' + domain);
  if (matches('twitch.tv')) {
    return 'twitch';
  }
  if (matches('kick.com')) {
    return 'kick';
  }
  return null;
}

/**
 * Stream category search (Twitch, Kick) for the live subscription category
 * picker, with a per-platform cache shared by every component showing them.
 */
@Injectable({
  providedIn: 'root',
})
export class StreamCategoriesService {
  private http = inject(HttpClient);

  /** Emits whenever new category details arrive in the cache. */
  readonly changed = new Subject<void>();
  /** Known details by platform + lower-cased name; null = asked, the site doesn't know it. */
  private known = new Map<string, StreamCategory | null>();
  /** Names waiting for the next batched lookup, per platform, by lower-cased name. */
  private queued: Record<CategoryPlatform, Map<string, string>> = {
    twitch: new Map(),
    kick: new Map(),
  };
  private flushTimer: ReturnType<typeof setTimeout> | null = null;

  private cacheKey(platform: CategoryPlatform, name: string) {
    return `${platform}:${categoryKey(name)}`;
  }

  /** Cached details for a category name on a platform, if any have been fetched. */
  get(platform: CategoryPlatform | null, name: string): StreamCategory | null {
    return platform ? (this.known.get(this.cacheKey(platform, name)) ?? null) : null;
  }

  /** Adds categories (e.g. from a search) to the cache. */
  remember(platform: CategoryPlatform, categories: StreamCategory[]) {
    for (const cat of categories) {
      this.known.set(this.cacheKey(platform, cat.name), cat);
    }
    if (categories.length) {
      this.changed.next();
    }
  }

  /**
   * Fetches details for names not cached yet. Requests from many components
   * in the same tick are batched into one lookup per platform; `changed`
   * fires when done.
   */
  ensure(platform: CategoryPlatform | null, names: string[]) {
    if (!platform) {
      return;
    }
    const queue = this.queued[platform];
    for (const name of names) {
      const k = categoryKey(name);
      if (k && !this.known.has(this.cacheKey(platform, name)) && !queue.has(k)) {
        queue.set(k, name.trim());
      }
    }
    if (this.flushTimer === null && this.hasQueued()) {
      this.flushTimer = setTimeout(() => this.flush(), 50);
    }
  }

  private hasQueued() {
    return Object.values(this.queued).some((q) => q.size > 0);
  }

  private flush() {
    this.flushTimer = null;
    for (const platform of Object.keys(this.queued) as CategoryPlatform[]) {
      const queue = this.queued[platform];
      if (!queue.size) {
        continue;
      }
      const batch = [...queue].slice(0, 50);
      for (const [k, name] of batch) {
        queue.delete(k);
        // Marked as asked up front so a failed lookup isn't retried on every render.
        this.known.set(this.cacheKey(platform, name), null);
      }
      this.lookup(
        platform,
        batch.map(([, name]) => name),
      ).subscribe((res) => {
        this.remember(platform, res.categories);
        if (!res.categories.length) {
          this.changed.next();
        }
      });
    }
    if (this.hasQueued()) {
      this.flushTimer = setTimeout(() => this.flush(), 50);
    }
  }

  /** Categories matching `query`, or the most-watched ones when it is empty. */
  search(platform: CategoryPlatform, query: string): Observable<StreamCategoryResult> {
    const label = PLATFORM_LABELS[platform];
    return this.http
      .get<CategoryResponse>(`categories/${platform}`, { params: { q: query } })
      .pipe(
        map((res) => this.toResult(res, `${label} category search failed`)),
        catchError(() => of({ categories: [], error: `${label} category search failed` })),
      );
  }

  /** Details (thumbnails) for exact category names; unknown names are left out. */
  lookup(platform: CategoryPlatform, names: string[]): Observable<StreamCategoryResult> {
    if (!names.length) {
      return of({ categories: [] });
    }
    const label = PLATFORM_LABELS[platform];
    return this.http.post<CategoryResponse>(`categories/${platform}/lookup`, { names }).pipe(
      map((res) => this.toResult(res, `${label} category lookup failed`)),
      catchError(() => of({ categories: [], error: `${label} category lookup failed` })),
    );
  }

  private toResult(res: CategoryResponse, fallback: string): StreamCategoryResult {
    if (res?.status !== 'ok') {
      return { categories: [], error: res?.msg || fallback };
    }
    return { categories: res.categories ?? [] };
  }
}
