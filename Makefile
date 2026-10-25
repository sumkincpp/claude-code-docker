.PHONY: build scan help

IMAGE_NAME ?= ccd
IMAGE_TAG ?= latest
IMAGE_FULL ?= $(IMAGE_NAME):$(IMAGE_TAG)
GRYPE_VERSION ?= latest

help:
	@echo "Available targets:"
	@echo "  make build           - Build Docker image"
	@echo "  make scan            - Scan Docker image for vulnerabilities using grype"
	@echo "  make help            - Show this help message"

.PHONY: build docker-build docker-scan
build: docker-build
docker-build:
	docker build -t $(IMAGE_FULL) .

GRYPE_VERSION ?= v0.110.0
GRYPE_IMAGE ?= anchore/grype:$(GRYPE_VERSION)

docker-scan: docker-build
	@SCAN_DATE=$$(date -u +"%Y-%m-%dT%H:%M:%SZ"); \
	SCAN_TMP=$$(mktemp scan-results.XXXXXX.txt); \
	{ \
	  docker run --rm \
	    -v /var/run/docker.sock:/var/run/docker.sock \
	    $(GRYPE_IMAGE) \
	    --fail-on high \
	    $(IMAGE_FULL) | grep -E "high|critical|^-|Vulnerability|ID" || true; \
	  echo ""; \
	  echo "Scanned: $$SCAN_DATE"; \
	  echo "Image:   $(IMAGE_FULL)"; \
	} > "$$SCAN_TMP" && mv "$$SCAN_TMP" scan-results.txt
	@echo "Scan results saved to scan-results.txt"

docker-run:
	docker run --rm -it $(IMAGE_FULL) /bin/bash

ccd-build:
	ccd build