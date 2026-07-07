You are debugging a production issue in a FastAPI analytics backend (MetaQL/FunnelHub: funnel analytics running SQL on Trino and Druid).

## Bug report

Users are reporting intermittent wrong numbers on FunnelHub Trino-lane funnel runs, but only under load:

- When two users run different funnels at the same time (or one user fires two runs quickly), the Trino-lane counts sometimes come back at the WRONG grain — e.g. a funnel configured to count distinct customers returns counts that match a different funnel's configured metric dimension.
- Re-running the same funnel alone, seconds later, gives the correct number.
- The generated SQL (from the preview endpoint) always looks correct when inspected one request at a time.
- Druid-lane numbers for the same runs are always correct.
- No errors or exceptions in the logs; the responses are 200s with plausible-looking but wrong data.

## Your task

1. Find the root cause. Trace the actual code paths involved; do not guess.
2. State the root cause precisely: which object, which fields, which lifecycle, why only under concurrency, why Druid is unaffected.
3. Propose a minimal, safe fix (describe the code change precisely; you may quote code but you cannot edit files).
4. Describe the regression test you would add that fails before the fix and passes after.

Your entire final message is the deliverable: root cause → mechanism → fix → test.
