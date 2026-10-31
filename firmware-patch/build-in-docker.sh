#!/bin/bash
# Runs inside the QMK container: builds jdlien's firmware plus our host-colors patch.
# Clones inside the container so Windows line endings can't reach the build.
set -e
BASE=$(cat /out/base-commit.txt)
TC_VER=13.3.1-1.1   # the compiler the firmware's author pins (setup.sh)

echo ">> compiler $TC_VER"
curl -sSL "https://github.com/xpack-dev-tools/arm-none-eabi-gcc-xpack/releases/download/v$TC_VER/xpack-arm-none-eabi-gcc-$TC_VER-linux-x64.tar.gz" | tar -xz -C /opt
export PATH="/opt/xpack-arm-none-eabi-gcc-$TC_VER/bin:$PATH"
arm-none-eabi-gcc --version | head -1

echo ">> firmware source $BASE"
git init -q /qmk && cd /qmk
git remote add origin https://github.com/jdlien/qmk_firmware.git
git fetch -q --depth 1 origin "$BASE"
git checkout -q FETCH_HEAD
git apply --whitespace=nowarn /out/host-colors.patch
git submodule update --init --recursive --depth 1 2>&1 | tail -2

echo ">> building"
FLAGS="-g -DAK820_BUILD_TOKEN=0x41513231u -DAK820_BUILD_HASH=0x${BASE:0:8}u -DAK820_BUILD_DIRTY=1 -DAK820PRO_LCD_VARIANT_FPB"
qmk compile -kb a_jazz/ak820pro -km via -e "EXTRAFLAGS=$FLAGS" 2>&1 | tail -12
cp a_jazz_ak820pro_via.bin /out/ak820pro-hostcolors-fpb.bin
ls -l /out/ak820pro-hostcolors-fpb.bin
sha256sum /out/ak820pro-hostcolors-fpb.bin
