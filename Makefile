PORT        ?= /dev/ttyUSB0
MPREMOTE    ?= mpremote connect $(PORT)
MPY_CROSS   ?= ./mpy-cross-1.29.0
MPY_FLAGS   ?= -O2 -march=xtensawin

BUILD   := build

MODULES := app pm1006 bme688 bthome config
PLAIN   := main.py
FILES   := $(MODULES:%=$(BUILD)/%.mpy) $(PLAIN:%=$(BUILD)/%)

.PHONY: all build upload reset deploy repl clean pkg

all: deploy

build: $(FILES)

$(BUILD)/%.mpy: %.py
	@mkdir -p $(@D)
	$(MPY_CROSS) $(MPY_FLAGS) -o $@ $<

$(BUILD)/%.py: %.py
	@mkdir -p $(@D)
	cp $< $@

upload: build
	cd $(BUILD) && $(MPREMOTE) fs cp $(notdir $(FILES)) :

pkg: build
	tar cvzf app.tgz -C $(BUILD) $(notdir $(FILES))

reset:
	$(MPREMOTE) reset

deploy: upload
	$(MPREMOTE) reset

repl:
	$(MPREMOTE) repl

clean:
	rm -rf $(BUILD) app.tgz
