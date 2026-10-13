---
doc_id: deploy-guide
title: Deployment Guide
min_role: employee
source: synthetic / Acme Financial (fictional)
---

# Deployment Guide

## How to deploy

Services are deployed with `acmectl deploy <service> --env <env>`. Every production deploy starts as a canary that receives 10% of traffic for 30 minutes before full rollout.

## Deploy freeze

There are no production deploys on Fridays after 2pm local time, or during the last 5 business days of a quarter. Emergency fixes during a freeze need approval from the on-call engineering manager.

## Rollback

Run `acmectl rollback <service>` to return to the previous release. Rollbacks do not need approval.
