# CaptureAudio Virtual Audio Driver — Makefile
# Forked from BlackHole (GPLv3). Builds an AudioServerPlugin bundle with clang.

DRIVER_NAME    = CaptureAudio2ch
BUNDLE_NAME    = $(DRIVER_NAME).driver
SRC            = CaptureAudio/CaptureAudio.c
PLIST          = CaptureAudio/Info.plist
INSTALL_DIR    = /Library/Audio/Plug-Ins/HAL
BUILD_DIR      = build

# Identity constants — override BlackHole defaults at compile time
CFLAGS = -std=gnu11 -Os -Wall -Wno-unused-label -Wno-format-extra-args -Wno-unused-but-set-variable \
    -DkNumber_Of_Channels=2 \
    -DkHas_Driver_Name_Format=false \
    -DkDriver_Name='"CaptureAudio"' \
    -DkPlugIn_BundleID='"com.tdeshane.CaptureAudio2ch"' \
    -DkDevice_Name='"CaptureAudio 2ch"' \
    -DkDevice2_Name='"CaptureAudio 2ch Mirror"' \
    -DkDevice2_IsHidden=true \
    -DkCanBeDefaultDevice=true \
    -DkCanBeDefaultSystemDevice=true \
    -DkManufacturer_Name='"Todd Deshane"'

LDFLAGS = -bundle \
    -framework CoreAudio \
    -framework CoreFoundation \
    -framework Accelerate

.PHONY: build install uninstall clean

build: $(BUILD_DIR)/$(BUNDLE_NAME)/Contents/MacOS/CaptureAudio

$(BUILD_DIR)/$(BUNDLE_NAME)/Contents/MacOS/CaptureAudio: $(SRC) $(PLIST)
	@echo "==> Building $(DRIVER_NAME)..."
	mkdir -p $(BUILD_DIR)/$(BUNDLE_NAME)/Contents/MacOS
	cp $(PLIST) $(BUILD_DIR)/$(BUNDLE_NAME)/Contents/Info.plist
	clang $(CFLAGS) $(LDFLAGS) -o $@ $(SRC)
	@echo "==> Signing with ad-hoc identity..."
	codesign --force --sign - $(BUILD_DIR)/$(BUNDLE_NAME)
	@echo "==> Build complete: $(BUILD_DIR)/$(BUNDLE_NAME)"

install: build
	@echo "==> Installing $(BUNDLE_NAME) to $(INSTALL_DIR)..."
	sudo cp -R $(BUILD_DIR)/$(BUNDLE_NAME) $(INSTALL_DIR)/
	@echo "==> Restarting coreaudiod (launchd will respawn it)..."
	sudo killall coreaudiod
	@echo "==> Waiting for coreaudiod to restart..."
	sleep 3
	@echo "==> Installation complete."

uninstall:
	@echo "==> Removing $(BUNDLE_NAME) from $(INSTALL_DIR)..."
	sudo rm -rf $(INSTALL_DIR)/$(BUNDLE_NAME)
	@echo "==> Restarting coreaudiod (launchd will respawn it)..."
	sudo killall coreaudiod
	@echo "==> Waiting for coreaudiod to restart..."
	sleep 3
	@echo "==> Uninstall complete."

clean:
	rm -rf $(BUILD_DIR)
