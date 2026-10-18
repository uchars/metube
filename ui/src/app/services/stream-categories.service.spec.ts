import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting, HttpTestingController } from '@angular/common/http/testing';
import {
  StreamCategoriesService,
  StreamCategory,
  categoryPlatformForUrl,
} from './stream-categories.service';

const cat = (name: string): StreamCategory => ({
  name,
  display_name: name,
  box_art_url: `https://static-cdn.jtvnw.net/ttv-boxart/${name}-52x72.jpg`,
  viewers: null,
});

describe('categoryPlatformForUrl', () => {
  it('picks the platform from the hostname', () => {
    expect(categoryPlatformForUrl('https://www.twitch.tv/someone')).toBe('twitch');
    expect(categoryPlatformForUrl('https://m.twitch.tv/someone')).toBe('twitch');
    expect(categoryPlatformForUrl('https://kick.com/someone')).toBe('kick');
    expect(categoryPlatformForUrl(' https://www.kick.com/someone ')).toBe('kick');
    expect(categoryPlatformForUrl('https://www.youtube.com/@x/live')).toBeNull();
    expect(categoryPlatformForUrl('https://kick.com.evil.example/x')).toBeNull();
    expect(categoryPlatformForUrl('not a url')).toBeNull();
    expect(categoryPlatformForUrl('')).toBeNull();
  });
});

describe('StreamCategoriesService', () => {
  let httpMock: HttpTestingController;
  let service: StreamCategoriesService;

  beforeEach(() => {
    vi.useFakeTimers();
    TestBed.configureTestingModule({
      providers: [StreamCategoriesService, provideHttpClient(), provideHttpClientTesting()],
    });
    service = TestBed.inject(StreamCategoriesService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.verify();
    vi.useRealTimers();
  });

  it('batches lookups per platform and caches the answers separately', () => {
    let changes = 0;
    service.changed.subscribe(() => changes++);
    service.ensure('twitch', ['Chess']);
    service.ensure('twitch', ['Art', 'chess']);
    service.ensure('kick', ['Chess']);
    service.ensure(null, ['Ignored']);
    vi.advanceTimersByTime(60);

    const twitch = httpMock.expectOne('categories/twitch/lookup');
    expect(twitch.request.body).toEqual({ names: ['Chess', 'Art'] });
    twitch.flush({ status: 'ok', categories: [cat('Chess')] });
    const kick = httpMock.expectOne('categories/kick/lookup');
    expect(kick.request.body).toEqual({ names: ['Chess'] });
    kick.flush({ status: 'ok', categories: [] });

    expect(service.get('twitch', 'CHESS')?.box_art_url).toContain('Chess');
    expect(service.get('kick', 'Chess')).toBeNull();
    expect(service.get(null, 'Chess')).toBeNull();
    expect(changes).toBe(2);

    // Everything was answered (some as unknown), so nothing is asked again.
    service.ensure('twitch', ['Chess', 'Art']);
    service.ensure('kick', ['Chess']);
    vi.advanceTimersByTime(60);
    httpMock.expectNone('categories/twitch/lookup');
    httpMock.expectNone('categories/kick/lookup');
  });

  it('search hits the platform route and reports errors', () => {
    let error: string | undefined;
    service.search('kick', 'chess').subscribe((res) => (error = res.error));
    const req = httpMock.expectOne((r) => r.url === 'categories/kick');
    expect(req.request.params.get('q')).toBe('chess');
    req.flush({ status: 'error', msg: 'Kick category lookup is unavailable' });
    expect(error).toContain('Kick');
  });
});
