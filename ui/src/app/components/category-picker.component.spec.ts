import { TestBed } from '@angular/core/testing';
import { Subject, of } from 'rxjs';
import { CategoryPickerComponent } from './category-picker.component';
import {
  CategoryPlatform,
  StreamCategoriesService,
  StreamCategory,
} from '../services/stream-categories.service';

const cat = (name: string, platform: CategoryPlatform = 'twitch'): StreamCategory => ({
  name,
  display_name: name,
  box_art_url: `https://${platform}.example/${name}.jpg`,
  viewers: 1200,
});

class StreamCategoriesServiceStub {
  searchCalls: [CategoryPlatform, string][] = [];
  ensureCalls: [CategoryPlatform | null, string[]][] = [];
  results: Record<CategoryPlatform, StreamCategory[]> = {
    twitch: [cat('Just Chatting'), cat('Chess'), cat('Art')],
    kick: [cat('Slots & Casino', 'kick'), cat('Just Chatting', 'kick')],
  };
  changed = new Subject<void>();
  private known = new Map<string, StreamCategory>();

  search(platform: CategoryPlatform, query: string) {
    this.searchCalls.push([platform, query]);
    const q = query.toLocaleLowerCase();
    return of({ categories: this.results[platform].filter((c) => c.name.toLocaleLowerCase().includes(q)) });
  }

  get(platform: CategoryPlatform | null, name: string) {
    return this.known.get(`${platform}:${name.toLocaleLowerCase()}`) ?? null;
  }

  remember(platform: CategoryPlatform, cats: StreamCategory[]) {
    for (const c of cats) {
      this.known.set(`${platform}:${c.name.toLocaleLowerCase()}`, c);
    }
  }

  ensure(platform: CategoryPlatform | null, names: string[]) {
    this.ensureCalls.push([platform, names]);
    if (platform) {
      this.remember(platform, names.filter((n) => n !== 'Custom').map((n) => cat(n, platform)));
      this.changed.next();
    }
  }
}

describe('CategoryPickerComponent', () => {
  let svc: StreamCategoriesServiceStub;

  beforeEach(async () => {
    vi.useFakeTimers();
    svc = new StreamCategoriesServiceStub();
    await TestBed.configureTestingModule({
      imports: [CategoryPickerComponent],
      providers: [{ provide: StreamCategoriesService, useValue: svc }],
    }).compileComponents();
  });

  afterEach(() => vi.useRealTimers());

  function create(selected: string[] = [], platform: CategoryPlatform | null = 'twitch') {
    const fixture = TestBed.createComponent(CategoryPickerComponent);
    fixture.componentRef.setInput('categories', selected);
    fixture.componentRef.setInput('platform', platform);
    fixture.detectChanges();
    return fixture;
  }

  function text(fixture: { nativeElement: HTMLElement }) {
    return fixture.nativeElement.textContent ?? '';
  }

  it('lists selected categories first and leaves them out of the results', () => {
    const fixture = create(['Chess']);
    const picker = fixture.componentInstance;
    picker.open();
    vi.advanceTimersByTime(300);
    fixture.detectChanges();

    expect(svc.searchCalls).toEqual([['twitch', '']]);
    expect(picker.available().map((c) => c.name)).toEqual(['Just Chatting', 'Art']);
    expect(text(fixture).indexOf('Selected (1)')).toBeLessThan(text(fixture).indexOf('Popular on Twitch'));
  });

  it('keeps the selection on top while searching', () => {
    const fixture = create(['Chess']);
    const picker = fixture.componentInstance;
    picker.open();
    picker.onQuery('ar');
    vi.advanceTimersByTime(300);
    fixture.detectChanges();

    expect(svc.searchCalls).toContainEqual(['twitch', 'ar']);
    expect(text(fixture)).toContain('Selected (1)');
    expect(text(fixture)).toContain('Chess');
    expect(text(fixture)).toContain('Twitch results');
  });

  it('browses Kick categories for a Kick URL', () => {
    const fixture = create([], 'kick');
    const picker = fixture.componentInstance;
    picker.open();
    vi.advanceTimersByTime(300);
    fixture.detectChanges();

    expect(svc.searchCalls).toEqual([['kick', '']]);
    expect(picker.available().map((c) => c.name)).toEqual(['Slots & Casino', 'Just Chatting']);
    expect(text(fixture)).toContain('Popular on Kick');
    expect(picker.placeholder()).toBe('Search Kick categories');
  });

  it('switches lists when the platform changes', () => {
    const fixture = create([], 'twitch');
    const picker = fixture.componentInstance;
    picker.open();
    vi.advanceTimersByTime(300);
    fixture.componentRef.setInput('platform', 'kick');
    fixture.detectChanges();
    vi.advanceTimersByTime(300);
    fixture.detectChanges();

    expect(svc.searchCalls).toEqual([
      ['twitch', ''],
      ['kick', ''],
    ]);
    expect(picker.available().map((c) => c.name)).toEqual(['Slots & Casino', 'Just Chatting']);
  });

  it('offers only custom names for sites without a category list', () => {
    const fixture = create([], null);
    const picker = fixture.componentInstance;
    picker.open();
    picker.onQuery('Gaming');
    vi.advanceTimersByTime(300);
    fixture.detectChanges();

    expect(svc.searchCalls).toEqual([]);
    expect(picker.customCandidate()).toBe('Gaming');
    expect(text(fixture)).toContain('Enter a Twitch or Kick channel URL');
    expect(picker.placeholder()).toBe('Type a category name');
  });

  it('adds and removes categories', () => {
    const fixture = create([]);
    const picker = fixture.componentInstance;
    picker.add('Chess');
    picker.add('chess');
    expect(picker.categories()).toEqual(['Chess']);
    picker.remove('CHESS');
    expect(picker.categories()).toEqual([]);
  });

  it('offers the typed text as a custom category when there is no exact match', () => {
    const fixture = create([]);
    const picker = fixture.componentInstance;
    picker.onQuery('Slots');
    vi.advanceTimersByTime(300);
    expect(picker.customCandidate()).toBe('Slots');
    picker.onQuery('chess');
    vi.advanceTimersByTime(300);
    expect(picker.customCandidate()).toBeNull();
  });

  it('Enter picks the first result and does not submit the form', () => {
    const fixture = create([]);
    const picker = fixture.componentInstance;
    picker.onQuery('che');
    vi.advanceTimersByTime(300);
    const event = new KeyboardEvent('keydown', { key: 'Enter', cancelable: true });
    picker.onKeydown(event);
    expect(event.defaultPrevented).toBe(true);
    expect(picker.categories()).toEqual(['Chess']);
  });

  it('looks up thumbnails for already selected categories on the right platform', () => {
    const fixture = create(['Chess', 'Custom'], 'kick');
    const picker = fixture.componentInstance;
    expect(svc.ensureCalls).toContainEqual(['kick', ['Chess', 'Custom']]);
    expect(picker.thumbnail('chess')).toContain('kick.example');
    expect(picker.thumbnail('Custom')).toBeNull();
  });
});
