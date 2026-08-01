# Run the Luna gateway probe from another region

This experiment compares three accounts against the same Luna model:

1. Personal direct OpenAI, using `PERSONAL_OPENAI_API_KEY`.
2. Internal direct OpenAI, using `OPENAI_API_KEY`.
3. OpenRouter's shared OpenAI account, using `OPENROUTER_API_KEY`.

All calls target `gpt-5.6-luna` with reasoning effort `none`. The runner omits
`temperature` and `top_p` equally on every route, randomizes route order, records
cold and verified same-socket warm samples, and replaces the entire matched
three-way block whenever one request fails. Failures and retries are recorded
separately from the requested success-only latency analysis.

The main runner uses only the Python standard library plus this repository.
Statistical analysis and live progress snapshots additionally use NumPy/SciPy.

## Set up the other machine

```bash
git clone https://github.com/sdcoffey/openbench.git
cd openbench
python3 -m pip install -e .
python3 -m pip install -r experiments/luna-region-probe/requirements-analysis.txt
```

Provide all three API keys through your shell or secret manager. If they already
live in `~/.zshrc_local`, load them without printing their values:

```bash
source ~/.zshrc_local
```

The exact required environment-variable names are:

```text
PERSONAL_OPENAI_API_KEY
OPENAI_API_KEY
OPENROUTER_API_KEY
```

OpenRouter BYOK is intentionally not used. Its stream must explicitly confirm
`is_byok=false` for every successful shared-account sample.

## Record the region and run a smoke test

```bash
python3 experiments/luna-region-probe/probe_region.py

python3 experiments/luna-region-probe/run_large_three_arm.py \
  --samples 2 \
  --workers 2 \
  --output experiments/luna-region-probe/large-three-arm-smoke

python3 experiments/luna-region-probe/debug_large_upstream.py
```

The region helper records safe cloud-region and edge-colocation details without
storing the machine's hostname or public IP. The upstream debug probe verifies
that OpenRouter does not forward mismatched sampling parameters.

## Run the full comparison

```bash
python3 experiments/luna-region-probe/openrouter_credit_snapshot.py before

python3 experiments/luna-region-probe/run_large_three_arm.py \
  --samples 2000 \
  --workers 8

python3 experiments/luna-region-probe/openrouter_credit_snapshot.py after

python3 experiments/luna-region-probe/analyze_large_three_arm.py
python3 experiments/luna-region-probe/fetch_large_generations.py
python3 experiments/luna-region-probe/enrich_large_buckets.py
python3 experiments/luna-region-probe/write_large_findings.py
python3 experiments/luna-region-probe/validate_large_three_arm.py
```

The full run produces 2,000 successful cold and 2,000 successful warm
three-account blocks: 12,000 successful measured calls plus approximately 6,000
warm primers. It resumes safely if interrupted; rerun the same command with the
same sample target and output directory.

OpenRouter spend is hard-capped at a conservative $1.95, with reservations for
in-flight and unpriced calls. At observed Luna promotional prices, a full run
cost about $0.06. Direct-account spend is deliberately uncapped.

From another terminal, inspect progress with:

```bash
python3 experiments/luna-region-probe/progress_large_three_arm.py
```

The final report is written to:

```text
experiments/luna-region-probe/large-three-arm/FINDINGS.md
```

Raw request IDs, OpenRouter generations, sanitized headers, latency samples,
cost snapshots, and failed attempts remain inside ignored local output
directories. Never commit result files or API credentials.
