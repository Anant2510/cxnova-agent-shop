.PHONY: install test health health-live serve console console-test data package

install:            ## install the framework with model adapters and dev tools
	pip install -e ".[anthropic,openai,mcp,dev]"

data:               ## regenerate the Meridian Mutual demo data
	python3 scripts/generate_sample_data.py

test:               ## full Python suite (offline model)
	CAF_LLM_PROVIDER=offline python3 -m pytest -q

health:             ## liveness probe of all 28 agents (offline model)
	caf health

health-live:        ## liveness probe on the configured model provider
	caf health --live

serve:              ## REST + MCP + A2A on :8080
	caf serve --port 8080

console:            ## build the hosted console (needs node: npm install --prefix console)
	python3 console/build.py

console-test:       ## run every browser agent headless under a no-eval CSP
	python3 console/test/harness.py && python3 console/test/harness.py --llm mock

package:            ## source zip in dist/ (no caches, run outputs or node_modules)
	python3 scripts/package.py
