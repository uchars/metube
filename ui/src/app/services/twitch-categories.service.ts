import { inject, Injectable } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable, of } from 'rxjs';
import { catchError, map } from 'rxjs/operators';

export interface TwitchCategory {
  name: string;
  display_name: string;
  box_art_url: string | null;
  viewers: number | null;
}

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
