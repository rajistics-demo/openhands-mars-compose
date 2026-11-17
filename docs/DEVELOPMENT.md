# Development and offline verification

Run commands from the package root.

## Reproducing the offline tests

These steps need a licensed 1.88.0 source tree, the Enterprise dev dependencies (`uv sync --all-groups`) and PostgreSQL. The Enterprise test fixtures normally start PostgreSQL through Docker.

```bash
mkdir -p /tmp/e188
git -C <enterprise-1.88.0 checkout> archive 1.88.0 | tar -x -C /tmp/e188
python3 scripts/mars_apply.py enterprise /tmp/e188
cp -r enterprise/tests/unit/. /tmp/e188/tests/unit/
(cd /tmp/e188 && patch -p1 < "$OLDPWD/enterprise/tests/test_api_key_store_expiry.patch")
(cd /tmp/e188 && uv run pytest tests/unit/app_server/test_do_mars*.py tests/unit/server/routes/test_service_run_key_expiry.py \
   tests/unit/storage/test_api_key_store.py tests/unit/test_migration_graph.py tests/unit/app_server/test_sandbox_store.py)
```

For Automation, run `mars_apply.py automation <1.30.0 checkout>`, copy `automation/tests/*` into its `tests/`, then run `uv run pytest tests`. To run the installer portability tests, set `ENTERPRISE_188_TREE` and `AUTOMATION_130_TREE` and run `python -m pytest scripts/test_mars_apply.py`.
