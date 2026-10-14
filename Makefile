.PHONY: install train run test simulate
install:
	python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
train:
	.venv/bin/python -m streamguard.train
run:
	.venv/bin/uvicorn streamguard.api:create_app --factory --reload
test:
	.venv/bin/python -m pytest -q
simulate:
	.venv/bin/python -m streamguard.simulate
