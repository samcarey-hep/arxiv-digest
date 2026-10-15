# arXiv Digest

A personal daily digest of hep-ph, hep-ex, nucl-th and hep-th. Every weekday morning a GitHub
Action:

1. reads **today's announcement** from arXiv's RSS feed (only new papers and cross-lists, never
   the previous day's papers);
2. has Google's Gemini Flash-Lite (free tier) score every abstract 0–10 against the interest
   profile in `config.yaml`;
3. has Gemini Flash **read the full text** of the top ~10 papers (arXiv HTML, falling back to the
   PDF) and write a summary covering the novel idea, the method, the key results, why it matters
   to you, and caveats;
4. publishes a static site on GitHub Pages, with an archive, search and saved papers, and can
   optionally email you the digest.

## One-time setup (about 10 minutes)

1. **Create a repo.** On GitHub, create a new repository called `arxiv-digest`. It can be private
   if you have GitHub Pro (MSU/education accounts get Pro free); otherwise make it public. Then
   push this folder:
   ```bash
   cd arxiv-digest
   git init && git add . && git commit -m "arXiv digest"
   git branch -M main
   git remote add origin https://github.com/<you>/arxiv-digest.git
   git push -u origin main
   ```
2. **Add a free Gemini API key.** Go to aistudio.google.com, sign in with a Google account, and
   click **Get API key → Create API key** (no card needed). Then, in the repo, go to Settings →
   Secrets and variables → Actions → New repository secret, and add it as `GEMINI_API_KEY`.
3. **Turn on Pages.** Go to Settings → Pages → Build and deployment, choose *Deploy from a
   branch*, and pick branch `main` with folder `/docs`.
4. **Allow the workflow to push.** Go to Settings → Actions → General → Workflow permissions and
   choose *Read and write*.
5. **Do a first run.** Go to Actions → *Daily arXiv digest* → *Run workflow*. After about 3
   minutes the site is live at `https://<you>.github.io/arxiv-digest/`.
6. **Add it to your iPhone.** Open the site in Safari, then tap Share → **Add to Home Screen**.
   It opens full screen like an app. On a Mac, use Safari → File → **Add to Dock**.

### Optional: email

Add these secrets: `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD` and `EMAIL_TO`. For
Gmail, use `smtp.gmail.com` with port `465` and an *app password*.

## Schedule

arXiv posts at 8 PM ET Sunday to Thursday, and the RSS feed refreshes around midnight. The
workflow tries at about 1:20, 5:20 and 7:50 AM ET on weekdays. A run does nothing if that day's
digest already exists or the feed hasn't changed, so you never get duplicates or stale days.
Each page is headed with the arXiv announcement date it covers.

## Tuning

Everything is in `config.yaml`:

- `interests` is a prose profile, the most important knob. Be specific.
- `favorite_authors` get a score boost.
- `summaries.max_detailed` and `summaries.min_score` set how many full summaries you get.
- `auto-flash` / `auto-flash-lite` always pick the newest Gemini model of that family that works
  on the free tier. You can also give an exact model id.

## Cost

**Free.** A day's digest uses only about 4–5 requests (ranking abstracts in batches of 60,
summarizing 5 papers per request), well within the Gemini free tier. If the free quota ever
runs out, that day falls back to keyword ranking without summaries, and the next day recovers.
On the free tier Google may use the prompts to improve its products; everything sent is
public arXiv text.

To use Claude instead (paid, roughly $10–14 a month with Sonnet or $2–3 with Haiku), set
`provider: anthropic` and the models `auto-haiku` / `auto-sonnet` in `config.yaml`, and add an
`ANTHROPIC_API_KEY` secret.

## Local use

```bash
pip install -r requirements.txt
python run.py --dry-run          # no API key: keyword ranking, preview the site in docs/
GEMINI_API_KEY=... python run.py
python -m pytest -q tests        # offline tests (feed parsing, text extraction, full pipeline)
```
