PORT        ?= /dev/ttyUSB0
MPY_CROSS   ?= mpy-cross
MPREMOTE    ?= mpremote connect $(PORT)

MODULES := app pm1006 bme688 bthome
MPY     := $(MODULES:%=%.mpy)
PLAIN   := main.py config.py

.PHONY: all build upload reset deploy repl clean

all: deploy

build: $(MPY)

%.mpy: %.py
	$(MPY_CROSS) $<

upload: build
	$(MPREMOTE) fs cp $(MPY) $(PLAIN) :

reset:
	$(MPREMOTE) reset

deploy: upload reset

repl:
	$(MPREMOTE) repl

clean:
	rm -f $(MPY)
