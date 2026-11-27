# Day trader (tylko papier)

Nauka day tradingu na papierze, z pakietem skilli
[day-trader-skill](https://github.com/Jakeschincariol/day-trader-skill) i własnym
dziennikiem transakcji. Z użytkownikiem rozmawiaj po polsku, prostymi słowami.

## Zasada nr 1: TYLKO PAPIER

- Żadnych prawdziwych zleceń i pieniędzy.
- Nie konfiguruj Alpaki ani innego brokera. Żadnych kluczy API.
- `trader-broker` jest NIEAKTYWNY: nie używaj go ani `ticket.py`, dopóki
  użytkownik wprost tego nie powie. Nie usuwaj blokady z jego SKILL.md.
- `"paper": true` w `trader/config.json` zostaje.
- Inne skille odsyłają na końcu do `/trader-broker`. Zamiast tego zapisz
  transakcję papierową przez `/trader-review`, po "tak" użytkownika.
- Nie dawaj porad inwestycyjnych i nie wymyślaj cen ani danych.

## Bot papierowy (GitHub Actions)

- `bot/paper_bot.py`: jedno uruchomienie = jeden krok. Wybicie z 15-minutowego
  zakresu otwarcia na 100 akcjach z `bot/universe.txt`, stop w połowie zakresu,
  cel 2R, wielkość z trader-budget, każda transakcja przez trader-risk.
- Limity: `bot/limits.json` (bez danych osobowych). `paper` musi być `true`.
- Wielkość = min(50 $ / odległość do stopu, 1250 $ / cena): pozycja maks. 25%
  budżetu (`max_position_pct` 25, `max_order_usd` 1250; po stratach 25% mniejszego
  konta), bez bufora gotówki (`cash_buffer_pct` 0), żeby zmieściły się 4 pozycje.
- Najwyżej 4 otwarte pozycje bota (`max_open_positions`), 6 transakcji dziennie,
  pozycja min. 1000 $ (`min_order_usd`); mniejsze sygnały są pomijane i nie
  zużywają limitu dnia. Przy wielu sygnałach naraz pierwszeństwo ma wolumen.
- Strata dzienna 150 $ to strata ZREALIZOWANA dziś (wynik zamkniętych dzisiejszych
  transakcji, bez ryzyka otwartych). Wejście jest blokowane, gdy ta strata plus
  ryzyko nowej transakcji przekracza 150 $; po stracie 150 $ koniec wejść na dziś.
- Cena wykonania: ostatnia cena, 0,05% na niekorzyść. Stop i cel sprawdzane na
  ostatniej cenie w danym kroku. Sygnał starszy niż 20 min jest pomijany.
- 10 min przed końcem sesji zamyka wszystko. Gdy krok przepadnie, następny
  zamyka pozycję po ostatniej cenie tamtej sesji.
- Stan to `journal/trades.csv`; transakcje bota mają notatkę `[bot]`. Log dnia:
  `journal/log/RRRR-MM-DD.txt`.
- Próba bez zapisu: `python bot/paper_bot.py --dry-run --now "2026-11-24 10:20"`
- Kalendarz NYSE w `trader-entry` kończy się na 2027: potem trzeba go uzupełnić.
- `bot/loop.py`: pętla, krok bota co 5 min (pełne 5 min + 40 s), pierwszy krok
  od razu po starcie. Kończy się po sesji (gdy nic nie jest otwarte), w dzień
  bez sesji po jednym kroku, albo po 5 h 45 min. Commit `journal/` po kroku
  z transakcją, poza tym co 30 min i na końcu.
  Próba: `python bot/loop.py --dry-run` (kopia dziennika, bez commita), powtórka:
  `python bot/loop.py --dry-run --now "2026-11-25 15:30"` (bez czekania).
- `.github/workflows/paper-bot.yml`: dwie pętle pon-pt, cron 13:00 UTC (przed
  otwarciem) i 17:30 UTC (w połowie sesji; czeka w kolejce `concurrency` na
  koniec pierwszej). Ręcznie: `workflow_dispatch` z `dry_run` i `now`; próba ma
  osobną grupę `concurrency`. GitHub może się spóźniać lub pominąć start.
- `.github/workflows/weekly-report.yml`: w piątek 22:30 UTC i ręcznie uruchamia
  `bot/weekly_report.py` i tworzy Issue "Raport tygodnia RRRR-MM-DD" (ponownie
  tego samego dnia: aktualizuje je). Dni z problemami bierze z logów dnia.
- Po każdym kroku `bot/dashboard.py` rysuje `journal/dashboard.svg` (pokazany
  w README.md). Poza sesją plik zmienia się tylko wtedy, gdy zmienia się treść.
- Przed ręczną zmianą dziennika `git pull`, po niej commit i `git push`.
- Bot nie używa SEC, brokera ani kluczy. E-mail, `trader/` i `repo/` nie mogą
  trafić na GitHuba. Commity tylko z adresem noreply (ustawiony w tym repo).

## Jak pracować

- Nie wychodź poza `C:\day-trader`. Uruchamiaj wszystko z tego folderu.
- Zawsze `python`, nigdy `python3` (nawet gdy SKILL.md pisze inaczej).
- Tylko biblioteka standardowa, Python 3.8+.
- Testy (bez `-t repo`, bo `tests` nie ma `__init__.py`):
  `python -m unittest discover -s repo/tests`

## Foldery

| folder | co |
| --- | --- |
| `.claude/skills/` | skille, których używa Claude |
| `repo/` | sklonowane repo; własne pliki: `repo/tests/test_review.py`, `test_bot.py`, `test_weekly_report.py`, `test_dashboard.py` (nie ma ich na GitHubie) |
| `bot/` | bot papierowy, limity, lista akcji |
| `trader/` | `config.json` i pliki robocze skilli (dane osobowe, nie publikuj) |
| `journal/trades.csv` | dziennik transakcji |

## Skille

| skill | co robi |
| --- | --- |
| `trader-scanner` | szuka układów na wykresach |
| `trader-news` | wiadomości i raporty 8-K |
| `trader-entry` | mówi, kiedy jest sygnał wejścia |
| `trader-exit` | stop i cel przed wejściem |
| `trader-timemachine` | symulacja możliwych wyników |
| `trader-budget` | wielkość pozycji |
| `trader-risk` | sprawdza limity |
| `trader-alerts` | alerty cenowe |
| `trader-analyst` | raporty finansowe spółek |
| `trader-pelosi` | transakcje członków Kongresu |
| `trader-review` | własny: dziennik i podsumowanie tygodnia |
| `trader-broker` | NIEAKTYWNY |

Kolejność: scanner, news, entry, exit, timemachine, budget, risk, review.
