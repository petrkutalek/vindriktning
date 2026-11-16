PORT        ?= /dev/ttyUSB0
MPREMOTE    ?= mpremote connect $(PORT)
MPY_CROSS   ?= ./mpy-cross-1.29.0
MPY_FLAGS   ?= -O3 -march=xtensa

MODULES := app pm1006 bme688 bthome
MPY     := $(MODULES:%=%.mpy)
PLAIN   := main.py config.py

.PHONY: all build upload reset deploy repl clean

all: deploy

build: $(MPY)

%.mpy: %.py
	$(MPY_CROSS) $(MPY_FLAGS) $<

upload: build
	$(MPREMOTE) fs cp $(MPY) $(PLAIN) :

reset:
	$(MPREMOTE) reset

# The reset must follow the upload, also with make -j: two mpremote processes cannot share the port.
deploy: upload
	$(MPREMOTE) reset

repl:
	$(MPREMOTE) repl

clean:
	rm -f $(MPY)
