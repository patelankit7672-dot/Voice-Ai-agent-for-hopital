# Deploying

## Read this first

This app keeps appointments in a **JSON file**, and staff sessions plus the
rate limiter in **process memory**. That shapes which hosts suit it.

| Host | Bookings persist? | Suitable for |
| --- | --- | --- |
| Render / Railway / Fly.io (with a disk) | **Yes** | a real demo, staff testing |
| Vercel / Netlify / Lambda | **No** — see below | showing the UI and the voice flow |

On serverless, each request may hit a fresh instance with an empty temp
directory. A booking can therefore disappear immediately, or be invisible to
the very next request. `/api/health` reports this as `ephemeral_storage: true`
so a deployment describes itself honestly.

To make a deployment durable, either set `APPOINTMENTS_PATH` to a path on a
mounted volume, or replace the flat-file store with a database.

---

## Vercel

The repository already contains `vercel.json` and `api/index.py`, which
re-exports the FastAPI app as an ASGI handler.

```bash
npm i -g vercel
vercel login
vercel --prod
```

Then set the environment variables, either in the Vercel dashboard under
**Settings → Environment Variables**, or from the CLI:

```bash
vercel env add ASSEMBLYAI_API_KEY production
vercel env add SARVAM_API_KEY production
vercel env add ADMIN_PASSCODE production
vercel env add APP_ENV production          # value: production
```

`APP_ENV=production` matters: it marks the staff session cookie `Secure` and
disables the interactive API docs at `/docs`.

`CORS_ALLOW_ORIGINS` can be left unset. The page and the API share an origin,
so the browser never issues a cross-origin request.

Redeploy after adding variables — Vercel does not apply them to an existing
deployment:

```bash
vercel --prod
```

### What works and what does not on Vercel

Works: the full patient page, English and Hindi voice (the browser talks to
AssemblyAI directly over WebSocket, and Hindi speech is a normal HTTPS call to
`/api/speech/hindi`), doctor and department lookup, and the staff portal UI.

Does not: durable bookings, and staff sessions across instances — signing in
on one instance does not sign you in on another, so the portal may ask you to
sign in again mid-session.

---

## Render (recommended for a working demo)

Render gives a persistent disk and a single long-lived process, which is what
this app actually wants.

1. New → Web Service → connect the repository.
2. **Build command:** `pip install -r requirements.txt`
3. **Start command:**
   ```
   uvicorn backend.main:app --host 0.0.0.0 --port $PORT
   ```
4. Add a disk, mounted at `/var/data`.
5. Environment variables:
   ```
   ASSEMBLYAI_API_KEY=...
   SARVAM_API_KEY=...
   ADMIN_PASSCODE=...
   APP_ENV=production
   APPOINTMENTS_PATH=/var/data/appointments.json
   ```

`APPOINTMENTS_PATH` on the mounted disk is what makes bookings survive a
restart. With it set, `/api/health` reports `ephemeral_storage: false`.

---

## Microphone requires HTTPS

`getUserMedia` only works on `https://` or `localhost`. Every host above
terminates TLS for you, so this is automatic — but it does mean you cannot
test the voice agent by visiting a deployment over plain `http://`, or by IP
address on a LAN.

---

## After deploying

```bash
curl https://<your-deployment>/api/health
```

Expect:

```json
{"status":"ok","voice_ready":true,"demo_data":true,"ephemeral_storage":false}
```

- `voice_ready: false` → `ASSEMBLYAI_API_KEY` is not reaching the app.
- `ephemeral_storage: true` on a host you expected to be durable → set
  `APPOINTMENTS_PATH` to your mounted volume.

Open `/admin` and sign in with `ADMIN_PASSCODE` to confirm the staff portal.
If it returns 503, the passcode is unset and the portal is switched off by
design.
