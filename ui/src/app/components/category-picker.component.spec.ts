import { TestBed } from '@angular/core/testing';
import { of } from 'rxjs';
import { CategoryPickerComponent } from './category-picker.component';
import { TwitchCategoriesService, TwitchCategory } from '../services/twitch-categories.service';

const cat = (name: string): TwitchCategory => ({
  name,
  display_name: name,
  box_art_url: `https://static-cdn.jtvnw.net/ttv-boxart/${name}-52x72.jpg`,
  viewers: 1200,
});

class TwitchCategoriesServiceStub {
  searchCalls: string[] = [];
  lookupCalls: string[][] = [];
  results: TwitchCategory[] = [cat('Just Chatting'), cat('Chess'), cat('Art')];

  search(query: string) {
    this.searchCalls.push(query);
    return of({ categories: this.results });
  }

  lookup(names: string[]) {
    this.lookupCalls.push(names);
    return of({ categories: names.filter((n) => n !== 'Custom').map(cat) });
  }
}

describe('CategoryPickerComponent', () => {
  let svc: TwitchCategoriesServiceStub;

  beforeEach(async () => {
    vi.useFakeTimers();
    svc = new TwitchCategoriesServiceStub();
    await TestBed.configureTestingModule({
      imports: [CategoryPickerComponent],
      providers: [{ provide: TwitchCategoriesService, useValue: svc }],
    }).compileComponents();
  });

  afterEach(() => vi.useRealTimers());

  function create(selected: string[] = []) {
    const fixture = TestBed.createComponent(CategoryPickerComponent);
    fixture.componentRef.setInput('categories', selected);
    fixture.detectChanges();
    return fixture;
  }

  it('lists selected categories first and leaves them out of the results', () => {
    const fixture = create(['Chess']);
    const picker = fixture.componentInstance;
    picker.open();
    vi.advanceTimersByTime(300);
    fixture.detectChanges();

    expect(svc.searchCalls).toEqual(['']);
    expect(picker.available().map((c) => c.name)).toEqual(['Just Chatting', 'Art']);
    const text = (fixture.nativeElement as HTMLElement).textContent ?? '';
    expect(text.indexOf('Selected (1)')).toBeLessThan(text.indexOf('Popular on Twitch'));
  });

  it('keeps the selection on top while searching', () => {
    const fixture = create(['Chess']);
    const picker = fixture.componentInstance;
    picker.open();
    svc.results = [cat('Art')];
    picker.onQuery('ar');
    vi.advanceTimersByTime(300);
    fixture.detectChanges();

    expect(svc.searchCalls).toContain('ar');
    const text = (fixture.nativeElement as HTMLElement).textContent ?? '';
    expect(text).toContain('Selected (1)');
    expect(text).toContain('Chess');
    expect(text).toContain('Twitch results');
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

  it('offers the typed text as a custom category when Twitch has no exact match', () => {
    const fixture = create([]);
    const picker = fixture.componentInstance;
    svc.results = [cat('Chess')];
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
    svc.results = [cat('Chess')];
    picker.onQuery('che');
    vi.advanceTimersByTime(300);
    const event = new KeyboardEvent('keydown', { key: 'Enter', cancelable: true });
    picker.onKeydown(event);
    expect(event.defaultPrevented).toBe(true);
    expect(picker.categories()).toEqual(['Chess']);
  });

  it('looks up thumbnails for already selected categories', () => {
    const fixture = create(['Chess', 'Custom']);
    const picker = fixture.componentInstance;
    expect(svc.lookupCalls).toEqual([['Chess', 'Custom']]);
    expect(picker.thumbnail('chess')).toContain('Chess');
    expect(picker.thumbnail('Custom')).toBeNull();
  });
});
