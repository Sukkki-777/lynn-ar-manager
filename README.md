# Lynn AR Agent Hackathon Demo

Lynn is a human-in-the-loop accounts receivable agent for PO intake, invoice creation, payment follow-up, Stripe payment reconciliation, and buyer-risk reasoning.

## What The Demo Shows

- Upload or seed PO data.
- Lynn identifies orders ready to invoice and prepares invoice PDFs, Stripe payment links, and customer email drafts.
- Customer-facing actions wait in `Waiting for your OK`.
- Morning check-in prepares pre-due reminders, overdue escalations, partial-payment follow-ups, and a 90-day cash forecast.
- Stripe sandbox payments are synced back into the dashboard and mark invoices as paid.
- Kimi-assisted Ask Lynn answers cash-flow and AR workflow questions.

## Local Run

1. Copy `.env.example` to `.env`.
2. Add secrets as needed:

```env
LLM_PROVIDER=kimi
KIMI_API_KEY=...
STRIPE_SECRET_KEY=sk_test_...
EXA_API_KEY=...
```

3. Install dependencies:

```bash
pip install -r requirements.txt
```

4. Start the app:

```bash
python server.py
```

5. Open `http://127.0.0.1:8765`.

## Deploy

This repo is deployable as a single Python web service. For the hackathon live URL, Render or Railway is lower-risk than Vercel because the current app uses a Python backend plus local JSON/PDF files.

### Render

1. Push this folder to a public GitHub repo.
2. In Render, create a new Blueprint or Web Service from the repo.
3. Use:
   - Build command: `pip install -r requirements.txt`
   - Start command: `HOST=0.0.0.0 python server.py`
4. Add environment variables:
   - `SEED_DEMO_DATA=1`
   - `LLM_PROVIDER=kimi`
   - `KIMI_API_KEY=...`
   - `KIMI_MODEL=kimi-k2.6`
   - `KIMI_BASE_URL=https://api.moonshot.ai/v1`
   - `STRIPE_SECRET_KEY=sk_test_...`
   - `EXA_API_KEY=...` if available

## Stripe

The app supports two payment-update paths:

- Public webhook: `/api/stripe/webhook`
- Demo sync: `/api/stripe/sync-payments`

The frontend calls Stripe sync on refresh so sandbox payments can appear even if webhook forwarding is not configured.

## DoraHacks Submission Checklist

- Public GitHub repo URL
- Live URL
- Presentation file link: `.ppt` or `.keynote`, with demo recording embedded
