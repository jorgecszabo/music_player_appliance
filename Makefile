# Thin automation over scripts/*.sh. One board at a time:
#   make image BOARD=rpi1        (or BOARD=rpi1 make image; default pandaboard)
# Run `make help` for the targets.
#
# Stamp files (build/.stamps/<board>/) record finished steps. They are rebuilt when
# their inputs change. Packages and kernels are built in containers, so an apk
# deleted by hand is not noticed: use `make rebuild-<step>` to force a step.

BOARD ?= pandaboard
export BOARD

ifeq ($(wildcard boards/$(BOARD)/board.conf),)
$(error unknown board '$(BOARD)': no boards/$(BOARD)/board.conf (boards: $(filter-out _template,$(notdir $(wildcard boards/*)))))
endif

# board.conf is shell; read the few variables we need from it.
conf = $(shell . boards/$(BOARD)/board.conf; echo "$$$(1)")
ALPINE_ARCH    := $(call conf,ALPINE_ARCH)
KERNEL_PACKAGE := $(call conf,KERNEL_PACKAGE)
UBOOT_TAG      := $(call conf,UBOOT_TAG)
ALPINE_VERSION := $(shell sed -n 's/^ALPINE_VERSION=//p' scripts/env.sh)

STAMPS := build/.stamps/$(BOARD)
# Generic packages every board needs (the appliance itself is rebuilt by build-image.sh).
PACKAGES := pipewire mpv shairport-sync panda-spectrum

# Our own kernel is an aport; boards on Alpine's kernel (linux-rpi) have nothing to build.
OWN_KERNEL := $(or $(wildcard boards/$(BOARD)/aports/$(KERNEL_PACKAGE)/APKBUILD),$(wildcard aports/$(KERNEL_PACKAGE)/APKBUILD))

files = $(shell find $(1) -type f ! -path '*/src/*' ! -path '*/pkg/*' 2>/dev/null)

.DEFAULT_GOAL := all
.PHONY: all help container kernel uboot packages image ota \
        rebuild-container rebuild-kernel rebuild-uboot rebuild-packages rebuild-image rebuild-ota \
        check test-graph test-ota demo clean

all: ota ## image + signed OTA bundle for $(BOARD)

help: ## this list
	@echo "BOARD=$(BOARD) (arch $(ALPINE_ARCH), kernel $(KERNEL_PACKAGE))"
	@echo "boards: $(filter-out _template,$(notdir $(wildcard boards/*)))"
	@echo
	@grep -hE '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | sed 's/:.*## /\t/' | expand -t16

# ---- pipeline: container -> kernel/packages -> image -> ota ----

container: $(STAMPS)/container ## build container for the board's arch
$(STAMPS)/container: container/Containerfile scripts/build-container.sh scripts/env.sh
	@mkdir -p $(@D)
	scripts/build-container.sh
	@touch $@

kernel: $(STAMPS)/kernel ## cross-build + package the board's kernel (skipped for stock kernels)
ifneq ($(OWN_KERNEL),)
$(STAMPS)/kernel: $(STAMPS)/container $(call files,$(dir $(OWN_KERNEL))) scripts/cross-build-kernel.sh
	@mkdir -p $(@D)
	scripts/build-package.sh $(KERNEL_PACKAGE)
	@touch $@
else
$(STAMPS)/kernel:
	@mkdir -p $(@D)
	@echo "$(KERNEL_PACKAGE): Alpine's kernel, nothing to build"
	@touch $@
endif

uboot: $(STAMPS)/uboot ## build U-Boot (skipped if the board has none)
ifneq ($(UBOOT_TAG),)
$(STAMPS)/uboot: scripts/build-u-boot.sh $(call files,boards/$(BOARD)/u-boot) boards/$(BOARD)/board.conf
	@mkdir -p $(@D)
	scripts/build-u-boot.sh
	@touch $@
else
$(STAMPS)/uboot:
	@mkdir -p $(@D)
	@echo "no UBOOT_TAG in board.conf, nothing to build"
	@touch $@
endif

packages: $(STAMPS)/packages ## build pipewire, mpv, shairport-sync, panda-spectrum
$(STAMPS)/packages: $(STAMPS)/container $(foreach p,$(PACKAGES),$(call files,aports/$(p)))
	@mkdir -p $(@D)
	@for p in $(PACKAGES); do echo ">>> $$p"; scripts/build-package.sh $$p || exit 1; done
	@touch $@

# build-image.sh rebuilds panda-appliance itself, so appliance/ is an input here.
image: $(STAMPS)/image ## SD card image -> build/sdimage/$(BOARD)/
$(STAMPS)/image: $(STAMPS)/kernel $(STAMPS)/uboot $(STAMPS)/packages \
		$(call files,appliance) $(call files,aports/panda-appliance) \
		$(call files,boards/$(BOARD)) scripts/build-image.sh scripts/build-rootfs.sh
	@mkdir -p $(@D)
	scripts/build-image.sh
	@touch $@

ota: $(STAMPS)/ota ## signed update bundle -> build/ota/panda-$(BOARD)-<version>.pota
$(STAMPS)/ota: $(STAMPS)/image scripts/build-ota.sh
	scripts/build-ota.sh --no-build
	@touch $@
	@ls -t build/ota/panda-$(BOARD)-*.pota | head -1

# ---- force a step (and everything after it) ----

rebuild-container: ; rm -f $(STAMPS)/container $(STAMPS)/kernel $(STAMPS)/packages $(STAMPS)/image $(STAMPS)/ota
rebuild-kernel:    ; rm -f $(STAMPS)/kernel $(STAMPS)/image $(STAMPS)/ota
rebuild-uboot:     ; rm -f $(STAMPS)/uboot $(STAMPS)/image $(STAMPS)/ota
rebuild-packages:  ; rm -f $(STAMPS)/packages $(STAMPS)/image $(STAMPS)/ota
rebuild-image:     ; rm -f $(STAMPS)/image $(STAMPS)/ota
rebuild-ota:       ; rm -f $(STAMPS)/ota

# ---- tests and misc ----

check: ## lint + unit/consistency/fuzz tests (ARGS="-k radio" for pytest args)
	scripts/check.sh $(ARGS)
test-graph: ## audio-graph integration suite in podman
	scripts/test-graph.sh $(ARGS)
test-ota: ## OTA slot tests against build/sdimage/$(BOARD)
	scripts/test-ota.sh $(ARGS)
demo: ## regenerate docs/panel-demo.html + .png
	scripts/build-demo.sh
clean: ## scripts/clean.sh, then forget the stamps
	scripts/clean.sh
	rm -rf build/.stamps
