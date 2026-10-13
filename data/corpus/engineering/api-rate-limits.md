---
doc_id: api-rate-limits
title: Public API Rate Limits
min_role: employee
source: synthetic / Acme Financial (fictional)
---

# Public API Rate Limits

## Limits

Each API key is limited to 1,000 requests per minute and 100,000 requests per day. Burst traffic up to 50 requests per second is allowed.

## When a limit is exceeded

The API returns HTTP 429 Too Many Requests with a Retry-After header in seconds. Clients should back off exponentially.
