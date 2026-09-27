# Návod: instalace a spuštění na novém počítači (Windows)

Tento návod počítá s úplně čistým počítačem s Windows 10 nebo 11: není na něm Python, Git ani žádný editor. Všechno se dělá v **PowerShellu**. Nic dalšího (Visual Studio, VS Code, Claude) není potřeba.

Celá instalace trvá asi 15 minut. První zpracování dat pak běží dalších 20–40 minut.

---

## 0. Co budeš potřebovat

| Co | K čemu | Povinné |
|---|---|---|
| Odkaz na GitHub repozitář | stažení projektu | ano |
| **OpenAI API klíč** | AI rozbor článků, příspěvků a obrázků | ano |
| **Bright Data API klíč** | stahování příspěvků z Facebooku | ano, pokud chceš FB příspěvky |
| **Telegram bot token** | číselné ID Telegram kanálů v reportu | ne |
| Připojení k internetu | stahování článků a volání API | ano |

Jak klíče získat, popisuje [kapitola 4](#4-api-klíče).

---

## 1. Otevři PowerShell

Stiskni **Start**, napiš `PowerShell` a otevři **Windows PowerShell**. Správcovská práva nejsou potřeba.

Všechny příkazy v návodu kopíruj do tohoto okna a potvrď Enterem. Řádky začínající `#` jsou jen komentáře a nemusíš je kopírovat.

---

## 2. Nainstaluj Python a Git

Windows 10/11 obsahují instalátor `winget`. Spusť:

```powershell
winget install -e --id Python.Python.3.12
winget install -e --id Git.Git
```

Při prvním použití se winget může zeptat na souhlas s podmínkami, potvrď `Y`.

**Po instalaci zavři PowerShell a otevři ho znovu.** Nově nainstalované programy jsou vidět až v novém okně.

Kontrola:

```powershell
py -3.12 --version
git --version
```

Mělo by se vypsat např. `Python 3.12.x` a `git version 2.x`.

> **Když `winget` nefunguje** (starší Windows), stáhni instalátory ručně:
> - Python 3.12: <https://www.python.org/downloads/>. V instalátoru **zaškrtni „Add python.exe to PATH“**.
> - Git: <https://git-scm.com/download/win> (stačí výchozí volby).
>
> Pak znovu otevři PowerShell.

---

## 3. Stáhni projekt a nainstaluj knihovny

Projekt se stáhne do složky Dokumenty. Místo `ODKAZ_NA_GITHUB` vlož odkaz na repozitář, např. `https://github.com/uzivatel/news-fb-matcher.git`:

```powershell
cd $HOME\Documents
git clone ODKAZ_NA_GITHUB news-fb-matcher
cd news-fb-matcher
```

Vytvoř pro projekt vlastní prostředí Pythonu, aby se knihovny nemíchaly s ničím jiným, a nainstaluj do něj knihovny:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Instalace knihoven trvá 1–3 minuty. Na konci nesmí být červená chybová hláška.

> Python se v návodu vždy spouští přes `.\.venv\Scripts\python.exe`, takže není potřeba prostředí „aktivovat“. Tím se obejde častá chyba PowerShellu *„running scripts is disabled on this system“*.

---

## 4. API klíče

### 4.1 OpenAI (povinné)

1. Zaregistruj se na <https://platform.openai.com/>.
2. V **Settings → Billing** dobij kredit. Aplikace platí za použití, viz odhad nákladů v [kapitole 9](#9-náklady).
3. V **API keys** (<https://platform.openai.com/api-keys>) klikni na **Create new secret key** a klíč si zkopíruj. Začíná `sk-` a zobrazí se jen jednou.

Aplikace používá model uvedený v `config.toml` (`[openai] model`). Pokud tvůj účet tento model nenabízí, přepiš ho na model, ke kterému máš přístup.

### 4.2 Bright Data (povinné pro Facebook)

1. Zaregistruj se na <https://brightdata.com/>.
2. Ověř účet přidáním platební metody (**Billing → Add payment method**). Nic se nestrhne, ale bez ověření API odpovídá chybou *„Customer is not active“*. Free tarif dává **5 000 záznamů měsíčně** (1 příspěvek = 1 záznam), a to na běžný provoz stačí.
3. Doporučeno: v **Billing** nastav měsíční limit útraty (např. 5 USD).
4. V **Account settings → API keys** vytvoř klíč a zkopíruj ho.

Nic dalšího v Bright Data nastavovat nemusíš. Použitý scraper („Facebook – Pages Posts by Profile URL“) je nastavený v `config.toml`.

### 4.3 Telegram bot (nepovinné)

1. V Telegramu napiš uživateli **@BotFather** zprávu `/newbot` a postupuj podle pokynů.
2. Na konci dostaneš token ve tvaru `123456789:AA…`.

Bez tokenu report uvede u Telegram kanálů jen název a počet odběratelů.

### 4.4 Ulož klíče do souboru `.env`

V kořeni projektu vytvoř z předlohy soubor `.env` a otevři ho v Poznámkovém bloku:

```powershell
Copy-Item .env.example .env
notepad .env
```

Doplň klíče za rovnítka (bez uvozovek a bez mezer) a soubor ulož:

```
OPENAI_API_KEY=sk-...
BRIGHTDATA_TOKEN=...
TELEGRAM_BOT_TOKEN=...
```

> ⚠️ Soubor `.env` **nikomu neposílej a nenahrávej na GitHub**. Je v `.gitignore`, takže se do gitu sám nedostane. Když klíč unikne, zruš ho na webu služby a vytvoř nový.

---

## 5. Spuštění

```powershell
.\.venv\Scripts\python.exe run_server.py
```

Až se vypíše `Uvicorn running on http://127.0.0.1:8010`, otevři v prohlížeči **<http://127.0.0.1:8010>**.

- Okno PowerShellu **nech otevřené**. Dokud běží, běží i aplikace.
- Aplikaci vypneš klávesami **Ctrl + C** v okně PowerShellu.
- Aplikace je dostupná jen z tohoto počítače, ne z internetu.

Když nahoře na stránce vidíš žluté varování „Chybí OPENAI_API_KEY“ nebo „Chybí BRIGHTDATA_TOKEN“, zkontroluj soubor `.env` (kapitola 4.4) a aplikaci restartuj.

---

## 6. První nastavení v aplikaci

Databáze je na začátku prázdná. Postupuj takto:

1. **Zdroje → Zpravodajské weby:** vlož adresu webu (např. `https://czechia.news-pravda.com/`) a klikni **Přidat web**. Aplikace najde feed a ukáže ukázku vytaženého textu.
   - U news-pravda doplň v části **Pokročilé**:
     - selektor bloku s textem: `.article__text`
     - atribut s původním zdrojem: `data-source-url`
2. **Zdroje → Facebook profily:** vlož adresu veřejné FB stránky a klikni **Přidat profil**. Ověření (stažení 1 příspěvku) trvá asi minutu.
3. **Zdroje → Vazby:** vyber web a profil a klikni **Propojit**. Porovnávají se jen propojené dvojice.
4. Klikni vpravo nahoře na **Spustit teď**. První běh stáhne:
   - příspěvky z posledních 14 dní,
   - články, které web aktuálně nabízí (u news-pravda posledních 48 hodin),
   - pak proběhne AI zpracování.

   Trvá to 20–40 minut. Průběh uvidíš na stránce **Běhy**.
5. Výsledky najdeš na stránce **Shody**, týdenní PDF shrnutí na stránce **Shrnutí** (tlačítko **Vytvořit**).

### Nepovinné: dotažení starších článků

Když chceš hned porovnat celý uplynulý týden, spusť v **novém** okně PowerShellu (web může běžet dál):

```powershell
cd $HOME\Documents\news-fb-matcher
.\.venv\Scripts\python.exe backfill_articles.py --from 2026-09-20
```

Datum uprav podle potřeby. U news-pravda jde asi o 450 článků za den, tedy nezanedbatelnou spotřebu OpenAI (viz kapitola 9).

---

## 7. Příští spuštění

Po restartu počítače stačí:

```powershell
cd $HOME\Documents\news-fb-matcher
.\.venv\Scripts\python.exe run_server.py
```

a otevřít <http://127.0.0.1:8010>. Nová data stáhneš tlačítkem **Spustit teď**.

---

## 8. Automatický noční běh (nepovinné)

Aby se data stahovala sama každou noc ve 00:00, i když web neběží, vytvoř úlohu v Plánovači úloh Windows. Spusť tyto příkazy **ve složce projektu**:

```powershell
$dir = (Get-Location).Path
$action = New-ScheduledTaskAction -Execute "$dir\.venv\Scripts\python.exe" -Argument "run_nightly.py" -WorkingDirectory $dir
$trigger = New-ScheduledTaskTrigger -Daily -At 00:00
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable
Register-ScheduledTask -TaskName "news-fb-matcher" -Action $action -Trigger $trigger -Settings $settings -Description "Noční stažení a párování článků a příspěvků"
```

- Když byl počítač o půlnoci vypnutý, běh se spustí hned po jeho zapnutí (`StartWhenAvailable`).
- Úloha běží pod tvým účtem, když jsi přihlášený. Správcovská práva nejsou potřeba.
- Každých 7 dní se na konci běhu automaticky vytvoří PDF report.
- Výsledek každého běhu uvidíš ve webu na stránce **Běhy**.
- Vyzkoušet hned: `Start-ScheduledTask -TaskName "news-fb-matcher"`
- Zrušit: `Unregister-ScheduledTask -TaskName "news-fb-matcher" -Confirm:$false`

---

## 9. Náklady

| Služba | Za co | Orientačně |
|---|---|---|
| OpenAI | AI rozbor článků a příspěvků, posouzení shod, report | ~0,8 mil. tokenů denně při ~450 článcích (news-pravda); dotažení týdne zpětně ~3 mil. |
| Bright Data | příspěvky z FB | ~20–30 příspěvků denně = zdarma v rámci 5 000 záznamů měsíčně |
| Telegram | zjištění ID kanálů | zdarma |

Skutečnou spotřebu OpenAI vidíš na <https://platform.openai.com/usage>. Každý běh ji navíc vypisuje na stránce **Běhy** (řádek „Tokeny“).

---

## 10. Aktualizace na novou verzi z GitHubu

```powershell
cd $HOME\Documents\news-fb-matcher
git pull
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Data (`data\`) i klíče (`.env`) zůstanou zachované.

---

## 11. Řešení problémů

| Problém | Řešení |
|---|---|
| `py` / `python` / `git` *is not recognized* | Zavři a znovu otevři PowerShell. Pomůže to po každé instalaci. Když to nepomůže, přeinstaluj Python se zaškrtnutým „Add python.exe to PATH“. |
| Po napsání `python` se otevře Microsoft Store | Používej `py -3.12` (viz kapitola 2), případně vypni v Nastavení → Aplikace → Aliasy spouštění aplikací položky „python.exe“. |
| *running scripts is disabled on this system* | Nepoužívej `Activate.ps1`. Spouštěj vždy `.\.venv\Scripts\python.exe …` jako v návodu. |
| `pip install` hlásí chybu | Zkontroluj internet. Spusť znovu `.\.venv\Scripts\python.exe -m pip install --upgrade pip` a potom instalaci knihoven. |
| *address already in use* / port 8010 obsazen | Aplikace už běží v jiném okně. Buď ji použij, nebo ji tam vypni (Ctrl + C). Případně změň `port` v `config.toml`. |
| Žluté varování „Chybí OPENAI_API_KEY“ | Soubor `.env` chybí, je jinde než v kořeni projektu, nebo je klíč špatně zapsaný. Po opravě aplikaci restartuj. |
| Bright Data: *Customer is not active* | Účet není ověřený, přidej platební metodu (kapitola 4.2). |
| Běh skončil stavem `error` | Otevři stránku **Běhy**. V logu je napsáno, který krok selhal a proč. |
| Web nejde přidat („nenašel jsem feed“) | Web nemá RSS ani sitemapu. Zkus zadat přímo adresu jeho RSS feedu. |

---

## Pro autora: nahrání projektu na GitHub

Nahrávat se má jen kód. Data a klíče ne, o to se stará `.gitignore`. Ve složce projektu:

```powershell
git init
git add .
git status
```

Ve výpisu `git status` **nesmí být** `.env` ani nic ze složky `data/`. Pak:

```powershell
git commit -m "news-fb-matcher"
git branch -M main
git remote add origin ODKAZ_NA_GITHUB
git push -u origin main
```

Repozitář na GitHubu doporučuji nastavit jako **Private**. API klíče pošli příjemci zvlášť, ne přes GitHub.
