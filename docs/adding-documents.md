# Adding documents

The copilot answers from whatever is indexed under `data/corpus`. Adding a policy is not model
training: the language model never changes. Ingest splits your files into chunks, embeds them and
stores them in the search index; at question time the copilot retrieves the chunks the caller is
allowed to see and the model answers from those, with citations. A new document is searchable as
soon as it is ingested.

## 1. Put the file in the right folder

```
data/corpus/
  policy/        <- tenant "policy"
  engineering/   <- tenant "engineering"
```

The first folder under `data/corpus` is the tenant. Subfolders below it are fine
(`policy/hr/leave.md` is still in `policy`).

Supported formats: Markdown (`.md`, `.markdown`), plain text (`.txt`), HTML (`.html`, `.htm`) and
PDF (`.pdf`). Markdown gives the best results, because each heading becomes its own section and
citations point at that section. PDFs are split by page and scanned PDFs without a text layer
produce nothing.

## 2. Tag it with a role

Every document needs a `min_role`: the lowest role allowed to see it (`employee`, `manager` or
`admin`; higher roles see everything lower roles see). **A document without `min_role` is
admin-only**, so a forgotten tag hides the document rather than leaking it.

For Markdown and text, put the tags in front matter at the very top of the file:

```markdown
---
doc_id: travel-policy-2027
title: Travel Policy 2027
min_role: employee
source: HR handbook, January 2027
---

# Travel Policy 2027

## Booking

Book flights through the travel portal at least 14 days ahead.

## Approval limits
<!-- acl: manager -->

Managers can approve trips up to $3,000.
```

For PDF and HTML, put the same keys in a sidecar file next to it, named after the full file name
plus `.meta.yaml`. For `data/corpus/policy/benefits.pdf` that is
`data/corpus/policy/benefits.pdf.meta.yaml`:

```yaml
doc_id: benefits-2027
title: Benefits Guide 2027
min_role: employee
source: Benefits Guide 2027 (PDF)
```

| Key | Required | Meaning |
| --- | --- | --- |
| `min_role` | yes (else admin-only) | `employee`, `manager` or `admin` |
| `doc_id` | no | stable id used in citations; defaults to the file name. Must be unique within a tenant |
| `title` | no | shown in citations; defaults to the file name (or the PDF/HTML title) |
| `source` | no | where the document came from; shown with the citation |
| `tenant` | no | overrides the folder name; normally leave it out |

**Restricting one section.** In Markdown, a `<!-- acl: manager -->` (or `admin`) line inside a
section raises that section and its subsections to that role, as in "Approval limits" above. A
marker can only tighten access, never loosen it below the document's `min_role`. PDFs can't be
restricted per section; split the restricted part into its own file with a higher `min_role`.

**Replacing a policy.** Edit the file in place, or delete the old file and add the new one. Ingest
rebuilds the whole index each time, so removed files and removed sections disappear from answers.

## 3. Re-index

With the Docker setup, from the repository folder (Windows and Linux alike):

```bash
python scripts/manage.py ingest
```

This rebuilds the index from `data/corpus` and restarts the API so it loads the new index. The
corpus folder is mounted into the container, so there is no image to rebuild. Cached answers are
keyed by corpus version, so old answers aren't served after the change.

For a native install (no Docker), run `copilot ingest` in your virtual environment and restart
`copilot serve`.

The command prints a summary such as `documents`, `chunks`, `by_tenant` and `by_min_role` (also
saved in `data/index/manifest.json`). Check that the document count went up and that no warning
says `has no min_role; defaulting to admin`.

## 4. Check it

Sign in to the web UI as a user of the right tenant and role and ask about the new document, or
ask from the command line (Docker):

```bash
docker compose run --rm api copilot ask "How far ahead must I book flights?" --role employee --tenant policy
```

Then ask as a lower role about a restricted section to confirm it is refused: the demo users are
alice (employee), bob (manager) and carol (admin).

## Adding a new tenant

Create a new top-level folder, for example `data/corpus/legal/`, and ingest. Users only see the
tenants listed on their account, so give the tenant to the users who need it:

```bash
docker compose run --rm api copilot users add dana --role manager --tenants policy,legal
```

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| Answers never mention the new document | ingest wasn't re-run, or the file extension isn't supported |
| Only admins can see it | `min_role` is missing or the front matter isn't the first thing in the file |
| `duplicate doc_id` error | two files in the same tenant share a `doc_id` (or file name) |
| `unknown role` error | `min_role` or an `acl:` marker has a typo |
| A PDF yields no chunks | it is a scanned image with no text layer; run OCR first |
