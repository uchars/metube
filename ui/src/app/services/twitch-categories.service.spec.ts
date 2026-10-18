import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting, HttpTestingController } from '@angular/common/http/testing';
import { TwitchCategoriesService, TwitchCategory } from './twitch-categories.service';

const cat = (name: string): TwitchCategory => ({
  name,
  display_name: name,
  box_art_url: `https://static-cdn.jtvnw.net/ttv-boxart/${name}-52x72.jpg`,
  viewers: null,
});

describe('TwitchCategoriesService', () => {
  let httpMock: HttpTestingController;
  let service: TwitchCategoriesService;

  beforeEach(() => {
    vi.useFakeTimers();
    TestBed.configureTestingModule({
      providers: [TwitchCategoriesService, provideHttpClient(), provideHttpClientTesting()],
    });
    service = TestBed.inject(TwitchCategoriesService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.verify();
    vi.useRealTimers();
  });

  it('batches lookups from several callers into one request and caches the answer', () => {
    let changes = 0;
    service.changed.subscribe(() => changes++);
    service.ensure(['Chess']);
    service.ensure(['Art', 'chess']);
    vi.advanceTimersByTime(60);

    const req = httpMock.expectOne('twitch/categories/lookup');
    expect(req.request.body).toEqual({ names: ['Chess', 'Art'] });
    req.flush({ status: 'ok', categories: [cat('Chess')] });

    expect(service.get('CHESS')?.box_art_url).toContain('Chess');
    expect(service.get('Art')).toBeNull();
    expect(changes).toBe(1);

    // Both names were answered (Art as unknown), so nothing is asked again.
    service.ensure(['Chess', 'Art']);
    vi.advanceTimersByTime(60);
    httpMock.expectNone('twitch/categories/lookup');
  });

  it('search sends the query and reports errors', () => {
    let error: string | undefined;
    service.search('chess').subscribe((res) => (error = res.error));
    const req = httpMock.expectOne((r) => r.url === 'twitch/categories');
    expect(req.request.params.get('q')).toBe('chess');
    req.flush({ status: 'error', msg: 'Twitch rejected the category query: nope' });
    expect(error).toContain('nope');
  });
});
