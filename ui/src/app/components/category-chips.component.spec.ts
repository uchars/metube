import { TestBed } from '@angular/core/testing';
import { Subject } from 'rxjs';
import { CategoryChipsComponent } from './category-chips.component';
import { TwitchCategoriesService } from '../services/twitch-categories.service';

describe('CategoryChipsComponent', () => {
  it('shows box art once the lookup answers and the plain name until then', async () => {
    const changed = new Subject<void>();
    const known = new Map<string, { name: string; display_name: string; box_art_url: string }>();
    const ensured: string[][] = [];
    await TestBed.configureTestingModule({
      imports: [CategoryChipsComponent],
      providers: [
        {
          provide: TwitchCategoriesService,
          useValue: {
            changed,
            ensure: (names: string[]) => ensured.push(names),
            get: (name: string) => known.get(name.toLocaleLowerCase()) ?? null,
          },
        },
      ],
    }).compileComponents();

    const fixture = TestBed.createComponent(CategoryChipsComponent);
    fixture.componentRef.setInput('names', ['Chess', 'Custom']);
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;
    expect(ensured).toEqual([['Chess', 'Custom']]);
    expect(el.querySelectorAll('img').length).toBe(0);
    expect(el.textContent).toContain('Custom');

    known.set('chess', {
      name: 'Chess',
      display_name: 'Chess',
      box_art_url: 'https://static-cdn.jtvnw.net/ttv-boxart/743-52x72.jpg',
    });
    changed.next();
    fixture.detectChanges();
    const imgs = el.querySelectorAll('img');
    expect(imgs.length).toBe(1);
    expect(imgs[0].getAttribute('src')).toContain('743-52x72');
  });
});
