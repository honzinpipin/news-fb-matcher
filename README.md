# news-fb-matcher

Porovnává články ze zpravodajského webu s příspěvky z Facebooku a hledá mezi nimi shody.

## Jak to funguje

1. **Stažení.** Ze zpravodajských webů (feed se najde automaticky: news sitemap, RSS nebo Atom) se stáhnou nové články, u každého i původní zdroj (Telegram kanál), pokud ho web uvádí. Příspěvky veřejných FB stránek se stáhnou přes Bright Data. Stahuje se jen to, co je propojené (viz Zdroje a vazby).
2. **Vrstva 1, AI extrakce.** Pro každý článek a příspěvek se jednou uloží shrnutí, hlavní teze, entity, klíčová slova, čísla a dva embeddingy.
3. **Vrstva 2, skórování bez AI.** Páry článek × příspěvek propojených zdrojů v okně ±7 dní dostanou skóre ze signálů S1–S6 (viz `app/candidates.py`). Počítá se inkrementálně, jen s novými položkami.
4. **Vrstva 3, AI posouzení.** Všechny páry se skóre ≥ 0,55 (`[judge] min_score`). Výstupem je verdikt, typ shody a zdůvodnění.
5. **Web.** Seznam shod, rozpad skóre, hodnocení 👍/👎 (ukládá se pro pozdější učení vah).

## Spuštění

**Instalace na novém počítači krok za krokem: [NAVOD.md](NAVOD.md).**

Stručně (Windows, PowerShell, Python 3.12):

```powershell
git clone https://github.com/honzinpipin/news-fb-matcher.git
cd news-fb-matcher
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env   # doplň OPENAI_API_KEY, BRIGHTDATA_TOKEN, (TELEGRAM_BOT_TOKEN)
.\.venv\Scripts\python.exe run_server.py      # -> http://127.0.0.1:8010
.\.venv\Scripts\python.exe run_nightly.py     # jeden běh ručně (= tlačítko „Spustit teď“)
```

Klíče se čtou ze souboru `.env` (není v gitu) nebo z proměnných prostředí. Noční běh přes Plánovač úloh: viz NAVOD.md, kapitola 8.

## Zdroje a vazby (web → Zdroje)

- **Zpravodajský web:** zadej URL. Aplikace hned najde feed, stáhne 1 článek na ukázku a ukáže vytažený text. Pod „Pokročilé“ lze zadat CSS selektor bloku s textem (když obecná extrakce bere i postranní bloky; u news-pravda `.article__text`) a HTML atribut s původním zdrojem (`data-source-url`).
- **Facebook profil/stránka:** zadej URL. Ověří se stažením 1 příspěvku (1 záznam Bright Data) a doplní se název.
- **Vazby:** vyber web a profil a propoj je. Porovnávají se jen propojené dvojice; jeden web může mít víc profilů a naopak. Zdroj bez vazby se nestahuje a nic nestojí.
- **Vypnout** zastaví stahování (data zůstanou), **Smazat** odstraní zdroj včetně článků/příspěvků, obrázků, párů a posouzení.
- Na hlavní stránce lze filtrovat shody podle vazby.

## Vyhledávání

Pole „Hledat“ je na stránkách Shody, Články a Příspěvky. Hledá se v titulku, textu, textu z obrázků i v AI datech (shrnutí, entity, klíčová slova, odvozené osoby). Diakritika a velikost písmen nehrají roli.

| Zápis | Význam |
|---|---|
| `Babiš` | slova začínající na „Babiš“; běžné koncovky se ořežou („Babišovi“ → Babiš*) |
| `Babiš vláda` | musí obsahovat všechna slova |
| `"cena nafty"` | přesná fráze |
| `Rakušan NEBO STAN` | kterékoli ze slov (funguje i OR) |
| `Ukrajina -Zelenskyj` | vyloučí slovo |
| `STAN`, `ODS` | zkratky velkými písmeny se hledají přesně i s pády (STANu), ne jako začátek slova |

Index (SQLite FTS5) se obnovuje na konci každého běhu.

## PDF report (shrnutí za období)

- Každých 7 dní (`[report] period_days`) se na konci běhu automaticky vytvoří PDF do `data/reports/`: období, zdroje, přehled čísel, AI shrnutí obsahu, témata (koláčový graf), vývoj témat po dnech (sloupcový graf), podrobně shody, stručně související témata a Telegram kanály, ze kterých články pocházejí (název, odběratelé, ID).
- Ve webu **Shrnutí**: historie reportů (otevřít / stáhnout / smazat) a tlačítko „Vytvořit“ s obdobím 7/14/30 dní nebo vlastním rozsahem.
- Ručně: `python make_report.py` (výchozí období) nebo `python make_report.py --days 30`.
- Telegram ID kanálů zjišťuje bot: `TELEGRAM_BOT_TOKEN` v `.env` (token od @BotFather). Bez něj se uvede jen název a počet odběratelů.

## Příspěvky z FB (Bright Data)

- Scraper „Facebook – Pages Posts by Profile URL“ (`gd_lkaxegm826bjpoo9m5`), jen veřejná data, bez FB účtu.
- První běh po propojení profilu stáhne 14 dní zpětně, pak každou noc jen nové příspěvky (od posledního uloženého; už uložené se posílají v `posts_to_not_include`, aby se neplatily znovu).
- Free kredit 5 000 záznamů měsíčně (obnova 1. v měsíci). Aplikace si spotřebu počítá, ve webu ukáže varování při 80 % a po vyčerpání stahování zastaví (`stop_at_free_limit`).
- Surové odpovědi se ukládají do `data/fb_raw/`. Úloha, která nestihne doběhnout, se dokončí při dalším běhu.
- Nastavení v `config.toml`, sekce `[facebook]`.

## Ladění a údržba

Váhy a prahy jsou v `config.toml` (`[matching]`, `[judge]`). Detail shody ve webu ukazuje, jak se skóre poskládalo. Obecné entity (vyskytují se ve ≥ 5 % textů, např. Rusko, NATO) se do shody entit nepočítají (`entity_common_share`).

- `python rescore.py [--judge]` – přepočítá skóre všech párů (po změně vah); existující AI verdikty se znovu použijí.
- `python refetch_articles.py` – znovu stáhne texty článků propojených webů; obsahově změněné vrátí ke zpracování.
- `python reextract_posts.py` – vrátí všechny příspěvky k nové AI extrakci.
- `python backfill_articles.py --from 2026-09-20 [--to …]` – dotáhne starší články propojených webů z archivních sitemap (i `.xml.gz`) a zpracuje je.
