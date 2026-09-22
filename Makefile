.PHONY: all check format lint syntax test build clean

PYTHON ?= python3

ifeq ($(OS),Windows_NT)
BACKEND ?= pcan
else
BACKEND ?= socketcan
endif

all: check build

check:
	$(PYTHON) run_checks.py

format:
	$(PYTHON) run_checks.py --format

lint:
	$(PYTHON) -m ruff check can_tcp_gateway.py can_tcp_client.py tests/test_protocol.py

syntax:
	$(PYTHON) -c "from pathlib import Path; [compile(Path(p).read_text(), p, 'exec') for p in ('can_tcp_gateway.py', 'can_tcp_client.py', 'tests/test_protocol.py')]; print('syntax ok')"

test:
	$(PYTHON) -m pytest -q -p no:cacheprovider

build:
	$(PYTHON) -m PyInstaller --onefile --clean --name CanTcpGateway \
		--hidden-import can.interfaces.$(BACKEND) can_tcp_gateway.py
	$(PYTHON) -m PyInstaller --onefile --clean --name CanTcpClient \
		--hidden-import can.interfaces.$(BACKEND) can_tcp_client.py

clean:
	$(PYTHON) -c "import shutil; [shutil.rmtree(p, ignore_errors=True) for p in ('build', 'dist', '__pycache__', '.pytest_cache', '.ruff_cache')]"
