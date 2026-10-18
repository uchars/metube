import { TestBed } from '@angular/core/testing';
import { Subject } from 'rxjs';
import { CategoryChipsComponent } from './category-chips.component';
import { CategoryPlatform, StreamCategoriesService } from '../services/stream-categories.service';

describe('CategoryChipsComponent', () => {
  it("shows the platform's thumbnail once the lookup answers and the plain name until then", async () => {
    const changed = new Subject<void>();
    const known = new Map<string, { name: string; display_name: string; box_art_url: string }>();
    const ensured: [CategoryPlatform | null, string[]][] = [];
    await TestBed.configureTestingModule({
      imports: [CategoryChipsComponent],
      providers: [
        {
          provide: StreamCategoriesService,
          useValue: {
            changed,
            ensure: (platform: CategoryPlatform | null, names: string[]) =>
              ensured.push([platform, names]),
            get: (platform: CategoryPlatform | null, name: string) =>
              known.get(`${platform}:${name.toLocaleLowerCase()}`) ?? null,
          },
        },
      ],
    }).compileComponents();

    const fixture = TestBed.createComponent(CategoryChipsComponent);
    fixture.componentRef.setInput('names', ['Chess', 'Custom']);
    fixture.componentRef.setInput('platform', 'kick');
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;
    expect(ensured).toEqual([['kick', ['Chess', 'Custom']]]);
    expect(el.querySelectorAll('img').length).toBe(0);
    expect(el.textContent).toContain('Custom');

    // A Twitch thumbnail for the same name must not show on a Kick subscription.
    known.set('twitch:chess', { name: 'Chess', display_name: 'Chess', box_art_url: 'https://t/1.jpg' });
    changed.next();
    fixture.detectChanges();
    expect(el.querySelectorAll('img').length).toBe(0);

    known.set('kick:chess', { name: 'Chess', display_name: 'Chess', box_art_url: 'https://k/1.webp' });
    changed.next();
    fixture.detectChanges();
    const imgs = el.querySelectorAll('img');
    expect(imgs.length).toBe(1);
    expect(imgs[0].getAttribute('src')).toBe('https://k/1.webp');
  });
});
