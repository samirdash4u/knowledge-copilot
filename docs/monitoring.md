# Monitoring the KPIs

Knowledge Copilot tracks seven KPIs in two places:

- **Live traffic.** Every question asked through the web UI or `POST /ask` is recorded: latency,
  outcome, how well the answer is supported, whether anything restricted showed up, tokens and
  estimated cost. Records go into a SQLite file (`data/metrics.db`, or `/index/metrics.db` in
  Docker). They hold no question or answer text and no user names, only tenant, role, retrieval
  mode and numbers. They are kept for 30 days (`COPILOT_METRICS_RETENTION_DAYS`).
- **The eval.** `copilot eval` runs the 40 known questions in `eval/questions.jsonl` and writes
  `reports/eval.json`. Recall@5 and MRR need known right answers, so they come only from here.

Nothing extra has to be installed or started: it works the same natively on Windows and in
Docker. Prometheus is optional.

## The KPIs

| KPI | Live | Eval | Target | What it means |
|---|---|---|---|---|
| Recall@5 | | ✓ | ≥ 0.90 | Share of questions where a correct document is in the top 5 retrieved chunks |
| MRR | | ✓ | ≥ 0.75 | Mean of 1 / rank of the first correct document (1.0 = always first) |
| Groundedness | ✓ | ✓ | ≥ 90% | Share of answers (refusals left out) whose cited sources support them |
| Faithfulness | ✓ | ✓ | ≥ 0.80 | Average share of each answer's content found in the sources it cites |
| Unauthorized retrieval rate | ✓ | ✓ | 0% | Share of questions where a document the caller may not see was retrieved, or its canary text reached the answer |
| p95 latency | ✓ | ✓ | ≤ 10 s | 95% of questions are answered at least this fast |
| Cost per successful answer | ✓ | ✓ | ≤ 0.0001 USD | Estimated cost of all questions divided by the successful ones |

How each one is measured:

- **Faithfulness** is scored on every answer without another model call. Each sentence scores the
  share of its content words that appear in the chunks it cites (exact words, or the first five
  letters so "approved" matches "approval"); a sentence without a valid citation scores 0. It is a
  cheap proxy. For a stricter number, run the eval with `--judge`: the local LLM reads each answer
  with its cited sources and lists the sentences they don't support. The KPI view then uses the
  judged score for the eval column.
- **Groundedness** counts an answer as grounded when it has citations and its faithfulness is at
  least `COPILOT_GROUNDED_THRESHOLD` (0.7).
- **Unauthorized retrieval** is checked three ways on every question: the retriever's own
  post-retrieval check (a hit is blocked, the user gets an error, and it is counted as `blocked`),
  every retrieved and cited chunk is checked again against the caller's role and tenant, and the
  answer is searched for the `CANARY-*` tokens of documents the caller may not see. Any hit is also
  logged as an error. The eval does the same per question, plus a cache-poisoning pass, and exits
  with an error if anything leaks.
- **p95 latency** covers every question that didn't fail, including cache hits and refusals.
- **Cost** has no API bill behind it, since the model runs locally. Each question is priced as
  `seconds × COPILOT_COST_PER_COMPUTE_SECOND + tokens × the per-1k token prices`. The default
  compute price is 0.00000625 USD per second: a laptop drawing about 150 W at 0.15 USD per kWh. The
  token prices default to 0; set them to see what the same traffic would cost on a hosted model.
  Token counts come from Ollama. A **successful answer** is a grounded answer live; in the eval it
  is a question with the right refusal decision, the expected facts and, if answered, a grounded
  answer. The cost of failed and refused questions is spread over the successful ones.

## See the KPIs

### In the web UI

Sign in as an admin (for example `carol`) and click **KPIs** in the header. The page shows each
KPI with its target, the live value for the window you pick (last hour, 24 hours, 7 or 30 days)
and the value from the latest eval, each marked *on target* or *off target*. Other roles don't see
the button, and `GET /kpis` returns 403 for them.

### On the command line

Natively (with `.venv` activated):

```powershell
copilot kpis                # live: last 24 hours, plus the latest eval
copilot kpis --hours 168    # last 7 days
copilot kpis --json         # everything, as JSON
```

With Docker:

```powershell
python scripts/manage.py kpis
python scripts/manage.py kpis --hours 168
```

Example:

```
| KPI | target | live, last 24 h | eval |
|---|---|---|---|
| Recall@5 | ≥ 0.900 | – | 0.967 ✓ |
| MRR | ≥ 0.750 | – | 0.928 ✓ |
| Groundedness | ≥ 90.0% | 96.0% ✓ | 100.0% ✓ |
| Faithfulness | ≥ 0.800 | 0.912 ✓ | 0.954 ✓ |
| Unauthorized retrieval rate | ≤ 0.0% | 0.0% ✓ | 0.0% ✓ |
| p95 latency | ≤ 10,000 ms | 7,420 ms ✓ | 8,105 ms ✓ |
| Cost per successful answer | ≤ 0.000100 USD | 0.000041 USD ✓ | 0.000047 USD ✓ |
```

(Illustrative numbers. Yours depend on the model and the laptop.)

### Refresh the eval numbers

```powershell
copilot eval                       # natively; writes reports/eval.json
copilot eval --quick               # 10 questions, one of each type: a few minutes
copilot eval --judge               # also scores faithfulness with the LLM (slower)
python scripts/manage.py eval      # with Docker (takes the same options)
python scripts/manage.py eval --quick
```

The LLM answers each question once, in `COPILOT_RETRIEVAL_MODE`. The other modes are scored on
retrieval only, which takes seconds. Each answer prints a progress line with the time left. With
the model on an 8 GB GPU a full run takes roughly 5 to 10 minutes; on the CPU (Docker without
`COPILOT_GPU=true`) it can take 40 minutes or more, so start with `--quick`.

The KPI view reads the mode set in `COPILOT_RETRIEVAL_MODE` (default `hybrid_rerank`) from the
report, or the last mode in it. The running server picks up a new report straight away; no restart
is needed.

### With Prometheus (optional)

`GET /metrics` serves the same numbers in the Prometheus text format: request counts by outcome,
grounded answers, unauthorized retrievals, cache hits, faithfulness, a latency histogram, LLM
tokens, total cost, and the latest eval KPIs as `copilot_eval_*` gauges. It needs no token and
holds counts only, so keep the server on `127.0.0.1` or behind your firewall if those should stay
private, or turn recording and the endpoint off with `COPILOT_METRICS_ENABLED=false`.

Prometheus runs natively on Windows too (download it from prometheus.io). A minimal
`prometheus.yml`:

```yaml
scrape_configs:
  - job_name: knowledge-copilot
    scrape_interval: 15s
    static_configs:
      - targets: ["localhost:8000"]
```

Queries for the KPIs:

```promql
# p95 latency over 5 minutes
histogram_quantile(0.95, sum by (le) (rate(copilot_request_latency_seconds_bucket[5m])))

# groundedness
sum(rate(copilot_grounded_answers_total[1h]))
  / sum(rate(copilot_requests_total{status="answered"}[1h]))

# faithfulness
sum(rate(copilot_answer_faithfulness_sum[1h])) / sum(rate(copilot_answer_faithfulness_count[1h]))

# unauthorized retrieval rate (alert if above 0)
sum(rate(copilot_unauthorized_retrievals_total[1h])) / sum(rate(copilot_requests_total[1h]))

# cost per successful answer
sum(rate(copilot_cost_total[1h])) / sum(rate(copilot_grounded_answers_total[1h]))

# offline KPIs from the latest eval
copilot_eval_recall_at_5
copilot_eval_mrr
```

The counters start from zero when the server restarts; Prometheus handles that. The SQLite records
behind the web view and `copilot kpis` survive restarts.

## Settings

All in `.env` (prefix `COPILOT_`):

| Setting | Default | |
|---|---|---|
| `METRICS_ENABLED` | `true` | record each question and serve `/metrics` and `/kpis` |
| `METRICS_DB` | `data/metrics.db` | where the records go (`/index/metrics.db` in Docker) |
| `METRICS_RETENTION_DAYS` | `30` | older records are deleted at startup |
| `EVAL_REPORT` | `reports/eval.json` | the eval report the offline KPIs come from |
| `GROUNDED_THRESHOLD` | `0.7` | faithfulness an answer needs to count as grounded |
| `COST_CURRENCY` | `USD` | label only |
| `COST_PER_COMPUTE_SECOND` | `0.00000625` | price of one second of answering |
| `COST_PER_1K_PROMPT_TOKENS` | `0` | price per 1,000 tokens the LLM reads |
| `COST_PER_1K_COMPLETION_TOKENS` | `0` | price per 1,000 tokens the LLM writes |
| `TARGET_RECALL_AT_5`, `TARGET_MRR`, `TARGET_GROUNDED_RATE`, `TARGET_FAITHFULNESS`, `TARGET_UNAUTHORIZED_RATE`, `TARGET_P95_MS`, `TARGET_COST_PER_ANSWER` | see the table above | the targets shown next to each KPI |

`python scripts/manage.py clean --data` deletes the records along with the other data.
