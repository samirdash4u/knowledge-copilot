# Adding documents

The copilot answers from whatever is indexed under `data/corpus`. Adding a policy is not model
training: the language model never changes. Ingest splits your files into chunks, embeds them and
stores them in the search index; at question time the copilot retrieves the chunks the caller is
allowed to see and the model answers from those, with citations. A new document is searchable as
soon as it is ingested.

## Easiest: upload in the browser

Click **Documents** in the web UI, choose the workspace and who may see the document, pick a PDF,
Word, Markdown or text file and click **Upload**, with **Index it straight away** ticked. Everyone
can do this for the workspaces they use, but only for documents their own role can see; admins
can also create tenants there (**Admin > Tenants**). See [admin-console.md](admin-console.md). The
rest of this page is the command-line way, and explains the tags the upload writes for you.

## Docker or not: which commands to use

Use the commands for the way you started the copilot. Each step below has both.

| You started it with | Run commands as | Needs |
| --- | --- | --- |
| Docker (`python scripts/manage.py up`) | `python scripts/manage.py ...` | Docker Desktop running ("Engine running") |
| No Docker (`copilot serve`) | `copilot ...` | the virtual environment active: `.venv\Scripts\Activate.ps1` on Windows, `source .venv/bin/activate` on Linux |

Run both kinds from the repository folder. `manage.py` always goes through Docker, so without
Docker it stops with *the Docker daemon is not reachable*; use the `copilot` commands instead.

Both setups read documents and `tenant.yaml` files from the same `data/corpus` folder, but they
keep user accounts apart: Docker in its `index` volume, the native install in `data/users.db`.
Access granted to a user by name in one setup doesn't carry over to the other; access by role
(`roles` in `tenant.yaml`) applies to both.

## Quick way: import a PDF or Word file

`add-doc` converts a `.pdf` or `.docx` into a tagged Markdown file in `data/corpus/<tenant>/`.

**With Docker:**

```bash
python scripts/manage.py add-doc "C:\Users\me\Downloads\Travel Policy.docx" --tenant policy --min-role employee --ingest
```

`--ingest` re-indexes and restarts the API straight away. Without it, review the new file and run
`python scripts/manage.py ingest` yourself.

**Without Docker** (virtual environment active):

```powershell
copilot add-doc "C:\Users\me\Downloads\Travel Policy.docx" --tenant policy --min-role employee
copilot ingest
```

Then stop `copilot serve` with Ctrl+C and start it again, so it loads the new index. Natively,
`add-doc` has no `--ingest` option.

In both, leave out `--tenant` or `--min-role` and it asks for them. Other options: `--title`,
`--doc-id` (also the file name; defaults to the source file name), `--source`, `--force` to
replace a document you imported before, and `--no-suggest` (see
[Suggested questions](#suggested-questions)).

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

The next three sections cover adding files by hand; they are the same with or without Docker
until the re-index step.

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

**With Docker** (Windows and Linux alike):

```bash
python scripts/manage.py ingest
```

This rebuilds the index from `data/corpus` and restarts the API so it loads the new index. The
corpus folder is mounted into the container, so there is no image to rebuild.

**Without Docker:**

```powershell
copilot ingest
```

Then stop `copilot serve` with Ctrl+C and start it again.

Cached answers are keyed by corpus version, so old answers aren't served after the change. The
command prints a summary such as `documents`, `chunks`, `by_tenant` and `by_min_role` (also saved
in `manifest.json` in the index folder). Check that the document count went up and that no
warning says `has no min_role; defaulting to admin`.

## 4. Check it

Sign in to the web UI as a user of the right tenant and role and ask about the new document, or
ask from the command line:

```bash
# with Docker
docker compose run --rm api copilot ask "How far ahead must I book flights?" --role employee --tenant policy
# without Docker
copilot ask "How far ahead must I book flights?" --role employee --tenant policy
```

Then ask as a lower role about a restricted section to confirm it is refused: the demo users are
alice (employee), bob (manager) and carol (admin).

## Adding a new tenant

A tenant is a separate workspace with its own documents, for example `legal` next to `policy`
and `engineering`. Give access by role, by user, or both.

**With Docker:**

```bash
python scripts/manage.py add-tenant legal --label "Legal" --grant admin,manager
python scripts/manage.py add-doc "Contracts.docx" --tenant legal --min-role employee --ingest
```

**Without Docker:**

```powershell
copilot tenants add legal --label "Legal" --grant admin,manager
copilot add-doc "Contracts.docx" --tenant legal --min-role employee
copilot ingest
```

Then restart `copilot serve` (Ctrl+C, then start it again).

Either way, this creates `data/corpus/legal/` with a `tenant.yaml`:

```yaml
label: "Legal"                  # shown in the web UI's workspace picker
roles: ["admin", "manager"]     # every user with one of these roles can use the tenant
```

Access by role is a rule, not a one-time copy: a manager added next month gets Legal too. In
`--grant`, the role names `employee`, `manager` and `admin` mean roles; anything else is a
username, so `--grant manager,alice` opens it to all managers plus alice. `--grant-all` gives
every existing user access. The new workspace shows up in the web UI's picker right away.

Roles and `min_role` work together: `--grant admin,manager` decides who can open the Legal
workspace, and each document's `min_role` still decides what they see inside it.

### Suggested questions

After someone picks a workspace, the chat shows a few questions they can click. `add-doc` writes
them for you: for each document it imports, it asks the local LLM for a likely question per
section and adds them to the top of the tenant's `tenant.yaml`, per role. A question about a
manager-only section is only added for managers and admins. If Ollama isn't running, it uses the
section headings instead (`What does the Brand Guidelines say about "Logo usage"?`). Each role
keeps the six newest; pass `--no-suggest` to skip this for a document.

```yaml
label: "Marketing"
suggestions:
  employee:
    - "How much space should I leave around the logo?"
  manager:
    - "Who signs off on a large campaign?"
```

You can edit or reorder them in `tenant.yaml`; changes show up the next time the workspace is
opened, with no restart or re-index. A tenant with no list at all gets questions made from its
section headings. `data/corpus/policy/tenant.yaml` holds the examples for Company policy. The
first time a workspace is opened after the server starts, the questions can take a few seconds
to appear while the index loads.

### Managing access later

**With Docker:**

```bash
python scripts/manage.py users list                          # who has which tenants
python scripts/manage.py users grant dana legal              # add a tenant to one account
python scripts/manage.py users revoke dana legal             # remove it (takes effect at once)
python scripts/manage.py users add erin --role employee --tenants legal
```

**Without Docker:** the same commands as `copilot users list`, `copilot users grant dana legal`,
`copilot users revoke dana legal` and `copilot users add erin --role employee --tenants legal`.
`copilot tenants list` shows every tenant with its document count.

To change which roles can use a tenant, edit `roles` in its `tenant.yaml`; the change applies on
the next request, and removing a role signs those users out of that workspace. Revoking a tenant
from one user doesn't override their role: if the tenant is open to managers, a manager keeps it.

Tenant names are lowercase letters, digits, `-` and `_`. To remove a tenant, delete its folder,
revoke it from its users and re-index.

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| `manage.py` says *the Docker daemon is not reachable* | Docker Desktop isn't running. Start it, or if you run the copilot without Docker, use the `copilot ...` commands instead |
| *'copilot' is not recognized* | the virtual environment isn't active in this window: run `.venv\Scripts\Activate.ps1` (Windows) or `source .venv/bin/activate` (Linux) from the repository folder |
| Answers never mention the new document | ingest wasn't re-run, the server wasn't restarted after a native `copilot ingest`, or the file extension isn't supported |
| The sign-in page still shows only two workspaces | with Docker, the API container is running code from before the upgrade: run `python scripts/manage.py up`, then reload the page with Ctrl+F5 |
| A new workspace isn't in the picker for a user | grant it with `users grant <user> <tenant>` in the setup you sign in to (Docker and native keep separate accounts) |
| A workspace lost its name, suggestions or role access | its `tenant.yaml` has a YAML error and is being ignored; the server log names it. Put questions in quotes |
| Only admins can see it | `min_role` is missing or the front matter isn't the first thing in the file |
| `duplicate doc_id` error | two files in the same tenant share a `doc_id` (or file name) |
| `unknown role` error | `min_role` or an `acl:` marker has a typo |
| A PDF yields no chunks | it is a scanned image with no text layer; run OCR first |
