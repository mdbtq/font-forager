VENV   := .venv
PYTHON := $(VENV)/bin/python
PIP    := $(VENV)/bin/pip

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show this help
	@awk 'BEGIN {FS = ":.*?## "} \
		/^##@ / {printf "  \033[1m%s\033[0m\n", substr($$0, 5); next} \
		/^[a-zA-Z0-9_\/.-]+:.*?## / {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}' \
		$(MAKEFILE_LIST)

##@ Environment

# Create the venv and install deps; re-runs when requirements.txt changes.
$(PYTHON): requirements.txt
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt
	touch $(PYTHON)

.PHONY: env/setup
env/setup: $(PYTHON) ## Create the venv and install dependencies

.PHONY: env/clean
env/clean: ## Remove the venv
	rm -rf $(VENV)

##@ Run

.PHONY: fonts/download
fonts/download: $(PYTHON) ## Download the fonts a page loads + build specimen.html, e.g. make fonts/download URL=https://example.com
	@test -n "$(URL)" || { echo "Usage: make fonts/download URL=https://example.com"; exit 1; }
	$(PYTHON) font-forager.py "$(URL)"

##@ Data

.PHONY: data/clean
data/clean: ## Remove the data/ output directory
	rm -rf data

# Deprecated aliases, kept so existing habits and scripts keep working.
# They carry no ## comment, so `make help` lists only the names above.
.PHONY: setup clean clean-data run
setup: env/setup
clean: env/clean
clean-data: data/clean
run: fonts/download
