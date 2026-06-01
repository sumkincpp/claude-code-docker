.PHONY: build scan help

IMAGE_NAME ?= ccd
IMAGE_TAG ?= latest
IMAGE_FULL ?= $(IMAGE_NAME):$(IMAGE_TAG)
GRYPE_VERSION ?= latest
USER_UID ?= $(shell id -u)
USER_GID ?= $(shell id -g)

help:
	@echo "Available targets:"
	@echo "  make build           - Build Docker image"
	@echo "  make scan            - Scan Docker image for vulnerabilities using grype"
	@echo "  make help            - Show this help message"

.PHONY: build docker-build docker-scan
build: docker-build
docker-build:
	docker build \
		--build-arg USER_UID=$(USER_UID) \
		--build-arg USER_GID=$(USER_GID) \
		-t $(IMAGE_FULL) .

GRYPE_VERSION ?= v0.110.0
GRYPE_IMAGE ?= anchore/grype:$(GRYPE_VERSION)

docker-scan: docker-build
	docker run --rm \
		-v /var/run/docker.sock:/var/run/docker.sock \
		$(GRYPE_IMAGE) \
		--fail-on high \
		$(IMAGE_FULL) | grep -E "high|critical|^-|Vulnerability|ID"

ccd-build:
	ccd build