# Deploy del Coach su Railway (cloud, gratis, sempre acceso)

Tempo totale: ~15-20 minuti la prima volta. Poi mai più.
Costo: gratis con $5 di credito mensile incluso (l'app consuma ~$2/mese).

---

## Passo 1 — Crea un account GitHub (2 min)

Serve solo perché Railway prende il codice da lì.

1. Vai su **[github.com](https://github.com)** → bottone verde "Sign up" in alto a destra.
2. Username: scegli quello che vuoi (es. `edmondoval`).
3. Email: la tua email Apple va bene.
4. Password: una nuova, non riusarne una.
5. Conferma l'email.

Fatto. Hai un account GitHub.

---

## Passo 2 — Crea un repository col codice del Coach (5 min)

Un "repository" è una cartella su GitHub.

1. Sulla home di GitHub clicca **"+" in alto a destra → New repository**.
2. Repository name: `coach-personale` (o quello che vuoi).
3. Lascia "Public" selezionato.
4. **NON** spuntare niente sotto (no README, no .gitignore, no license).
5. Bottone verde "Create repository".

Ora sei sulla pagina vuota del repo. Vedi un'istruzione "or upload an existing file".

6. Clicca **"uploading an existing file"** (è un link blu nel testo della pagina).

7. Dal tuo Mac, prendi la cartella `run for fun app` (quella che usi con Cowork) e **trascina TUTTI i suoi file e cartelle** dentro la pagina GitHub.
   - Devono esserci: cartella `app/`, file `Dockerfile`, `railway.json`, `.gitignore`, e tutti i `.command`/`.md` (anche se per il cloud non servono, non rompono niente)

8. In fondo alla pagina, nel riquadro "Commit changes":
   - Titolo: `primo upload`
   - Clicca **"Commit changes"** (bottone verde)

Aspetta 10-20 secondi che si carichi tutto.

---

## Passo 3 — Crea l'account Railway (2 min)

1. Vai su **[railway.app](https://railway.app)** → bottone "Login" o "Start a New Project".
2. Clicca **"Login with GitHub"** — autorizza Railway a leggere i tuoi repo.

Railway ti dà $5 di credito gratis al mese. Il Coach consuma circa $1-2/mese, quindi resti sempre dentro il free tier.

---

## Passo 4 — Deploya l'app (3 min)

1. Su Railway, click **"New Project"**.
2. Scegli **"Deploy from GitHub repo"**.
3. Seleziona il repo `coach-personale` che hai appena creato.
4. Railway inizia il build (vedi i log scorrere). Aspetta 3-5 minuti.

Durante il build, prepariamo la persistenza dei dati:

5. Una volta che il build è partito, vai nel pannello del progetto → **Settings** → scorri fino a **Volumes** → **"+ New Volume"**.
6. Mount path: `/data`
7. Size: 1 GB (basta e avanza)
8. Conferma.

Questo serve per non perdere i dati Garmin e il database ogni volta che Railway riavvia l'app.

---

## Passo 5 — Imposta le variabili d'ambiente (2 min)

Sempre nel pannello del progetto su Railway → **Variables** → **"+ New Variable"**.

Aggiungi una variabile alla volta:

| Nome | Valore | Note |
|------|--------|------|
| `ANTHROPIC_API_KEY` | `sk-ant-api03-...` | La chiave Claude che hai creato |
| `DATA_DIR` | `/data` | Il volume montato al passo 4 |

Salva. L'app si riavvia da sola con le nuove variabili.

---

## Passo 6 — Apri l'app (1 min)

1. Sempre nel pannello Railway → **Settings** → scorri fino a **Networking** → **"Generate Domain"**.
2. Railway ti dà un URL tipo `coach-personale-production.up.railway.app`.
3. Aprilo dal browser. Vedi la schermata di benvenuto.

---

## Passo 7 — Primo setup nell'app (5 min)

Nella pagina di benvenuto:

1. **Email e password Garmin** → inserisci, clicca "Accedi".
2. Se Garmin ti chiede il **codice MFA**, ti arriva sulla mail. Inserisci nella finestra che compare.
3. Una volta loggato, clicca "Inizia sincronizzazione". L'app scarica le ultime 50 attività + 14 giorni di sonno/wellness.
4. Per importare TUTTO lo storico: vai in Impostazioni → "Importa archivio Garmin" → carica il file zip dell'export Garmin (quello che hai già scaricato).

---

## Da qui in poi

- Apri l'URL Railway in qualsiasi browser, anche dal telefono.
- L'app ricorda tutto: token Garmin, chiave Claude, tutte le attività e chat.
- Quando hai una nuova corsa, clicca "Sincronizza ora" nelle Impostazioni. L'app va a pescare le nuove attività direttamente da Garmin Connect.
- L'AI coach funziona già: ti basta scrivere una domanda nel tab "Coach".

---

## Se qualcosa va storto

### Garmin login fallisce dal cloud

Garmin a volte blocca i datacenter. Se vedi errori "blocked" o "captcha required" al primo login:
- Prova a fare il login da un IP residenziale: temporaneamente metti l'app sul tuo Mac (ci torniamo dopo), fai il login UNA volta, copia i token sul server.
- In alternativa, scrivimi in Cowork: c'è un workaround.

### Build fallisce su Railway

- Controlla che il file `Dockerfile` sia nella radice del repo (non dentro `app/`).
- Vai nei log del deploy su Railway e mandami il messaggio d'errore.

### L'app si addormenta

Su Railway free tier l'app NON si addormenta (a differenza di Render free). Resta sempre attiva finché hai credito.

---

## Costi attesi

- Railway free: $5/mese di credito incluso
- Il Coach consuma: ~$1-2/mese (RAM bassa, CPU bassa, traffico minimo)
- Claude API: ~$1-3/mese in base a quanto chatti col coach
- **Totale realistico: $1-3/mese, dentro il free tier Railway**

Sei sempre dentro al gratuito a meno che tu non lasci girare cose pesanti.
