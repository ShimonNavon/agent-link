.PHONY: run test deploy install

run:   ## hub on :18080 with a throwaway database
	PORT=18080 AGENT_LINK_DB=/tmp/agent-link.db PUBLIC_URL=http://127.0.0.1:18080 python3 hub.py

test:  ## hub + MCP + CLI smoke tests, no network beyond localhost
	python3 tests/test_link.py

deploy: ## publish the hub to the k8s cluster (ubuntu03, NodePort 30110)
	./deploy/deploy.sh

install: ## install the CLI on this machine from the live hub
	curl -fsSL http://ubuntu03:30110/install.sh | sh
