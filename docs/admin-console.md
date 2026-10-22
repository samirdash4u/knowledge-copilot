# Running everything from the browser

After the copilot is started, everything else can be done in the web UI: downloading the model,
indexing, adding documents and tenants, managing users and access, and running the evaluation.
The command-line tools (`manage.py`, `copilot`) still work and do the same things.

## Start it

| Setup | Start with | Then |
| --- | --- | --- |
| Docker | `python scripts/manage.py up` (or `make up`) | open the URL it prints |
| No Docker | `copilot serve` (venv active, Ollama running) | open the URL it prints |

Sign in as an admin. On a fresh install that is `carol` with `COPILOT_DEMO_PASSWORD` (default
`ChangeMe123`); you are asked to choose a new password first.

On a fresh install, go to **Admin > Status** and:

1. Click **Download model** if the LLM card says the model is not downloaded yet (about 5 GB,
   once). With Docker this goes into the `ollama` container; natively into your local Ollama.
2. Click **Run ingest**. The first run also downloads the embedding and reranker models.

Both run in the background with a progress bar. You can leave the page, or close the browser,
and come back.

## Who can do what

The server checks every request against the signed-in account. Hiding a button in the page is
only a convenience; calling the API directly gets the same answer.

| | Employee and manager | Admin |
| --- | --- | --- |
| Upload documents | to tenants they can use, visible to their own role or lower | any tenant they can use, any role |
| Replace a document | only one their role can see | yes |
| List documents | those their role can see | all |
| Run ingest | yes | yes |
| See and cancel jobs | see every ingest; cancel their own | everything |
| Delete documents | no | yes |
| Status, tenants, users, evaluation | no | yes |

An account that still has to change its password can't use any of it until it does.

## Documents

Click **Documents** in the chat header (everyone has it; admins also find it as the first tab
under **Admin**).

- **Upload a document**: pick the workspace and who may see it, then a PDF, Word (`.docx`),
  Markdown or text file (25 MB by default, `COPILOT_MAX_UPLOAD_MB`). It is converted to tagged
  Markdown in `data/corpus/<tenant>/`, just like `add-doc`. Any front matter in an uploaded
  Markdown file is replaced, so a file can't choose its own tenant or role. `<!-- acl: manager -->`
  markers inside it still work, and can only restrict a section further.
- **Index it straight away** queues an ingest after the upload. Without it, the document waits
  until someone clicks **Run ingest**. The list shows each document as *indexed* or *waiting for
  ingest*.
- **Add suggested questions** writes questions about the document into the tenant's
  `tenant.yaml`, as `add-doc` does (with the LLM if it's reachable, otherwise from the headings).
- **Replace a document with the same id** overwrites an existing document. Without it, an upload
  whose document id (from the file name, or the id you type) is taken is refused.

## Ingest

Ingest re-reads all of `data/corpus`, embeds it and swaps the new index into the running server.
Questions keep working while it runs (with Qdrant, the new index is written to a new collection
and the old one is kept until the next ingest). Nobody has to restart anything afterwards, in
Docker or natively.

Only one background job runs at a time, so an ingest started during an evaluation waits for it.
Clicking **Run ingest** while an ingest is already waiting doesn't queue a second one: the waiting
one will see your changes.

## Admin > Status

Shows whether the LLM, the vector store (Qdrant) and the answer cache (Redis) are reachable, the
index size per tenant, documents changed since the last ingest, the main settings, and recent
jobs with their logs. Settings come from `.env`; change them there and restart
(`python scripts/manage.py up`, or restart `copilot serve`).

## Admin > Tenants

Create a tenant (a folder in `data/corpus` plus its `tenant.yaml`), open it to whole roles (which
covers users added later) and/or give individual users access. Existing tenants can be relabelled
and opened to different roles.

## Admin > Users

Create accounts with a temporary password, change a user's role or tenants, reset a password
(they must change it at their next sign-in) and delete accounts. Changing someone's role signs
them out. You can't remove your own admin role or delete yourself, and the last admin can't be
demoted or deleted.

## Admin > Evaluation

Runs the evaluation set (`eval/questions.jsonl`) in the background: quick (10 questions), 20, or
all. It scores retrieval in every mode, gets LLM answers in the configured mode, checks for leaks
and cache poisoning, and writes `reports/eval.json`, which the **KPIs** page reads. *Retrieval
only* skips the LLM and finishes in under a minute; *LLM judge* adds an LLM-scored faithfulness
and takes longer. The results table lists each mode and every question that did not pass, with
the reason.

## Docker notes

`data/corpus` and `reports` are mounted writable into the API container, so uploads and eval
reports land in those folders on your machine. On Linux, files the API creates there get the
owner of the folder, not root, so you can still edit them. Job history is kept in memory and is
cleared when the API restarts; what the jobs produced (documents, index, eval report) is not.
