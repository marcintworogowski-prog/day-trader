---
name: trader-review
description: >-
  Trade Review. Dziennik transakcji papierowych i podsumowanie tygodnia.
  Zapisuje wejście, stop, cel, wyjście, wynik w $ i w R oraz czy trzymano się
  planu w journal/trades.csv. Liczy % wygranych, średni zysk i stratę, wartość
  oczekiwaną, maksymalne obsunięcie, porównuje z kup i trzymaj SPY i pokazuje
  3 najczęstsze błędy. Nigdy nie składa zleceń. Użyj, gdy użytkownik mówi
  "dodaj transakcję", "zamknij transakcję", "pokaż dziennik", "podsumowanie
  tygodnia", "jak mi poszło", "jakie robię błędy", "trade journal", "weekly
  review".
argument-hint: "[dodaj|zamknij|lista|tydzien]"
---

# trader-review

Dziennik mówi prawdę, której pamięć nie powie. Każda transakcja trafia do
jednego pliku CSV, a raz w tygodniu liczby odpowiadają na pytanie, czy to
w ogóle działa i czy zwykłe trzymanie SPY nie byłoby lepsze.

Tylko papier. Ten skill niczego nie kupuje i nie sprzedaje, nie łączy się
z brokerem i nie potrzebuje kluczy.

`${CLAUDE_SKILL_DIR}` to folder tego skilla. Uruchamiaj z głównego folderu
projektu.

```bash
python "${CLAUDE_SKILL_DIR}/review.py" dodaj --ticker TSLA --setup orb --wejscie 364.18 --stop 358.32 --cel 370.04 --wielkosc 8
python "${CLAUDE_SKILL_DIR}/review.py" zamknij --id 1 --wyjscie 370.04 --plan tak --notatka "cel"
python "${CLAUDE_SKILL_DIR}/review.py" lista --otwarte
python "${CLAUDE_SKILL_DIR}/review.py" tydzien
python "${CLAUDE_SKILL_DIR}/review.py" tydzien --data 2026-10-19
python "${CLAUDE_SKILL_DIR}/review.py" tydzien --od 2026-10-01 --do 2026-10-31
```

Inne flagi: `--data RRRR-MM-DD` (wejścia lub wyjścia), `--blad KOD`,
`--offline PLIK` (zapisany wykres SPY), `--journal PLIK`, `--now DATA`.

## Co czyta i zapisuje

| plik | co |
| --- | --- |
| `journal/trades.csv` | dziennik, jeden wiersz na transakcję |
| `trader/config.json` | `budget`, `max_loss_per_trade_usd`, `min_reward_risk`, `paper` |
| Yahoo chart API | dzienne notowania SPY, darmowe i nieoficjalne |

Kolumny: id, data, ticker, setup, wejscie, stop, cel, wyjscie, wielkosc,
wynik_usd, wynik_r, plan (tak/nie), notatka, data_wyjscia, bledy.

## Dodaj transakcję

1. Jeśli istnieje `trader/plan.json` z dzisiejszą datą i tym tickerem, możesz
   zaproponować z niego wejście, stop i cel. Wielkość bierz od użytkownika
   albo z `trader/orders.json`.
2. Brakujące dane dopytaj. Nigdy nie wymyślaj ceny, stopu ani wielkości.
3. Pokaż, co zapiszesz, i dopiero po "tak" uruchom `dodaj`.
4. Wejście bez sygnału zapisuj z `--setup brak`. Nie upiększaj.

## Zamknij transakcję

1. Zapytaj o cenę wyjścia i czy użytkownik trzymał się planu (tak/nie).
2. Jeśli nie, zapytaj, co się stało, i dodaj `--blad KOD`.
3. Gdy ticker ma kilka otwartych transakcji, użyj `--id` z `lista --otwarte`.

## Błędy

| kod | skąd |
| --- | --- |
| `zlamany-stop` | sam: strata większa niż 1.1R (może to też być poślizg lub luka) |
| `bez-sygnalu` | sam: setup "brak" |
| `za-duze-ryzyko` | sam: ryzyko ponad `max_loss_per_trade_usd` |
| `slaby-cel` | sam: cel poniżej `min_reward_risk` |
| `przez-noc` | sam: wyjście w innym dniu niż wejście |
| `wyjscie-za-wczesnie`, `przesuniety-stop`, `gonienie-ceny`, `odgrywanie-sie` | użytkownik podaje |
| `inny-blad` | plan "nie" bez innego błędu |

## Jak liczy

- R = (wyjście - wejście) / (wejście - stop). -1R to pełna zaplanowana strata.
- Wartość oczekiwana to średni wynik na zamkniętą transakcję.
- Obsunięcie: największy spadek sumy wyników od szczytu, licząc od zera na
  początku okresu. Nie widzi wahań w trakcie otwartej transakcji.
- Tydzień: poniedziałek do niedzieli (Nowy Jork), według daty wyjścia.
- SPY: od ostatniego zamknięcia przed okresem, z dywidendami, na całym
  `budget`. Dzienny trader zwykle trzyma gotówkę, więc ryzyko nie jest takie samo.

## Zasady

- Podawaj liczby dokładnie tak, jak wypisał je program.
- Przy mniej niż 30 transakcjach mów wprost, że to za mało na wnioski.
- Nie zmieniaj ręcznie starych wyników, żeby wyglądały lepiej.
- Nie proponuj przejścia na prawdziwe pieniądze.
- Wyniki są bez prowizji i podatków. To nie jest porada inwestycyjna
  (not financial advice). Większość day traderów traci pieniądze.
