.PHONY: test demo scan
test:
	bash tests/run-isolated.sh tests
	PYTHONDONTWRITEBYTECODE=1 python3 tools/test_sensitivity_scan.py
	bash tests/test_notify_guard.sh
demo:
	PYTHONDONTWRITEBYTECODE=1 python3 demo.py
scan:
	bash tools/sensitivity-scan.sh
