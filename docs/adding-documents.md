# Adding documents

The copilot answers from whatever is indexed under `data/corpus`. Adding a policy is not model
training: the language model never changes. Ingest splits your files into chunks, embeds them and
stores them in the search index; at question time the copilot retrieves the chunks the caller is
allowed to see and the model answers from those, with citations. A new document is searchable as
soon as it is ingested.

## Quick way: import a PDF or Word file

`add-doc` converts a `.pdf` or `.docx` into a tagged Markdown file in `data/corpus/<tenant>/`:

```bash
python scripts/manage.py add-doc "C:\Users\me\Downloads\Travel Policy.docx" --tenant policy --min-role employee --ingest
```

Leave out `--tenant` or `--min-role` and it asks for them. `--ingest` re-indexes straight away;
without it, review the new file and run `python scripts/manage.py ingest` yourself. Other options:
`--title`, `--doc-id` (also the file name; defaults to the source file name), `--source` and
`--force` to replace a document you imported before. For a native install, the same command is
`copilot add-doc <file> ...`.

How the conversion works:

- **Word:** Heading 1–3 styles become sections, bullet and numbered lists stay lists, and tables
  become Markdown tables. The document's Title property (or Title-styled paragraph) becomes the
  title.
- **PDF:** PDFs carry no heading structure, so it recognises numbered headings such as
  `2.1 Scope` and short ALL-CAPS lines. If it finds none, each page becomes a section
  (`Page 1`, `Page 2`, ...). Scanned PDFs without a text layer are refused; run OCR first.
- Old `.doc` files aren't supported; save them as `.docx`.

The result is plain Markdown, so open it and check it before indexing: fix any headings the PDF
conversion missed, and add `<!-- acl: manager -->` under a heading to restrict that section (see
below). The original file isn't copied into the corpus.

The rest of this guide covers adding files by hand.

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

A tenant is a separate workspace with its own documents, for example `legal` next to `policy`
and `engineering`. Give access by role, by user, or both:

```bash
python scripts/manage.py add-tenant legal --label "Legal" --grant admin,manager
```

This creates `data/corpus/legal/` with a `tenant.yaml`:

```yaml
label: "Legal"                  # shown in the web UI's workspace picker
roles: ["admin", "manager"]     # every user with one of these roles can use the tenant
```

Access by role is a rule, not a one-time copy: a manager added next month gets Legal too. In
`--grant`, the role names `employee`, `manager` and `admin` mean roles; anything else is a
username, so `--grant manager,alice` opens it to all managers plus alice. `--grant-all` gives
every existing user access. The new workspace shows up in the web UI right away. Then add
documents to it and re-index:

```bash
python scripts/manage.py add-doc "Contracts.docx" --tenant legal --min-role employee --ingest
```

Roles and `min_role` work together: `--grant admin,manager` decides who can open the Legal
workspace, and each document's `min_role` still decides what they see inside it.

**Suggested questions.** After someone picks a workspace, the chat shows a few questions they
can click. A new tenant gets them automatically after `ingest`: they are made from the titles and
section headings of the documents that user's role can see, so a restricted section's heading is
never suggested to a lower role. To write your own, add them to the tenant's `tenant.yaml`, either
one list for everyone or one per role (a role without a list gets the automatic ones):

```yaml
label: "Marketing"
suggestions:
  employee:
    - "Who approves posts on company social media accounts?"
  manager:
    - "Which campaigns need CMO sign-off?"
```

Changes show up the next time the workspace is opened; no restart or re-index needed.
`data/corpus/policy/tenant.yaml` holds the examples for the Company policy workspace.

Managing access later:

```bash
python scripts/manage.py users list                          # who has which tenants
python scripts/manage.py users grant dana legal              # add a tenant to one account
python scripts/manage.py users revoke dana legal             # remove it (takes effect at once)
python scripts/manage.py users add erin --role employee --tenants legal
```

To change which roles can use a tenant, edit `roles` in its `tenant.yaml`; the change applies on
the next request, and removing a role signs those users out of that workspace. Revoking a tenant
from one user doesn't override their role: if the tenant is open to managers, a manager keeps it.

For a native install, use `copilot tenants add ...`, `copilot tenants list` and
`copilot users grant ...`. Tenant names are lowercase letters, digits, `-` and `_`. To remove a
tenant, delete its folder, revoke it from its users and re-index.

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| Answers never mention the new document | ingest wasn't re-run, or the file extension isn't supported |
| The sign-in page still shows only two workspaces | the API container is running code from before the upgrade: run `python scripts/manage.py up`, then reload the page with Ctrl+F5 |
| A new workspace isn't in the picker for a user | grant it with `users grant <user> <tenant>` |
| A workspace lost its name, suggestions or role access | its `tenant.yaml` has a YAML error and is being ignored; the API log names it. Put questions in quotes |
| Only admins can see it | `min_role` is missing or the front matter isn't the first thing in the file |
| `duplicate doc_id` error | two files in the same tenant share a `doc_id` (or file name) |
| `unknown role` error | `min_role` or an `acl:` marker has a typo |
| A PDF yields no chunks | it is a scanned image with no text layer; run OCR first |
