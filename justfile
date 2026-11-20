# Run `just` to list targets.

apps := "bookstack invoiceninja indico"

default:
    @just --list

# Fetch the pinned benchmark and install Python dependencies.
setup:
    git submodule update --init
    uv sync

# Lint.
lint:
    uv run ruff check .

# Recompute scenarios/selection.json (one test per distinct bug).
select:
    uv run python -m hoh.selection --apps {{apps}} --out scenarios/selection.json

# Generate code-only pytest files for the selected tests into build/tests.
gen:
    uv run python -m hoh.generator --selection scenarios/selection.json --out build/tests

# Rewrite the selected bug scripts into build/payloads and refresh the disclosure report.
neutralize:
    uv run python -m hoh.neutralize --selection scenarios/selection.json --out build/payloads --report docs/neutralization-report.md

# Write each arm's system prompt and user message into build/prompts.
prompts max_runs="4":
    uv run python -m hoh.prompts --out build/prompts --max-runs {{max_runs}}

# Write the reference fix of every target in a rules file into build/reference_fixes.
fixes rules="scenarios/rename/rules.yaml":
    uv run python -m hoh.rules fixes --rules {{rules}} --tests-root build/tests --out build/reference_fixes

# The paper's numbers from the stages' job records (read only) into build/analysis.
analyze results="results" stages="1a 1b 2-mixed 2-c-assert explore" *args:
    uv run python -m hoh.analysis --results {{results}} --stages {{stages}} {{args}}

# The session tables of data/ from the stages' job records (read only), checked against
# the provenance file of the analysis.
export results="results" stages="1a 1b 2-mixed 2-c-assert explore" provenance="analysis/provenance.json":
    uv run python -m hoh.artifact --results {{results}} --stages {{stages}} --provenance {{provenance}} --out data
