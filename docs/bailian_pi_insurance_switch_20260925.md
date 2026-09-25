# Bailian switch for Pi + Insurance (2026-09-25)

## Isolation (Claude untouched)
| Lane | Gateway | Upstream | Upstream key (last4) | Local master key |
|------|---------|----------|----------------------|------------------|
| Claude (in-flight) | :4001 | aliyun_maas `token-plan…/apps/anthropic` | …akk- | LITELLM_MASTER_KEY …eway (len21) |
| Insurance + Pi | :4002 | Bailian OpenAI `dashscope…/compatible-mode/v1` | …8ebe | INSURANCE_LITELLM_MASTER_KEY …eway (len26) |

Local keys differ (4002 rejects Claude master key). Upstream keys differ (Bailian ≠ MaaS). Ledger: shared `llm_calls.jsonl` but case_id `PI_*` / Insurance lane attribution / Claude `A*` headers.

## What was switched (additive)
- `llm_gateway/.env.bailian` + `.env.bailian_openai` (working key from insurance backup; old bak …Uh7w is Arrearage)
- `start_insurance_litellm.sh --profile` + sticky profile (preflight won't clobber Bailian back to penguin)
- Pi provider `local-relay-bailian` → :4002; profile `pi_coding_local_relay_bailian.yaml`; config `configs/experiments/pi_0922_shared_bailian.yaml`
- Pi thinking remains **off**

## Run started
- stamp `20260925_000123_0922_bailian_pi_ins`
- root `eval_runs/triple_datasetA_20260925_000123_0922_bailian_pi_ins`
- agents `insurance,pi` cases A01,C20 (PI_A01,PI_C20)
- Insurance 2/2 done; Pi in progress at report time

## Continue full 0922
```bash
cd eval_harness
# ensure :4002 bailian + :18063 up first
../llm_gateway/start_insurance_litellm.sh   # sticky bailian_openai
PYTHONPATH=src python3 -m eval_harness.suite \
  --bundle bundles/120_prompt_only_0922_shared.jsonl \
  --pi-config configs/experiments/pi_0922_shared_bailian.yaml \
  --agents insurance,pi --cases A01,A02,... # or full list
```
Do **not** run `start_litellm.sh --profile bailian` while Claude is on :4001.
