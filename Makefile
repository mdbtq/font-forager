VENV   := .venv
PYTHON := $(VENV)/bin/python
PIP    := $(VENV)/bin/pip

.PHONY: help setup run clean clean-data

help:
	@echo "Targets:"
	@echo "  make setup                 Create the venv and install dependencies"
	@echo "  make run URL=<url>         Download the fonts a page loads + build specimen.html"
	@echo "  make clean                 Remove the venv"
	@echo "  make clean-data            Remove the data/ output directory"

# Create the venv and install deps; re-runs when requirements.txt changes.
$(PYTHON): requirements.txt
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt
	touch $(PYTHON)

setup: $(PYTHON)

run: $(PYTHON)
	@test -n "$(URL)" || { echo "Usage: make run URL=https://example.com"; exit 1; }
	$(PYTHON) font-forager.py "$(URL)"

clean:
	rm -rf $(VENV)

clean-data:
	rm -rf data
