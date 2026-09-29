# Eval terminal output

This is the terminal output of `.venv/bin/python -u -m eval.run`, the run that wrote `reports/eval.json`. The process exited 0.

```
resetting the database
DO
GRANT
DROP TABLE
CREATE TABLE
INSERT 0 10
CREATE TABLE
CREATE INDEX
INSERT 0 17
CREATE TABLE
GRANT
GRANT
GRANT
GRANT
reset support: tables recreated from db/seed.sql
{"status": "ok", "model": "gemini-3.8-flash", "db": "ok", "toolbox": "ok", "judge": "ok", "masker": "ok", "mem0": "ok", "phoenix": "ok"}
wrote /Users/muthukumars/Documents/workspace/agentic-customer-support-using-adk/reports/eval.json
read these traces for gate 5: success f946699921c98a132d7c113a3926a7db failing ed0073db1966130190c2898273c16cd5
```
