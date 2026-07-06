# Build the native PipeWire bridge (wolverine_pw.so).
#
# Requires the PipeWire development headers:
#   Arch/CachyOS : already in the `pipewire` package
#   Debian/Ubuntu: libpipewire-0.3-dev
#   Fedora       : pipewire-devel
#
# Usage: make        (build)
#        make clean  (remove the .so)

CFLAGS  := -O2 -fPIC -Wall $(shell pkg-config --cflags libpipewire-0.3)
LDLIBS  := $(shell pkg-config --libs libpipewire-0.3)

wolverine_pw.so: wolverine_pw.c
	$(CC) -shared $(CFLAGS) -o $@ $< $(LDLIBS)

clean:
	rm -f wolverine_pw.so

.PHONY: clean
