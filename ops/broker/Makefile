CC ?= gcc
CFLAGS ?= -O2 -Wall -Wextra -Werror -pedantic -std=c11 -fstack-protector-strong -D_FORTIFY_SOURCE=2 -Wl,-z,relro,-z,now

TARGET = aipm-systemd-restart
SRC = aipm-systemd-restart.c

all: $(TARGET)

$(TARGET): $(SRC)
	$(CC) $(CFLAGS) -o $(TARGET) $(SRC)

clean:
	rm -f $(TARGET)

.PHONY: all clean
